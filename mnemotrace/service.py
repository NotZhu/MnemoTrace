"""MemoryService — 所有接入面共享的多租户记忆核心（AGENTS.md 「代码导航」）。

一个进程承载多个相互隔离的命名空间（每个用户/api-key 一个 `Memory`），落盘持久化，热用户常驻内存
并用 LRU 管理。HTTP API（`mnemotrace/server/app.py`）、MCP server（`mnemotrace/mcp/`）与 OpenAI 兼容代理
都构建在这同一个对象之上，因此这里修一处等于处处修复，三个接入面永远不会彼此漂移。

刻意不依赖 FastAPI：导入本模块只会引入核心库（外加你选择的 embedder/LLM），
MCP server 和导入 CLI 在没有安装 web 依赖时也能使用它。
"""
from __future__ import annotations

import math
import os
import queue
import shutil
import threading
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
from typing import Any, Optional

from ._namespace_lock import NamespaceLock, namespace_lock
from ._namespace_paths import contained_child, namespace_directory, namespace_path
from .localize import display_of
from .memory import Memory
from .metrics import Metrics, timed
from .store._validation import is_snapshot_candidate
from .store.persist import (
    DimensionMismatchError,
    EmbedderMismatchError,
    PersistenceError,
    snapshot_version,
)
from .topics import TOPICS, Topic
from .util import fmt_date, fmt_datetime, now

FACTS_PAGE_LIMIT = 500
EPISODES_PAGE_LIMIT = 200


class MemoryRevisionChanged(ValueError):
    """A later page belongs to a different committed collection."""


DEFAULT_DATA_DIR = os.path.expanduser("~/.mnemotrace/data")

# _BASIC 归一化字段中允许多值共存的那些。audit 的 slot_overflow 预扫描（"单值槽位不该有 N 个值"）
# 只对其余字段成立。
_MULTI_VALUED_FIELDS = {"children", "language", "education"}

# 与控制台 Health 的颜色优先级一致。截取 limit 前排序，避免高优先级发现被低优先级占满首页。
_AUDIT_KIND_PRIORITY = {
    "empty_value": 0, "embedder_blind": 0,
    "machine_token": 1, "slot_overflow": 1, "fragment": 1, "dangling_subject": 1,
    "unreduced_claim": 2, "code_artifact": 2,
    "orphan_entity": 3,
}

# 为 /v1/recall 的 answerer（控制台的"问答"视图）准备拼装好的记忆框架。
_ANSWER_SYSTEM = (
    "你是用户的记忆助手。根据提供的【记忆】回答问题，简洁、准确、自然。"
    "准确区分人物及其关系归属；不同称呼只有明确的身份依据时，才可视为同一人。"
    "回答前核对记忆中的主语及关系是否对应问题；缺少对应依据时说明信息不足，不借用其他人物的信息。"
    "原始记录时间表示内容被记录的时间，不等同于事件发生时间。"
    "根据原文判断事件时间；相对时间以对应的原始记录时间为参照推算，保留原文的时间精度。"
    "信息发生更新时，按问题所指的时间采用对应版本。"
    "只回答证据支持的内容；信息不足或有歧义时明确说明，不猜测。"
)


def _answer_from_memory(answerer, query: str, ctx: str) -> str:
    """基于拼装好的精简上下文生成回答——即使用这套记忆的 agent 会说的话。
    尽力而为：缺少 answerer 或模型瞬时出错都不会打断 recall（返回 ""）。"""
    if answerer is None or not ctx.strip():
        return ""
    try:
        return answerer.complete(f"【记忆】\n{ctx}\n\n【问题】{query}", system=_ANSWER_SYSTEM).strip()
    except Exception:  # noqa: BLE001 -- 绝不让回答打断 recall
        return ""


def _est_tokens(text: str) -> int:
    """对中文更公平的 token 估算：每个 CJK 字符 ≈ 1 token，每个非 CJK 单词 ≈ 1 token。
    （中文不按空格分词，`len(text.split())` 会严重低估。）"""
    import re

    cjk = len(re.findall(r"[一-鿿]", text))
    words = len(re.findall(r"[A-Za-z0-9]+", re.sub(r"[一-鿿]", " ", text)))
    return cjk + words


def _all_facts(mem: Memory) -> list:
    return mem.fact_store.values() + mem.cold_store.values()


def _clamp_limit(limit: Optional[int], default: Optional[int] = None, max_value: int = 500) -> Optional[int]:
    if limit is None:
        return default
    return max(0, min(int(limit), max_value))


def _page(items: list, offset: int = 0, limit: Optional[int] = None) -> tuple[list, dict]:
    start = max(0, int(offset or 0))
    effective_limit = None
    if limit is None:
        sliced = items[start:]
        next_offset = None
    else:
        effective_limit = max(0, int(limit))
        if effective_limit == 0:
            sliced = []
            next_offset = None
        else:
            sliced = items[start:start + effective_limit]
            end = start + effective_limit
            next_offset = end if end < len(items) else None
    return sliced, {
        "total": len(items),
        "offset": start,
        "limit": effective_limit,
        "has_more": next_offset is not None,
        "next_offset": next_offset,
    }


@dataclass
class _Access:
    writing: bool = False
    memory: Memory | None = None


def _read_access(method):
    @wraps(method)
    def read(self, user, *args, **kwargs):
        with self._read_scope(user):
            # JSON 响应必须与可变缓存分离，避免向调用方暴露缓存内部的列表和字典。
            return deepcopy(method(self, user, *args, **kwargs))
    return read


