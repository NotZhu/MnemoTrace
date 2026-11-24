"""从推理端口验证请求失败、重试与并发策略。"""

import asyncio
from collections.abc import AsyncIterator
from gzip import compress
from uuid import UUID

import httpx
import pytest

from mnemotrace.adapters.inference import InferenceRequestError, open_inference
from mnemotrace.diagnostics import correlate
from mnemotrace.ports.inference import InferenceError

from .sample import CHAT_ENV, EMBED_ENV, chat_response, configured_chat


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks
        self.read = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.read += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("status", [200, 503])
@pytest.mark.parametrize("encoding", ["identity", "gzip"])
def test_provider_response_body_limit_stops_reading_without_retry(
    status: int, encoding: str
) -> None:
    """响应没有 Content-Length 时仍限制原始及解压正文，并释放连接。"""
    body = chat_response().content + b" " * 512
    chunks = (
        (body[:128], body[128:], b" ")
        if encoding == "identity"
        else (compress(body), compress(b" "))
    )
    streams = []

    def respond(_: httpx.Request) -> httpx.Response:
        stream = _ResponseStream(chunks)
        streams.append(stream)
        return httpx.Response(
            status,
            headers={
                "content-encoding": encoding,
                "retry-after": "0",
                "x-request-id": "fixture-request",
            },
            stream=stream,
        )

    async def exercise() -> None:
        env = {**CHAT_ENV, "MNEMOTRACE_INFERENCE_MAX_RESPONSE_BYTES": "256"}
        async with configured_chat(respond, env) as llm:
            with pytest.raises(InferenceRequestError) as captured:
                await llm.complete(system="回复", prompt="输入")
            assert captured.value.kind == "response_limit"
            assert captured.value.status_code == status
            attempt = captured.value.attempt_id
            assert attempt is not None and UUID(attempt).version == 4
            assert "fixture-request" not in str(captured.value)
            (stream,) = streams
            assert stream.read == len(chunks) - 1
            assert stream.closed

    asyncio.run(exercise())


@pytest.mark.parametrize("encoding", ["identity", "gzip"])
def test_provider_response_at_the_limit_is_accepted(encoding: str) -> None:
    """分段正文恰好达到上限时仍可解析，包括分段 gzip。"""
    body = chat_response().content.ljust(256)
    wire = body if encoding == "identity" else compress(body)
    stream = _ResponseStream((wire[:5], wire[5:]))

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-encoding": encoding}, stream=stream
        )

    async def exercise() -> None:
        env = {**CHAT_ENV, "MNEMOTRACE_INFERENCE_MAX_RESPONSE_BYTES": "256"}
        async with configured_chat(respond, env) as llm:
            assert (await llm.complete(system="回复", prompt="输入")).text == "合成回答"
        assert stream.closed

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("status", "code", "kind"),
    [
        (400, "unsupported_value", "request"),
        (400, "context_length_exceeded", "input_limit"),
        (401, "invalid_api_key", "auth"),
        (429, "insufficient_quota", "quota"),
        (402, None, "quota"),
    ],
)
def test_permanent_provider_error_is_sanitized_and_not_retried(
    status: int, code: str | None, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    """鉴权和配额等永久失败保留诊断字段，省略上游正文。"""
    attempts = 0
    caplog.set_level("INFO")
    echo = "fixture-private-response"
    prompt = "fixture-private-input"
    key = CHAT_ENV["MNEMOTRACE_LLM_API_KEY"]

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            status,
            headers={"x-request-id": "fixture-request", "retry-after": "0"},
            json={"error": {"message": f"{key} {echo} {prompt}", "code": code}},
        )

    async def exercise() -> None:
        async with configured_chat(respond) as llm:
            with pytest.raises(InferenceError) as captured:
                await llm.complete(system="合成指令", prompt=prompt)
        error = captured.value
        assert isinstance(error, InferenceRequestError)
        assert (error.kind, error.status_code) == (kind, status)
        assert error.attempt_id is not None and UUID(error.attempt_id).version == 4
        assert attempts == 1
        assert f"attempt_id={error.attempt_id}" in caplog.text
        for private in (key, echo, prompt, "fixture-request"):
            assert private not in str(error)
            assert private not in caplog.text

    asyncio.run(exercise())


