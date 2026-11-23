"""通过已部署的 v1 API 验证合成记忆的写入、召回、溯源和清空。"""

import argparse
import os
import re
import sys
from time import monotonic, sleep
from uuid import uuid4

import httpx

_TEXT = "项目 Atlas-903 默认导出 XLSX。"
_SPACES = "/v1/spaces"


class SmokeError(RuntimeError):
    """表示 HTTP 状态或服务行为不符合验收预期。"""


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    expected: int,
    *,
    body: object = None,
    key: str | None = None,
    params: dict[str, str] | None = None,
    read_timeout: int | None = None,
) -> httpx.Response:
    headers = {"Idempotency-Key": key} if key is not None else None
    timeout = client.timeout
    if read_timeout is not None:
        timeout = httpx.Timeout(timeout)
        timeout.read = read_timeout
    response = client.request(
        method, path, json=body, headers=headers, params=params, timeout=timeout
    )
    if response.status_code != expected:
        request_id = response.headers.get("X-Request-ID", "-")
        raise SmokeError(
            f"{method} {path} 返回 {response.status_code}，预期 {expected}"
            f"（request_id={request_id}）"
        )
    return response


def _location(response: httpx.Response, prefix: str) -> str:
    location: str = response.headers.get("Location", "")
    if not location.startswith(prefix):
        raise SmokeError(f"响应未返回 {prefix} 下的资源 Location")
    return location


def _find_space(client: httpx.Client, name: str) -> str | None:
    params = {"limit": "100"}
    while True:
        page = _request(client, "GET", _SPACES, 200, params=params).json()
        for item in page["items"]:
            if item["name"] == name:
                space_id = item["space_id"]
                if space_id in (".", "..") or not re.fullmatch(
                    r"[A-Za-z0-9._~-]+", space_id
                ):
                    raise SmokeError("验收空间的标识格式无效")
                return f"{_SPACES}/{space_id}"
        cursor = page["next_cursor"]
        if cursor is None:
            return None
        params["cursor"] = cursor


def _write(
    client: httpx.Client, space: str, key: str, role: str, text: str, timeout: int
) -> tuple[str, str]:
    session = _location(
        _request(client, "POST", f"{space}/sessions", 201), f"{space}/sessions/"
    )
    payload = {"messages": [{"role": role, "content": text}]}
    path = f"{session}/remember"
    accepted = _request(client, "POST", path, 202, body=payload, key=key)
    operation = _location(accepted, f"{space}/operations/")
    replay = _request(client, "POST", path, 202, body=payload, key=key)
    if replay.headers.get("Location") != operation:
        raise SmokeError("重放写入幂等键后任务发生变化")

    deadline = monotonic() + timeout
    while monotonic() < deadline:
        result = _request(client, "GET", operation, 200).json()
        status = result["status"]
        if status == "succeeded":
            (message_id,) = result["message_ids"]
            return operation, f"{space}/messages/{message_id}"
        if status == "failed":
            raise SmokeError(f"任务失败，错误码 {result['error']['code']}")
        sleep(1)
    raise SmokeError(f"等待任务超过 {timeout} 秒（{operation}）")


def _verify(
    client: httpx.Client, space: str, timeout: int, recall_read_timeout: int
) -> None:
    operation, message = _write(client, space, "write-1", "user", _TEXT, timeout)
    query = {"query": "Atlas-903 默认导出格式", "top_k": 5}
    recall = _request(
        client,
        "POST",
        f"{space}/recall",
        200,
        body=query,
        read_timeout=recall_read_timeout,
    ).json()
    message_id = message.rsplit("/", 1)[1]
    if recall["degraded_reason"] is not None or not any(
        message_id in evidence["source_message_ids"]
        and "XLSX" in evidence["text"].upper()
        for evidence in recall["evidence"]
    ):
        raise SmokeError("召回未返回可溯源的 XLSX 事实，或查询嵌入已经降级")
    if _request(client, "GET", message, 200).json()["content"] != _TEXT:
        raise SmokeError("原文与提交的内容不一致")

    purge = f"{space}/purge"
    _request(client, "POST", purge, 204, key="purge-1")
    _request(client, "GET", message, 404)
    _request(client, "GET", operation, 404)
    cleared = _request(
        client,
        "POST",
        f"{space}/recall",
        200,
        body=query,
        read_timeout=recall_read_timeout,
    ).json()
    if cleared["evidence"]:
        raise SmokeError("清空后仍能召回证据")

    note = "这是一条清空后新写入的验收消息。"
    _, fresh_message = _write(client, space, "write-2", "assistant", note, timeout)
    _request(client, "POST", purge, 204, key="purge-1")
    if _request(client, "GET", fresh_message, 200).json()["content"] != note:
        raise SmokeError("重放清空请求后未读到新写入的原文")


def main() -> int:
    """运行 HTTP 闭环并删除本次创建的独立空间。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument(
        "--timeout", type=int, default=240, help="每个任务的轮询上限（秒）"
    )
    parser.add_argument(
        "--recall-read-timeout", type=int, default=45, help="召回的 HTTP 读取超时（秒）"
    )
    args = parser.parse_args()
    token = os.environ.get("MNEMOTRACE_SMOKE_TOKEN")
    if not token:
        parser.error("请通过 MNEMOTRACE_SMOKE_TOKEN 提供账号凭据")
    if args.timeout < 1:
        parser.error("--timeout 须为正数")
    if args.recall_read_timeout < 1:
        parser.error("--recall-read-timeout 须为正数")
    try:
        url = httpx.URL(args.base_url)
    except httpx.InvalidURL:
        parser.error("--base-url 格式无效")
    loopback_http = url.scheme == "http" and url.host in (
        "localhost",
        "127.0.0.1",
        "::1",
    )
    if not url.host or url.userinfo or not (url.scheme == "https" or loopback_http):
        parser.error("--base-url 须为 HTTPS 或本机 HTTP 服务地址，且不能含凭据")

    name = None
    space = None
    failed = False
    with httpx.Client(
        base_url=url, headers={"Authorization": f"Bearer {token}"}, trust_env=False
    ) as client:
        try:
            _request(client, "GET", "/livez", 200)
            _request(client, "GET", "/readyz", 200)
            schema = _request(client, "GET", "/openapi.json", 200).json()
            if not schema["openapi"].startswith("3.1."):
                raise SmokeError("服务未发布 OpenAPI 3.1 契约")
            name = f"HTTP 验收 {uuid4().hex}"
            created = _request(client, "POST", _SPACES, 201, body={"name": name})
            space = _location(created, f"{_SPACES}/")
            _verify(client, space, args.timeout, args.recall_read_timeout)
        except SmokeError as exc:
            print(exc, file=sys.stderr)
            failed = True
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            print(f"验收请求或响应异常（{type(exc).__name__}）", file=sys.stderr)
            failed = True
        finally:
            if name is not None:
                try:
                    if space is None:
                        space = _find_space(client, name)
                    if space is None:
                        raise SmokeError("当前空间列表未找到该名称")
                    _request(client, "DELETE", space, 204)
                except (SmokeError, httpx.HTTPError, ValueError, KeyError, TypeError):
                    reference = f"（{space}）" if space is not None else ""
                    print(f"未能确认清理验收空间「{name}」{reference}", file=sys.stderr)
                    failed = True
    if failed:
        return 1
    print("HTTP 闭环通过，验收空间已删除。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