class MemoryService:
    """按命名空间持久化的记忆，共享 embedder/LLM，热用户用 LRU 管理。

    每个操作都接收一个 `user`（命名空间 / api-key 身份），返回普通可 JSON 化的 dict——
    与 HTTP 路由返回的载荷一致，因此 MCP 工具和 REST 端点共享同一套实现与契约。
    """

    def __init__(
        self,
        data_dir: Optional[str] = None,
        embedder_name: Optional[str] = None,
        llm_name: Optional[str] = None,
        max_hot_users: Optional[int] = None,
    ) -> None:
        from .llm.providers import load_dotenv, make_embedder, make_llm

        load_dotenv()  # 从本地 .env 读取 provider 密钥（ARK_API_KEY、DEEPSEEK_API_KEY 等）
        self.data_dir = os.path.abspath(
            os.path.expanduser(data_dir or os.environ.get("MNEMOTRACE_DATA_DIR", DEFAULT_DATA_DIR))
        )
        os.makedirs(self.data_dir, exist_ok=True)
        self.max_hot_users = max_hot_users or int(os.environ.get("MNEMOTRACE_MAX_HOT_USERS", "64"))
        self.embedder = make_embedder(embedder_name or os.environ.get("MNEMOTRACE_EMBEDDER", "hashing"))
        name = llm_name if llm_name is not None else os.environ.get("MNEMOTRACE_LLM", "")
        self.llm = make_llm(name) if name else None  # 无 LLM 时使用确定性的规则抽取器
        # /v1/recall 的 answerer（控制台的"问答"）——设置时用比抽取器更强的模型；
        # 否则复用主 LLM。让 Ask 页面给出真正的回答，而不只是上下文。
        answerer_name = os.environ.get("MNEMOTRACE_ANSWERER", "")
        self.answerer = make_llm(answerer_name) if answerer_name else self.llm
        # 多模态写入（ingest）用的视觉描述器（AGENTS.md 「代码导航」）：把图片转成可检索的描述文本。
        # 默认用主 LLM（很多对话模型具备视觉能力）；需要专用视觉模型时设置 MNEMOTRACE_VISION_LLM。
        # 为 None 时图片以占位符存储，离线路径保持纯文本。
        vision_name = os.environ.get("MNEMOTRACE_VISION_LLM", "")
        self.captioner = make_llm(vision_name) if vision_name else self.llm
        from .config import Config

        self.config = Config()
        self.config.llm_topics = os.environ.get("MNEMOTRACE_LLM_TOPICS") != "0"
        if os.environ.get("MNEMOTRACE_MAX_HOT_FACTS"):
            self.config.max_hot_facts = int(os.environ["MNEMOTRACE_MAX_HOT_FACTS"])
        # 可选开启的 System-2 LLM 冲突检测 -> detect->confirm 循环（需要 LLM）。默认关闭，
        # 使离线/零配置路径保持确定性（纯规则的冲突处理）。
        if os.environ.get("MNEMOTRACE_CONFLICT_DETECTION") == "1" and self.llm is not None:
            self.config.conflict_detection = True
        # 运维侧的强制关闭开关。session outcomes 是唯一默认就要在每个 session 关闭时花一次 LLM 调用的
        # 功能，而部署中的服务端无法控制客户端在请求体里传什么——所以成本上限必须能在服务端设置，
        # 而不是逐请求设置。这意味着它不能只是个默认值：/v1/sessions/close 接受 `outcomes` 字段，
        # 且本仓库自己的 watcher（connectors/watch.py）导入每个 session 时都会传 `outcomes: True`，
        # 只改默认值的开关会恰好放走那个无人值守运行的调用方，让运维的成本上限形同虚设。
        # 因此保留该标记，并在 close_session 里作为强制覆盖执行。
        self._outcomes_forced_off = os.environ.get("MNEMOTRACE_SESSION_OUTCOMES") == "0"
        if self._outcomes_forced_off:
            self.config.session_outcomes = False
        self._hot: "OrderedDict[str, Memory]" = OrderedDict()
        self._hot_versions: dict[str, str | None] = {}
        self._locks: dict[str, NamespaceLock] = {}
        self._access = threading.local()
        self._g = threading.Lock()
        # 在线服务指标（Bet D 的在线版）：承载 SLO 的路径上的延迟分位数 + token 节省比，
        # 仅以聚合形式经 GET /metrics 暴露。见 mnemotrace/metrics.py。
        self.metrics = Metrics()
        # System-1 / System-2 拆分（AGENTS.md Bet F）。启用后 remember() 只做快速的 System-1 写入
        # （追加 + 轻量嵌入 + 持久化保存），把整合交给后台 worker，写入路径保持低延迟。
        # 默认关闭——同步路径保留了控制台、测试和现有调用方依赖的"remember 后立即可查"语义。
        self._async = os.environ.get("MNEMOTRACE_ASYNC_CONSOLIDATION") == "1"
        self._queue: "Optional[queue.Queue]" = None
        self._pending: set[str] = set()  # 已排队等待整合的用户（合并重复入队）
        self._worker: Optional[threading.Thread] = None
        if self._async:
            self._queue = queue.Queue()
            self._worker = threading.Thread(target=self._consume, name="mnemotrace-system2", daemon=True)
            self._worker.start()

    # --- namespace lifecycle ------------------------------------------------
    def _safe_user(self, user: str) -> str:
        return namespace_directory(user)

    def _contained_child(self, name: str) -> str:
        return contained_child(self.data_dir, name)

    def _secure_path(self, user: str) -> str:
        return namespace_path(self.data_dir, user)

    def _path(self, user: str) -> str:
        # 不认领旧名字：原始 namespace 只能通过自己的摘要目录访问存储。
        return self._secure_path(user)

    @contextmanager
    def _user_file_lock(self, user: str):
        # 锁位于摘要存储目录外，清空与重建存储时仍保留同一锁文件，
        # 避免等待中的进程和后续写入锁住不同文件。
        with self._coordinator(user).file_lock():
            yield

    def _coordinator(self, user: str) -> NamespaceLock:
        path = self._contained_child(f"@{self._safe_user(user)}.service.lock")
        state = namespace_lock(path)
        with self._g:
            self._locks[user] = state
        return state

    def _frames(self) -> dict[str, _Access]:
        if not hasattr(self._access, "frames"):
            self._access.frames = {}
        return self._access.frames

    def _discard(self, user: str) -> None:
        with self._g:
            self._hot.pop(user, None)
            self._hot_versions.pop(user, None)

    def _publish(self, user: str, mem: Memory) -> None:
        with self._g:
            self._hot[user] = mem
            self._hot_versions[user] = mem._snapshot_version
            self._hot.move_to_end(user)
            while len(self._hot) > self.max_hot_users:
                evicted, _ = self._hot.popitem(last=False)
                self._hot_versions.pop(evicted, None)

    @contextmanager
    def write_lock(self, user: str):
        """跨线程和 MCP stdio 进程串行化本地的读-改-存事务。"""
        with self.lock(user):
            frames = self._frames()
            previous = frames.get(user)
            if previous is not None and not previous.writing:
                raise PersistenceError("cannot upgrade an active read operation to a write transaction")
            with self._user_file_lock(user):
                if previous is None:
                    frames[user] = _Access(writing=True)
                try:
                    yield
                except BaseException:  # noqa: BLE001 -- 写入中断时也必须移除脏缓存
                    self._discard(user)
                    frames[user].memory = None
                    raise
                finally:
                    if previous is None:
                        frames.pop(user, None)

    def _store_version(self, user: str) -> str | None:
        return snapshot_version(self._path(user))

    def _save(self, user: str, mem: Memory) -> None:
        frame = self._frames().get(user)
        if (frame is None or not frame.writing or frame.memory is not mem
                or not self._coordinator(user).file_held):
            raise PersistenceError("save requires the current memory in a service write transaction")
        try:
            if mem._snapshot_version != self._store_version(user):
                raise PersistenceError("stale memory snapshot; reload before writing")
            mem.save()
        except BaseException:  # noqa: BLE001 -- 不保留只提交了部分内容的缓存对象
            self._discard(user)
            frame.memory = None
            raise
        self._publish(user, mem)

    def lock(self, user: str):
        return self._coordinator(user).thread_lock

    @contextmanager
    def _read_scope(self, user: str):
        with self.lock(user):
            frames = self._frames()
            previous = frames.get(user)
            if previous is not None:
                if previous.memory is None:
                    previous.memory = self._get_locked(user)
                yield previous.memory
                return
            frame = frames[user] = _Access()
            try:
                # 持久化索引在调入冷层事实时可能修改共享表，因此读取期间也要持文件锁。
                guard = self._user_file_lock(user) if self.config.storage == "lancedb" else nullcontext()
                with guard:
                    frame.memory = self._get_locked(user)
                    yield frame.memory
            finally:
                frames.pop(user, None)

    def get(self, user: str) -> Memory:
        """按 manifest 提交标识刷新缓存；嵌套调用复用本次操作的对象。

        返回的裸 Memory 只供顺序使用。并发修改及保存必须位于 write_lock 内；服务读取入口
        自行保持整个读取作用域，不能把本方法返回后的对象当成不可变快照。
        """
        with self._read_scope(user) as mem:
            return mem

    def _cached(self, user: str, version: str | None) -> Optional[Memory]:
        with self._g:
            if user in self._hot:
                if version == self._hot_versions.get(user):
                    self._hot.move_to_end(user)
                    return self._hot[user]
                self._hot.pop(user, None)
                self._hot_versions.pop(user, None)
        return None

    def _get_locked(self, user: str) -> Memory:
        cached = self._cached(user, self._store_version(user))
        if cached is not None and self.config.storage != "lancedb":
            return cached
        # 文件锁须覆盖全部集合的读取和缓存发布；保存会先逐个替换集合文件再提交 manifest，
        # 只保护 manifest 读取仍可能加载不同提交代次的集合。
        with self._user_file_lock(user):
            cached = self._cached(user, self._store_version(user))
            if cached is not None:
                return cached
            return self._load_locked(user)

    def _load_locked(self, user: str) -> Memory:
        if not self._coordinator(user).file_held:
            raise PersistenceError("snapshot loading requires the namespace file lock")
        try:
            try:
                mem = Memory.open(self._path(user), embedder=self.embedder, llm=self.llm, config=self.config)
            except (DimensionMismatchError, EmbedderMismatchError):
                if os.environ.get("MNEMOTRACE_REEMBED_ON_MISMATCH") != "1":
                    raise
                mem = Memory.open(self._path(user), allow_mismatch=True,
                                  embedder=self.embedder, llm=self.llm, config=self.config)
                mem.reembed()
                mem.save()
        except BaseException:  # noqa: BLE001 -- 迁移失败或中断时不能发布缓存
            self._discard(user)
            raise
        self._publish(user, mem)
        return mem

    def forget(self, user: str) -> dict:
        # 线程锁与目录外的稳定文件锁覆盖整个擦除过程，包括最后的目录删除。
        with self.write_lock(user):
            self._discard(user)
            self._frames()[user].memory = None
            # 与读取使用同一摘要路径；删除前重新校验真实目录边界及链接别名。
            target = self._path(user)
            if os.path.exists(target):
                if os.path.isdir(target):
                    # 标准库不会遍历目录符号链接或 Windows junction 的目标。
                    shutil.rmtree(target)
                else:
                    os.remove(target)
        return {"ok": True, "message": f"all memory for '{user}' erased"}

    @property
    def hot_count(self) -> int:
        return len(self._hot)

    def metrics_snapshot(self) -> dict:
        """/metrics 载荷：延迟分位数 + 计数器 + token 节省（来自 Metrics），外加服务级指标
        （内存中的热命名空间数、异步 System-2 积压）。只含聚合数字。"""
        snap = self.metrics.snapshot()
        snap["users_hot"] = self.hot_count
        snap["async"] = {
            "enabled": self._async,
            "queue_depth": self._queue.qsize() if self._queue is not None else 0,
            "pending_users": len(self._pending),
        }
        return snap

    # --- async System-2 consolidation (AGENTS.md Bet F; opt-in via MNEMOTRACE_ASYNC_CONSOLIDATION=1) --------
    def _enqueue(self, user: str) -> None:
        """把用户待整合的 episode 排入后台整合，合并重复的入队请求。"""
        with self._g:
            if user in self._pending:
                return
            self._pending.add(user)
        self._queue.put(user)  # type: ignore[union-attr]

    def _consume(self) -> None:
        """后台 worker：消费队列，在写入路径之外执行 System-2（整合 + 摘要）。"""
        while True:
            user = self._queue.get()  # type: ignore[union-attr]
            if user is None:  # 关闭哨兵
                self._queue.task_done()  # type: ignore[union-attr]
                break
            # 在整合之前先清除 pending，这样整合过程中新到的 episode 会触发下一轮整合，而不是被
            # 悄悄跳过（consolidate() 只处理尚未整合的 episode，重复一轮成本很低且幂等）。
            with self._g:
                self._pending.discard(user)
            try:
                self._consolidate_now(user)
            except Exception:  # noqa: BLE001 — 坏任务绝不能杀死 worker
                pass
            finally:
                self._queue.task_done()  # type: ignore[union-attr]

    def _consolidate_now(self, user: str) -> None:
        with self.write_lock(user):
            mem = self.get(user)
            try:
                mem.consolidate()
                mem.summarize_episodes(list(mem.episodes_doc.values()))
            finally:
                self._save(user, mem)

    def flush(self) -> None:
        """阻塞直到所有排队的 System-2 整合完成（用于测试 / 优雅停机）。
        同步模式下是空操作。"""
        if self._queue is not None:
            self._queue.join()

    def close(self) -> None:
        """停止后台 worker（尽力而为）。异步未开启时调用也安全。"""
        if self._worker is not None and self._queue is not None:
            self._queue.put(None)  # 哨兵
            self._worker.join(timeout=5)
            self._worker = None

    # --- 写入路径 -----------------------------------------------------------
    @timed("remember")
    def remember(self, user: str, content: str, session_id: str = "default",
                 scope: str = "auto") -> dict:
        """存储一条消息 + 执行 System-2 整合/摘要（尽力而为：模型瞬时故障也绝不丢失原始 episode）。
        `scope`（auto|long|working）按时效性路由——瞬态内容留在按日历史 + 工作记忆中，
        不会被提升为持久化的 profile fact（见 Memory.remember）。"""
        with self.write_lock(user):
            mem = self.get(user)
            routed = mem.remember(content, user_id=user, session_id=session_id, scope=scope)
            if routed["scope"] == "working":
                self._save(user, mem)
                return {"ok": True, "scope": "working", "kind": routed["kind"],
                        "id": routed["working_id"], "episode_id": routed["episode_id"],
                        "note": "kept in dated history (askable later); not added to the durable profile"}
            if self._async:
                # 仅 System-1：episode 在此完成嵌入 + 持久化保存；System-2（整合 + 摘要）
                # 在后台 worker 中运行，写入立即返回（Bet F）。
                self._save(user, mem)
                self._enqueue(user)
                return {"ok": True, "scope": "long", "queued": True, "episode_id": routed.get("episode_id"),
                        "note": "consolidation scheduled off the write path (async System-2)"}
            try:
                added = mem.consolidate().get("facts_added", 0)
                mem.summarize_episodes(list(mem.episodes_doc.values()))
            except Exception as exc:  # noqa: BLE001 — keep the raw episode no matter what
                self._save(user, mem)
                self.metrics.count("remember_degraded")
                return {"ok": True, "extracted": 0, "degraded": type(exc).__name__, "stored_raw": True}
            self._save(user, mem)
            return {"ok": True, "scope": "long", "extracted": added,
                    "total_facts": len([f for f in _all_facts(mem) if f.is_live()])}

    @timed("import")
    def import_(self, user: str, sessions: Optional[list] = None, format: str = "auto",
                data: Any = None, consolidate: Optional[bool] = None, summarize: bool = True,
                session_id: str = "imported", dedupe: bool = True) -> dict:
        """批量导入：要么传入预解析的 `sessions`（ImportSession/dict 列表），要么传入待解析的原始
        `data` 并用 `format` 指定格式（chatgpt/messages/records/jsonl/transcript/auto）。
        单次批量写入 + 整合。默认幂等：重复导入同一份导出会跳过已写入的 episode（计入 `skipped`）。
        `consolidate=None` 在 Memory.import_messages 内部决定：除 agent-session transcript 外的所有
        来源都做逐轮抽取；transcript 则先存储，留到关闭会话时再蒸馏。"""
        with self.write_lock(user):
            mem = self.get(user)
            if sessions is None:
                from .connectors import load_json, parse
                # 忆痕 导出快照可直接还原（fact 保留双时间轴标记，不做任何重新抽取）——
                # 要在消息历史解析器之前路由，否则 dict 会被误判为 'records' 而遭拒绝。
                obj = data
                if isinstance(obj, (str, bytes, bytearray)):
                    try:
                        obj = load_json(obj)
                    except Exception:  # noqa: BLE001 — 纯文本（transcript 等）：走下面的旧路径
                        obj = data
                if format == "mnemotrace" or (format == "auto" and is_snapshot_candidate(obj)):
                    stats = mem.import_snapshot(obj, user_id=user, dedupe=dedupe)
                    self._save(user, mem)
                    return {"ok": True, **stats}
                sessions = parse(data, format=format, session_id=session_id)
            stats = mem.import_messages(sessions, user_id=user, consolidate=consolidate,
                                        summarize=summarize, dedupe=dedupe)
            self._save(user, mem)
            return {"ok": True, **stats}

    @timed("import_document")
    def import_document(self, user: str, data, filename: Optional[str] = None,
                        content_type: Optional[str] = None, session_id: str = "document",
                        consolidate: bool = True, summarize: bool = True) -> dict:
        """写入一个上传的文档或图片（AGENTS.md 「代码导航」 多模态）。PDF/DOCX/文本被拍平为纯文本；图片由
        视觉模型生成描述（未配置时以占位符存储）——无论哪种方式都进入与其他内容相同的 System-2
        管线，立即可检索。"""
        from .connectors.documents import detect_kind, document_text, to_data_url, to_session
        from .llm import vision

        kind = detect_kind(data, filename, content_type)
        if kind == "image":
            cap = vision.caption_image(self.captioner, to_data_url(data, content_type))
            text = f"[image] {cap}" if cap else "[image] (no caption — set MNEMOTRACE_VISION_LLM to describe images)"
            session = to_session(text, filename=filename, session_id=session_id,
                                 metadata={"kind": "image", "media_type": content_type or ""})
        else:
            try:
                text = document_text(data, kind)
            except ImportError as exc:  # 可选抽取依赖缺失 -> 给出可操作的错误，而不是 500
                return {"ok": False, "kind": kind, "error": str(exc)}
            if not text:
                return {"ok": False, "kind": kind, "error": f"no extractable text in {filename or kind}"}
            session = to_session(text, filename=filename, session_id=session_id,
                                 metadata={"kind": kind, "media_type": content_type or ""})
        with self.write_lock(user):
            mem = self.get(user)
            stats = mem.import_messages([session], user_id=user, consolidate=consolidate,
                                        summarize=summarize)
            self._save(user, mem)
        return {"ok": True, "kind": kind, **stats}

    def add_fact(
        self,
        user: str,
        subject: str,
        predicate: str,
        object: str,
        sensitive: Optional[bool] = None,
        category: Optional[str] = None,
        topic: Optional[Topic] = None,
    ) -> dict:
        with self.write_lock(user):
            mem = self.get(user)
            f = mem.add_fact(
                subject,
                predicate,
                object,
                user_id=user,
                sensitive=sensitive,
                category=category,
                topic=topic,
            )
            self._save(user, mem)
            return {"ok": True, "id": f.id, "text": f.text,
                    "topic": f.topic, "topic_source": f.topic_source}

    def update_fact(self, user: str, fact_id: str, subject: Optional[str] = None,
                    predicate: Optional[str] = None, object: Optional[str] = None,
                    sensitive: Optional[bool] = None, category: Optional[str] = None,
                    statement: Optional[str] = None, topic: Optional[Topic] = None,
                    reset_topic: bool = False) -> Optional[dict]:
        with self.write_lock(user):
            mem = self.get(user)
            f = mem.update_fact(fact_id, subject=subject, predicate=predicate, object=object,
                                 sensitive=sensitive, category=category, statement=statement,
                                 topic=topic, reset_topic=reset_topic)
            if f is None:
                return None
            self._save(user, mem)
            # 返回实际 `source`：正文修正会成为用户断言，隐私/分类编辑则保留原来源。
            return {"ok": True, "id": f.id, "text": f.text, "display": display_of(f), "source": f.source,
                    "topic": f.topic, "topic_source": f.topic_source}

    def delete_fact(self, user: str, fact_id: str) -> dict:
        with self.write_lock(user):
            mem = self.get(user)
            ok = mem.delete_fact(fact_id)
            self._save(user, mem)
            return {"ok": ok}

    def clear_slot(self, user: str, subject: str, predicate: str, expect_count: int) -> dict:
        """硬删除某个 (subject, predicate) 槽位上的所有 fact。

        audit 的 slot_overflow 发现会让 owner 去清空该槽位；没有这个接口，控制台就只是
        "能诊断、无药可医"——在真实存储上意味着 84 行数据躲在 84 个确认对话框后面。这里选择
        擦除而非失效化：一个装了 84 个值的槽位，从来不曾包含"曾经为真、后来不再为真"的断言，
        因此没有值得保留的历史。这与 `delete_fact` 已有的"被遗忘权"路径一致。

        `expect_count` 是必需的乐观并发保护，不是可选项。owner 批准删除的是 audit 展示的那 N 行；
        如果期间存储发生了变化（一次 watcher 运行、另一个 session），拒绝是唯一安全的答案——
        悄悄删除超出批准范围的内容，恰恰是 AGENTS.md 「核心不变量」 禁止的损坏。把它做成可选，会让
        "无保护调用"成为 API 的默认形态，而这正是该操作绝不能有的形态。
        """
        with self.write_lock(user):
            mem = self.get(user)
            pred = predicate.strip().lower()
            # 待删集合必须与 audit() 分组的集合完全一致，否则计数保护会在比较两个不同群体的
            # 同时放行、删掉错误的行：
            #   * 与 audit() 一样精确匹配原始 user_id，其他用户的同名槽位不参与计数或删除。
            #   * 只取 live fact。audit() 只对 live fact 分组，因此 `expect_count` 是 live 计数；
            #     在这里匹配已被取代的行，既会让任何带 supersedes 链的槽位的保护永久失效，也会在
            #     保护被省略时硬删除 owner 从未见过的双时间轴历史
            #     （AGENTS.md 「对象与时间语义」：被取代的 fact 要失效化，绝不擦除）。
            #   * 跳过 source="user"。owner 亲手输入的 fact 不是抽取垃圾，audit() 出于同样原因
            #     也把它排除在分组之外。
            doomed = [f for f in _all_facts(mem)
                      if f.is_live() and f.source != "user"
                      and f.user_id == user
                      and f.subject == subject and f.predicate.strip().lower() == pred]
            if len(doomed) != expect_count:
                return {"ok": False, "deleted": 0, "found": len(doomed),
                        "expected": expect_count, "reason": "slot changed since it was audited"}
            for f in doomed:
                mem.delete_fact(f.id)
            self._save(user, mem)
            return {"ok": True, "deleted": len(doomed), "subject": subject, "predicate": predicate}

    def set_focus(self, user: str, track: Optional[list[str]] = None,
                  mute: Optional[list[str]] = None) -> dict:
        with self.write_lock(user):
            mem = self.get(user)
            focus = mem.set_focus(track=track, mute=mute)
            self._save(user, mem)
            return {"ok": True, "focus": deepcopy(focus)}

    def set_policy(self, user: str, **fields: Optional[str]) -> dict:
        with self.write_lock(user):
            mem = self.get(user)
            clean = {k: v for k, v in fields.items() if v is not None}
            result = mem.set_policy(**clean)
            self._save(user, mem)
            return {"ok": True, **result}

    # --- 读取路径 -----------------------------------------------------------
    @timed("recall")
    def recall(self, user: str, query: str, lean: bool = True, n_chunks: int = 6,
               session_id: Optional[str] = None, as_of: Optional[float] = None,
               redact_sensitive: bool = False,
               answer: bool = False) -> dict:
        """返回一份小的检索上下文（lean）或直接的事实性回答（lean=False）。设置 `session_id` 时，
        精简上下文还会带出该 session 的瞬态工作记忆。

        `answer=True`（HTTP /v1/recall 路径，用于控制台的"问答"视图）会额外基于该上下文生成一个
        真正的回答，并报告 full-context 基线的 token 数，UI 由此展示 token 节省。这要多花一次
        LLM 调用，因此只需要注入上下文的 MCP/OpenAI 兼容接入面保持关闭（默认）。"""
        with self._read_scope(user) as mem:
            if not lean:
                res = mem.search(query, user_id=user, as_of=as_of)
                visible = [f for f in res.facts[:10]
                           if not redact_sensitive or not getattr(f, "sensitive", False)]
                text = (res.answer() if not redact_sensitive else
                        (visible[0].object or visible[0].text) if visible else "I don't have that in memory.")
                return {"answer": text, "facts": [f.text for f in visible], "as_of": as_of,
                        "redacted_sensitive": redact_sensitive}
            ctx = mem.lean_context(query, user_id=user, n_chunks=n_chunks, session_id=session_id,
                                   as_of=as_of, redact_sensitive=redact_sensitive)
            out = {"context": ctx, "tokens_est": _est_tokens(ctx), "as_of": as_of,
                   "redacted_sensitive": redact_sensitive}
            if answer:
                full = "\n".join(ep.content for ep in mem.episodes_doc.values()
                                 if as_of is None or ep.event_time <= as_of)
                out["full_tokens"] = _est_tokens(full)
        # 记忆读取与状态调整已完成；回答生成只使用独立字符串，可在退出读取作用域后执行。
        if answer:
            out["answer"] = _answer_from_memory(self.answerer, query, ctx)
        self.metrics.tokens(out["tokens_est"], out.get("full_tokens"))
        return out

    @timed("recall_multi")
    def recall_multi(self, spaces: list[str], query: str, n_chunks: int = 6,
                     session_id: Optional[str] = None, as_of: Optional[float] = None,
                     redact_sensitive: bool = False) -> dict:
        """跨多个 space 读取并融合结果（AGENTS.md 「代码导航」——agent + 团队共享 + 用户记忆在读取时组合）。
        每个 space 贡献自己的精简上下文，并标注来源以保留来源溯源。不做跨 space 的图遍历
        （实体保存在各自 space 的存储里）；这里做的是 fact + chunk 组合，这正是多 scope 的价值
        所在——多跳规划器仍在单个 space 内工作。"""
        blocks: list[str] = []
        for s in spaces:
            with self._read_scope(s) as mem:
                ctx = mem.lean_context(query, user_id=s, n_chunks=n_chunks, session_id=session_id,
                                       as_of=as_of, redact_sensitive=redact_sensitive)
            if ctx.strip():
                blocks.append(f"## Space: {s}\n{ctx}")
        context = "\n\n".join(blocks)
        out = {"context": context, "tokens_est": _est_tokens(context), "spaces": list(spaces),
               "as_of": as_of, "redacted_sensitive": redact_sensitive}
        self.metrics.tokens(out["tokens_est"])
        return out

    @_read_access
    def structured_profile(self, user: str) -> dict:
        """L2 结构化 profile（基本信息 / 偏好 / 习惯，区分已确认与待确认）。仅用于展示。"""
        return self.get(user).structured_profile(user)

    # --- suspected conflicts (LLM-detected in System-2, user-confirmed; never auto-resolved) --------
    @_read_access
    def conflicts(self, user: str) -> dict:
        """等待用户裁决的可疑冲突。文本已本地化用于展示。"""

        mem = self.get(user)

        def disp(fid: str, fallback: str) -> str:
            f = mem.fact_store.get(fid) or mem.cold_store.get(fid)
            return display_of(f) if f is not None else fallback

        return {"conflicts": [{
            "id": c.id, "older": c.older, "newer": c.newer,
            "older_text": disp(c.older, c.text_older), "newer_text": disp(c.newer, c.text_newer),
            "reason": c.reason,
        } for c in mem.pending_conflicts(user)]}

    def resolve_conflict(self, user: str, conflict_id: str, keep: str = "newer") -> dict:
        """执行用户的裁决：保留较新/较旧的一方（取代另一方），或 both（撤销冲突）。
        用户是权威——绝不悄悄覆盖；这是人工确认闭环的最后一环。"""
        with self.write_lock(user):
            mem = self.get(user)
            ok = (mem.dismiss_conflict(conflict_id) if keep == "both"
                  else mem.resolve_conflict(conflict_id, keep=keep))
            self._save(user, mem)
            return {"ok": ok}

    # --- working memory (ephemeral, session/TTL-scoped; feature ①) ----------
    def add_working(self, user: str, content: str, session_id: str = "default", kind: str = "state",
                    ttl_seconds: Optional[float] = None) -> dict:
        with self.write_lock(user):
            mem = self.get(user)
            wm = mem.remember_working(content, user_id=user, session_id=session_id, kind=kind,
                                      ttl_seconds=ttl_seconds)
            self._save(user, mem)
            return {"ok": True, "id": wm.id, "kind": wm.kind, "expires_at": wm.expires_at}

    @_read_access
    def working_memory(self, user: str, session_id: Optional[str] = None) -> dict:
        mem = self.get(user)
        items = mem.working_memory(user, session_id=session_id)
        return {"items": [{
            "id": w.id, "content": w.content, "kind": w.kind, "session_id": w.session_id,
            "created": fmt_date(w.created_at),
            "expires_at": fmt_date(w.expires_at) if w.expires_at else None,
        } for w in items]}

    def clear_working(self, user: str, session_id: str) -> dict:
        with self.write_lock(user):
            mem = self.get(user)
            n = mem.clear_session(user, session_id)
            self._save(user, mem)
            return {"ok": True, "cleared": n}

    def close_session(
        self,
        user: str,
        session_id: str,
        summarize: bool = True,
        clear_working: bool = True,
        outcomes: Optional[bool] = None,
    ) -> dict:
        """面向 agent 客户端的会话收尾。

        不删除原始 transcript。它会处理指定 session 待完成的 System-2 工作，可选地为后续精简
        读取索引 session 摘要，把知识更新反映进这些摘要，清理瞬态工作记忆，并持久化该命名空间。
        这是 Claude Code / Codex / Cursor 类客户端在线程关闭或切换任务时可调用的生命周期钩子。
        """
        with self.write_lock(user):
            mem = self.get(user)
            episodes = [
                ep for ep in mem.episodes_doc.values()
                if ep.user_id == user and ep.session_id == session_id
            ]
            pending = [ep for ep in episodes if not ep.consolidated]
            stats = (
                mem.consolidate(pending)
                if pending
                else {"facts_added": 0, "duplicates": 0, "invalidated": 0}
            )
            summaries = mem.summarize_episodes(episodes) if summarize else 0
            reflected = mem.reflect(user) if summarize else 0
            working_cleared = mem.clear_session(user, session_id) if clear_working else 0

            # 会话结束时结论才存在、才值得记录：会话中途只有进行中的任务。正是这次写入让个人
            # 记忆保持有料——逐轮抽取器产出的是传记式三元组，而工作会话里根本没有这类内容。
            outcome_facts = 0
            want = self.config.session_outcomes if outcomes is None else outcomes
            if self._outcomes_forced_off:
                want = False  # MNEMOTRACE_SESSION_OUTCOMES=0 是运维设定的上限；任何请求体都不能解除
            if want and self.llm is not None and episodes:
                from .consolidate.outcomes import (
                    OUTCOME_PREDICATES,
                    extract_outcomes,
                    split_outcome_text,
                )

                # 同一个 session 关闭两次不能让结论翻倍。extract_outcomes 只在单次调用内去重，
                # 而重复关闭是常态：connectors/watch.py 会重发任何增长过的 transcript 并再次关闭。
                # 使用与它相同的 120 字符键。
                seen_keys = {
                    split_outcome_text(f.text)[0].lower()[:120]
                    for f in _all_facts(mem)
                    if f.is_live() and f.user_id == user
                    and f.predicate.lower() in OUTCOME_PREDICATES and f.subject == session_id
                }
                for fact in extract_outcomes(self.llm, episodes, user, session_id=session_id,
                                              llm_topics=self.config.llm_topics):
                    if split_outcome_text(fact.text)[0].lower()[:120] in seen_keys:
                        continue
                    fact.embedding = mem.embedder.embed(fact.text)
                    mem.fact_store.upsert(fact.id, fact.embedding, fact)
                    mem.engine.graph_builder.add_fact(fact)
                    outcome_facts += 1
                if outcome_facts:
                    mem._persona_cache.clear()

            self._save(user, mem)
            return {
                "ok": True,
                "session_id": session_id,
                "episodes": len(episodes),
                "pending_consolidated": len(pending),
                "facts_added": stats.get("facts_added", 0),
                "outcomes": outcome_facts,
                "duplicates": stats.get("duplicates", 0),
                "invalidated": stats.get("invalidated", 0),
                "summaries": summaries,
                "reflected": reflected,
                "working_cleared": working_cleared,
            }

    @_read_access
    def profile(self, user: str) -> dict:
        mem = self.get(user)
        return {"profile": mem.build_persona(user),
                "facts": [f.text for f in _all_facts(mem) if f.is_live()][:50]}

    @_read_access
    def get_focus(self, user: str) -> dict:
        return self.get(user).get_focus()

    @_read_access
    def get_policy(self, user: str) -> dict:
        return self.get(user).get_policy()

    @_read_access
    def agent_status(self, user: str, session_id: Optional[str] = None) -> dict:
        """agent 在使用 忆痕 前可检查的无内容状态。

        与 recall/profile/memories 不同，这里刻意不含 profile 文本、fact 文本、原始 episode、
        摘要和存储路径。它回答的是："我连的是哪个命名空间/session、这个 session 有没有工作状态、
        focus 策略是什么、接下来该做什么？"
        """
        mem = self.get(user)
        stats = self.stats(user)
        session_episodes = [
            ep for ep in mem.episodes_doc.values()
            if ep.user_id == user and (session_id is None or ep.session_id == session_id)
        ]
        session_working = [
            w for w in mem.working_mem.values()
            if w.user_id == user
            and w.is_live()
            and (session_id is None or w.session_id == session_id)
        ]
        pending_session_episodes = [ep for ep in session_episodes if not ep.consolidated]
        focus = mem.get_focus()
        next_actions = [
            "Call mnemotrace_recall before answering tasks that depend on prior user/project context.",
            "Call mnemotrace_remember(scope='long' or 'auto') for durable preferences, decisions, and reusable facts.",
            "Use scope='working' for current-task state that should be cleared when the session closes.",
            "Call mnemotrace_close_session when this thread ends or switches tasks.",
        ]
        if (stats.get("counts") or {}).get("pending_conflicts", 0):
            next_actions.append("Review pending conflicts before relying on disputed memories.")
        blind = self._embedder_blindness(mem, user)
        if blind:
            next_actions.append(
                f"{round(blind['ratio'] * 100)}% of this namespace's memories cannot be ranked by the "
                f"configured embedder ({blind['embedder']}); recall will return the same items for "
                f"unrelated queries. Ask the operator to run: {blind['migrate']}")
        return {
            "ok": True,
            "user": user,
            "session_id": session_id,
            "mode": "content_free_agent_status",
            "focus": focus,
            "session": {
                "id": session_id,
                "episodes": len(session_episodes),
                "episodes_pending": len(pending_session_episodes),
                "working_live": len(session_working),
            },
            "counts": stats.get("counts", {}),
            "consolidation_backlog": stats.get("consolidation_backlog", False),
            "feed": stats.get("feed"),
            "storage": stats.get("storage"),
            "embedder": stats.get("embedder"),
            "llm_configured": stats.get("llm_configured"),
            "recommended_next_actions": next_actions,
            "tools": {
                "read_context": "mnemotrace_recall",
                "write_memory": "mnemotrace_remember",
                "close_session": "mnemotrace_close_session",
                "inspect_facts": "mnemotrace_list_facts",
                "correct_fact": "mnemotrace_update_fact",
                "delete_fact": "mnemotrace_delete_fact",
                "focus": "mnemotrace_get_focus / mnemotrace_set_focus",
            },
        }

    @_read_access
    def session_report(
        self,
        user: str,
        session_id: str,
        include_sensitive: bool = False,
    ) -> dict:
        """审计某个特定 session 对记忆的贡献。

        与 `agent_status` 不同，这是显式的检查端点。它可以返回 fact 文本，但敏感 fact 默认脱敏，
        agent 可以放心展示会话后审计，而不会意外暴露私人的健康/财务/PII 细节。
        """

        mem = self.get(user)
        episodes = [
            ep for ep in mem.episodes_doc.values()
            if ep.user_id == user and ep.session_id == session_id
        ]
        episode_ids = {ep.id for ep in episodes}
        pending = [ep for ep in episodes if not ep.consolidated]
        session_facts = [
            f for f in _all_facts(mem)
            if f.user_id == user and episode_ids.intersection(f.provenance)
        ]
        session_facts.sort(key=lambda f: f.valid_at, reverse=True)
        redacted = 0

        def fact_view(f) -> dict:
            nonlocal redacted
            is_sensitive = bool(getattr(f, "sensitive", False))
            hidden = is_sensitive and not include_sensitive
            if hidden:
                redacted += 1
            base = {
                "id": f.id,
                "valid_at": fmt_datetime(f.valid_at),
                "invalid_at": fmt_datetime(f.invalid_at) if f.invalid_at else None,
                "status": "live" if f.is_live() else "superseded",
                "source": f.source,
                "category": getattr(f, "category", ""),
                "sensitive": is_sensitive,
                "redacted": hidden,
                "provenance": [ep for ep in f.provenance if ep in episode_ids],
            }
            if hidden:
                return {**base, "text": "[redacted sensitive fact]", "display": "[redacted sensitive fact]"}
            return {
                **base,
                "topic": f.topic, "topic_source": f.topic_source,
                "text": f.text,
                "display": display_of(f),
                "subject": f.subject,
                "predicate": f.predicate,
                "object": f.object,
            }

        facts_payload = [fact_view(f) for f in session_facts]
        working_live = [
            w for w in mem.working_mem.values()
            if w.user_id == user and w.session_id == session_id and w.is_live()
        ]
        return {
            "ok": True,
            "user": user,
            "session_id": session_id,
            "include_sensitive": include_sensitive,
            "episodes": len(episodes),
            "episodes_consolidated": len([ep for ep in episodes if ep.consolidated]),
            "episodes_pending": len(pending),
            "working_live": len(working_live),
            "facts_added": len(session_facts),
            "facts_redacted": redacted,
            "facts": facts_payload,
        }

    @_read_access
    def sessions(
        self,
        user: str,
        limit: Optional[int] = None,
        offset: int = 0,
        q: str = "",
    ) -> dict:
        """面向跨 agent 记忆管理的无内容 session 索引。

        刻意只返回计数和时间戳，不含原始 episode 文本、摘要、profile 文本或 fact 文本。
        产品可以先列出 Codex/Claude/应用的 session 列表，只在用户选择审计某个 session 时
        才调用 `session_report()`。
        """
        mem = self.get(user)
        sessions: dict[str, dict] = {}

        def ensure(sid: str) -> dict:
            if sid not in sessions:
                sessions[sid] = {
                    "id": sid,
                    "episodes": 0,
                    "episodes_consolidated": 0,
                    "episodes_pending": 0,
                    "facts_added": 0,
                    "facts_sensitive": 0,
                    "working_live": 0,
                    "summaries": 0,
                    "first_event_at": None,
                    "first_event_at_h": None,
                    "last_event_at": None,
                    "last_event_at_h": None,
                }
            return sessions[sid]

        episode_ids_by_session: dict[str, set[str]] = {}
        for ep in mem.episodes_doc.values():
            if ep.user_id != user:
                continue
            row = ensure(ep.session_id)
            row["episodes"] += 1
            if ep.consolidated:
                row["episodes_consolidated"] += 1
            else:
                row["episodes_pending"] += 1
            if ep.summary:
                row["summaries"] += 1
            first = row["first_event_at"]
            last = row["last_event_at"]
            if first is None or ep.event_time < first:
                row["first_event_at"] = ep.event_time
                row["first_event_at_h"] = fmt_datetime(ep.event_time)
            if last is None or ep.event_time > last:
                row["last_event_at"] = ep.event_time
                row["last_event_at_h"] = fmt_datetime(ep.event_time)
            episode_ids_by_session.setdefault(ep.session_id, set()).add(ep.id)

        for w in mem.working_mem.values():
            if w.user_id == user and w.is_live():
                ensure(w.session_id)["working_live"] += 1

        if episode_ids_by_session:
            for f in _all_facts(mem):
                if f.user_id != user:
                    continue
                provenance = set(f.provenance)
                if not provenance:
                    continue
                for sid, episode_ids in episode_ids_by_session.items():
                    if provenance.intersection(episode_ids):
                        row = ensure(sid)
                        row["facts_added"] += 1
                        if getattr(f, "sensitive", False):
                            row["facts_sensitive"] += 1

        needle = q.strip().lower()
        rows = list(sessions.values())
        if needle:
            rows = [row for row in rows if needle in row["id"].lower()]
        rows.sort(key=lambda row: row["last_event_at"] or 0, reverse=True)
        lim = _clamp_limit(limit, default=None, max_value=500)
        page_items, page = _page(rows, offset, lim)
        return {
            "ok": True,
            "user": user,
            "sessions": page_items,
            "page": {**page, "items": page_items},
            "next_offset": page["next_offset"],
        }

    @_read_access
    def graph(
        self,
        user: str,
        as_of: float | None = None,
        include_sensitive: bool = False,
        q: str = "",
        live_only: bool = False,
        limit: Optional[int] = None,
    ) -> dict:
        lim = _clamp_limit(limit, default=None, max_value=500)
        return self.get(user).graph_data(
            user,
            as_of=as_of,
            include_sensitive=include_sensitive,
            q=q,
            live_only=live_only,
            limit=lim,
        )

    @_read_access
    def memories(
        self,
        user: str,
        facts_limit: Optional[int] = None,
        facts_offset: int = 0,
        episodes_limit: Optional[int] = None,
        episodes_offset: int = 0,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        q: str = "",
        include_sensitive: bool = False,
        category: Optional[str] = None,
        outcome_kind: Optional[str] = None,
        expected_revision: Optional[str] = None,
        evaluated_at: Optional[float] = None,
        topic: Optional[Topic] = None,
    ) -> dict:
        """浏览已存储的记忆。

        默认是可安全分享的：非敏感 fact 加上无内容计数，不含 profile/原始 episode。
        设置 `include_sensitive=True` 时，这是 owner 可见的完整检查载荷：profile、
        原始 episode + L2 摘要，以及敏感 fact。

        `kind` 把 fact 集拆成读者实际会分开浏览的两类：'outcomes'（session 得出的结论）与
        'attributes'（其余全部）。未知值像 `status` 一样被忽略而不是报错。
        `topic` 必须是合法已存主题；unknown 同时匹配人工及未赋值的未知，只作用于事实。
        """
        from .consolidate.outcomes import OUTCOME_PREDICATES, split_outcome_text

        if topic is not None and (not isinstance(topic, str) or topic not in TOPICS):
            raise ValueError("topic must be one of " + ", ".join(TOPICS))
        if outcome_kind is not None and outcome_kind not in OUTCOME_PREDICATES:
            raise ValueError("outcome_kind must be decision, finding, lesson or open_question")
        if (expected_revision is None) != (evaluated_at is None):
            raise ValueError("expected_revision and evaluated_at must be provided together")
        if evaluated_at is not None and (not math.isfinite(evaluated_at) or evaluated_at < 0):
            raise ValueError("evaluated_at must be a finite non-negative Unix timestamp")
        mem = self.get(user)
        # Pin the token to the loaded object, not a second manifest read that could see a newer commit.
        data_revision = mem._snapshot_version or "empty"
        if expected_revision is not None and expected_revision != data_revision:
            raise MemoryRevisionChanged("记录已变化，请重新加载")
        # A commit fixes stored records; the evaluation time also fixes membership as time passes.
        evaluated_at = now() if evaluated_at is None else evaluated_at
        all_facts = sorted(_all_facts(mem), key=lambda f: (f.valid_at, f.id), reverse=True)
        episodes_all = sorted(mem.episodes_doc.values(), key=lambda ep: (ep.event_time, ep.id), reverse=True)

        needle = q.strip().lower()

        def fact_view(f) -> dict:
            return {
                "id": f.id, "text": f.text, "display": display_of(f),
                "subject": f.subject, "predicate": f.predicate,
                "object": f.object, "valid_at": fmt_datetime(f.valid_at),
                "invalid_at": fmt_datetime(f.invalid_at) if f.invalid_at else None,
                "status": "live" if f.is_live(as_of=evaluated_at) else "superseded",
                "source": f.source, "supersedes": f.supersedes,
                "category": getattr(f, "category", ""), "sensitive": getattr(f, "sensitive", False),
                "topic": f.topic, "topic_source": f.topic_source,
                "salience": round(f.salience, 2), "provenance": f.provenance,
                # outcome 的 `text` 是"结论 （依据：why）"——单个内嵌字符串，因为检索匹配的就是它。
                # 读者需要把两者分开，所以在这里拆一次，而不是要求每个接入面各自重新解析分隔符。
                # 始终存在，attributes 为 ""。
                "why": split_outcome_text(f.text)[1]
                       if f.predicate.lower() in OUTCOME_PREDICATES else "",
            }

        def episode_view(ep) -> dict:
            return {
                # fact 以 episode id 的形式携带来源溯源；没有这里的 id，拿到 fact 的调用方
                # 就无法回答"这条从哪来？"——而这正是 provenance 的全部意义。
                "id": ep.id,
                "date": ep.metadata.get("date") or fmt_date(ep.event_time),
                "session": ep.session_id,
                # 返回全文，而不是 500 字符的截断片段。这个视图是 owner 回答"我到底写了什么"
                # 的方式——这也是普通 .md 文件一直做对、而此前这里没做对的一点。想要预览的调用方
                # 自行切片；想要完整记录的调用方需要全文。响应大小已由 episodes_limit/episodes_offset
                # 分页约束。
                "content": ep.content,
                "chars": len(ep.content),
                "summary": ep.summary,
            }

        filtered_facts = all_facts
        # 只作用于 fact：episode 没有 predicate，因此 `kind` 不会影响 filtered_episodes。
        kind_key = (kind or "").strip().lower()
        if kind_key == "outcomes":
            filtered_facts = [f for f in filtered_facts if f.predicate.lower() in OUTCOME_PREDICATES]
        elif kind_key == "attributes":
            filtered_facts = [f for f in filtered_facts if f.predicate.lower() not in OUTCOME_PREDICATES]
        if outcome_kind is not None:
            filtered_facts = [f for f in filtered_facts if f.predicate.lower() == outcome_kind]
        if not include_sensitive:
            filtered_facts = [f for f in filtered_facts if not getattr(f, "sensitive", False)]
        def category_of(f) -> str:
            return (getattr(f, "category", "") or "").strip() or "其他"

        # 分类候选来自本空间、同一事实类型及可见范围，不随分页、状态和搜索结果消失。
        fact_categories = sorted({category_of(f) for f in filtered_facts}, key=lambda value: (value.casefold(), value))
        if topic is not None:
            filtered_facts = [f for f in filtered_facts if f.topic == topic]
        if category and category.strip():
            filtered_facts = [f for f in filtered_facts if category_of(f) == category.strip()]
        if status in {"live", "current"}:
            filtered_facts = [f for f in filtered_facts if f.is_live(as_of=evaluated_at)]
        elif status in {"superseded", "old", "history"}:
            filtered_facts = [f for f in filtered_facts if not f.is_live(as_of=evaluated_at)]
        if needle:
            def matches_fact(f) -> bool:
                haystack = " ".join([
                    f.text,
                    display_of(f),
                    f.subject,
                    f.predicate,
                    f.object,
                    getattr(f, "category", ""),
                ]).lower()
                return needle in haystack
            filtered_facts = [f for f in filtered_facts if matches_fact(f)]

        filtered_episodes = [] if not include_sensitive else episodes_all
        if needle and include_sensitive:
            filtered_episodes = [
                ep for ep in filtered_episodes
                if needle in " ".join([ep.content or "", ep.summary or "", ep.session_id or ""]).lower()
            ]

        facts_lim = _clamp_limit(facts_limit, default=None, max_value=FACTS_PAGE_LIMIT)
        episodes_lim = _clamp_limit(episodes_limit, default=None, max_value=EPISODES_PAGE_LIMIT)
        facts_page_items, facts_page = _page(filtered_facts, facts_offset, facts_lim)
        episodes_page_items, episodes_page = _page(filtered_episodes, episodes_offset, episodes_lim)
        facts_payload = [fact_view(f) for f in facts_page_items]
        episodes_payload = [episode_view(ep) for ep in episodes_page_items]
        return {
            "user": user,
            "data_revision": data_revision,
            "evaluated_at": evaluated_at,
            "profile": mem.build_persona(user) if include_sensitive else "",
            "counts": {"episodes": len(episodes_all),
                       "facts_live": sum(1 for f in all_facts if f.is_live(as_of=evaluated_at)),
                       "facts_superseded": sum(1 for f in all_facts if not f.is_live(as_of=evaluated_at)),
                       # 与 facts_live/facts_superseded 一样，基于未过滤集合统计：若计数随当前
                       # 过滤收缩，调用方就无法判断 outcomes 到底存不存在。
                       "facts_outcomes": sum(1 for f in all_facts
                                             if f.is_live(as_of=evaluated_at) and f.predicate.lower() in OUTCOME_PREDICATES),
                       "summaries": len(mem.summary_vec.values())},
            "facts": facts_payload,
            "fact_categories": fact_categories,
            "episodes": episodes_payload,
            "facts_page": {**facts_page, "items": facts_payload},
            "episodes_page": {**episodes_page, "items": episodes_payload},
            "next_offsets": {
                "facts": facts_page["next_offset"],
                "episodes": episodes_page["next_offset"],
            },
        }

    def _embedder_blindness(self, mem: Memory, user: str) -> Optional[dict]:
        """当配置的 embedder 无法对命名空间内的内容排序时，给出一条存储级发现。

        在 owner 的真实存储上实测：HashingEmbedder 对每对中文查询/fact 都打 0.000 分，
        /v1/recall 对三个不相关的查询返回了完全相同的四条 fact，而产品里没有任何地方说明原因。根因是结构性的——
        hashing 词干器只输出 [a-z0-9] token，以 CJK 为主的文本几乎哈希不出任何东西，
        BM25 看到的也是同样的空 token 流——因此无需模型调用即可检测：统计语料中非 ASCII 的占比。
        由 audit() 和 agent_status() 共享，保证控制台与 agent 看到同一个结论。
        """
        from .embed import HashingEmbedder

        embedder = self.embedder
        if isinstance(embedder, HashingEmbedder):
            exact = True  # 这种失明是分词器的固有属性，不是启发式
        elif (embedder.__class__.__name__ == "SentenceTransformerEmbedder"
              and "-en-" in str(getattr(embedder, "model_name", "") or "")):
            exact = False  # 纯英文模型在 CJK 上是退化而非归零
        else:
            return None

        def split(text: str) -> tuple[int, int]:
            """返回 (rankable, dark) 字符数。rankable = ASCII 字母数字，是 hashing 词干器
            [a-z0-9]+ 分词器唯一保留的内容；dark = 其他任何文字系统的字母，会被直接丢弃。
            标点和空白两者都不算：一段 JSON 或一行 shell 是 100% 可排序的，哪怕大部分字符是
            花括号和管道符——第一版把这些计入不利侧，把一个代码类 fact 的存储判定为
            "33% 不可排序"。"""
            rankable = dark = 0
            for c in text:
                if c.isascii():
                    if c.isalnum():
                        rankable += 1
                elif c.isalpha():
                    dark += 1
            return rankable, dark

        def blind(text: str) -> bool:
            rankable, dark = split(text)
            return (rankable + dark) > 0 and dark / (rankable + dark) >= 0.5

        facts = [f for f in _all_facts(mem)
                 if f.user_id == user and f.is_live()]
        episodes = sorted((ep for ep in mem.episodes_doc.values() if ep.user_id == user),
                          key=lambda ep: ep.ingested_at, reverse=True)[:2000]
        facts_blind = sum(1 for f in facts if blind(f.text or ""))
        episodes_blind = sum(1 for ep in episodes if blind(ep.content or ""))
        checked = len(facts) + len(episodes)
        total_blind = facts_blind + episodes_blind
        # 按字母量而非条目数设门槛：一个存着两份 12 万字符中文 transcript 的命名空间完全不可排序，
        # 不能因为"只有 2 条记忆"而保持沉默。200 个字母是一段的量——足以让只有三行内容的存储
        # 不触发，也小到一打单行笔记不会触发。（十几条英文 fact 里带一个 CJK 人名，无论哪种口径
        # 都远低于 20% 的占比。）
        rankable_chars = dark_chars = 0
        for text in [f.text or "" for f in facts] + [ep.content or "" for ep in episodes]:
            r, d = split(text)
            rankable_chars += r
            dark_chars += d
        if rankable_chars + dark_chars < 200:
            return None
        ratio = total_blind / checked if checked else 0.0
        mass_ratio = dark_chars / (rankable_chars + dark_chars)
        if ratio < 0.20 and mass_ratio < 0.20:
            return None
        ratio = max(ratio, mass_ratio)
        name = embedder.__class__.__name__
        pct = round(ratio * 100)
        migrate = "MNEMOTRACE_EMBEDDER=multilingual MNEMOTRACE_REEMBED_ON_MISMATCH=1"
        return {
            "kind": "embedder_blind",
            "text": f"{name} cannot rank {pct}% of {checked} memories",
            "why": (f"{pct}% of what this namespace holds is written in a script {name} cannot "
                    f"tokenize — it only indexes [a-z0-9] tokens, so those memories are invisible to both "
                    f"the semantic and lexical channels and recall returns the same items for unrelated "
                    f"queries"),
            "action": (f"switch the server embedder and re-embed: {migrate} "
                       f"(or bge-small-zh, 92MB, if memory is tight)"),
            "embedder": name,
            "model": getattr(embedder, "model_name", None),
            "blind": total_blind, "checked": checked, "ratio": ratio,
            "dark_chars": dark_chars, "rankable_chars": rankable_chars,
            "facts_blind": facts_blind, "episodes_blind": episodes_blind,
            "exact": exact, "recommended": "multilingual", "alternative": "bge-small-zh",
            "migrate": migrate,
        }

    @_read_access
    def audit(self, user: str, limit: int = 40) -> dict:
        """记忆健康检查：把人们会想修的条目连同原因一并呈现。

        个人记忆存储的意义不在于记住很多，而在于可以检查并修正它记住的东西。真实语料上的
        抽取必然产生垃圾（从源文本里抄来的 snake_case token、object 重复 predicate 的 fact、
        永远不会被召回的一次性噪声），而今天除非去翻原始 fact 列表，否则这些全都不可见。
        每条发现都带 `why` 和建议的 `action`，让控制台能提供修复入口，而不只是一个计数。
        """
        import re

        from .consolidate.outcomes import OUTCOME_PREDICATES
        from .consolidate.structured import _BASIC

        mem = self.get(user)
        # 与 clear_slot() 精确匹配同一 user_id，审计计数与实际删除集合必须一致。
        facts = [f for f in _all_facts(mem)
                 if f.user_id == user and f.is_live()]
        graph = mem.graph_data(user, include_sensitive=True)
        referenced = {e["source"] for e in graph.get("edges", [])} | {e["target"] for e in graph.get("edges", [])}

        findings: list[dict] = []

        def add(kind: str, why: str, action: str, fact=None, entity: str = "") -> None:
            row = {"kind": kind, "why": why, "action": action}
            if fact is not None:
                row.update({"fact_id": fact.id, "text": fact.text, "display": display_of(fact), "subject": fact.subject,
                            "predicate": fact.predicate, "object": fact.object,
                            "source": fact.source, "valid_at": fact.valid_at,
                            "valid_at_h": fmt_date(fact.valid_at)})
            if entity:
                row["entity"] = entity
            findings.append(row)

        # 预扫描：一个存着 84 条 live `occupation` fact 的存储，问题不在任何一行——每一行都能通过
        # 单条规则——而在于一个单值身份槽位装了 84 个值。只有 _BASIC predicate 适用此规则：
        # `likes` 有 20 个值是正常的列表，`occupation` 有 3 个则不可能。每个槽位只报告一次，
        # 且其 fact 跳过单条循环，避免一个坏槽位把其他发现埋在 84 条重复行下面。
        by_slot: dict[tuple[str, str], list] = {}
        for f in facts:
            # owner 亲手输入的 fact 按定义不是抽取产物，因此不可能是这条规则所指的垃圾——
            # 而且它若在批量删除中丢失，是槽位里代价最高的损失。clear_slot() 使用同样的排除：
            # 两者必须描述同一群体，否则计数保护比较的是一个集合，删除执行在另一个上。
            if f.source == "user":
                continue
            by_slot.setdefault((f.subject, f.predicate), []).append(f)
        overflowed: set[str] = set()
        for (subject, predicate), group in by_slot.items():
            if len(group) < 3 or predicate.lower() not in _BASIC:
                continue
            field, label = _BASIC[predicate.lower()]
            # ……而且只针对单值字段。_BASIC 还归一化 children / speaks / graduated_from，
            # 对它们来说三个值意味着一个人有三个孩子、三种语言、三个学位——并非抽取垃圾。
            # 对这些字段告诉 owner "单值属性不可能有 3 个值"是错误的，而该发现附带一键不可逆删除。
            if field in _MULTI_VALUED_FIELDS:
                continue
            findings.append({
                "kind": "slot_overflow",
                # 文案与其他规则保持同一语言；控制台会用 count/predicate/label 自行拼装本地化
                # 句子，而不渲染这条文案，因为一键不可逆删除必须用读者的语言来解释。
                "why": f"{len(group)} facts all claim {label} ({predicate}) — a single-valued slot "
                       f"cannot have {len(group)} values; these are extraction fragments, not memories",
                "action": "look at the samples, then clear the slot",
                "subject": subject, "predicate": predicate, "label": label, "count": len(group),
                "samples": [{"fact_id": g.id, "text": g.text} for g in group[:5]],
            })
            overflowed.update(g.id for g in group)

        # 存储级，而非单条：当 embedder 无法对语料排序时，任何单条发现都不得要领——
        # owner 问的是"为什么 recall 总是返回同样四条 fact"，答案是 embedder 本身，
        # 只报告一次并附上迁移命令。
        row = self._embedder_blindness(mem, user)
        if row:
            findings.append(row)

        for f in facts:
            # outcome 语句是 400 字符级别的长句，且经常包含配置路径，因此 unreduced_claim 和
            # code_artifact 会把大多数会话结论误判为垃圾。它们本来就以完整形式写入；
            # 单条规则的存在目的是抓住抽取器搞砸的三元组。
            if f.predicate.lower() in OUTCOME_PREDICATES or f.id in overflowed:
                continue
            obj, pred = f.object.strip(), f.predicate.strip()
            # snake_case 的 object 是从未被转成取值的源文本："user interested in
            # kind_bar_fruit_nut_flavor" 读起来就是机器输出，检索时匹配不到任何自然表述。
            if "_" in obj and " " not in obj and len(obj) > 12:
                add("machine_token", f"object '{obj}' looks like a raw token, not a value",
                    "edit into natural wording, or delete if meaningless", f)
            # object 复述 predicate 时不含任何信息（"occupation: named Rex"）。
            elif obj.lower().replace("_", " ") == pred.lower().replace("_", " "):
                add("empty_value", f"object repeats the predicate ('{pred}')",
                    "delete: it asserts nothing", f)
            # 句子长度的 object 是抽取器没能压缩成 fact 的断言。
            elif len(obj) > 120:
                add("unreduced_claim", f"object is {len(obj)} chars — a sentence, not a value",
                    "shorten to the value, or delete", f)
            # 以下规则来自对真实个人语料（笔记 + 技术文档）的审计，离线抽取器在其中批量产出
            # 这些形态。没有它们，这个检查会在一个明显满是碎片的存储上报告"一切正常"。
            elif re.search(r'[)"\'`\]]\s*$|^[("\'`\[]', obj) or "`" in obj:
                add("fragment", f"object '{obj[:40]}' carries stray punctuation — a clipped sentence",
                    "rewrite as a plain value, or delete", f)
            elif re.search(r"[/\\]|\.(py|ts|md|json|yaml|sh)\b", obj):
                add("code_artifact", f"object '{obj[:40]}' looks like a path or code, not a memory",
                    "delete: source-code detail does not belong in a personal profile", f)
            elif len(obj.split()) <= 2 and obj.isupper():
                add("empty_value", f"object '{obj}' is a bare token with no statement around it",
                    "delete: it asserts nothing on its own", f)
            elif f.subject.lower() in {"this", "that", "it", "these", "those", "there", "here"}:
                add("dangling_subject", f"subject is the pronoun '{f.subject}' — no identifiable entity",
                    "delete, or rewrite naming who/what it refers to", f)

        for node in graph.get("nodes", []):
            if node.get("id") not in referenced:
                add("orphan_entity", f"entity '{node.get('name')}' has no relations",
                    "harmless, but it means no fact connects it", entity=node.get("name", ""))

        findings.sort(key=lambda row: _AUDIT_KIND_PRIORITY.get(row["kind"], 3))
        by_kind: dict[str, int] = {}
        for row in findings:
            by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
        return {
            "user": user,
            "checked": {"facts": len(facts), "entities": len(graph.get("nodes", []))},
            "total_findings": len(findings),
            "by_kind": by_kind,
            "findings": findings[:limit],
            "truncated": len(findings) > limit,
        }

    @_read_access
    def stats(self, user: str) -> dict:
        """面向仪表盘/就绪探针的无内容命名空间统计。刻意不含 profile 文本、fact 文本、
        episode 片段和数据路径，因此在生产环境中轮询是安全的。"""
        mem = self.get(user)
        episodes = [ep for ep in mem.episodes_doc.values() if ep.user_id == user]
        hot_facts = [f for f in mem.fact_store.values() if f.user_id == user]
        cold_facts = [f for f in mem.cold_store.values() if f.user_id == user]
        facts = hot_facts + cold_facts
        facts_by_id = {f.id: f for f in facts}
        working = [w for w in mem.working_mem.values() if w.user_id == user]
        live_facts = [f for f in facts if f.is_live()]
        superseded = [f for f in facts if not f.is_live()]
        sensitive = [f for f in facts if getattr(f, "sensitive", False)]
        pending_conflicts = [
            c for c in mem.conflicts.values()
            if c.user_id == user and c.status == "pending"
        ]
        consolidated_episodes = [ep for ep in episodes if ep.consolidated]
        pending_episodes = [ep for ep in episodes if not ep.consolidated]
        ephemeral_episodes = [ep for ep in episodes if ep.metadata.get("ephemeral")]
        event_times = [ep.event_time for ep in episodes]
        fact_times = [f.valid_at for f in facts]
        user_entities = [e for e in mem.graph.entities.values() if e.user_id == user]
        all_relations = mem.graph.relations()
        user_entity_ids = {e.id for e in user_entities}
        user_relations = [
            r for r in all_relations
            if r.subject_id in user_entity_ids or r.object_id in user_entity_ids
        ]
        referenced_entity_ids = {
            eid for r in user_relations for eid in (r.subject_id, r.object_id)
        }
        stale_relations = [r for r in user_relations if r.fact_id not in facts_by_id]
        # Feed：这台机器的 agent 会话到底有没有在给这份记忆喂数据？在此功能出现前测得：owner
        # 自己的 1909 个 session 里，remember/recall/close_session 的调用次数为零——空记忆与
        # 健康记忆看起来一模一样，除非有什么东西说明上次喂入时间。由 episode 元数据推导，
        # 不持久化任何内容。
        from .consolidate.outcomes import OUTCOME_PREDICATES

        fed = [ep for ep in episodes if ep.metadata.get("source") == "agent_session"]
        fed_sessions = {ep.session_id for ep in fed}
        last_fed_at = max((ep.ingested_at for ep in fed), default=None)
        feed = {
            "last_fed_at": last_fed_at,
            "last_fed_at_h": fmt_datetime(last_fed_at) if last_fed_at is not None else None,
            "sessions": len(fed_sessions),
            "conclusions": sum(1 for f in live_facts
                               if f.predicate.lower() in OUTCOME_PREDICATES and f.subject in fed_sessions),
        }
        return {
            "user": user,
            "counts": {
                "episodes": len(episodes),
                "episodes_consolidated": len(consolidated_episodes),
                "episodes_pending": len(pending_episodes),
                "episodes_ephemeral": len(ephemeral_episodes),
                "facts_hot": len(hot_facts),
                "facts_cold": len(cold_facts),
                "cold_pages_out": int(mem.cold_pages_out.get(user, 0)),
                "cold_pages_in": int(mem.cold_pages_in.get(user, 0)),
                "facts_live": len(live_facts),
                "facts_superseded": len(superseded),
                "facts_sensitive": len(sensitive),
                "working_live": sum(1 for w in working if w.is_live()),
                "summaries": len([s for s in mem.summary_vec.values() if s.user_id == user]),
                "entities": len(user_entities),
                "relations": sum(1 for r in user_relations if r.fact_id in facts_by_id),
                "graph_orphan_entities": sum(1 for e in user_entities if e.id not in referenced_entity_ids),
                "graph_stale_relations": len(stale_relations),
                "pending_conflicts": len(pending_conflicts),
            },
            "time_range": {
                "first_event_at": min(event_times) if event_times else None,
                "first_event_at_h": fmt_datetime(min(event_times)) if event_times else None,
                "last_event_at": max(event_times) if event_times else None,
                "last_event_at_h": fmt_datetime(max(event_times)) if event_times else None,
                "oldest_fact_valid_at": min(fact_times) if fact_times else None,
                "oldest_fact_valid_at_h": fmt_datetime(min(fact_times)) if fact_times else None,
                "newest_fact_valid_at": max(fact_times) if fact_times else None,
                "newest_fact_valid_at_h": fmt_datetime(max(fact_times)) if fact_times else None,
            },
            "storage": self.config.storage,
            "max_hot_facts": self.config.max_hot_facts,
            "embedder": self.embedder.__class__.__name__,
            "llm_configured": self.llm is not None,
            "answerer_configured": self.answerer is not None,
            "consolidation_backlog": bool(pending_episodes),
            "feed": feed,
        }

    @_read_access
    def export(self, user: str, include_sensitive: bool = False) -> dict:
        """数据导出。

        默认是可安全分享的结构化导出：仅含非敏感 fact 及由这些 fact 推导出的图。自由文本层
        （profile、原始 episode、摘要）被省略，因为它们可能把敏感内容揉进行文里。
        设置 `include_sensitive=True` 时，这是全保真的可迁移导出：每个 fact 的双时间轴标记 +
        来源溯源、原始 episode、摘要、profile、focus 和图。
        """
        mem = self.get(user)
        _facts = [f for f in _all_facts(mem)
                  if include_sensitive or not getattr(f, "sensitive", False)]
        graph = mem.graph_data(user, include_sensitive=include_sensitive)
        out = {
            "user": user,
            "include_sensitive": include_sensitive,
            "redacted_sensitive": not include_sensitive,
            "profile": mem.build_persona(user) if include_sensitive else "",
            "focus": mem.get_focus(),
            "facts": [{
                "id": f.id, "subject": f.subject, "predicate": f.predicate, "object": f.object,
                "text": f.text, "source": f.source, "status": "live" if f.is_live() else "superseded",
                "category": getattr(f, "category", ""), "sensitive": getattr(f, "sensitive", False),
                "topic": f.topic, "topic_source": f.topic_source,
                "salience": round(f.salience, 3), "confidence": f.confidence,
                "valid_at": f.valid_at, "valid_at_h": fmt_date(f.valid_at),
                "invalid_at": f.invalid_at, "invalid_at_h": fmt_date(f.invalid_at) if f.invalid_at else None,
                "created_at": f.created_at, "expired_at": f.expired_at,
                "supersedes": f.supersedes, "provenance": f.provenance,
            } for f in sorted(_facts, key=lambda x: x.valid_at, reverse=True)],
            "episodes": [] if not include_sensitive else [{
                "id": ep.id, "session_id": ep.session_id, "speaker": ep.speaker,
                "event_time": ep.event_time, "date": ep.metadata.get("date") or fmt_date(ep.event_time),
                "content": ep.content, "summary": ep.summary,
            } for ep in mem.episodes_doc.values()],
            "graph": graph,
        }
        if not include_sensitive:
            out["redaction_note"] = (
                "Sensitive facts and all free-text layers (profile, raw episodes, summaries) were omitted."
            )
        return out
