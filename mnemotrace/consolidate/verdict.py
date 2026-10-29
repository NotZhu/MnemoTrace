"""Parse finite model verdicts without matching negated phrases."""
import logging

_LOG = logging.getLogger(__name__)


def parse_verdict(raw, allowed):
    value = raw.strip().upper() if isinstance(raw, str) else ""
    if value not in allowed:
        _LOG.warning("Model adjudication failed: invalid verdict")
        return None
    return value
