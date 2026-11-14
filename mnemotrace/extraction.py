"""Stable, content-free failures shared by extraction and service adapters."""
from __future__ import annotations


class ExtractionError(RuntimeError):
    code = "extraction_failed"
    status_code = 502
    message = "Memory extraction failed; check the model and processing configuration, then retry consolidation."

    def __init__(self, *, stored_raw: bool = False, episode_ids: list[str] | None = None):
        super().__init__(self.message)
        self.stored_raw = stored_raw
        self.episode_ids = list(episode_ids or [])

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message,
                "stored_raw": self.stored_raw, "episode_ids": self.episode_ids}


class LLMNotConfiguredError(ExtractionError):
    code = "llm_not_configured"
    status_code = 503
    message = "Configure an LLM: pass llm= to the Python entry point, or set MNEMOTRACE_LLM for the service."


class ProcessingIncompleteError(ExtractionError):
    code = 'processing_incomplete'
    status_code = 503
    message = 'Session processing is incomplete. Inspect processing status before retrying.'


class ExtractionConflictError(ExtractionError):
    code = "extraction_conflict"
    status_code = 409
    message = ("A concurrent write to this namespace invalidated in-flight extraction; "
               "the source is stored raw — retry consolidation or flush again later.")


class ExtractionResponseError(ExtractionError):
    code = "extraction_invalid_response"
    message = "The model returned an invalid extraction response; check its prompt and retry."


class ExtractionTruncatedError(ExtractionResponseError):
    code = "extraction_truncated"
    message = "The extraction response was truncated; increase its output budget or reduce the window and retry."


def require_llm(llm) -> None:
    """验证语言模型是否存在配置。"""
    if llm is None:
        raise LLMNotConfiguredError()
