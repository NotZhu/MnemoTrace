"""Fixed responses for ontology ingestion regressions."""
import json

from mnemotrace.consolidate.planning import WINDOW_PREFIX


def mention(code=None, aliases=(), *, meaning=None):
    return {'kind': 'entity' if code else 'value', 'type': code,
            'type_meaning': meaning, 'aliases': list(aliases)}


def assertion(subject, predicate, object_, *, subject_type='person', object_type='organization',
              aliases=(), meaning=None, type_meaning=None, sensitive=False, time_kind='current'):
    return dict(subject=subject, predicate=predicate, object=object_, categories=[], sensitive=sensitive,
                time_kind=time_kind, effective_date=None, retention='unknown',
                ontology=dict(subject=mention(subject_type, aliases), object=mention(object_type, meaning=type_meaning),
                              meaning=meaning or predicate.replace('_', ' '), direction='forward'))


class Answers:
    def __init__(self, inputs, relations, *, duplicate=True, on_relation=None, entity_decision=None):
        self.inputs = inputs
        self.relations = relations
        self.duplicate = duplicate
        self.on_relation = on_relation
        self.entity_decision = entity_decision

    def __call__(self, prompt, system):
        if prompt.startswith(WINDOW_PREFIX):
            data = json.loads(prompt.removeprefix(WINDOW_PREFIX))
            return json.dumps(dict(facts=[dict(self.inputs[item['content']], text=item['content'], source_ids=[item['id']])
                                          for item in data['pending']], digests=[]))
        return ontology_reply(prompt, system, self.relations, self.entity_decision, self.on_relation, self.duplicate)


def ontology_reply(prompt, system, relations, entity_decision=None, on_relation=None, duplicate=False):
    data = json.loads(prompt)
    answers = []
    if 'MNEMOTRACE_FACT_COMPARISON' in system:
        for item in data['items']:
            first, second = data['facts'][item['first']], data['facts'][item['second']]
            decision = ('duplicate' if duplicate and 'duplicate' in item['allowed'] else
                        'conflict' if all('Full-time' in row['text'] for row in (first, second)) else 'coexists')
            answers.append(dict(id=item['id'], decision=decision, reason='Fixed synthetic comparison'))
        return json.dumps(dict(decisions=answers))
    for item in data['items']:
        candidates = [data['candidates'][key] for key in item['candidates']]
        expanded = dict(item, candidates=candidates, statement=data['assertions'][item['assertion']]['text'])
        if 'MNEMOTRACE_ENTITY_RESOLUTION' in system:
            expanded['source'] = '\n'.join(data['sources'][key] for key in item['source_ids'])
            choice = entity_decision(expanded) if entity_decision is not None else None
            if choice is not None:
                answers.append(dict(id=item['id'], decision=choice['decision'], target=choice['id'],
                                    evidence=choice['evidence']))
                continue
            key = item['mention'].casefold()
            candidate = next((row for row in candidates
                              if key in {name.casefold() for name in (row['name'], *row['aliases'])}), None)
            answers.append(dict(id=item['id'], decision='reuse' if candidate else 'new',
                                target=candidate['id'] if candidate else None, evidence=item['mention']))
        elif 'MNEMOTRACE_RELATION_RESOLUTION' in system:
            if on_relation:
                on_relation(expanded)
            choice, reverse = relations[item['predicate']]
            candidate = next((row for row in candidates if row['predicate'] == choice), None)
            answers.append(dict(id=item['id'], decision='reuse' if candidate else ('new' if choice else 'unknown'),
                                target=candidate['id'] if candidate else None, reverse=bool(candidate and reverse)))
    return json.dumps(dict(decisions=answers))
