"""Model failure classification and durable operation retry policy."""
from ..extraction import LLMNotConfiguredError

RETRY_DELAYS = (30, 120)
ATTEMPT_LIMIT = len(RETRY_DELAYS) + 1

_PERMANENT_CODES = {
    "authentication_error", "invalid_api_key", "permission_denied", "insufficient_quota",
    "invalid_request_error", "model_not_found",
}
_TRANSIENT_CODES = {
    "timeout", "rate_limit_exceeded", "overloaded_error", "server_error",
}
_PERMANENT_TYPES = {
    "AuthenticationError", "PermissionDeniedError", "BadRequestError",
    "NotFoundError", "UnprocessableEntityError", "UnsupportedParamsError",
}


def retryable(exc: Exception) -> bool:
    cause = exc.__cause__ or exc
    if isinstance(cause, (ImportError, TypeError, LLMNotConfiguredError)):
        return False
    body = getattr(cause, "body", None)
    if isinstance(body, dict):
        body = body.get("error", body)
    code = getattr(cause, "code", None)
    if not isinstance(code, str) and isinstance(body, dict):
        code = body.get("code") or body.get("type")
    if isinstance(code, str):
        if code.lower() in _PERMANENT_CODES:
            return False
        if code.lower() in _TRANSIENT_CODES:
            return True
    status = getattr(cause, "status_code", None)
    if status is None:
        status = getattr(getattr(cause, "response", None), "status_code", None)
    if type(status) is int:
        if status in (408, 409, 425, 429) or status >= 500:
            return True
        if 400 <= status < 500:
            return False
    return not any(cls.__name__ in _PERMANENT_TYPES for cls in type(cause).__mro__)
