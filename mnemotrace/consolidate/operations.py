"""Pure request and response policies; execution and retries live in one runner."""
import json

from ..extraction import ExtractionResponseError
from .planning import request, system_prompt, validate_response, validate_result


def prepare(data, config, policy, compensation=False):
    operation = data['operation']
    if operation == 'conflicts':
        system = ('Compare only the supplied fact IDs. Return {"decisions":[{"older":"id","newer":"id",'
                  '"decision":"conflict|replaces|coexists","reason":"brief reason"}]}. '
                  'Do not infer missing facts. Prefer coexists when unsure.')
        return json.dumps(data, ensure_ascii=False), system, config.joint_max_output_tokens
    system = system_prompt(policy, compensation=compensation, llm_topics=config.llm_topics, operation=operation)
    return request(data, config), system, config.summary_max_output_tokens if operation == 'summary' else config.joint_max_output_tokens


def decode(raw, data, config, compensation=False):
    if data['operation'] == 'conflicts':
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict) or set(obj) != {'decisions'}:
                raise ValueError()
            result = dict(facts=[], working=[], summaries={}, decisions=obj['decisions'])
        except (ValueError, TypeError) as exc:
            raise ExtractionResponseError() from exc
    else:
        return validate_response(raw, data, config, compensation=compensation)
    try:
        return validate_result(result, data, config)
    except ValueError as exc:
        raise ExtractionResponseError() from exc
