"""Pure request and response policies; execution and retries live in one runner."""

from ..extraction import ExtractionResponseError
from .fact_rules import _norm_predicate
from .planning import request, system_prompt, validate_response


def prepare(data, config, policy, compensation=False):
    operation = data['operation']
    system = system_prompt(policy, compensation=compensation, operation=operation, typed='entity_types' in data)
    return request(data, config), system, config.digest_max_output_tokens if operation == 'digest' else config.joint_max_output_tokens


def decode(raw, data, config, compensation=False):
    result = validate_response(raw, data, config, compensation=compensation)
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
