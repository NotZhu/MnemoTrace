"""Pure request and response policies; execution and retries live in one runner."""

from contextlib import contextmanager

from ..extraction import ExtractionResponseError, ExtractionTruncatedError
from ..llm.base import complete_result
from ..llm.execution import discard_cached_completion, one_attempt
from .fact_rules import _norm_predicate
from .planning import request, system_prompt, validate_response


def prepare(data, config, policy):
    system = system_prompt(policy, typed='entity_types' in data)
    return request(data, config), system


@contextmanager
def generate(llm, data, config, policy, *, prepared=None):
    prompt, system = prepare(data, config, policy) if prepared is None else prepared
    options = dict(system=system)
    completion = complete_result(one_attempt(llm), prompt, **options)
    try:
        yield completion
    except ExtractionResponseError:
        discard_cached_completion(llm, prompt, completion, **options)
        raise


def decode(completion, data, config):
    if completion.finish_reason not in {None, 'stop', 'end_turn'}:
        raise ExtractionTruncatedError()
    result = validate_response(completion.text, data, config)
    facts = []
    for item in result['facts']:
        subject = item['subject'].strip()
        if 'entity_types' in data:
            facts.append({**item, 'subject': subject, 'object': item['object'].strip()})
            continue
        predicate = _norm_predicate(item['predicate'])
        if not predicate:
            raise ExtractionResponseError()
        facts.append({**item, 'subject': subject, 'predicate': predicate})
    result['facts'] = facts
    return result
