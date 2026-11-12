"""Adjudicate a scoped window and plan fact changes before publishing any of them."""
from collections import defaultdict
from copy import deepcopy
import json

from ..extraction import ExtractionResponseError, ExtractionTruncatedError
from ..llm.base import complete_result
from ..llm.execution import one_attempt
from ..types import Conflict
from ..util import cosine, time_key
from .pipeline import content_hash
from .fact_rules import same_assertion

_SYSTEM = """Compare supplied fact pairs as evidence. Return JSON
{"decisions":[{"id":"pair id","decision":"conflict|coexists"}]} with one decision per pair.
Decide whether the statements are incompatible in the same applicable context.
Separate simultaneous values, independent events, and plans from incompatible states.
Treat insufficient evidence as coexistence. The application decides temporal ordering."""
_DECISIONS = {'conflict', 'coexists'}


def _order(fact):
    return time_key(fact.valid_at), fact.created_at, fact.id


def _subject(fact):
    return fact.user_id, fact.subject_key


def _timeline_candidates(new, candidates, now):
    at = new.valid_at
    selected = set()
    if at is None:
        return selected | {old.id for old in candidates if old.is_published() and old.is_valid_at(now)}
    selected.update(old.id for old in candidates if old.is_valid_at(at))
    earlier = max((old.valid_at for old in candidates if old.valid_at is not None and old.valid_at < at), default=None)
    later = min((old.valid_at for old in candidates if old.valid_at is not None and old.valid_at > at), default=None)
    return selected | {old.id for old in candidates if old.valid_at is not None and old.valid_at in (earlier, at, later)}


def _pairs(incoming, facts, config, embedder, now):
    subjects = defaultdict(list)
    for fact in facts.values():
        if fact.is_published():
            subjects[_subject(fact)].append(fact)
    pairs = {}
    for new in incoming:
        peers = [old for old in subjects[_subject(new)] if old.id != new.id]
        selected = {old.id for old in peers if old.slot == new.slot}
        same_object = [old for old in peers if
                       (old.object_id == new.object_id if old.object_id or new.object_id
                        else old.object.strip().casefold() == new.object.strip().casefold())]
        selected.update(_timeline_candidates(new, same_object, now))
        if new.embedding and embedder is not None:
            scores = [(cosine(new.embedding, old.embedding), old.id) for old in peers
                      if old.slot != new.slot and old.embedding]
            selected.update(key for score, key in sorted(scores, key=lambda item: (-item[0], item[1]))[:8]
                            if score >= config.conflict_sim_threshold)
        for key in sorted(selected):
            pair = tuple(sorted((new.id, key)))
            pairs[pair] = tuple(sorted((new, facts[key]), key=_order))
    return pairs


def _judge(pairs, llm, config):
    def view(fact):
        return dict(text=fact.text, subject=fact.subject, predicate=fact.predicate, object=fact.object,
                    source=fact.source)

    input_limit = config.extraction_max_input_bytes
    if config.joint_context_tokens is not None:
        input_limit = min(input_limit, config.joint_context_tokens - config.joint_max_output_tokens)
    system_bytes = len(_SYSTEM.encode())

    def serialize(entries):
        return json.dumps([item for _, item in entries], ensure_ascii=False, separators=(',', ':'))

    def fit_pair(entry):
        if len(serialize([entry]).encode()) + system_bytes <= input_limit:
            return
        pair = entry[1]
        pair['truncated'] = True
        fields = [(pair[side], field) for side in ('first', 'second')
                  for field in ('text', 'object', 'subject', 'predicate')]
        while len(serialize([entry]).encode()) + system_bytes > input_limit:
            evidence, key = max(fields, key=lambda item: len(item[0][item[1]].encode()))
            value = evidence[key]
            if not value:
                raise ExtractionResponseError()
            kept = len(value) // 4
            evidence[key] = value[:kept] + '…' + value[-kept:] if kept else ''

    def batches():
        batch = []
        for pair, (older, newer) in pairs.items():
            entry = (pair, dict(id=str(len(batch)), first=view(older), second=view(newer)))
            fit_pair(entry)
            encoded = serialize([*batch, entry])
            if batch and (len(batch) >= 16 or len(encoded.encode()) + system_bytes > input_limit
                          or (len(batch) + 1) * 48 + 16 > config.joint_max_output_tokens):
                yield batch
                batch = []
                entry[1]['id'] = '0'
            batch.append(entry)
        if batch:
            yield batch

    for batch in batches():
        prompt = serialize(batch)
        if (len(prompt.encode()) + system_bytes > input_limit
                or len(batch) * 48 + 16 > config.joint_max_output_tokens):
            raise ExtractionResponseError()
        response = complete_result(one_attempt(llm), prompt, system=_SYSTEM, max_tokens=config.joint_max_output_tokens)
        if response.finish_reason not in {None, 'stop', 'end_turn'}:
            raise ExtractionTruncatedError()
        try:
            data = json.loads(response.text)
            if not isinstance(data, dict) or set(data) != {'decisions'} or not isinstance(data['decisions'], list):
                raise ValueError()
            expected = {item['id']: key for key, item in batch}
            decoded = {}
            for entry in data['decisions']:
                if (not isinstance(entry, dict) or set(entry) != {'id', 'decision'}
                        or entry['id'] not in expected or entry['id'] in decoded or entry['decision'] not in _DECISIONS):
                    raise ValueError()
                decoded[entry['id']] = entry['decision']
            if decoded.keys() != expected.keys():
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise ExtractionResponseError() from exc
        yield {key: ('review' if item.get('truncated') and decoded[item['id']] == 'conflict'
                     else decoded[item['id']]) for key, item in batch}


