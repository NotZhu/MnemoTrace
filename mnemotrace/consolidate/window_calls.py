"""Budget and validate window requests shared by ontology and fact comparisons."""
import json

from ..extraction import ExtractionResponseError, ExtractionTruncatedError
from ..llm.base import complete_result
from ..llm.execution import one_attempt


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def requests(items, pack, system, config, *, tokens_per_item):
    """Yield budget-sized requests, or None for an indivisible oversized item.

    pack is evaluated after each completed request so later requests can use
    already resolved proposals. No decision count cap substitutes for a budget.
    UTF-8 bytes are the existing conservative offline token upper bound.
    """
    pending = list(items)
    offset = 0
    while offset < len(pending):
        batch, prompt, tokens = [], None, 0
        for item in pending[offset:]:
            proposed = [*batch, item]
            output = 32 + sum(tokens_per_item(entry) for entry in proposed)
            candidate = encode(pack(proposed))
            size = len((system + candidate).encode('utf-8'))
            fits = (output <= config.joint_max_output_tokens and size <= config.extraction_max_input_bytes
                    and (config.joint_context_tokens is None or size + output <= config.joint_context_tokens))
            if not fits:
                break
            batch, prompt, tokens = proposed, candidate, output
        if not batch:
            batch = [pending[offset]]
        yield batch, prompt, tokens
        offset += len(batch)


def ask(llm, prompt, system, max_tokens, expected):
    """Require exactly one object for every request ID; business validation is local."""
    response = complete_result(one_attempt(llm), prompt, system=system, max_tokens=max_tokens)
    if response.finish_reason not in (None, 'stop', 'end_turn'):
        raise ExtractionTruncatedError()
    try:
        result = json.loads(response.text)
        if not isinstance(result, dict) or set(result) != {'decisions'} or not isinstance(result['decisions'], list):
            raise ValueError()
        decoded = {}
        for item in result['decisions']:
            if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                    or item['id'] not in expected or item['id'] in decoded):
                raise ValueError()
            decoded[item['id']] = item
        if decoded.keys() != set(expected):
            raise ValueError()
        return decoded
    except (ValueError, TypeError, RecursionError) as exc:
        raise ExtractionResponseError() from exc
