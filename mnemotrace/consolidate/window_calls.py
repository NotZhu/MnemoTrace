"""Budget and validate window requests shared by ontology and fact comparisons."""
import json

from ..extraction import ExtractionBudgetError, ExtractionResponseError, ExtractionTruncatedError
from ..llm.base import complete_result
from ..llm.execution import one_attempt
from ..llm.tokens import count_tokens, input_limit


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def requests(items, pack, system, config, llm, *, tokens_per_item):
    """Yield budget-sized requests and fail if one item cannot fit on its own.

    pack is evaluated after each completed request so later requests can use
    already resolved proposals. No decision count cap substitutes for a budget.
    """
    pending = list(items)
    offset = 0
    while offset < len(pending):
        batch, prompt, tokens = [], None, 0
        for item in pending[offset:]:
            proposed = [*batch, item]
            output = 32 + sum(tokens_per_item(entry) for entry in proposed)
            limit = input_limit(llm, config, output)
            if output > config.memory_max_output_tokens or limit <= 0:
                break
            candidate = encode(pack(proposed))
            if count_tokens(llm, candidate, system) > limit:
                break
            batch, prompt, tokens = proposed, candidate, output
        if not batch:
            raise ExtractionBudgetError()
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
