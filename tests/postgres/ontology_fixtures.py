"""Fixed responses for PostgreSQL's typed extraction and relation comparison."""
import json

from mnemotrace.consolidate.planning import WINDOW_PREFIX


def mention(code=None, *, meaning=None):
    return {'type': code, 'type_meaning': meaning} if code else None


def assertion(subject, predicate, object_, *, subject_type='person', object_type='organization',
              meaning=None, type_meaning=None, sensitive=False, time_kind='current'):
    return dict(subject=subject, predicate=predicate, object=object_, categories=[], sensitive=sensitive,
                time_kind=time_kind, effective_date=None, retention='unknown',
                ontology=dict(subject=mention(subject_type), object=mention(object_type, meaning=type_meaning),
                              meaning=meaning or predicate.replace('_', ' '), direction='forward'))


def typed_result(result, entity_types=()):
    """Adapt older recorded fixture data to the new wire shape at the model seam."""
    entity_objects = {
        'colleague': 'person', 'sister': 'person', 'works_at': 'organization', 'lives_in': 'location',
        'current_city': 'location', 'visited': 'location', 'born_in': 'location',
    }
    facts = []
    for row in result.get('facts', ()):
        item = dict(row)
        predicate = item.pop('predicate')
        item.setdefault('ontology', dict(
            subject=mention('person'), object=mention(entity_objects.get(predicate)),
            meaning=predicate.replace('_', ' '),
            direction='symmetric' if predicate == 'colleague' else 'forward'))
        item['ontology'] = dict(item['ontology'])
        for side in ('subject', 'object'):
            endpoint = item['ontology'][side]
            if endpoint and endpoint['type'] in entity_types:
                item['ontology'][side] = dict(endpoint, type_meaning=None)
        facts.append(item)
    return {**result, 'facts': facts} if 'facts' in result else result


class Answers:
    def __init__(self, inputs, relations, *, duplicate=True, on_relation=None):
        self.inputs = inputs
        self.relations = {row['ontology']['meaning']: relations.get(row['predicate'], (row['predicate'], False))
                          for row in inputs.values() if row is not None}
        self.duplicate = duplicate
        self.on_relation = on_relation

    def __call__(self, prompt, system):
        if prompt.startswith(WINDOW_PREFIX):
            data = json.loads(prompt.removeprefix(WINDOW_PREFIX))
            return json.dumps(typed_result(dict(facts=[
                dict(self.inputs[item['content']], text=item['content'], source_ids=[item['id']])
                for item in data['pending'] if self.inputs[item['content']] is not None], digests=[]),
                data['entity_types']))
        return ontology_reply(prompt, system, self.relations, self.on_relation, self.duplicate)


def ontology_reply(prompt, system, relations=None, on_relation=None, duplicate=False):
    data = json.loads(prompt)
    answers = []
    if 'MNEMOTRACE_FACT_COMPARISON' in system:
        for item in data['items']:
            first, second = data['facts'][item['first']], data['facts'][item['second']]
            decision = ('duplicate' if duplicate and 'duplicate' in item['allowed'] else
                        'conflict' if all('Full-time' in row['text'] for row in (first, second)) else 'coexists')
            answers.append(dict(id=item['id'], decision=decision))
        return json.dumps(dict(decisions=answers))
    names = {}
    relations = relations or {}
    items = {item['id']: item for item in data['items']}
    for item in data['items']:
        candidates = []
        for key in item['candidates']:
            if key in data['candidates']:
                candidates.append(data['candidates'][key])
            else:
                prior = items[key]
                candidates.append(dict(id=key, predicate=None, meaning=prior['meaning'], direction=prior['direction'],
                                       endpoint_types=[[prior['subject_type'], prior['object_type']]]))
        if on_relation:
            on_relation(dict(item, candidates=candidates, statement=item['text']))
        code = item['meaning'].strip().lower().replace(' ', '_')
        choice, reverse = relations.get(item['meaning'], relations.get(code, (code, False)))
        candidate = next((row for row in candidates if (row['predicate'] or names.get(row['id'])) == choice), None)
        action = 'unknown'
        if candidate:
            signature = [item['subject_type'], item['object_type']]
            if reverse:
                signature.reverse()
            action = ('reuse' if signature in candidate['endpoint_types'] or candidate['direction'] == 'symmetric'
                      and signature[::-1] in candidate['endpoint_types'] else 'extend')
        elif choice:
            action = 'new'
        names[item['id']] = choice
        answers.append(dict(id=item['id'], decision=action, target=candidate['id'] if candidate else None,
                            reverse=bool(candidate and reverse), predicate=choice if action == 'new' else None))
    return json.dumps(dict(decisions=answers))
