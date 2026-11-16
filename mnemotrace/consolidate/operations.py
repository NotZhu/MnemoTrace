"""Pure request and response policies; execution and retries live in one runner."""

from contextlib import contextmanager

from ..extraction import ExtractionResponseError, ExtractionTruncatedError
from ..llm.base import complete_result
from ..llm.execution import discard_cached_completion, one_attempt
from .fact_rules import _norm_predicate
from .planning import request, system_prompt, validate_response


def prepare(data, config, policy):
    """组装抽取请求的用户提示和系统提示。

    Args:
        data: 批次抽取输入。
        config: 记忆处理配置。
        policy: 当前空间的抽取规则。

    Returns:
        用户提示和系统提示组成的二元组。
    """
    system = system_prompt(policy, typed='entity_types' in data)
    return request(data, config), system


@contextmanager
def generate(llm, data, config, policy, *, prepared=None):
    """执行一次模型抽取，无效响应被拒绝时清除对应缓存。

    Args:
        llm: 抽取模型。
        data: 批次抽取输入。
        config: 记忆处理配置。
        policy: 当前空间的抽取规则。
        prepared: 可选的已准备提示词。

    Yields:
        模型生成结果。
    """
    prompt, system = prepare(data, config, policy) if prepared is None else prepared
    options = dict(system=system)
    completion = complete_result(one_attempt(llm), prompt, **options)
    try:
        yield completion
    except ExtractionResponseError:
        discard_cached_completion(llm, prompt, completion, **options)
        raise


def decode(completion, data, config):
    """校验模型输出并规范化抽取结果。

    Args:
        completion: 模型生成结果。
        data: 模型使用的批次输入。
        config: 记忆处理配置。

    Returns:
        经过验证的事实与摘要。

    Raises:
        ExtractionTruncatedError: 模型未正常完成生成。
        ExtractionResponseError: 响应不符合抽取契约。
    """
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