@pytest.mark.parametrize("host", ["api.openai.com", "fixture.example"])
def test_provider_correlation_uses_a_local_id_per_attempt(
    host: str, caplog: pytest.LogCaptureFixture
) -> None:
    """官方端点接收本地标识，兼容端点仅记录本地关联。"""
    caplog.set_level("INFO")
    sent: list[str | None] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("x-client-request-id"))
        if len(sent) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        response = chat_response()
        return httpx.Response(
            200, json={**response.json(), "request_id": "fixture-body-id"}
        )

    async def exercise() -> None:
        env = {**CHAT_ENV, "MNEMOTRACE_LLM_BASE_URL": f"https://{host}/v1"}
        async with configured_chat(respond, env) as llm:
            with correlate("fixture-local-operation"):
                await llm.complete(system="回复", prompt="输入")
        events = [
            record.message
            for record in caplog.records
            if record.name == "mnemotrace.adapters.inference._http"
        ]
        attempts = [event.split("attempt_id=")[1].split()[0] for event in events]
        assert len(attempts) == len(set(attempts)) == 2
        assert all(UUID(attempt).version == 4 for attempt in attempts)
        assert all(
            "correlation_id=fixture-local-operation" in event for event in events
        )
        assert sent == (attempts if host == "api.openai.com" else [None, None])
        assert "fixture-body-id" not in caplog.text

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("retry_after", "retries"),
    [("0", True), ("Wed, 21 Oct 2015 07:28:00 GMT", True), ("120", False)],
)
def test_rate_limit_respects_retry_after_and_deadline(
    retry_after: str, retries: bool
) -> None:
    """支持秒数和日期格式，退避超出预算时保留原限流信息。"""
    attempts = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": retry_after})
        return chat_response()

    async def exercise() -> None:
        async with configured_chat(respond) as llm:
            if retries:
                assert (
                    await llm.complete(system="回复", prompt="输入")
                ).text == "合成回答"
            else:
                with pytest.raises(InferenceRequestError) as captured:
                    await llm.complete(system="回复", prompt="输入")
                assert captured.value.kind == "rate_limit"
        assert attempts == (2 if retries else 1)

    asyncio.run(exercise())


@pytest.mark.parametrize("error", [httpx.ConnectTimeout, httpx.ReadTimeout])
def test_connection_timeout_retries_but_read_timeout_does_not(
    error: type[httpx.TimeoutException], caplog: pytest.LogCaptureFixture
) -> None:
    """区分连接超时与可能已计费的读超时。"""
    attempts = 0
    caplog.set_level("INFO")

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise error("fixture-provider-exception", request=request)

    async def exercise() -> None:
        async with configured_chat(respond) as llm:
            with pytest.raises(TimeoutError, match="推理请求超时") as captured:
                await llm.complete(system="回复", prompt="输入")
        assert "fixture-provider-exception" not in str(captured.value)
        attempt_id = str(captured.value).split("attempt_id=")[1]
        assert UUID(attempt_id).version == 4
        assert f"attempt_id={attempt_id}" in caplog.text
        assert attempts == (2 if error is httpx.ConnectTimeout else 1)

    asyncio.run(exercise())


def test_shared_endpoint_queue_counts_toward_timeout_and_cancellation_releases_slot() -> (
    None
):
    """共享并发上限跨角色生效，排队超时与取消不会遗留占用。"""

    async def exercise() -> None:
        started = asyncio.Event()
        paths: list[str] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path.endswith("/embeddings"):
                started.set()
                await asyncio.Event().wait()
            return chat_response()

        env = {
            **CHAT_ENV,
            **EMBED_ENV,
            "MNEMOTRACE_EMBED_BASE_URL": CHAT_ENV["MNEMOTRACE_LLM_BASE_URL"] + "/",
            "MNEMOTRACE_EMBED_API_KEY": CHAT_ENV["MNEMOTRACE_LLM_API_KEY"],
            "MNEMOTRACE_INFERENCE_MAX_IN_FLIGHT": "1",
            "MNEMOTRACE_LLM_TIMEOUT_SECONDS": "0.1",
        }
        async with open_inference(
            env,
            llm=True,
            embed=True,
            transport_factory=lambda: httpx.MockTransport(respond),
        ) as clients:
            assert clients.llm is not None and clients.embedder is not None
            pending = asyncio.create_task(clients.embedder.embed(("合成输入",)))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                with pytest.raises(TimeoutError):
                    await clients.llm.complete(system="回复", prompt="排队请求")
                assert paths == ["/compatible-mode/v1/embeddings"]
            finally:
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            assert (
                await clients.llm.complete(system="回复", prompt="下一条")
            ).text == "合成回答"

    asyncio.run(exercise())
