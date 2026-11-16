"""Model failure classification and durable operation retry policy."""
from ..extraction import ExtractionBudgetError, ExtractionConfigurationError, ExtractionResponseError, LLMNotConfiguredError

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


def _provider_code(error):
    code = getattr(error, "code", None)
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        body = body.get("error", body)
    if not isinstance(code, str) and isinstance(body, dict):
        code = body.get("code") or body.get("type")
    return code.lower() if isinstance(code, str) else None


def exceeds_model_budget(exc):
    if isinstance(exc, ExtractionBudgetError):
        return True
    cause = exc.__cause__ or exc
    return (_provider_code(cause) in {
        "context_length_exceeded", "max_tokens_exceeded", "max_output_tokens_exceeded",
    } or type(cause).__name__ == 'ContextWindowExceededError')


def retryable(exc: Exception) -> bool:
    if isinstance(exc, ExtractionResponseError):
        return True
    cause = exc.__cause__ or exc
    if isinstance(exc, ExtractionConfigurationError) or isinstance(cause, (ImportError, TypeError, LLMNotConfiguredError)):
        return False
    if exceeds_model_budget(exc):
        return False
    code = _provider_code(cause)
    if code in _PERMANENT_CODES:
        return False
    if code in _TRANSIENT_CODES:
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
