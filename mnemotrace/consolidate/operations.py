"""Pure request and response policies; execution and retries live in one runner."""
import json

from ..extraction import ExtractionResponseError
from .fact_rules import _norm_predicate
from .planning import request, system_prompt, validate_response, validate_result


def prepare(data, config, policy, compensation=False):
    operation = data['operation']
    if operation == 'conflicts':
        system = ('Compare only the supplied fact IDs. Return {"decisions":[{"older":"id","newer":"id",'
                  '"decision":"conflict|replaces|coexists","reason":"brief reason"}]}. '
                  'Do not infer missing facts. Prefer coexists when unsure.')
        return json.dumps(data, ensure_ascii=False), system, config.joint_max_output_tokens
    system = system_prompt(policy, compensation=compensation, operation=operation)
    return request(data, config), system, config.digest_max_output_tokens if operation == 'digest' else config.joint_max_output_tokens


def decode(raw, data, config, compensation=False):
    if data['operation'] == 'conflicts':
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict) or set(obj) != {'decisions'}:
                raise ValueError()
            result = dict(facts=[], digests={}, decisions=obj['decisions'])
        except (ValueError, TypeError) as exc:
            raise ExtractionResponseError() from exc
    else:
        result = validate_response(raw, data, config, compensation=compensation)
        facts = []
        for item in result['facts']:
            subject = item['subject'].strip()
            if subject.casefold() == 'user':
                subject = 'user'
            predicate = _norm_predicate(item['predicate'])
            if not predicate:
                raise ExtractionResponseError()
            facts.append({**item, 'subject': subject, 'predicate': predicate})
        result['facts'] = facts
        return result
    try:
        return validate_result(result, data, config)
    except ValueError as exc:
        raise ExtractionResponseError() from exc