def plan_merge(incoming, previous, llm, config, embedder, now, existing_pairs=(), *, verdicts=None):
    """Return changed facts, new conflicts, and call counts for the caller's commit."""
    facts = {fact.id: deepcopy(fact) for fact in previous}
    subjects = defaultdict(list)
    for fact in facts.values():
        subjects[_subject(fact)].append(fact)
    changed = {}
    incoming_unique = []
    duplicates = 0
    for new in sorted(deepcopy(incoming), key=_order):
        old = facts.get(new.id)
        if old is None:
            old = next((fact for fact in subjects[_subject(new)] if same_assertion(fact, new)), None)
        if old is not None:
            duplicates += 1
            if new.id != old.id:
                provenance = list(dict.fromkeys((*old.provenance, *new.provenance)))
                source_roles = list(dict.fromkeys((*old.source_roles, *new.source_roles)))
                sensitive = old.sensitive or (old.source != 'user' and new.sensitive and not old.sensitive_override)
                if provenance != old.provenance or source_roles != old.source_roles or sensitive != old.sensitive:
                    old.provenance = provenance
                    old.source_roles = source_roles
                    old.sensitive = sensitive
                    changed[old.id] = old
            continue
        facts[new.id] = changed[new.id] = new
        subjects[_subject(new)].append(new)
        incoming_unique.append(new)

    pairs = _pairs(incoming_unique, facts, config, embedder, now)
    batches = _judge(pairs, llm, config) if verdicts is None else (verdicts,)
    decisions = {}
    calls = 0
    for batch in batches:
        decisions.update(batch)
        calls += int(verdicts is None)
    if any(pair not in pairs or decision not in _DECISIONS | {'review'} for pair, decision in decisions.items()):
        raise ValueError("Invalid semantic verdict")

    incoming_ids = {fact.id for fact in incoming_unique}
    conflicts = []
    known_pairs = set(existing_pairs)
    ordered = []
    for pair, decision in decisions.items():
        if decision == 'coexists':
            continue
        first, second = pairs[pair]
        if not _overlap(first, second):
            continue
        if decision == 'review' or first.valid_at is None or second.valid_at is None or first.valid_at == second.valid_at:
            for fact in (first, second):
                if fact.id in incoming_ids:
                    fact.review_pending = True
            if pair not in known_pairs:
                prior, candidate = ((second, first) if first.id in incoming_ids and second.id not in incoming_ids
                                    else (first, second))
                conflicts.append(Conflict(id='cf_' + content_hash(pair)[:24], older=prior.id, newer=candidate.id,
                                          text_older=prior.text, text_newer=candidate.text, user_id=prior.user_id,
                                          reason='Incompatible assertions with unresolved effective order', detected_at=now))
            continue
        ordered.append((first, second))

    invalidated = set()
    for first, second in sorted(ordered, key=lambda pair: (_order(pair[1]), _order(pair[0]))):
        if not (first.is_published() and second.is_published() and _overlap(first, second)):
            continue
        # A newly discovered intermediate state can shorten an already recorded predecessor.
        for successor in facts.values():
            if first.id in successor.supersedes and successor.valid_at == first.invalid_at:
                successor.supersedes = sorted(set(successor.supersedes) - {first.id})
                changed[successor.id] = successor
        first.end_at(second.valid_at, now)
        second.supersedes = sorted({*second.supersedes, first.id})
        invalidated.add(first.id)
        changed[first.id] = first
        changed[second.id] = second
    return list(changed.values()), conflicts, dict(facts_added=len(incoming_unique), duplicates=duplicates,
                                                  invalidated=len(invalidated), model_calls=calls)


def _overlap(first, second):
    return not (
        first.invalid_at is not None and second.valid_at is not None and first.invalid_at <= second.valid_at
        or second.invalid_at is not None and first.valid_at is not None and second.invalid_at <= first.valid_at
    )


def conflict_is_reviewable(first, second):
    return all(fact is not None and fact.withdrawn_at is None for fact in (first, second))


def resolve_facts(facts, keep, at):
    if keep == 'dismiss':
        return []
    if keep == 'both':
        changed = [fact for fact in facts.values() if fact.review_pending]
        for fact in changed:
            fact.review_pending = False
        return changed
    winner = facts[keep]
    other = facts['newer' if keep == 'older' else 'older']
    winner.review_pending = other.review_pending = False
    other.withdrawn_at = at
    return [winner, other]
