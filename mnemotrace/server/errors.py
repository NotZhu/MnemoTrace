"""将 HTTP、校验和数据库错误转换为公开的 Problem Details。"""

import logging
from http import HTTPStatus

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pydantic_core import ErrorDetails
from starlette.exceptions import HTTPException

from ..ports.inference import InferenceError
from ..ports.storage import CommitOutcomeUnknown, SpaceUnavailable
from .response_headers import request_id

_logger = logging.getLogger(__name__)
_DETAILS = {
    "invalid_api_key": "Bearer API key 缺失、无效或已撤销。",
    "space_not_found": "空间不存在或不可访问。",
    "session_not_found": "会话不存在或不可访问。",
    "fact_not_found": "事实不存在或不可访问。",
    "message_not_found": "原文不存在或不可访问。",
    "operation_not_found": "任务不存在或不可访问。",
    "idempotency_conflict": "该幂等键已用于不同的会话或消息批次。",
    "remember_queue_full": "空间的待处理任务已达上限。",
    "write_models_unavailable": "服务未配置写入所需的补全模型和嵌入模型。",
    "invalid_content_length": "Content-Length 须为非负整数。",
    "invalid_json": "请求体不是合法的 JSON。",
    "unexpected_body": "此接口不接受请求正文。",
    "invalid_request": "请求字段未通过校验。",
    "invalid_cursor": "分页游标无效或与本次查询不匹配。",
    "request_too_large": "请求体超过允许的字节数。",
    "unsupported_media_type": "此接口需要 application/json 请求体。",
    "database_unavailable": "数据库暂时不可用。",
    "database_not_ready": "数据库迁移与当前服务版本不一致。",
    "embedding_not_configured": "服务未配置查询嵌入。",
    "reranker_not_configured": "服务未配置候选重排。",
    "inference_failed": "查询嵌入请求失败或返回了无效结果。",
    "commit_outcome_unknown": "数据库提交结果暂未确认。",
    "internal_error": "服务处理请求时发生错误。",
}
_FIELD_CODES = {
    "missing": "required",
    "extra_forbidden": "unknown_field",
    "greater_than": "out_of_range",
    "greater_than_equal": "out_of_range",
    "less_than": "out_of_range",
    "less_than_equal": "out_of_range",
    "too_short": "invalid_length",
    "too_long": "invalid_length",
    "string_too_short": "invalid_length",
    "string_too_long": "invalid_length",
}


class Problem(BaseModel):
    """公开错误响应的稳定字段。"""

    type: str = "about:blank"
    title: str
    status: int
    detail: str
    code: str
    request_id: str


class FieldError(BaseModel):
    """公开字段校验失败的位置、JSON Pointer 和稳定错误码。"""

    location: str
    path: str
    code: str


def _field_error(error: ErrorDetails) -> FieldError:
    location, *path = error["loc"]
    kind = error["type"]
    if kind == "extra_forbidden":
        # 额外字段的末级名称来自请求，公开路径只定位到它的容器。
        path.pop()
    pointer = "".join(
        "/" + str(part).replace("~", "~0").replace("/", "~1") for part in path
    )
    return FieldError(
        location=str(location),
        path=pointer,
        code=_FIELD_CODES.get(kind, "invalid_value"),
    )


def problem(
    request: Request,
    status: int,
    code: str,
    headers: dict[str, str] | None = None,
    *,
    errors: list[FieldError] | None = None,
) -> JSONResponse:
    """构造使用固定公开文案的错误响应。"""
    title = HTTPStatus(status).phrase
    body = Problem(
        title=title,
        status=status,
        detail=_DETAILS.get(code, title),
        code=code,
        request_id=request_id(request.scope),
    )
    content = body.model_dump()
    if errors is not None:
        content["errors"] = [error.model_dump() for error in errors]
    return JSONResponse(
        content,
        status_code=status,
        headers=headers,
        media_type="application/problem+json",
    )


def problem_responses(*statuses: int) -> dict[int | str, dict[str, object]]:
    """声明路由真实返回的错误媒体类型及结构。"""
    responses: dict[int | str, dict[str, object]] = {}
    for status in statuses:
        schema = Problem.model_json_schema()
        if status == 422:
            schema["properties"]["errors"] = {
                "type": "array",
                "items": FieldError.model_json_schema(),
            }
        responses[status] = {
            "description": HTTPStatus(status).phrase,
            "content": {"application/problem+json": {"schema": schema}},
        }
    return responses


def log_unexpected_error(identifier: str, exc: Exception) -> None:
    """记录意外 HTTP 异常的请求标识和类别。"""
    _logger.error(
        "请求处理失败 request_id=%s reason=%s", identifier, type(exc).__name__
    )


def install_error_handlers(app: FastAPI) -> None:
    """为请求边界、授权及数据库失败安装统一映射。"""

    @app.exception_handler(HTTPException)
    async def handle_http(request: Request, exc: HTTPException) -> JSONResponse:
        default = HTTPStatus(exc.status_code).name.lower()
        code = (
            exc.detail
            if isinstance(exc.detail, str) and exc.detail in _DETAILS
            else default
        )
        return problem(request, exc.status_code, code, dict(exc.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def handle_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        if any(error["type"] == "json_invalid" for error in errors):
            return problem(request, 400, "invalid_json")
        return problem(
            request,
            422,
            "invalid_request",
            errors=[_field_error(error) for error in errors],
        )

    @app.exception_handler(SpaceUnavailable)
    async def handle_space(request: Request, exc: SpaceUnavailable) -> JSONResponse:
        return problem(request, 404, "space_not_found")

    @app.exception_handler(CommitOutcomeUnknown)
    async def handle_commit(
        request: Request, exc: CommitOutcomeUnknown
    ) -> JSONResponse:
        _logger.warning("提交结果未确认 request_id=%s", request.state.request_id)
        return problem(request, 503, "commit_outcome_unknown")

    @app.exception_handler(psycopg.Error)
    async def handle_database(request: Request, exc: psycopg.Error) -> JSONResponse:
        _logger.warning(
            "数据库操作失败 request_id=%s sqlstate=%s",
            request.state.request_id,
            exc.sqlstate,
        )
        if isinstance(exc, psycopg.OperationalError):
            return problem(request, 503, "database_unavailable")
        return problem(request, 500, "internal_error")

    @app.exception_handler(InferenceError)
    async def handle_inference(request: Request, exc: InferenceError) -> JSONResponse:
        _logger.warning("召回推理失败 request_id=%s", request.state.request_id)
        return problem(request, 502, "inference_failed")
