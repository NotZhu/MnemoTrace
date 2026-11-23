"""准备和检索 MnemoTrace、全文输入及原文 BM25 的上下文。"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from hashlib import sha256
from time import monotonic, sleep
from typing import Literal, Protocol

import httpx
import tiktoken
from pydantic import BaseModel, Field, JsonValue, ValidationError

from .config import Config
from .contracts import BackendName, Evidence, Retrieval, Session, Turn
from .http import EvaluationError, Http, RequestFailed

_RESOURCE_ID = re.compile(r"[a-f0-9]{32}")
_SCOPE = re.compile(r"mnemo-eval-[a-f0-9]{32}-[a-f0-9]{16}")
_WORD = re.compile(r"\w+")
_PERSONA_CHUNK_TOKENS = 512


def scoped_name(run_id: str, history_id: str) -> str:
    """派生专用于本次运行和一份历史的资源名。"""
    digest = sha256(history_id.encode()).hexdigest()[:16]
    return _check_scope(f"mnemo-eval-{run_id}-{digest}")


def _check_scope(scope: str) -> str:
    if not _SCOPE.fullmatch(scope):
        raise ValueError("评测资源名不在允许范围")
    return scope


@dataclass(slots=True)
class Prepared:
    """携带后端检索所需的稳定输入及可选写入凭据。"""

    evidence: tuple[Evidence, ...] = ()
    index: _BM25 | None = None
    remote_id: str | None = None
    source_ids: dict[str, tuple[str, str]] = field(default_factory=dict)
    receipts: list[JsonValue] = field(default_factory=list)


class Backend(Protocol):
    """为一份历史准备上下文，再按问题检索。"""

    name: BackendName

    def ingest(self, scope: str, history: tuple[Session, ...]) -> Prepared: ...

    def retrieve(self, prepared: Prepared, query: str, top_k: int) -> Retrieval: ...


def _session_evidence(session: Session) -> Evidence:
    date = f"[{session.event_time}]\n" if session.event_time else ""
    return Evidence(
        text=date + "\n".join(_turn_prefix(turn) + turn.text for turn in session.turns),
        session_ids=(session.id,),
        turn_ids=tuple(turn.id for turn in session.turns),
    )


def _turn_prefix(turn: Turn) -> str:
    return f"{turn.speaker or turn.role}: "


def _turn_fragments(turn: Turn, encoding: tiktoken.Encoding) -> Iterator[str]:
    prefix = _turn_prefix(turn)
    text = prefix + turn.text
    while True:
        tokens = encoding.encode(text, disallowed_special=())
        if len(tokens) <= _PERSONA_CHUNK_TOKENS:
            yield text
            return
        # 同一发言切开的后段仍须保留说话角色与同一个来源 ID。
        part = encoding.decode(tokens[:_PERSONA_CHUNK_TOKENS], errors="ignore")
        yield part
        text = prefix + text[len(part) :]


def _persona_candidates(
    session: Session, encoding: tiktoken.Encoding
) -> Iterator[Evidence]:
    parts: list[str] = []
    turn_ids: list[str] = []

    def candidate() -> Evidence:
        return Evidence(
            text="\n".join(parts),
            session_ids=(session.id,),
            turn_ids=tuple(dict.fromkeys(turn_ids)),
        )

    for turn in session.turns:
        for text in _turn_fragments(turn, encoding):
            if (
                parts
                and len(
                    encoding.encode("\n".join((*parts, text)), disallowed_special=())
                )
                > _PERSONA_CHUNK_TOKENS
            ):
                yield candidate()
                parts.clear()
                turn_ids.clear()
            parts.append(text)
            turn_ids.append(turn.id)
    if parts:
        yield candidate()


class OriginalHistory:
    """提供完整原文或基于原文候选的 BM25 对照。"""

    def __init__(
        self,
        name: Literal["full-context", "bm25"],
        *,
        chunk_encoding: tiktoken.Encoding | None = None,
    ) -> None:
        self.name: BackendName = name
        self._chunk_encoding = chunk_encoding

    def ingest(self, scope: str, history: tuple[Session, ...]) -> Prepared:
        """按历史准备原文和可供多题共享的 BM25 索引。"""
        encoding = self._chunk_encoding
        if encoding is None:
            evidence = tuple(_session_evidence(session) for session in history)
        else:
            evidence = tuple(
                item
                for session in history
                for item in _persona_candidates(session, encoding)
            )
        index = _BM25.build(evidence) if self.name == "bm25" else None
        return Prepared(evidence=evidence, index=index)

    def retrieve(self, prepared: Prepared, query: str, top_k: int) -> Retrieval:
        """返回全文或基于 BM25Okapi 的稳定排序。"""
        evidence = prepared.evidence
        if prepared.index is None:
            return Retrieval(evidence=evidence, provenance=False)
        scores = prepared.index.scores(query)
        ranked = sorted(range(len(scores)), key=lambda index: (-scores[index], index))[
            :top_k
        ]
        return Retrieval(
            evidence=tuple(evidence[i] for i in ranked),
            provenance=True,
            raw=[
                {
                    "session_id": evidence[i].session_ids[0],
                    "candidate_index": i,
                    "score": scores[i],
                }
                for i in ranked
            ],
        )


@dataclass(frozen=True, slots=True)
class _BM25:
    counts: tuple[Counter[str], ...]
    normalizers: tuple[float, ...]
    weights: dict[str, float]

    @classmethod
    def build(cls, evidence: tuple[Evidence, ...]) -> _BM25:
        counts = tuple(
            Counter(_WORD.findall(item.text.casefold())) for item in evidence
        )
        lengths = [sum(count.values()) for count in counts]
        average = sum(lengths) / len(lengths) if lengths else 0
        normalizers = tuple(
            1.5 * (0.25 + 0.75 * length / (average or 1)) for length in lengths
        )
        frequency = Counter(term for count in counts for term in count)
        # 与 BM25Okapi 的默认参数及负 IDF 修正一致。
        idf = {
            term: math.log(len(counts) - df + 0.5) - math.log(df + 0.5)
            for term, df in frequency.items()
        }
        floor = 0.25 * sum(idf.values()) / len(idf) if idf else 0.0
        weights = {
            term: (value if value >= 0 else floor) for term, value in idf.items()
        }
        return cls(counts, normalizers, weights)

    def scores(self, query: str) -> list[float]:
        terms = _WORD.findall(query.casefold())
        return [
            sum(
                self.weights[term] * count[term] * 2.5 / (count[term] + normalizer)
                for term in terms
                if term in count
            )
            for count, normalizer in zip(self.counts, self.normalizers, strict=True)
        ]


class _Space(BaseModel):
    space_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    name: str


class _SpacePage(BaseModel):
    items: list[_Space]
    next_cursor: str | None


class _Session(BaseModel):
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class _Operation(BaseModel):
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    status: Literal["queued", "running", "succeeded", "failed"]
    message_ids: list[str] | None = None


class _Fact(BaseModel):
    text: str
    source_message_ids: list[str]


class _Recall(BaseModel):
    evidence: list[_Fact]
    degraded_reason: str | None


def _response[M: BaseModel](model: type[M], raw: JsonValue) -> M:
    try:
        return model.model_validate(raw)
    except ValidationError:
        raise EvaluationError("后端响应不符合预期契约") from None


def _message(turn: Turn) -> dict[str, JsonValue]:
    text = f"{turn.speaker}: {turn.text}" if turn.speaker else turn.text
    if turn.role == "system":
        # 评测中的系统文本属于历史资料，不能替换目标系统的指令。
        text = f"[system] {text}"
    body: dict[str, JsonValue] = {
        "role": "assistant" if turn.role == "system" else turn.role,
        "content": text,
    }
    if turn.event_time:
        body["event_time"] = turn.event_time
    return body


def _batches(
    turns: tuple[Turn, ...], max_body_bytes: int
) -> list[tuple[tuple[Turn, ...], list[JsonValue]]]:
    batches = []
    start = 0
    messages: list[JsonValue] = []
    size = len(b'{"messages":[]}')
    for index, turn in enumerate(turns):
        message = _message(turn)
        length = len(
            json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
        )
        if length + len(b'{"messages":[]}') > max_body_bytes:
            raise EvaluationError("单条原文超出后端请求大小")
        if messages and (len(messages) == 64 or size + 1 + length > max_body_bytes):
            batches.append((turns[start:index], messages))
            start, size, messages = index, len(b'{"messages":[]}'), []
        size += length + int(bool(messages))
        messages.append(message)
    if messages:
        batches.append((turns[start:], messages))
    return batches


def validate_history(history: tuple[Session, ...], config: Config) -> None:
    """在发送网络请求前检查原文能否满足写入协议。"""
    if "mnemotrace" not in config.backends:
        return
    for session in history:
        _batches(session.turns, config.max_body_bytes)


class MnemoTrace:
    """通过 v1 HTTP 契约写入独立空间并映射事实来源。"""

    name: BackendName = "mnemotrace"

    def __init__(self, http: Http, config: Config) -> None:
        self._http = http
        self._max_body_bytes = config.max_body_bytes
        self._write_timeout = config.write_timeout_seconds

    @classmethod
    def from_config(cls, client: httpx.Client, config: Config) -> MnemoTrace:
        """使用本次运行登记的 MnemoTrace 地址和凭据。"""
        if config.mnemotrace is None:
            raise ValueError("缺少 mnemotrace 实例配置")
        return cls(Http(client, "mnemotrace", config.mnemotrace), config)

    def _remember(self, path: str, body: dict[str, JsonValue], key: str) -> JsonValue:
        for attempt in range(3):
            try:
                return self._http.request(
                    "POST", path, body=body, key=key, expected=(202,)
                )
            except RequestFailed as exc:
                if not exc.retryable or attempt == 2:
                    raise
                sleep(2**attempt)
        raise AssertionError("重试循环应已返回或抛错")

    def _await_operation(
        self, raw: JsonValue, base: str
    ) -> tuple[_Operation, JsonValue]:
        op = _response(_Operation, raw)
        deadline = monotonic() + self._write_timeout
        path = f"{base}/operations/{op.operation_id}"
        while op.status not in {"succeeded", "failed"}:
            if monotonic() >= deadline:
                raise EvaluationError("写入任务等待超时", raw=raw)
            sleep(1)
            try:
                raw = self._http.request("GET", path)
            except RequestFailed as exc:
                if not exc.retryable:
                    raise
                continue
            previous_id = op.operation_id
            op = _response(_Operation, raw)
            if op.operation_id != previous_id:
                raise EvaluationError("写入任务响应 ID 不一致")
        if op.status != "succeeded":
            raise EvaluationError("写入任务失败", raw=raw)
        return op, raw

    def ingest(
        self,
        scope: str,
        history: tuple[Session, ...],
        *,
        on_created: Callable[[], None] | None = None,
    ) -> Prepared:
        """按历史顺序写入，等待后台发布后建立来源映射。"""
        scope = _check_scope(scope)
        space = _response(
            _Space,
            self._http.request(
                "POST",
                "/v1/spaces",
                body={"name": scope},
                expected=(201,),
            ),
        )
        if space.name != scope:
            raise EvaluationError("创建的空间名称与评测范围不一致")
        if on_created is not None:
            on_created()
        prepared = Prepared(remote_id=space.space_id)
        base = f"/v1/spaces/{space.space_id}"
        for index, session in enumerate(history):
            batches = _batches(session.turns, self._max_body_bytes)
            if not batches:
                continue
            remote = _response(
                _Session,
                self._http.request(
                    "POST",
                    f"{base}/sessions",
                    expected=(201,),
                ),
            )
            endpoint = f"{base}/sessions/{remote.session_id}/remember"
            for batch, (turns, messages) in enumerate(batches):
                key = f"{scope}-{index}-{batch}"
                op, receipt = self._await_operation(
                    self._remember(endpoint, {"messages": messages}, key),
                    base,
                )
                ids = op.message_ids or []
                if (
                    len(ids) != len(turns)
                    or len(set(ids)) != len(ids)
                    or any(
                        not _RESOURCE_ID.fullmatch(mid) or mid in prepared.source_ids
                        for mid in ids
                    )
                ):
                    raise EvaluationError("写入结果不能与原文一一对应")
                for message_id, turn in zip(ids, turns, strict=True):
                    prepared.source_ids[message_id] = (session.id, turn.id)
                prepared.receipts.append(receipt)
        return prepared

    def retrieve(self, prepared: Prepared, query: str, top_k: int) -> Retrieval:
        """召回事实并映射到数据集既有来源标识。"""
        raw = self._http.request(
            "POST",
            f"/v1/spaces/{prepared.remote_id}/recall",
            body={"query": query, "top_k": top_k, "track_usage": False},
        )
        recall = _response(_Recall, raw)
        if recall.degraded_reason is not None:
            raise EvaluationError("MnemoTrace 召回发生降级", raw=raw)
        evidence = []
        provenance = True
        for fact in recall.evidence:
            known = [
                prepared.source_ids[mid]
                for mid in fact.source_message_ids
                if mid in prepared.source_ids
            ]
            provenance &= bool(known) and len(known) == len(fact.source_message_ids)
            evidence.append(
                Evidence(
                    text=fact.text,
                    session_ids=tuple(dict.fromkeys(sid for sid, _ in known)),
                    turn_ids=tuple(dict.fromkeys(tid for _, tid in known)),
                )
            )
        return Retrieval(evidence=tuple(evidence), provenance=provenance, raw=raw)

    def cleanup(
        self, scope: str, *, on_found: Callable[[], None] | None = None
    ) -> None:
        """用精确的派生名称找回本次空间，并只删除匹配项。"""
        scope = _check_scope(scope)
        ids: list[str] = []
        cursor = None
        visited = set()
        while True:
            params = {"limit": "100"}
            if cursor is not None:
                params["cursor"] = cursor
            page = _response(
                _SpacePage, self._http.request("GET", "/v1/spaces", params=params)
            )
            ids.extend(space.space_id for space in page.items if space.name == scope)
            cursor = page.next_cursor
            if cursor is None:
                break
            if cursor in visited:
                raise EvaluationError("空间列表游标出现循环")
            visited.add(cursor)
        if ids and on_found is not None:
            on_found()
        for space_id in ids:
            self._http.request("DELETE", f"/v1/spaces/{space_id}", expected=(204, 404))


def build_backend(name: BackendName, client: httpx.Client, config: Config) -> Backend:
    """根据运行配置装配对应的后端适配器。"""
    if name == "mnemotrace":
        return MnemoTrace.from_config(client, config)
    encoding = (
        tiktoken.get_encoding(config.tokenizer)
        if name == "bm25" and config.benchmark == "personamem-v2"
        else None
    )
    return OriginalHistory(name, chunk_encoding=encoding)
