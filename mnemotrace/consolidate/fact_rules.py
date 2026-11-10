"""Pure normalization and structured output decoding shared by write operations."""
import json
import re
from ..extraction import ExtractionResponseError

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
