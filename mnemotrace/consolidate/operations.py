"""Pure request and response policies; execution and retries live in one runner."""
import json

from ..extraction import ExtractionResponseError
from .planning import request, system_prompt, validate_response, validate_result
from .fact_rules import parse_json_facts
from .outcomes import OUTCOME_SYSTEM, OUTCOME_PREDICATES
from .topic_rules import topic_system


def prepare(data, config, policy, compensation=False):
    operation = data['operation']
    if operation == 'outcomes':
        system = topic_system(policy or OUTCOME_SYSTEM, config.llm_topics)
        prompt = '<session>\n' + json.dumps(data, ensure_ascii=False) + '\n</session>\n返回结论 JSON 数组。'
        return prompt, system, config.joint_max_output_tokens
    if operation == 'conflicts':
        system = ('Compare only the supplied fact IDs. Return {"decisions":[{"older":"id","newer":"id",'
                  '"decision":"conflict|replaces|coexists","reason":"brief reason"}]}. '
                  'Do not infer missing facts. Prefer coexists when unsure.')
        return json.dumps(data, ensure_ascii=False), system, config.joint_max_output_tokens
    system = system_prompt(policy, compensation=compensation, llm_topics=config.llm_topics, operation=operation)
    return request(data, config), system, config.summary_max_output_tokens if operation == 'summary' else config.joint_max_output_tokens


def decode(raw, data, config, compensation=False):
    if data['operation'] == 'outcomes':
        facts = []
        seen = set()
        for item in parse_json_facts(raw):
            if (not isinstance(item.get('kind'), str) or item['kind'].strip().lower() not in OUTCOME_PREDICATES
                    or not isinstance(item.get('statement'), str) or not item['statement'].strip()
                    or 'why' in item and not isinstance(item['why'], str)):
                raise ExtractionResponseError()
            statement = ' '.join(item['statement'].split())
            if not 15 <= len(statement) <= 400 or statement.lower() in seen:
                continue
            seen.add(statement.lower())
            why = ' '.join(item.get('why', '').split())[:300]
            text = statement + (f' （依据：{why}）' if why else '')
            fact = dict(subject=data['candidates'][0]['session_id'], predicate=item['kind'].strip().lower(),
                object=statement, text=text, assertion_kind='unspecified', source_ids=[s['id'] for s in data['pending']])
            if 'topic' in item:
                fact['topic'] = item['topic']
            facts.append(fact)
        result = dict(facts=facts, working=[], summaries={}, decisions=[])
    elif data['operation'] == 'conflicts':
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
