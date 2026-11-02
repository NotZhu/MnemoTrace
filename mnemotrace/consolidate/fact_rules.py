"""Pure normalization and structured output decoding shared by write operations."""
import json
import re
from ..extraction import ExtractionResponseError

_NAME_PREDICATES = {"name", "name_is", "is_named", "called", "nickname", "call_me", "preferred_name",
                    "name_preference", "称呼", "昵称"}

_SELF_REFS = {"user", "i", "me", "myself", "用户", "我", "本人", "自己", "俺", "咱"}

def _norm_predicate(p: str) -> str:
    p = p.strip().lower().replace("-", " ").replace("/", " ")
    p = re.sub(r"\s+", "_", p).strip("_")
    return p

def parse_json_facts(raw: str) -> list[dict]:
    """Decode one complete outcome array without salvaging truncated JSON."""
    def reject_constant(value):
        raise ValueError('Non-finite JSON number')
    try:
        data = json.loads(raw, parse_constant=reject_constant)
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise ValueError('Expected an outcome array')
        return data
    except (ValueError, TypeError, RecursionError) as exc:
        raise ExtractionResponseError() from exc
