"""公开门面（facade）。将 System-1 写入、System-2 整合，以及 hybrid + multi-hop 读路径组织到
一个小 API 之后：add() / consolidate() / search() / as_of() / history() / profile()。

默认完全离线（hashing embedder、规则抽取器、内存 store），因此 `Memory()` 零配置即可运行。
传入真实的 `embedder` / `llm` / store 工厂即可在 benchmark 后端上运行。
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from .config import Config
from .consolidate import ConsolidationEngine, is_single_valued, reinforce
from .consolidate.llm_extractor import EXTRACT_SYSTEM
from .consolidate.summarizer import (
    PERSONA_SYSTEM,
    SESSION_SUMMARY_SYSTEM,
    ProfileBuilder,
    SessionSummarizer,
)
from .embed import Embedder, HashingEmbedder
from .ingest import Ingestor
from .llm import LLM
from .localize import display_of
from .retrieve import (
    HybridRetriever,
    MultiHopPlanner,
    extract_aggregation_candidates,
    history,
    plan_evidence,
    render_aggregation_candidates,
)
from .retrieve.lexical import bm25_scores, overlap_terms, stems
from .store import (
    GraphStore,
    InMemoryDocStore,
    InMemoryGraphStore,
    InMemoryVectorStore,
    VectorStore,
    load_memory,
    save_memory,
)
from .types import Conflict, Episode, Fact, WorkingMemory
from .util import DAY, fmt_date, now


class InvalidFactEditError(ValueError):
    """编辑请求不能安全地应用到该事实，且尚未修改存储。"""


# 可编辑的记忆策略 prompt（控制台的"提示词" / "要记录什么记忆"）。默认值为内置 prompt；
# 每用户覆盖项（空字符串 = 使用默认值）存储在 Memory.policy 中。
POLICY_DEFAULTS = {
    "extract_instruction": "",  # 追加指令：记录什么 / 忽略什么
    "extract_system": EXTRACT_SYSTEM,
    "summary_system": SESSION_SUMMARY_SYSTEM,
    "persona_system": PERSONA_SYSTEM,
}

# 过于泛化的词，单凭它们无法确认某个属性（"favorite food" 不能仅因都含 "favorite" 就匹配到
# "favorite programming language"）。
_GENERIC_ATTR_TERMS = {"favorite", "favourite", "name", "is", "are", "of", "the"}

# 答案类型对齐（#2/#3）：当问题要求结构化的值（id、日期、数字、邮箱、电话、url）时，答案的
# object 必须真的长得像该类型——否则高语义匹配是虚假的（例如问 "what's the project ID?" 却
# 检索到项目负责人的名字）。此时我们改为给出类型匹配的 fact，或拒答，而不是自信地返回类型
# 不符的 top fact。线索刻意取得很强（id/编号，而非单独的 "when"），以避免对自由文本答案误判拒答。
_ANSWER_TYPE_CUES = {
    "email": ("email", "e-mail", "邮箱", "邮件地址"),
    "url": ("url", "链接", "网址", "link to"),
    "phone": ("phone number", "telephone", "电话", "手机号", "联系电话", "phone"),
    "id": ("id", "编号", "工单号", "订单号", "identifier", "order number", "ticket number", "serial"),
    "date": ("what date", "which day", "date of", "日期", "什么时候", "哪天", "哪一天", "几号", "哪一年"),
    "number": ("how many", "how much", "number of", "多少", "几个", "数量", "几次", "几年", "几岁"),
}
_ANSWER_TYPE_MATCH = {
    "email": lambda o: bool(re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", o)),
    "url": lambda o: bool(re.search(r"https?://|www\.", o)),
    "phone": lambda o: bool(re.search(r"\+?\d[\d\s().\-]{6,}\d", o)),
    # id 是含数字、无空格的字母数字编码（PROJ-1024、A12B）；普通名字不具备这些特征
    "id": lambda o: bool(re.search(r"\d", o)) and len(o.strip()) <= 40 and " " not in o.strip(),
    "date": lambda o: bool(re.search(r"\b\d{4}\b|\d{1,2}[-/]\d{1,2}|年|月|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec", o.lower())),
    "number": lambda o: bool(re.search(r"\d", o)),  # 宽松：任意数字（数量/时长/年龄）
}
_TEMPORAL_HISTORY_RE = re.compile(
    r"\b(before|previous|previously|former|formerly|used\s+to|past|old(?:er)?|prior)\b|"
    r"以前|之前|曾经|过去|原来|从前|上一",
    re.IGNORECASE,
)
_PROCEDURAL_QUERY_RE = re.compile(
    r"\b(how\s+(?:do|should|can|to|would|am|is)|procedure|process|workflow|runbook|instruction|"
    r"rule|policy|protocol|steps?|checklist|always|never|remind|remember\s+to)\b|"
    r"怎么|如何|步骤|流程|规则|指令|操作|办法|提醒|记得",
    re.IGNORECASE,
)


def _expected_answer_type(query: str):
    q = query.lower()
    for t, cues in _ANSWER_TYPE_CUES.items():
        for c in cues:
            # ASCII 线索用词边界匹配，避免 "id" 误触发于 "did"/"said"；CJK 用子串匹配
            if c.isascii():
                if re.search(r"\b" + re.escape(c) + r"\b", q):
                    return t
            elif c in q:
                return t
    return None


@dataclass
class SearchResult:
    query: str
    facts: list[Fact] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    via: str = "hybrid"  # "hybrid" | "multi-hop" | "abstain"
    abstained: bool = False
    _answer: Optional[str] = None

    def answer(self) -> str:
        if self.abstained:
            return "I don't have that in memory."
        if self._answer is not None:
            return self._answer
        if not self.facts:
            return "I don't have that in memory."
        top = self.facts[0]
        return top.object or top.text

    def top(self) -> Optional[Fact]:
        return self.facts[0] if self.facts else None


class Memory:
    def __init__(
        self,
        config: Optional[Config] = None,
        embedder: Optional[Embedder] = None,
        llm: Optional[LLM] = None,
        reranker=None,
        vector_store_factory: Callable[[], VectorStore] = InMemoryVectorStore,
        graph_store_factory: Callable[[], GraphStore] = InMemoryGraphStore,
    ) -> None:
        self.config = config or Config()
        self.embedder = embedder or HashingEmbedder(self.config.embed_dim)
        self.reranker = reranker  # 可选 cross-encoder；提升 chunk/session 检索精度 (AGENTS.md「检索与模块」)
        self.llm = llm  # 启用时供 agentic 检索（查询分解）使用

        custom_vector_factory = vector_store_factory is not InMemoryVectorStore
        if self.config.storage == "lancedb" and not custom_vector_factory:
            from .store.lancedb_store import LanceDBVectorStore

            base = self.config.data_path or tempfile.mkdtemp(prefix="mnemotrace_lancedb_")

            def make_vector_store(name: str) -> VectorStore:
                return LanceDBVectorStore(base, name)
        else:
            def make_vector_store(name: str) -> VectorStore:
                return vector_store_factory()

        self.episodes_doc = InMemoryDocStore()
        self.episodes_vec = make_vector_store("episodes_vec")
        self.fact_store = make_vector_store("fact_store")  # 热层：快速、高频检索的工作集
        self.cold_store = make_vector_store("cold_store")  # 冷层：被换出的旧 fact，保留（从不删除）
        self.summary_vec = make_vector_store("summary_vec")  # L2 session 摘要，可检索用于精简读取切片
        self.graph = graph_store_factory()

        self.summarizer = SessionSummarizer(llm)
        self.profiles = ProfileBuilder()
        self._persona_cache: dict[str, str] = {}
        # 工作记忆：为最近一次查询组装的小型当前关注集（类似 OS 分页的"热"上下文）。
        # 由 lean_context 填充；可检查、瞬态——与持久化 store 不同。
        self.working_set: list[Fact] = []
        # 工作记忆层：瞬态的、按 session/TTL 划分范围的状态，刻意不进入持久化长期存储
        # (AGENTS.md 「架构概览与代码导航」 typed memory)。生命周期受管（过期/消费/清除）。
        self.working_mem: dict[str, WorkingMemory] = {}
        # 用户自定义关注领域（"关注点"面板）。不是摆设——真实生效：
        #   * track：用户希望被强调的主题 -> 提升显著度；显著度是一等检索评分信号
        #     (AGENTS.md 「检索与模块」 w_sal)，且这些主题豁免衰减/驱逐（保持热层）。
        #   * mute：需要抑制的主题 -> 从组装的读上下文和 persona 中过滤掉。
        self.focus: dict[str, list[str]] = {"track": [], "mute": []}
        # 每用户记忆策略：可编辑 prompt + "要记录什么"指令（控制台的 记忆策略 页面）。
        # prompt 为空字符串表示"使用内置默认值"。通过 _apply_policy() 接入真实的 extractor /
        # summarizer / persona，并随快照持久化。
        self.policy: dict[str, str] = {"extract_instruction": "", "extract_system": "",
                                       "summary_system": "", "persona_system": ""}
        # 已解析的用户身份（user_id -> 自称，如 "user123" -> "李雷"）。持久化保存，以便重新加载后
        # 第一人称归一化和 profile 仍知道用户是谁。
        self._identity: dict[str, str] = {}
        self._aliases: dict[str, set] = {}  # user_id -> {所有声明过的名字/昵称}（共指）
        # 已导入 episode 的内容指纹（幂等重导入：同一导出导入两次不能让每条记忆翻倍）。
        # 随快照持久化（manifest 状态）。
        self._import_seen: set[str] = set()
        # 疑似冲突，浮出给用户确认（System-2 LLM 检测；从不自动应用）。
        self.conflicts: dict[str, Conflict] = {}
        self.cold_pages_out: dict[str, int] = {}
        self.cold_pages_in: dict[str, int] = {}
        self._persist_path: Optional[str] = None
        self._snapshot_version: Optional[str] = None
        self._rewire()

    def _all_facts(self) -> list[Fact]:
        return self.fact_store.values() + self.cold_store.values()

    def _upsert_fact(self, fact: Fact, *, tier: str = "existing") -> None:
        """把被修改的 fact 写回所属层，或强制提升到热层。

        内存 store 持有对象引用，但真实的向量后端在 upsert/get 时常会拷贝 payload。因此仅修改
        返回的 Fact 不够；每次语义编辑/失效化都必须显式把 payload 写回唯一一个层。
        """
        in_cold = self.cold_store.get(fact.id) is not None
        in_hot = self.fact_store.get(fact.id) is not None
        if tier == "hot" or (tier == "existing" and not in_cold):
            self.cold_store.delete(fact.id)
            self.fact_store.upsert(fact.id, fact.embedding or [], fact)
            return
        if in_hot:
            self.fact_store.delete(fact.id)
        self.cold_store.upsert(fact.id, fact.embedding or [], fact)

    def _rewire(self) -> None:
        """把流水线组件（重新）绑定到当前 store。在初始化和加载快照后调用，这样持久化可以换入
        store 而不触碰 embedder/llm（它们不会被序列化）。"""
        self.ingestor = Ingestor(self.episodes_doc, self.episodes_vec, self.embedder)
        self.engine = ConsolidationEngine(self.fact_store, self.graph, self.embedder, self.config, self.llm)
        self.retriever = HybridRetriever(self.fact_store, self.graph, self.embedder, self.config)
        self.cold_retriever = HybridRetriever(self.cold_store, self.graph, self.embedder, self.config)
        self.planner = MultiHopPlanner(
            self.graph,
            self.fact_store,
            self.config,
            extra_fact_stores=[self.cold_store],
            llm=self.llm,
        )
        # 把持久化的自称恢复到（新建的）extractor 中，使身份在重载后得以保留
        ex = getattr(self.engine, "extractor", None)
        if ex is not None and hasattr(ex, "self_name"):
            ex.self_name.update(getattr(self, "_identity", {}))
            if hasattr(ex, "aliases"):
                for k, v in getattr(self, "_aliases", {}).items():
                    ex.aliases.setdefault(k, set()).update(v)

    # --- 持久化（让记忆跨进程/session 存活；AGENTS.md 「代码导航」） ---
    def save(self, path: Optional[str] = None) -> None:
        """把持久化记忆状态快照到安全的 JSONL+manifest 存储。

        embedder/llm 不会被序列化；重新打开时请挂载你想用的运行时后端。常规操作刻意不写 pickle。
        """
        path = path or self._persist_path
        if not path:
            raise ValueError("no path to save to")
        backend = "lancedb" if self.config.storage == "lancedb" else "durable"
        save_memory(self, path, backend=backend)
        self._persist_path = path

    @classmethod
    def open(cls, path: str, allow_mismatch: bool = False, **kwargs) -> "Memory":
        """从 JSONL+manifest 存储打开持久化 Memory；不存在则从空开始。

        `allow_mismatch=True` 仅仅为了让 `.reembed()` 能把 store 迁移到所挂载的 embedder 而绕过
        嵌入空间守卫——其他任何目的下的不匹配打开都会损坏检索。"""
        cfg = kwargs.get("config")
        if (
            cfg is not None
            and getattr(cfg, "storage", None) == "lancedb"
            and getattr(cfg, "data_path", None) is None
            and "vector_store_factory" not in kwargs
        ):
            kwargs["config"] = replace(cfg, data_path=f"{path}/lancedb")
        mem = cls(**kwargs)
        if load_memory(mem, path, allow_mismatch=allow_mismatch):
            mem._rewire()
            mem._classify()  # 为 feature ⑤ 之前保存的 fact 回填 category/sensitivity
        mem._persist_path = path
        return mem

    def reembed(self, embedder: Optional[Embedder] = None) -> dict[str, int]:
        """把所有已存储向量迁移到 `embedder` 的空间（默认：当前挂载的那个）——这是在既有 store
        上更换嵌入模型的受支持路径（manifest 守卫会拒绝不匹配的打开；这就是它们指向的逃生通道）。
        从源 TEXT 重新嵌入 fact（热层 + 冷层）、episode 内容、session 摘要和工作记忆。实体节点被
        跳过：检索按名称匹配实体，而非嵌入。之后请调用 `.save()`——manifest 随即记录新的
        embedder 身份，下次打开即无警告。"""
        if embedder is not None:
            self.embedder = embedder

        def _clear(store) -> None:
            for payload in list(store.values()):
                key = getattr(payload, "id", None)
                if key is not None:
                    store.delete(key)

        counts: dict[str, int] = {}
        for name, store in (("facts", self.fact_store), ("cold", self.cold_store)):
            facts = store.values()
            _clear(store)
            vecs = self.embedder.embed_batch([f.text for f in facts]) if facts else []
            for f, v in zip(facts, vecs):
                f.embedding = v
                store.upsert(f.id, v, f)
            counts[name] = len(facts)

        episodes = list(self.episodes_doc.values())  # doc 日志是 episode 文本的权威来源
        _clear(self.episodes_vec)
        vecs = self.embedder.embed_batch([ep.content for ep in episodes]) if episodes else []
        for ep, v in zip(episodes, vecs):
            ep.embedding = v
            self.episodes_vec.upsert(ep.id, v, ep)
        counts["episodes"] = len(episodes)

        summarized = [ep for ep in episodes if ep.summary]
        _clear(self.summary_vec)
        # 与 summarize_episodes 相同的嵌入来源：summary，回退到内容摘录
        vecs = (self.embedder.embed_batch([ep.summary or ep.content[:200] for ep in summarized])
                if summarized else [])
        for ep, v in zip(summarized, vecs):
            ep.summary_embedding = v
            self.summary_vec.upsert(ep.id, v, ep)
        counts["summaries"] = len(summarized)

        for wm in self.working_mem.values():
            wm.embedding = self.embedder.embed(wm.content)
        counts["working"] = len(self.working_mem)

        self._persona_cache.clear()
        self._rewire()  # retriever/engine 持有 embedder 引用（以及语义门控）——需重新绑定
        return counts

    # --- 写路径 ---
    def add(
        self,
        content: str,
        user_id: str = "default",
        session_id: str = "default",
        speaker: str = "user",
        event_time: Optional[float] = None,
        consolidate: bool = False,
        embedding: Optional[list] = None,
    ) -> Episode:
        ep = self.ingestor.ingest(content, user_id, session_id, speaker, event_time, embedding=embedding)
        if consolidate:
            self.consolidate()
        return ep

    def remember(self, content: str, user_id: str = "default", session_id: str = "default",
                 scope: str = "auto") -> dict:
        """带瞬态路由的高层写入。关键：它总是无损地存储带日期的 episode，因此无论路由到哪，
        "X 是什么时候发生的？"都能从历史中回答。对于瞬态状态（"今天我嗓子疼"），它额外写入一条
        工作记忆条目，并标记该 episode，使其中不会抽出持久的 profile FACT——*事件*被记住
        （带日期、可检索），但*状态*不会作为当前 profile 属性残留。持久内容保持 pending，由调用方
        consolidate() 成长期 fact。返回描述路由结果的 dict。

        这是修正后的模型：瞬态 != 删除。只是跳过向持久 profile 的提升；episode 记录
        (AGENTS.md「对象与时间语义」) 永远保留。"""
        ephemeral = scope == "working" or (scope == "auto" and self.is_ephemeral(content))
        ep = self.add(content, user_id=user_id, session_id=session_id)
        if ephemeral:
            ep.consolidated = True  # 留在带日期的 episode 日志中，但不产出持久 fact
            ep.metadata["ephemeral"] = True
            wm = self.remember_working(content, user_id=user_id, session_id=session_id)
            return {"scope": "working", "episode_id": ep.id, "working_id": wm.id, "kind": wm.kind}
        return {"scope": "long", "episode_id": ep.id}

    def consolidate(self, episodes: Optional[list[Episode]] = None) -> dict[str, int]:
        """System-2：从 `episodes`（默认：全部 pending）抽取 fact 并构建双时间轴图。
        live fact 集合刚发生变化，因此使 persona 缓存失效。"""
        eps = episodes if episodes is not None else self.ingestor.pending()
        self._apply_policy()  # 遵循用户可编辑的抽取 prompt / "要记录什么"指令
        self.sweep_working()  # 内务清理：丢弃已过期/已消费的瞬态条目
        stats = self.engine.consolidate(eps)
        ex = getattr(self.engine, "extractor", None)  # 捕获新解析出的身份以便持久化
        if ex is not None and hasattr(ex, "self_name"):
            self._identity.update(ex.self_name)
            for k, v in getattr(ex, "aliases", {}).items():
                self._aliases.setdefault(k, set()).update(v)
        self._classify()  # feature ⑤：给新 fact 打 category + 敏感标志（基于规则）
        self._detect_conflicts()  # System-2：将疑似冲突浮出给用户（需主动开启、有门控）
        self._enforce_hot_limit()
        self._persona_cache.clear()  # fact 变了 -> 缓存的 persona 已过期
        return stats

    def consolidate_full(
        self,
        fact_episodes: Optional[list[Episode]] = None,
        summary_episodes: Optional[list[Episode]] = None,
    ) -> dict[str, int]:
        """一次连贯的 System-2 处理，构建所有读取期层：
          L1 facts     来自 `fact_episodes`     （对最相关 session 做深度抽取），
          L2 summaries 来自 `summary_episodes`  （广泛、廉价的覆盖，用于聚合），
          L3 persona   从产出的 facts 刷新（惰性，首次读取时）。
        两个 episode 集合刻意不同：facts 需要在少数 session 上做深，summaries 需要广。
        未指定时两者都默认取 pending 队列。"""
        stats = self.consolidate(fact_episodes)
        stats["summaries"] = self.summarize_episodes(
            summary_episodes if summary_episodes is not None else self.ingestor.pending()
        )
        # Reflector：把摘要之后发生的知识更新传播进 L2 层，使精简读取不会浮出已被后续 fact
        # 纠正过的旧值。
        stats["reflected"] = sum(self.reflect(uid) for uid in {
            ep.user_id for ep in (summary_episodes or self.episodes_doc.values())})
        return stats

    # --- 批量导入（AGENTS.md 「代码导航」 采用层）：批量接入用户自己的历史 ---
    def import_messages(
        self,
        sessions,
        user_id: str = "default",
        consolidate: Optional[bool] = None,
        summarize: bool = True,
        roles: bool = True,
        batch_size: int = 256,
        base_time: Optional[float] = None,
        dedupe: bool = True,
    ) -> dict[str, int]:
        """批量写入预解析的 session（来自 `mnemotrace.connectors.parse`），每个 session 生成一条
        episode：正文用少数几次 encode 批量嵌入，然后仅对新 episode 跑一次 System-2（抽取 fact +
        构建图，可选 L2 摘要）。这是导入整段聊天历史的高效路径——远比逐轮 `add()` 便宜（N 个
        session 只做一次 model.encode，而非 N 次）。

        `sessions` 是 `ImportSession` 的可迭代对象（或形如 `{session_id, messages, ...}` 的 dict）。
        `base_time` 为源数据未打时间戳的 session 提供合成时钟（base + i·day），即使没有日期也能
        保持时间先后顺序。返回写入 + 整合统计。

        `dedupe`（默认开启）使重导入幂等：内容指纹已写入过的 episode 会被跳过（计入 `skipped`）
        ——重复提交同一份导出不会再让每条记忆翻倍。指纹由内容+身份构成（绝不含时间：重导入会
        拿到新的合成时钟）。`dedupe=False` 恢复原始追加行为。

        `consolidate=None`（默认）表示除 agent-session 转录（`metadata.source == "agent_session"`）
        外的所有来源都"抽取 fact"；后者只存储 + 摘要，除非调用方显式要求（`True`）且已挂载 LLM
        才做逐轮抽取——见下方的门控。
        """
        import hashlib

        from .connectors.base import ImportMessage, ImportSession, to_epoch

        def _time_of(d: dict) -> Optional[float]:
            for key in ("event_time", "timestamp", "time", "created_at", "create_time", "date", "ts"):
                if key in d and d[key] not in (None, ""):
                    return to_epoch(d[key])
            return None

        def _coerce(s) -> Optional[ImportSession]:
            if isinstance(s, ImportSession):
                return s
            if isinstance(s, dict) and "messages" in s:
                msgs = [ImportMessage(content=str(m.get("content", "")),
                                      speaker=str(m.get("speaker") or m.get("role") or "user"),
                                      event_time=_time_of(m),
                                      metadata=dict(m.get("metadata", {})))
                        for m in s["messages"] if isinstance(m, dict)]
                metadata = dict(s.get("metadata", {}))
                metadata.setdefault("source", "sessions")
                return ImportSession(session_id=str(s.get("session_id", "imported")), messages=msgs,
                                     event_time=_time_of(s), title=str(s.get("title", "")),
                                     metadata=metadata)
            return None

        items = [c for c in (_coerce(s) for s in sessions) if c is not None]
        base = base_time if base_time is not None else now()
        texts: list[str] = []
        metas: list[tuple] = []
        session_count = 0
        skipped = 0

        def _fp(sid: str, row_key: str) -> str:
            return hashlib.sha1(f"{user_id}|{sid}|{row_key}".encode()).hexdigest()
        for i, s in enumerate(items):
            source = str(s.metadata.get("source", ""))
            dated_times = {m.event_time for m in s.messages if m.event_time is not None}
            split_messages = source in {"records", "sessions"} and len(dated_times) > 1
            if split_messages:
                added = 0
                fallback = s.start_time()
                for j, m in enumerate(s.messages):
                    text = (m.content or "").strip()
                    if not text:
                        continue
                    body = f"{m.speaker}: {text}" if roles else text
                    sid = s.session_id or f"imported_{i}"
                    # 逐行指纹包含行号，因此同一 session 内合法重复的消息不会被当成重复——
                    # 只有重导入才会碰撞。
                    fp = _fp(sid, f"{j}|{body}")
                    if dedupe and fp in self._import_seen:
                        skipped += 1
                        continue
                    et = m.event_time if m.event_time is not None else fallback
                    et = et if et is not None else base + i * DAY + j
                    texts.append(body)
                    metas.append((sid, et, s.title, fp, source))
                    added += 1
                if added:
                    session_count += 1
                continue

            body = s.to_text(roles=roles)
            if not body.strip():
                continue
            sid = s.session_id or f"imported_{i}"
            fp = _fp(sid, body)
            if dedupe and fp in self._import_seen:
                skipped += 1
                continue
            et = s.start_time()
            et = et if et is not None else base + i * DAY  # 合成时间但保持有序
            texts.append(body)
            metas.append((sid, et, s.title, fp, source))
            session_count += 1
        if not texts:
            return {"sessions": 0, "episodes": 0, "facts_added": 0, "summaries": 0, "skipped": skipped,
                    "facts_deferred": 0, "deferred_reason": None}

        vecs: list = []
        for j in range(0, len(texts), batch_size):
            vecs.extend(self.embedder.embed_batch(texts[j:j + batch_size]))

        new_eps: list[Episode] = []
        for (sid, et, title, fp, source), text, vec in zip(metas, texts, vecs):
            ep = self.add(text, user_id=user_id, session_id=sid, speaker="session",
                          event_time=et, embedding=vec)
            ep.metadata["date"] = fmt_date(et)
            if title:
                ep.metadata["title"] = title
            if source:
                ep.metadata["source"] = source  # 读取侧（stats.feed、下方门控）以它为键
            new_eps.append(ep)
            self._import_seen.add(fp)  # 仅在 episode 真正落库之后记录

        agent_eps = [ep for ep in new_eps if ep.metadata.get("source") == "agent_session"]
        want_facts = consolidate is True and self.llm is not None
        if agent_eps and not want_facts:
            for ep in agent_eps:
                # 工作会话的持久记忆是其结束时的 outcome（consolidate/outcomes.py），而不是逐轮
                # 三元组：RuleExtractor 曾把 12 个真实轮次变成 11 条垃圾 fact 和 84 行
                # `occupation`，即便用 LLM 也会抽出工作会话里并不存在的生平三元组（357ae16）。
                # 打上 consolidated=True 正是为了让后续所有 drain（close_session、remember、异步
                # worker——均按 `not ep.consolidated` 过滤）永远不会抽取它。
                ep.consolidated = True
                ep.metadata["extraction"] = "outcomes_only"
        agent_ids = {ep.id for ep in agent_eps}
        extract = new_eps if want_facts else [ep for ep in new_eps if ep.id not in agent_ids]
        do_consolidate = True if consolidate is None else consolidate

        stats = {"sessions": session_count, "episodes": len(new_eps), "facts_added": 0, "summaries": 0,
                 "skipped": skipped}
        # 防御空列表：consolidate([]) 仍会空跑 _apply_policy/sweep。
        if do_consolidate and extract:
            stats["facts_added"] = self.consolidate(extract).get("facts_added", 0)
        stats["facts_deferred"] = 0 if want_facts else len(agent_eps)
        stats["deferred_reason"] = ("no_llm" if (consolidate is True and self.llm is None and agent_eps)
                                    else ("outcomes_only" if stats["facts_deferred"] else None))
        if summarize:
            stats["summaries"] = self.summarize_episodes(new_eps)
        return stats

    def import_data(self, data, format: str = "auto", user_id: str = "default",
                    session_id: str = "imported", **kwargs) -> dict[str, int]:
        """便捷方法：解析原始导出（ChatGPT/OpenAI/JSONL/transcript——自动嗅探）并一次调用导入。
        格式见 `mnemotrace.connectors.parse`。"""
        from .connectors import parse
        return self.import_messages(parse(data, format=format, session_id=session_id),
                                    user_id=user_id, **kwargs)

    def import_snapshot(self, snapshot: dict, user_id: str = "default", dedupe: bool = True) -> dict:
        """恢复 忆痕 导出快照（`/v1/export` / `MemoryService.export`）——可移植性往返。快照是
        已经整合过的记忆，所以这是恢复而非写入：fact 携带完整的双时间轴时间戳和取代链直接落库
        （重跑抽取会使其重复并抹平历史），episode 则重新进入带日期的原始日志并标记为已整合，
        System-2 永远不会重新消化它们。share-safe 导出不含 episode——只恢复 fact 是那里的预期
        结果。在 `dedupe`（默认）下幂等。"""
        from .store._validation import validate_embedding, validate_snapshot

        snapshot = validate_snapshot(snapshot)
        user = user_id

        # --- episodes：恢复无损带日期日志（仅 include_sensitive 导出中存在） ---
        ep_seen = {(ep.session_id, ep.content) for ep in self.episodes_doc.values()
                   if ep.user_id == user}
        eps_skipped = 0
        prepared_episodes: list[Episode] = []
        for row in snapshot["episodes"]:
            content = row["content"]
            session_id = row["session_id"]
            if dedupe and (session_id, content) in ep_seen:
                eps_skipped += 1
                continue
            ep = Episode(content=content, user_id=user, session_id=session_id,
                         speaker=row["speaker"], event_time=row["event_time"],
                         consolidated=True,  # 它的 fact 在下方恢复；绝不重新抽取
                         summary=row.get("summary", ""))
            ep.embedding = self.embedder.embed(content)
            validate_embedding(ep.embedding, "snapshot.episodes.embedding", self.embedder.dim)
            if ep.summary:
                ep.summary_embedding = self.embedder.embed(ep.summary)
                validate_embedding(ep.summary_embedding, "snapshot.episodes.summary_embedding", self.embedder.dim)
            prepared_episodes.append(ep)
            ep_seen.add((session_id, content))

        # --- facts：同时恢复 live 与被取代的行，保留 valid/transaction 时间 ---
        fact_seen = {(f.subject, f.predicate, f.object, round(f.valid_at, 1)): f.id
                     for f in self._all_facts() if f.user_id == user}
        id_map: dict[str, str] = {}  # 旧快照 id -> 新 id，用于重连取代链
        staged: list[tuple[Fact, str | None]] = []
        facts_skipped = 0
        for row in snapshot["facts"]:
            s, p, o = row["subject"], row["predicate"], row["object"]
            valid_at = row["valid_at"]
            key = (s, p, o, round(valid_at, 1))
            if dedupe and key in fact_seen:
                id_map[row["id"]] = fact_seen[key]
                facts_skipped += 1
                continue
            f = Fact(subject=s, predicate=p, object=o, user_id=user,
                     text=row.get("text", ""), source=row["source"],
                     category=row["category"], sensitive=row["sensitive"],
                     salience=row["salience"], confidence=row["confidence"],
                     valid_at=valid_at,
                     invalid_at=row["invalid_at"], created_at=row["created_at"],
                     expired_at=row["expired_at"],
                     # 来源 episode id：原样保留以保审计诚实——被引用的 episode 在这里拿到了新 id，
                     # 这些字段记录的是该 fact 当初来自哪里。
                     provenance=row["provenance"])
            f.embedding = self.embedder.embed(f.text)
            validate_embedding(f.embedding, "snapshot.facts.embedding", self.embedder.dim)
            id_map[row["id"]] = f.id
            staged.append((f, row["supersedes"]))
            fact_seen[key] = f.id
        # 校验和向量准备全部成功后才写入；取代链也链接到去重命中的已有事实。
        for ep in prepared_episodes:
            self.episodes_doc.put(ep.id, ep)
            self.episodes_vec.upsert(ep.id, ep.embedding, ep)
        for f, old_sup in staged:
            f.supersedes = id_map.get(old_sup) if old_sup else None
            self.fact_store.upsert(f.id, f.embedding, f)
            # add_fact 会把 fact.invalid_at 带到关系上，因此被取代的历史落库时就是已失效化的边
            # ——图的时间线在往返中得以保留。
            self.engine.graph_builder.add_fact(f)

        self._enforce_hot_limit()
        self._persona_cache.clear()
        return {"format": "mnemotrace", "facts_restored": len(staged), "facts_skipped": facts_skipped,
                "episodes_restored": len(prepared_episodes), "episodes_skipped": eps_skipped,
                "malformed": 0}

    # --- 用户手写的记忆管理（管理界面驱动的可编辑层） ---
    def add_fact(self, subject: str, predicate: str, object: str, user_id: str = "default",
                 valid_at: Optional[float] = None, sensitive: Optional[bool] = None,
                 category: Optional[str] = None) -> Fact:
        """手动断言一条 fact。它被标记为 source='user'（权威）：冲突解决允许它取代同一槽位上
        任何抽取出的值，并且此后受到保护，不会被未来的自动覆盖改写。"""
        user = user_id
        f = Fact(subject=subject, predicate=predicate, object=object, user_id=user, source="user",
                 valid_at=valid_at if valid_at is not None else now())
        f.embedding = self.embedder.embed(f.text)
        from .consolidate.classify import classify_fact
        classify_fact(f)  # 打 category + 敏感标签（feature ⑤）
        if category is not None:
            f.category = category
        if sensitive is not None:
            f.sensitive = sensitive
        live = [x for x in self._all_facts() if x.user_id == user and x.is_live()]
        action, invalidated = self.engine.conflict.reconcile(f, live)
        for old in invalidated:
            self.engine.graph_builder.invalidate(old.id, f.created_at)
            self._upsert_fact(old, tier="hot")
        if action != "duplicate":
            self.fact_store.upsert(f.id, f.embedding, f)
            self.engine.graph_builder.add_fact(f)
        self._enforce_hot_limit()
        self._persona_cache.clear()
        return f

    def update_fact(self, fact_id: str, subject: Optional[str] = None, predicate: Optional[str] = None,
                    object: Optional[str] = None, sensitive: Optional[bool] = None,
                    category: Optional[str] = None, statement: Optional[str] = None) -> Optional[Fact]:
        """实际修改 fact 内容时标记为用户手写（因此自动抽取不会把它改回去）。
        依据新内容重新分类 category/sensitivity；显式传入的 `sensitive`/`category` 覆盖自动结果
        （用户的选择永远优先）。只修改元数据或回传未变的内容时，保留来源、有效状态与历史。"""
        f = self.fact_store.get(fact_id) or self.cold_store.get(fact_id)
        if f is None:
            return None
        from .consolidate.outcomes import OUTCOME_CATEGORY, OUTCOME_PREDICATES, split_outcome_text
        if statement is not None:
            if any(value is not None for value in (subject, predicate, object)):
                raise InvalidFactEditError("statement cannot be combined with subject, predicate or object")
            statement = statement.strip()
            if not statement:
                raise InvalidFactEditError("statement must not be blank")
            # 属性的 object 是关系的取值，整句写入会把画像和图边变成错误的三元组。
            if f.predicate.lower() not in OUTCOME_PREDICATES:
                raise InvalidFactEditError(
                    "statement edits are only supported for session outcome facts; "
                    "edit subject, predicate and object for attribute facts"
                )
        claim_changed = any(
            value is not None and value != getattr(f, field)
            for field, value in (("subject", subject), ("predicate", predicate), ("object", object))
        ) or (statement is not None and statement != display_of(f) and statement != f.text)
        if not claim_changed:
            # 表单会回传未变的内容字段；隐私/分类修改不代表用户断言正文，也不应撤销替代历史。
            if sensitive is not None:
                f.sensitive = sensitive
            if category is not None:
                f.category = category
            self._upsert_fact(f, tier="hot")
            self._persona_cache.clear()
            return f
        self.graph.delete_relations_for_fact(f.id)
        self.graph.prune_orphan_entities()
        if subject is not None:
            f.subject = subject
        if predicate is not None:
            f.predicate = predicate
        if object is not None:
            f.object = object
        # outcome 不是三元组：它的 subject 是 session id，object 是整句话，因此按通用的
        # "subject predicate object" 重建会嵌入 `s-2026-08-31 lesson ...` 并丢掉作为检索依据的
        # 依据子句（人们按教训的成因搜索教训）。
        if statement is not None:
            f.object = statement
            _, why = split_outcome_text(f.text)
            f.text = f"{statement} （依据：{why}）" if why else statement
            f.display = statement
        elif f.predicate.lower() in OUTCOME_PREDICATES:
            _, why = split_outcome_text(f.text)
            f.text = f"{f.object} （依据：{why}）" if why else f.object
        else:
            f.text = f"{f.subject} {f.predicate.replace('_', ' ')} {f.object}".strip()
        # display_of() 优先用 `display` 而非 `text`，因此过期的 display 会让所有界面在用户被告知
        # 保存成功后仍渲染编辑前的措辞。这不只影响 outcome。outcome 的 display 就是它的 object
        # （outcomes.py 写入 `display=statement`），可以直接重新赋值。其他所有 fact 的 display 都是
        # extractor 生成的完整句子（"Wei works at Tencent"），若把裸 object 赋给 display 就会渲染成
        # "Moonshot AI"——本功能要修复的碎片化问题又从属性侧回来了。正确做法是清空 display，让
        # display_of() 回退到 render_display(subject, predicate, object)——所有规则抽取的 fact
        # （display=""）本来就是这么渲染的。
        # 以"text 是否被重建"为门控条件，而非"object 是否变化"：重命名 predicate（finding ->
        # lesson）或 subject 同样会重写 text，留下旧 display 是同一种过期渲染 bug。
        if statement is None and (subject is not None or predicate is not None or object is not None) \
                and (getattr(f, "display", "") or "").strip():
            f.display = f.object if f.predicate.lower() in OUTCOME_PREDICATES else ""
        f.embedding = self.embedder.embed(f.text)
        f.source = "user"
        f.invalid_at = None  # 用户编辑使其重新生效
        f.expired_at = None
        # 依据编辑后的内容重新分类，再应用显式的用户覆盖
        from .consolidate.classify import classify
        f.category, f.sensitive = classify(f.predicate, f.object, f.text)
        # 结论被纠正后仍是结论。classify() 只认识属性类目，缺了这一步，一次编辑就会把 fact
        # 悄悄降级出 Journal。
        if f.predicate.lower() in OUTCOME_PREDICATES:
            f.category = OUTCOME_CATEGORY
        if category is not None:
            f.category = category
        if sensitive is not None:
            f.sensitive = sensitive
        self._upsert_fact(f, tier="hot")
        self.engine.graph_builder.add_fact(f)
        self._persona_cache.clear()
        return f

    def delete_fact(self, fact_id: str) -> bool:
        """被遗忘权：硬删除一条 fact（区别于保留历史的自动失效化）。这是用户主动发起的擦除，
        数据真的被移除——热层和冷层都删。"""
        existed = self.fact_store.get(fact_id) is not None or self.cold_store.get(fact_id) is not None
        self.graph.delete_relations_for_fact(fact_id)
        self.graph.prune_orphan_entities()
        self.fact_store.delete(fact_id)
        self.cold_store.delete(fact_id)
        self._persona_cache.clear()
        return existed

    # --- 关注领域："关注点"定制（记忆应强调/抑制什么） ---
    def set_focus(self, track: Optional[list[str]] = None, mute: Optional[list[str]] = None) -> dict:
        """定制记忆的优先级。真实生效，不只是个标签：
          * `track` 主题获得显著度提升——显著度是一等检索评分信号 (AGENTS.md 「检索与模块」 w_sal)，
            且豁免衰减/驱逐，因此被 track 的主题确实排名更高并留在热层。
          * `mute` 主题从组装的读上下文（lean_context）和 persona 中被抑制。
        传入 None 表示该列表不变；传入 [] 表示清空。"""
        if track is not None:
            self.focus["track"] = [t.strip() for t in track if t.strip()]
        if mute is not None:
            self.focus["mute"] = [m.strip() for m in mute if m.strip()]
        self.apply_focus()
        self._persona_cache.clear()
        return self.get_focus()

    def get_focus(self) -> dict:
        return {"track": list(self.focus.get("track", [])), "mute": list(self.focus.get("mute", []))}

    @staticmethod
    def _matches(f: Fact, terms: list[str]) -> bool:
        if not terms:
            return False
        hay = f.text.lower()
        return any(t.lower() in hay for t in terms)

    def apply_focus(self, boost: float = 0.5, cap: float = 5.0) -> int:
        """给所有命中 track 主题的已存 fact 提升显著度，让用户声明的优先级确实排名更高并抵抗
        衰减/驱逐。设上限，使反复编辑趋于饱和而非无限膨胀。返回被提升的 fact 数量。"""
        track = self.focus.get("track", [])
        if not track:
            return 0
        n = 0
        for f in list(self.fact_store.values()) + list(self.cold_store.values()):
            if self._matches(f, track):
                f.salience = min(cap, f.salience + boost)
                n += 1
        return n

    def graph_data(
        self,
        user_id: str = "default",
        as_of: Optional[float] = None,
        include_sensitive: bool = True,
        q: str = "",
        live_only: bool = False,
        limit: Optional[int] = None,
    ) -> dict:
        """把语义图导出为节点 + 边，供关系图谱可视化使用。实体是节点；关系是携带 predicate 和
        双时间轴（live/被取代）状态的边。孤立实体（没有存留边）被丢弃，让画面聚焦于关系本身。

        `include_sensitive=False` 返回仅由未标记敏感的 fact 派生的 share-safe 图。
        `limit` 限制筛选后的关系数；`has_more` 表示仍有未返回的匹配关系。
        """
        user = user_id
        ents = {e.id: e for e in self.graph.entities.values() if e.user_id == user}
        relations = self.graph.relations()
        live_relation_fact_ids = {
            r.fact_id for r in relations
            if r.invalid_at is None
        }
        needle = q.strip().lower()
        edges, touched = [], set()
        has_more = False
        for r in relations:
            if r.subject_id not in ents or r.object_id not in ents:
                continue
            live = (
                r.invalid_at is None
                if as_of is None
                else r.valid_at <= as_of and (r.invalid_at is None or r.invalid_at > as_of)
            )
            if live_only and not live:
                continue
            if as_of is None and not live and r.fact_id in live_relation_fact_ids:
                continue
            if as_of is not None and not live:
                continue
            fact = self.fact_store.get(r.fact_id) or self.cold_store.get(r.fact_id)
            if fact is None:
                continue
            if not include_sensitive and getattr(fact, "sensitive", False):
                continue
            subject_name = ents[r.subject_id].name
            object_name = ents[r.object_id].name
            haystack = " ".join([
                subject_name,
                object_name,
                r.predicate,
                getattr(fact, "text", ""),
                getattr(fact, "object", ""),
                getattr(fact, "category", ""),
            ]).lower()
            if needle and needle not in haystack:
                continue
            # 仅用额外一条匹配关系判断截断，不把它的端点加入返回节点。
            if limit is not None and limit > 0 and len(edges) >= limit:
                has_more = True
                break
            edges.append({
                "source": r.subject_id,
                "target": r.object_id,
                "predicate": r.predicate.replace("_", " "),
                "live": live,
                "fact_id": r.fact_id,
                "fact_text": fact.text if fact is not None else "",
                "valid_at": r.valid_at,
                "valid_at_h": fmt_date(r.valid_at),
                "invalid_at": r.invalid_at,
                "invalid_at_h": fmt_date(r.invalid_at) if r.invalid_at is not None else None,
                "provenance": list(fact.provenance) if fact is not None else [],
            })
            touched.add(r.subject_id)
            touched.add(r.object_id)
        nodes = [{"id": eid, "name": ents[eid].name, "type": ents[eid].type} for eid in touched]
        return {"nodes": nodes, "edges": edges, "has_more": has_more}

    # --- 记忆策略：可编辑 prompt + "要记录什么"指令（记忆策略页面） ---
    def get_policy(self) -> dict:
        """返回用户覆盖项和内置默认值，让控制台能展示实际生效的 prompt，并允许用户在其基础上
        编辑。"""
        return {"policy": dict(self.policy), "defaults": dict(POLICY_DEFAULTS)}

    def set_policy(self, **fields: str) -> dict:
        """更新策略字段（extract_instruction / extract_system / summary_system / persona_system）。
        空字符串清除覆盖项（回退到默认值）。立即应用到 extractor/summarizer，下一次整合即生效。"""
        for k, v in fields.items():
            if k in self.policy and v is not None:
                self.policy[k] = v
        self._apply_policy()
        self._persona_cache.clear()
        return self.get_policy()

    def _effective(self, key: str) -> str:
        """有覆盖则返回覆盖，否则返回内置默认值。"""
        return self.policy.get(key) or POLICY_DEFAULTS[key]

    def _apply_policy(self) -> None:
        """把当前策略推入运行中的 extractor + summarizer。在每次整合/摘要/persona 构建前以及策略
        变更时调用。有守卫：离线 RuleExtractor（无可编辑 prompt）直接跳过、不做改动。"""
        ex = getattr(self.engine, "extractor", None)
        if ex is not None and hasattr(ex, "system") and hasattr(ex, "instruction"):
            ex.system = self._effective("extract_system")
            ex.instruction = self.policy.get("extract_instruction", "")
        self.summarizer.system = self._effective("summary_system")

    # --- 工作记忆层：瞬态的、按 session/TTL 划界的状态，不进长期存储（feature ①） ---
    # 标记一句话是瞬态的（一时的状态/意图）而非持久 fact——用于把"今天我嗓子疼"路由到工作
    # 记忆，而不是污染长期存储。
    _EPHEMERAL_MARKERS = (
        "today", "right now", "currently", "this morning", "this afternoon", "tonight", "this week",
        "for now", "at the moment", "temporarily", "this trip", "feeling a bit", "i feel ",
        "今天", "现在", "此刻", "暂时", "这会儿", "待会", "等下", "本次", "这趟", "这次", "最近想", "改天",
    )

    @classmethod
    def is_ephemeral(cls, content: str) -> bool:
        """启发式路由：这句话读起来像瞬态的状态/意图（→ 工作记忆）还是持久 fact（→ 长期）？
        确定性且零成本；调用方随时可以用显式 scope 覆盖。把瞬态上下文挡在长期存储之外是通用的
        记忆卫生收益。"""
        c = content.lower()
        return any(m in c for m in cls._EPHEMERAL_MARKERS)

    def remember_working(self, content: str, user_id: str = "default", session_id: str = "default",
                         kind: str = "state", ttl_seconds: Optional[float] = None,
                         event_time: Optional[float] = None) -> WorkingMemory:
        """把一条瞬态条目存入工作记忆层。不整合进长期存储，也不进入持久 profile。`ttl_seconds`
        设定硬性墙钟过期时间；否则存活到 session 被清除为止。"""
        user = user_id
        wm = WorkingMemory(
            content=content, user_id=user, session_id=session_id, kind=kind,
            event_time=event_time if event_time is not None else now(),
            expires_at=(now() + ttl_seconds) if ttl_seconds else None,
        )
        wm.embedding = self.embedder.embed(content)
        self.working_mem[wm.id] = wm
        return wm

    def working_memory(self, user_id: str = "default", session_id: Optional[str] = None,
                       as_of: Optional[float] = None, kind: Optional[str] = None) -> list[WorkingMemory]:
        """存活的工作记忆条目（可按 session / kind 过滤）；排除已过期和已消费的。
        读取时惰性清扫硬过期条目。"""
        user = user_id
        self.sweep_working(as_of)
        return [w for w in self.working_mem.values()
                if w.user_id == user and w.is_live(as_of, session_id) and (kind is None or w.kind == kind)]

    def clear_session(self, user_id: str = "default", session_id: str = "default") -> int:
        """会话结束/断电重启式清理：丢弃该 session 的工作记忆。返回清除数量。"""
        user = user_id
        ids = [i for i, w in self.working_mem.items() if w.user_id == user and w.session_id == session_id]
        for i in ids:
            del self.working_mem[i]
        return len(ids)

    def consume_working(self, wm_id: str) -> bool:
        """软清除：把工作条目标记为已消费，使其不再浮出（它已完成任务）。"""
        w = self.working_mem.get(wm_id)
        if w is None:
            return False
        w.consumed = True
        return True

    def sweep_working(self, as_of: Optional[float] = None) -> int:
        """丢弃硬过期/已消费的工作条目。读取时惰性调用，整合期间也会调用。"""
        t = now() if as_of is None else as_of
        dead = [i for i, w in self.working_mem.items()
                if w.consumed or (w.expires_at is not None and w.expires_at <= t)]
        for i in dead:
            del self.working_mem[i]
        return len(dead)

    # --- 冲突检测 -> pending（LLM 检测出含糊的部分；由用户确认） ---
    def _detect_conflicts(self) -> None:
        if not (self.llm is not None and self.config.conflict_detection):
            return  # 需主动开启；离线/纯规则模式保持确定性
        from .consolidate.detect import detect_conflicts
        seen = {c.pair_key for c in self.conflicts.values()}
        for user in {f.user_id for f in self._all_facts()}:
            live = [f for f in self._all_facts() if f.user_id == user and f.is_live()]
            for c in detect_conflicts(live, self.llm, user, seen, self.embedder):
                self.conflicts[c.id] = c
                seen.add(c.pair_key)

    def pending_conflicts(self, user_id: str = "default") -> list[Conflict]:
        """等待用户裁决的疑似冲突（两条 fact 都必须仍然 live）。"""
        user = user_id
        out = []
        for c in self.conflicts.values():
            if c.user_id != user or c.status != "pending":
                continue
            a = self.fact_store.get(c.older) or self.cold_store.get(c.older)
            b = self.fact_store.get(c.newer) or self.cold_store.get(c.newer)
            if a is not None and b is not None and a.is_live() and b.is_live():
                out.append(c)
            else:
                c.status = "dismissed"  # 有一侧已变化 -> 不再是存活冲突
        return out

    def resolve_conflict(self, conflict_id: str, keep: str = "newer") -> bool:
        """应用用户的裁决：keep='newer'|'older' 使另一方被取代；keep='both' 仅驳回。
        这是唯一会作用于已检出冲突的路径——始终由用户驱动。"""
        c = self.conflicts.get(conflict_id)
        if c is None:
            return False
        newer = self.fact_store.get(c.newer) or self.cold_store.get(c.newer)
        older = self.fact_store.get(c.older) or self.cold_store.get(c.older)
        if keep in ("newer", "older") and newer is not None and older is not None:
            winner, loser = (newer, older) if keep == "newer" else (older, newer)
            if loser.invalid_at is None:
                loser.invalid_at = max(winner.valid_at, loser.valid_at)
            loser.expired_at = now()
            winner.supersedes = loser.id
            self.engine.graph_builder.invalidate(loser.id, loser.expired_at)
            self._upsert_fact(loser)
            self._upsert_fact(winner)
            self._persona_cache.clear()
        c.status = "resolved"
        return True

    def dismiss_conflict(self, conflict_id: str) -> bool:
        """不是冲突（两条都保留）——不会再被标记。"""
        c = self.conflicts.get(conflict_id)
        if c is None:
            return False
        c.status = "dismissed"
        return True

    # --- 分类 + 敏感性（feature ⑤） ---
    def _classify(self) -> None:
        """给每条 fact 打粗粒度 category + 敏感标志（基于规则，幂等）。"""
        from .consolidate.classify import classify_fact
        for f in self.fact_store.values():
            classify_fact(f)
        for f in self.cold_store.values():
            classify_fact(f)

    # --- L2/L3 抽象（整合期间构建，为精简读取而存储） ---
    def summarize_episodes(self, episodes: list[Episode]) -> int:
        """L2：把每个 episode 蒸馏成紧凑摘要并索引进 summary_vec 供检索。摘要并行生成（相互
        独立的 LLM 调用）后批量嵌入——精简读取因此可以拉取少量 session 摘要，而不必把整段
        原始 session 拖入上下文。"""
        from concurrent.futures import ThreadPoolExecutor

        self._apply_policy()  # 遵循用户可编辑的摘要 prompt
        todo = [ep for ep in episodes if not ep.summary]
        if not todo:
            return 0
        if self.llm is not None and len(todo) > 1:
            import os
            _sw = int(os.environ.get("MNEMOTRACE_SUMMARIZE_WORKERS", "8"))
            with ThreadPoolExecutor(max_workers=min(_sw, len(todo))) as pool:
                summaries = list(pool.map(self.summarizer.summarize, todo))
        else:
            summaries = [self.summarizer.summarize(ep) for ep in todo]
        vecs = self.embedder.embed_batch([s or ep.content[:200] for s, ep in zip(summaries, todo)])
        for ep, summ, vec in zip(todo, summaries, vecs):
            ep.summary = summ
            ep.summary_embedding = vec
            self.summary_vec.upsert(ep.id, vec, ep)
        return len(todo)

    def retrieve_summaries(
        self,
        query: str,
        user_id: str = "default",
        k: int = 8,
        as_of: Optional[float] = None,
    ) -> list[Episode]:
        """为查询取 top-k 条 session 摘要，使用与 fact/episode 检索相同的 hybrid（dense + BM25,
        RRF）信号。词法匹配在这里很重要：聚合类问题（"how many trips"）依赖摘要里提到的精确词，
        纯嵌入查找可能把它们排在模糊相似项之后。返回携带 .summary 字段的 episode。"""
        user = user_id
        pool = max(k * 3, 30)
        cands = self.summary_vec.search(
            self.embedder.embed(query),
            pool,
            where=lambda e: e.user_id == user and (as_of is None or e.event_time <= as_of),
        )
        eps = [ep for _, ep in cands]
        if len(eps) <= k:
            return eps
        from .retrieve.hybrid import date_terms
        bm25 = bm25_scores(query, [
            (ep.id, f"{ep.summary or ep.content} {date_terms(ep.event_time)}") for ep in eps])
        if bm25:
            bm25_rank = {eid: r for r, (eid, _) in
                         enumerate(sorted(bm25.items(), key=lambda x: x[1], reverse=True))}
            K = 60  # 标准 RRF 常数
            order = sorted(range(len(eps)), key=lambda i: -(
                1.0 / (K + i + 1) + 1.0 / (K + bm25_rank.get(eps[i].id, len(eps)) + 1)))
            eps = [eps[i] for i in order]
        return eps[:k]

    def reflect(self, user_id: str = "default") -> int:
        """Reflector（Mastra 式摘要维护，AGENTS.md 「架构概览与代码导航」）。L2 摘要在其 session 被摘要时定格；若它
        陈述的 fact 之后被取代，旧值会残留在摘要文本里，精简读取就会把它浮出来。本方法把当前值
        追加到任何来源 fact 已失效化的摘要上，使知识更新传播进抽象层。返回被纠正的摘要数量。"""
        user = user_id
        facts = [f for f in self._all_facts() if f.user_id == user]
        replacement = {f.supersedes: f for f in facts if f.supersedes and f.is_live()}
        by_episode: dict[str, list] = {}
        for old in facts:
            if old.is_live() or old.id not in replacement:
                continue
            for ep_id in old.provenance:
                by_episode.setdefault(ep_id, []).append(old)
        corrected = 0
        for ep in self.summary_vec.values():
            if ep.user_id != user or "[updated:" in (ep.summary or ""):
                continue
            stale = by_episode.get(ep.id)
            if not stale:
                continue
            current = "; ".join(replacement[o.id].text for o in stale if o.id in replacement)
            if current:
                ep.summary = f"{ep.summary or ''} [updated: {current}]".strip()
                corrected += 1
        return corrected

    # 程序性记忆：how-to / 指令性知识——用户声明过的关于事情该怎么做的规则（"always remind
    # me…"、"I prefer responses in bullet points"）。这是 fact store 之上的一个独立类型化视图
    # （AGENTS.md 「架构概览与代码导航」 typed memory），浮出以便助手遵循长期有效的指令。
    _INSTRUCTION_PREDS = frozenset({
        "wants", "wants_reminder", "instruction", "prefers", "prefers_format", "asks_to", "always",
        "never", "remind", "rule", "routine", "how_to", "procedure", "wants_me_to",
    })

    def evict_cold(self, max_hot: int) -> int:
        """冷热分层分页（MemoryOS，AGENTS.md 「架构概览与代码导航」 / Bet E）。当热层 fact 集超过容量时，把最冷的
        fact（显著度最低、访问最旧）换页到冷层——但持久的身份/偏好 fact 除外，它们保持热层。
        非破坏性：冷 fact 是移动而非删除，历史与 as-of 查询保持完整，未来的查询仍可把它们换页
        回来。正是这一点让检索成本在 10M-token 规模下保持 O(hot working set) 而非
        O(all history)。返回换出数量。"""
        from .consolidate.decay import is_durable

        hot = self.fact_store.values()
        if len(hot) <= max_hot:
            return 0
        evictable = [f for f in hot if not is_durable(f.predicate)]
        evictable.sort(key=lambda f: (f.salience, f.last_access))  # 最冷的（显著度最低/最旧）优先
        n_out = min(len(evictable), len(hot) - max_hot)
        for f in evictable[:n_out]:
            self.cold_store.upsert(f.id, f.embedding or [], f)  # 保留在冷层
            self.fact_store.delete(f.id)  # 仅从热层索引移除
            self.cold_pages_out[f.user_id] = self.cold_pages_out.get(f.user_id, 0) + 1
        return n_out

    def _enforce_hot_limit(self) -> int:
        max_hot = int(getattr(self.config, "max_hot_facts", 0) or 0)
        if max_hot <= 0:
            return 0
        return self.evict_cold(max_hot)

    def _page_hot(self, facts: list[Fact]) -> None:
        """把检索命中的冷层 fact 提升回热层索引。fact id 和图的边保持不变。"""
        moved = False
        for f in facts:
            if self.cold_store.get(f.id) is not None:
                self.cold_store.delete(f.id)
                reinforce(f, self.config.access_boost)
                self.fact_store.upsert(f.id, f.embedding or [], f)
                self.cold_pages_in[f.user_id] = self.cold_pages_in.get(f.user_id, 0) + 1
                moved = True
        if moved:
            self._enforce_hot_limit()

    def _retrieve_cold(
        self,
        query: str,
        user: str,
        as_of: Optional[float],
        top_k: Optional[int],
    ) -> tuple[list[tuple[Fact, float]], dict]:
        ranked, diag = self.cold_retriever.retrieve(query, user, as_of, top_k)
        if ranked:
            self._page_hot([f for f, _ in ranked])
        return ranked, diag

    def procedural(self, user_id: str = "default", as_of: Optional[float] = None) -> list[Fact]:
        """该用户的长期指令 / how-to fact（程序性记忆）。"""
        user = user_id
        return [f for f in self._all_facts()
                if f.user_id == user and f.is_live(as_of)
                and (f.predicate.lower() in self._INSTRUCTION_PREDS
                     or any(f.predicate.lower().startswith(p + "_") for p in ("wants", "prefers", "remind")))]

    def _is_procedural_fact(self, f: Fact) -> bool:
        pred = f.predicate.lower()
        return pred in self._INSTRUCTION_PREDS or any(
            pred.startswith(p + "_") for p in ("wants", "prefers", "remind")
        )

    def _procedural_source_label(self, f: Fact) -> str:
        sessions: list[str] = []
        seen: set[str] = set()
        for ep_id in f.provenance:
            ep = self.episodes_doc.get(ep_id)
            if ep is None or ep.session_id in seen:
                continue
            seen.add(ep.session_id)
            sessions.append(ep.session_id)
        if sessions:
            return "sessions: " + ", ".join(sessions[:3])
        return f"fact: {f.id}"

    def _procedural_candidates(
        self,
        query: str,
        user: str,
        as_of: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> list[Fact]:
        if not self.config.procedural_memory:
            return []
        k = max(1, int(limit or getattr(self.config, "procedural_memory_k", 6) or 6))
        q_is_proc = bool(_PROCEDURAL_QUERY_RE.search(query))
        rows: list[tuple[int, float, Fact]] = []
        for f in self._all_facts():
            if f.user_id != user or not f.is_live(as_of) or not self._is_procedural_fact(f):
                continue
            text = " ".join(
                part for part in (
                    f.subject,
                    f.predicate.replace("_", " "),
                    f.object,
                    f.display,
                )
                if part
            )
            exact = overlap_terms(query, text) - _GENERIC_ATTR_TERMS
            object_hits = overlap_terms(query, f.object)
            subject_hits = overlap_terms(query, f.subject)
            if not exact and not q_is_proc:
                continue
            if q_is_proc and not (exact or subject_hits or object_hits):
                continue
            score = len(exact) + (3 if object_hits else 0) + (2 if subject_hits else 0)
            if f.provenance:
                score += 1
            rows.append((score, f.valid_at, f))
        rows.sort(key=lambda row: (-row[0], -row[1], row[2].text.lower()))
        return [f for _, _, f in rows[:k]]

    def _procedural_memory_block(
        self,
        query: str,
        user: str,
        as_of: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> str:
        rows = self._procedural_candidates(query, user, as_of, limit=limit)
        if not rows:
            return ""
        lines = []
        for f in rows:
            source = self._procedural_source_label(f)
            pred = f.predicate.replace("_", " ")
            lines.append(
                f"- [{fmt_date(f.valid_at)}] ({source}) {f.subject} {pred}: {f.object}"
            )
        return "PROCEDURAL MEMORY (standing rules/how-to, source-backed):\n" + "\n".join(lines)

    def structured_profile(self, user_id: str = "default") -> dict:
        """L2 结构化 profile：把用户的 live fact 按 基本信息/偏好/习惯 分组，并为展示区分
        "已确认"与"待确认"。这是只读的派生视图——从不过滤 fact store 或检索路径，因此召回
        不受影响（search/lean_context 仍看到全部 fact）。"""
        from .consolidate.structured import build_structured_profile
        user = user_id
        subject = self.engine.self_name(user)
        live = [f for f in self._all_facts() if f.user_id == user and f.is_live()]
        return build_structured_profile(live, subject, user)

    def build_persona(self, user_id: str = "default") -> str:
        """L3：由 live fact 合成的紧凑叙事式 profile（偏好/习惯/所有物）。"""
        user = user_id
        if user in self._persona_cache:
            return self._persona_cache[user]
        subject = self.engine.self_name(user)
        mute = self.focus.get("mute", [])
        live = [f for f in self._all_facts()
                if f.user_id == user and f.is_live() and not self._matches(f, mute)]
        persona = (self.profiles.narrative(subject, live, llm=self.llm,
                                           system=self._effective("persona_system")) if live else "")
        track = self.focus.get("track", [])
        if track:  # 把用户声明的优先级呈现在 profile 中
            line = "FOCUS AREAS (user asked to prioritize): " + ", ".join(track)
            persona = (persona + "\n" + line).strip() if persona else line
        self._persona_cache[user] = persona
        return persona

    def _persona_at(self, user: str, as_of: Optional[float]) -> str:
        """供读上下文使用的叙事式 profile。当前 profile 会被缓存；as-of profile 不缓存，因为
        时间戳是视图的一部分，不能把当前 fact 泄漏进过去上下文。"""
        if as_of is None:
            return self.build_persona(user)
        subject = self.engine.self_name(user)
        mute = self.focus.get("mute", [])
        live = [
            f for f in self._all_facts()
            if f.user_id == user and f.is_live(as_of) and not self._matches(f, mute)
        ]
        return self.profiles.narrative(
            subject,
            live,
            llm=self.llm,
            system=self._effective("persona_system"),
        ) if live else ""

    def _current_state_block(self, facts: list[Fact], limit: int = 18) -> str:
        """每个单值槽位取最新 live 值，外加所有存活的多数值状态行。"""
        latest: dict[tuple[str, str], Fact] = {}
        multi: list[Fact] = []
        for f in facts:
            if not is_single_valued(f.predicate):
                multi.append(f)
                continue
            key = (f.subject.lower(), f.predicate.lower())
            if key not in latest or f.valid_at > latest[key].valid_at:
                latest[key] = f
        rows = sorted(
            list(latest.values()) + multi,
            key=lambda f: (f.subject.lower(), f.predicate.lower(), f.object.lower(), -f.valid_at),
        )[:limit]
        if not rows:
            return ""
        lines = ["date | subject | attribute | current value", "--- | --- | --- | ---"]
        for f in rows:
            lines.append(f"{fmt_date(f.valid_at)} | {f.subject} | {f.predicate.replace('_', ' ')} | {f.object}")
        return "CURRENT STATE (live facts only):\n" + "\n".join(lines)

    _PREFERENCE_MARKERS = (
        "like", "dislike", "prefer", "favorite", "favourite", "love", "hate", "avoid", "allergic",
        "interested", "diet",
    )

    def _preference_block(self, user: str, query: str, facts: list[Fact], limit: int = 24) -> str:
        """结构化偏好证据：以 极性/日期/值 的行呈现，而非松散的散文。"""
        live = [f for f in self._all_facts() if f.user_id == user and f.is_live()]
        pool = {f.id: f for f in facts}
        for f in live:
            p = f.predicate.lower()
            if any(m in p for m in self._PREFERENCE_MARKERS):
                pool.setdefault(f.id, f)
        prefs = [f for f in pool.values() if any(m in f.predicate.lower() for m in self._PREFERENCE_MARKERS)]
        if not prefs:
            return ""
        prefs.sort(key=lambda f: (not overlap_terms(query, f"{f.predicate} {f.object}"), -f.valid_at))
        lines = ["日期 | 人物或对象 | 偏好关系 | 具体内容", "--- | --- | --- | ---"]
        for f in prefs[:limit]:
            lines.append(f"{fmt_date(f.valid_at)} | {f.subject} | {f.predicate.replace('_', ' ')} | {f.object}")
        return "当前偏好记录（结构化）：\n" + "\n".join(lines)

    @staticmethod
    def _chain_terms(f: Fact) -> str:
        pred = f.predicate.replace("_", " ")
        aliases = ""
        if f.predicate.lower() in {"works_at", "employer", "company"}:
            aliases = " work works worked employer employed employment company job"
        elif f.predicate.lower() in {"lives_in", "location", "city", "home"}:
            aliases = " live lives lived location city home"
        return f"{f.subject} {pred} {f.object} {f.text} {aliases}"

    def _history_block(
        self,
        user: str,
        query: str,
        as_of: Optional[float],
        limit: int = 16,
        redact_sensitive: bool = False,
    ) -> str:
        """面向知识更新问题的取代链证据。

        常规检索刻意只看 live/as-of 视图。而历史问题需要非破坏性的取代链作为证据：旧值、它何时
        失效、以及取代它的当前值。
        """
        facts = [f for f in self._all_facts() if f.user_id == user]
        if redact_sensitive:
            facts = [f for f in facts if not getattr(f, "sensitive", False)]
        if not facts:
            return ""

        by_id = {f.id: f for f in facts}
        replaces: dict[str, list[Fact]] = {}
        for f in facts:
            if f.supersedes:
                replaces.setdefault(f.supersedes, []).append(f)

        selected: dict[str, Fact] = {}

        def add_chain(f: Fact) -> None:
            selected[f.id] = f
            if f.supersedes and f.supersedes in by_id:
                selected[f.supersedes] = by_id[f.supersedes]
            for newer in replaces.get(f.id, []):
                selected[newer.id] = newer

        for f in facts:
            is_chain_member = (
                f.invalid_at is not None
                or f.expired_at is not None
                or f.supersedes is not None
                or f.id in replaces
            )
            if not is_chain_member:
                continue
            if overlap_terms(query, self._chain_terms(f)):
                add_chain(f)
                continue
            if any(overlap_terms(query, self._chain_terms(newer)) for newer in replaces.get(f.id, [])):
                add_chain(f)

        if not selected:
            return ""

        rows = sorted(
            selected.values(),
            key=lambda f: (f.subject.lower(), f.predicate.lower(), f.valid_at, f.object.lower()),
        )[:limit]
        lines = [
            "valid from | valid until | subject | attribute | value | status",
            "--- | --- | --- | --- | --- | ---",
        ]
        for f in rows:
            until = fmt_date(f.invalid_at) if f.invalid_at is not None else "current"
            view_time = now() if as_of is None else as_of
            if f.valid_at > view_time:
                status = "not yet valid"
            elif f.is_live(as_of):
                status = "current"
            elif f.id in replaces:
                status = "superseded"
            else:
                status = "past"
            lines.append(
                f"{fmt_date(f.valid_at)} | {until} | {f.subject} | "
                f"{f.predicate.replace('_', ' ')} | {f.object} | {status}"
            )
        return "FACT HISTORY (supersession chain for update/previous-value questions):\n" + "\n".join(lines)

    @staticmethod
    def _mentions_value(query: str, value: str) -> bool:
        q = query.lower()
        v = value.lower().strip()
        if not v:
            return False
        if v in q:
            return True
        value_terms = set(stems(value))
        return bool(value_terms) and value_terms <= set(stems(query))

    def _temporal_history_result(
        self,
        query: str,
        user: str,
        as_of: Optional[float],
    ) -> Optional[SearchResult]:
        """自然语言历史查询的直接回答路径。

        调用方提供时间戳时，`as_of()` 已能回答时间点问题。用户也会用自然语言问："Where did Wei
        work before Moonshot AI?" 或 "Where did Wei previously work?"。hybrid 检索刻意只看
        live fact，因此本解析器在只看 live 的检索器用当前值作答之前，先读取非破坏性的取代链。
        """
        if not self.config.temporal_history_queries or not _TEMPORAL_HISTORY_RE.search(query):
            return None
        view_time = now() if as_of is None else as_of
        facts = [f for f in self._all_facts() if f.user_id == user and f.valid_at <= view_time]
        if not facts:
            return None
        by_id = {f.id: f for f in facts}
        replacements: dict[str, list[Fact]] = {}
        for f in facts:
            if f.supersedes and f.supersedes in by_id:
                replacements.setdefault(f.supersedes, []).append(f)

        candidates: list[tuple[int, float, Fact, Fact]] = []
        for old, newers in ((by_id[old_id], ns) for old_id, ns in replacements.items()):
            visible_newers = [n for n in newers if n.valid_at <= view_time]
            if not visible_newers:
                continue
            newer = max(visible_newers, key=lambda f: (f.valid_at, f.created_at, f.id))
            if old.is_live(as_of):
                continue
            old_terms = self._chain_terms(old)
            newer_terms = self._chain_terms(newer)
            overlap = overlap_terms(query, old_terms) | overlap_terms(query, newer_terms)
            score = len(overlap)
            if self._mentions_value(query, newer.object):
                score += 6
            if self._mentions_value(query, old.subject):
                score += 2
            if overlap_terms(query, old.predicate.replace("_", " ")):
                score += 3
            if score <= 0:
                continue
            candidates.append((score, old.valid_at, old, newer))
        if not candidates:
            return None
        _, _, old, newer = max(candidates, key=lambda row: (row[0], row[1], row[2].created_at, row[2].id))
        reinforce(old, self.config.access_boost)
        return SearchResult(query=query, facts=[old, newer], scores=[1.0, 1.0], via="history", _answer=old.object)

    def _chain_facts_for_seeds(
        self,
        seeds: list[Fact],
        user: str,
        as_of: Optional[float],
        limit: int = 12,
        redact_sensitive: bool = False,
    ) -> list[Fact]:
        """把检索到的 fact 扩展为对其可见的取代链。

        这是非破坏性失效化的读路径一侧：检索仍只对 live 的槽位头部排序，但上下文组装可以展示
        有界的链，说明它取代了什么。`as_of` 守卫刻意基于 valid 时间，与现有时间点搜索语义一致，
        因此过去的视图绝不会泄漏未来的取代值。
        """
        if not seeds:
            return []
        view_time = now() if as_of is None else as_of
        facts = [f for f in self._all_facts() if f.user_id == user]
        if redact_sensitive:
            facts = [f for f in facts if not getattr(f, "sensitive", False)]
        by_id = {f.id: f for f in facts}
        replaces: dict[str, list[Fact]] = {}
        for f in facts:
            if f.supersedes:
                replaces.setdefault(f.supersedes, []).append(f)

        def visible(f: Fact) -> bool:
            return f.valid_at <= view_time

        selected: dict[str, Fact] = {}

        def add_visible(f: Fact) -> None:
            if visible(f):
                selected.setdefault(f.id, f)

        def add_chain(seed: Fact) -> None:
            add_visible(seed)
            cur = seed
            seen: set[str] = {seed.id}
            while cur.supersedes and cur.supersedes in by_id and cur.supersedes not in seen:
                cur = by_id[cur.supersedes]
                seen.add(cur.id)
                add_visible(cur)
            queue = [seed]
            while queue:
                cur = queue.pop(0)
                for newer in sorted(replaces.get(cur.id, []), key=lambda f: f.valid_at):
                    if newer.id in seen:
                        continue
                    seen.add(newer.id)
                    add_visible(newer)
                    queue.append(newer)

        for seed in seeds:
            add_chain(seed)

        # 单条可见 fact 已在 FACTS 中展示；只有当链补充了解释性证据（如被取代的值或可见的
        # 替代值）时才渲染。
        if len(selected) <= 1:
            return []
        return sorted(selected.values(), key=lambda f: (f.subject.lower(), f.predicate.lower(), f.valid_at))[:limit]

    def _fact_evolution_block(
        self,
        seeds: list[Fact],
        user: str,
        as_of: Optional[float],
        query: str = "",
        limit: int = 12,
        redact_sensitive: bool = False,
    ) -> str:
        if query:
            seeds = [f for f in seeds if overlap_terms(query, self._chain_terms(f))]
        rows = self._chain_facts_for_seeds(
            seeds,
            user,
            as_of,
            limit=limit,
            redact_sensitive=redact_sensitive,
        )
        if not rows:
            return ""
        replaced_ids = {f.supersedes for f in self._all_facts() if f.supersedes}

        def _role(f: Fact) -> str:
            if f.is_live(as_of):
                return "current"
            if f.id in replaced_ids:
                return "superseded"
            return "replacement" if f.supersedes else "past"

        if self.config.chain_current_first:
            # 按槽位分组，把当前值放在最前，历史缩进在其下方。扁平表格迫使阅读者扫描 `role` 列
            # 找哪一行是"现在为真"，而在知识更新类问题上，回答者选中被取代值的频率高到足以输给
            # 朴素的 full-context（例如在有明确搬家记录后仍回答旧城市）。把当前值作为一条断言
            # （而非一行）放在最前面，消除了这种扫描。
            slots: dict[tuple[str, str], list[Fact]] = {}
            for f in rows:
                slots.setdefault((f.subject, f.predicate), []).append(f)
            blocks = []
            for (subj, pred), facts in slots.items():
                facts.sort(key=lambda x: x.valid_at, reverse=True)
                attr = pred.replace("_", " ")
                live = next((f for f in facts if f.is_live(as_of)), None)
                head = live or facts[0]
                label = "CURRENT" if live is not None else f"LATEST KNOWN ({_role(head)})"
                blocks.append(f"{label}: {subj} {attr} = {head.object}  (since {fmt_date(head.valid_at)})")
                for f in facts:
                    if f is head:
                        continue
                    until = fmt_date(f.invalid_at) if f.invalid_at is not None else "?"
                    blocks.append(
                        f"    was: {f.object}  ({fmt_date(f.valid_at)} - {until}, {_role(f)})"
                    )
            return ("FACT EVOLUTION (current value first, then what it replaced):\n"
                    + "\n".join(blocks))

        lines = [
            "valid from | valid until | subject | attribute | value | role",
            "--- | --- | --- | --- | --- | ---",
        ]
        for f in rows:
            until = fmt_date(f.invalid_at) if f.invalid_at is not None else "current"
            lines.append(
                f"{fmt_date(f.valid_at)} | {until} | {f.subject} | "
                f"{f.predicate.replace('_', ' ')} | {f.object} | {_role(f)}"
            )
        return "FACT EVOLUTION (retrieved supersession chain):\n" + "\n".join(lines)

    @staticmethod
    def _snippet_for_evidence(content: str, needle: str, max_chars: int = 360) -> str:
        """返回紧凑的原始证据切片，优先选取与查询/fact 匹配的行/句子。

        原始来源很有价值，因为 fact 抽取可能丢掉限定词、数量或上下文细节。切片刻意很小，使来源
        证据提升召回的同时，不会悄悄把精简路径变成全文历史重放。
        """
        text = " ".join(content.split())
        if len(text) <= max_chars:
            return text
        parts = [p.strip() for p in re.split(r"(?<=[.!?。！？])\s+|\n+", content) if p.strip()]
        best = ""
        best_score = -1
        for part in parts:
            score = len(overlap_terms(needle, part))
            if score > best_score:
                best = part
                best_score = score
        snippet = " ".join((best or text).split())
        if len(snippet) <= max_chars:
            return snippet
        return snippet[: max_chars - 1].rstrip() + "…"

    def _provenance_raw_block(
        self,
        facts: list[Fact],
        query: str,
        user: str,
        as_of: Optional[float],
        exclude_episode_ids: set[str],
        limit: int = 4,
        snippet_chars: int = 360,
        redact_sensitive: bool = False,
    ) -> str:
        """为检索到的 fact 提供紧凑的来源 episode。

        这是 hybrid 记忆的原始侧：fact 给出精确、经过冲突解决的槽位；其来源 episode 恢复被丢掉
        的细节。只使用被检索 fact 明确引用的 episode，排除已渲染的完整细节 chunk，遵守 `as_of`，
        并在脱敏上下文中禁用该块——因为原始散文可能包含 fact 分类器覆盖不到的敏感内容。
        """
        if redact_sensitive or not facts:
            return ""
        rows: list[str] = []
        seen_eps: set[str] = set(exclude_episode_ids)
        view_time = now() if as_of is None else as_of
        history_query = bool(_TEMPORAL_HISTORY_RE.search(query or ""))

        def fact_order(f: Fact) -> tuple[int, float]:
            past = int(history_query and f.valid_at <= view_time and not f.is_live(as_of))
            return (past, f.valid_at)

        for fact in sorted(facts, key=fact_order, reverse=True):
            if getattr(fact, "sensitive", False):
                continue
            if query and not overlap_terms(query, self._chain_terms(fact)):
                continue
            for ep_id in fact.provenance:
                if ep_id in seen_eps:
                    continue
                ep = self.episodes_doc.get(ep_id)
                if ep is None or ep.user_id != user or ep.event_time > view_time:
                    continue
                seen_eps.add(ep_id)
                needle = f"{query} {fact.text} {fact.subject} {fact.predicate} {fact.object}"
                snippet = self._snippet_for_evidence(ep.content, needle, max_chars=snippet_chars)
                date = ep.metadata.get("date") or fmt_date(ep.event_time)
                rows.append(
                    f"- [{date}] (session: {ep.session_id}; supports: "
                    f"{fact.subject} {fact.predicate.replace('_', ' ')} {fact.object}) {snippet}"
                )
                if len(rows) >= limit:
                    return "PROVENANCE RAW EVIDENCE (source episodes for retrieved facts):\n" + "\n".join(rows)
        return "PROVENANCE RAW EVIDENCE (source episodes for retrieved facts):\n" + "\n".join(rows) if rows else ""


    @staticmethod
    def _merge_promoted_chunks(promoted: list, retrieved: list, k: int) -> list:
        """把经来源提升的 episode 与语义检索到的 episode 合并，并设语义下限：提升最多占用
        chunk 预算的一半。

        提升的 chunk 以排序后的 FACT 为键，因此当 fact 检索未命中时，语义 episode 是唯一仍能
        携带答案的路径。若放任提升占满整个预算，恰好会把这些 episode 挤出去，在单 session 召回
        上造成正确->拒答的回归（2026-08-27，相对 headline 日志的 50 项 A/B）；设上限保住了
        旧值/时间类收益，又不在此处付出代价。未用完的提升预算回流给检索 chunk，反之亦然。
        k=1 时唯一槽位保持提升优先（修复前的行为；k=2 采集的回归数据对它没有结论，且现有测试
        钉死了该行为）。
        """
        cap = max(1, k // 2)
        merged: list = []
        seen: set[str] = set()
        for ep in promoted[:cap]:
            if ep.id not in seen:
                seen.add(ep.id)
                merged.append(ep)
        for ep in retrieved:
            if len(merged) >= k:
                break
            if ep.id not in seen:
                seen.add(ep.id)
                merged.append(ep)
        for ep in promoted[cap:]:  # 仅当检索填不满预算时回填
            if len(merged) >= k:
                break
            if ep.id not in seen:
                seen.add(ep.id)
                merged.append(ep)
        return merged

    def _provenance_detail_chunks(
        self,
        facts: list[Fact],
        query: str,
        user: str,
        as_of: Optional[float],
        limit: int,
        redact_sensitive: bool = False,
    ) -> list[Episode]:
        """由检索 fact 的来源溯源支撑的完整原始 chunk。

        常规原始检索以查询为先。若它被干扰带偏，fact 仍能通过来源溯源定位正确的源 episode；
        把该 episode 提升进来即可恢复周边细节，同时同样的 `n_chunks` 预算保持读路径精简。
        """
        if limit <= 0 or redact_sensitive or not facts:
            return []
        view_time = now() if as_of is None else as_of
        history_query = bool(_TEMPORAL_HISTORY_RE.search(query or ""))
        scored: dict[str, tuple[int, int, float, Episode]] = {}
        for fact_rank, fact in enumerate(facts):
            if getattr(fact, "sensitive", False):
                continue
            fact_terms = self._chain_terms(fact)
            fact_overlap = len(overlap_terms(query, fact_terms)) if query else 0
            if query and fact_overlap == 0:
                continue
            for ep_id in fact.provenance:
                ep = self.episodes_doc.get(ep_id)
                if ep is None or ep.user_id != user or ep.event_time > view_time:
                    continue
                ep_overlap = len(overlap_terms(query, ep.content)) if query else 0
                support_overlap = len(overlap_terms(fact.text, ep.content))
                score = fact_overlap * 8 + support_overlap * 3 + ep_overlap * 2
                if history_query and fact.valid_at <= view_time and not fact.is_live(as_of):
                    score += 96
                prev = scored.get(ep.id)
                item = (score, fact_rank, ep.event_time, ep)
                if prev is None or (score, -fact_rank, ep.event_time) > (prev[0], -prev[1], prev[2]):
                    scored[ep.id] = item
        ranked = sorted(scored.values(), key=lambda item: (-item[0], item[1], -item[2], item[3].id))
        return [ep for _, _, _, ep in ranked[:limit]]

    def _aggregation_block(
        self,
        facts: list[Fact],
        summaries: list[Episode],
        detail_eps: list[Episode],
        query: str = "",
        limit: int = 36,
    ) -> str:
        """面向计数/列举/跨 session 问题的宽证据表。"""
        rows: list[tuple[int, float, str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add_row(t: float, source: str, evidence: str) -> None:
            if source in {"conversation", "summary"} and query:
                evidence = self._snippet_for_evidence(evidence, query, max_chars=520)
            text = " ".join(evidence.split())
            if not text:
                return
            key = (fmt_date(t), text.lower()[:140])
            if key in seen:
                return
            seen.add(key)
            score = len(overlap_terms(query, text)) if query else 0
            if query and score == 0:
                return
            if source == "fact":
                score += 2
            elif source == "conversation":
                score += 1
            rows.append((score, t, source, text[:520]))

        for f in facts:
            add_row(f.valid_at, "fact", f.text)
        for ep in summaries:
            add_row(ep.event_time, "summary", ep.summary or ep.content)
        for ep in detail_eps:
            add_row(ep.event_time, "conversation", ep.content)
        if not rows:
            return ""
        rows.sort(key=lambda x: (-x[0], x[1]))
        lines = ["date | source | evidence", "--- | --- | ---"]
        for _, t, source, evidence in rows[:limit]:
            lines.append(f"{fmt_date(t)} | {source} | {evidence}")
        evidence = (
            "AGGREGATION EVIDENCE (dedupe before counting/listing; include candidate names and key "
            "attributes in the answer):\n" + "\n".join(lines)
        )
        candidates = render_aggregation_candidates(
            extract_aggregation_candidates(
                query,
                facts,
                summaries + detail_eps,
                llm=self.llm,
                numeric=self.config.numeric_aggregation_candidates,
                constraint_filter=self.config.aggregation_constraint_filter,
            )
        )
        return f"{candidates}\n\n{evidence}" if candidates else evidence

    def _duration_block(
        self,
        query: str,
        facts: list[Fact],
        episodes: list[Episode],
        limit: int = 24,
    ) -> str:
        """为时长类问题提供带日期的原始片段——开始/结束对常在摘要中丢失。"""
        anchors = [a or b for a, b in re.findall(r"'([^']+)'|\"([^\"]+)\"", query)]
        anchors = [a.strip() for a in anchors if a and len(a.strip()) >= 3]
        if not anchors:
            anchors = sorted(overlap_terms(query, query), key=len, reverse=True)[:3]
        action_re = re.compile(
            r"\b(start(?:ed)?|begin|began|finish(?:ed)?|complete(?:d)?|listen(?:ed|ing)?|read(?:ing)?|"
            r"watch(?:ed|ing)?|today|last|week|weeks|month|months|day|days)\b",
            re.IGNORECASE,
        )
        rows: list[tuple[float, str, str]] = []
        seen: set[tuple[str, str, str]] = set()

        def add_row(t: float, anchor: str, source: str, evidence: str) -> None:
            text = " ".join(evidence.split())
            if not text:
                return
            key = (fmt_date(t), anchor.lower(), text.lower()[:180])
            if key in seen:
                return
            seen.add(key)
            rows.append((t, anchor, f"{source}: {text[:280]}"))

        for f in facts:
            lower = f.text.lower()
            for anchor in anchors:
                if anchor.lower() in lower:
                    add_row(f.valid_at, anchor, "fact", f.text)

        split_re = re.compile(r"(?<=[.!?])\s+|\n+")
        for ep in episodes:
            parts = split_re.split(ep.content)
            for anchor in anchors:
                al = anchor.lower()
                for part in parts:
                    pl = part.lower()
                    if al not in pl:
                        continue
                    if not action_re.search(part):
                        continue
                    add_row(ep.event_time, anchor, "conversation", part)
                    break

        if not rows:
            return ""
        rows.sort(key=lambda x: (x[1].lower(), x[0]))
        lines = ["date | item | evidence", "--- | --- | ---"]
        for t, anchor, evidence in rows[:limit]:
            lines.append(f"{fmt_date(t)} | {anchor} | {evidence}")
        return (
            "DURATION EVIDENCE (pair start/finish dates per item; compute each end-start, then sum):\n"
            + "\n".join(lines)
        )

    def _graph_paths_block(
        self,
        query: str,
        user: str,
        as_of: Optional[float],
        limit: int = 12,
        redact_sensitive: bool = False,
    ) -> str:
        """从查询锚点出发的有界图路径证据；为关系链问题提供足够的结构。"""
        lines: list[str] = []
        plan = self.planner.plan(query, user, as_of)
        if plan is not None and plan.facts:
            for f in plan.facts[:limit]:
                if redact_sensitive and getattr(f, "sensitive", False):
                    continue
                lines.append(f"- [{fmt_date(f.valid_at)}] {f.text}")
            if lines:
                return "GRAPH PATHS (relation evidence):\n" + "\n".join(lines)
            return ""
        if not self.config.graph_proximity:
            return ""
        qids = self.retriever.query_entity_ids(query, user)
        excluded = self.retriever.graph_exclusion_zone(query, user, as_of)
        frontier: list[tuple[str, int]] = [(eid, 0) for eid in qids]
        seen_nodes = set(qids)
        seen_edges: set[str] = set()
        max_hops = max(0, self.config.max_hops)
        while frontier and len(lines) < limit:
            eid, depth = frontier.pop(0)
            if depth > max_hops:
                continue
            for direction in ("out", "in"):
                for rel in self.graph.neighbors(eid, as_of, direction):
                    if rel.id in seen_edges:
                        continue
                    if rel.subject_id in excluded or rel.object_id in excluded:
                        continue
                    f = self.fact_store.get(rel.fact_id) or self.cold_store.get(rel.fact_id)
                    if f is None or not f.is_live(as_of):
                        continue
                    if redact_sensitive and getattr(f, "sensitive", False):
                        continue
                    seen_edges.add(rel.id)
                    subj = self.graph.entities.get(rel.subject_id)
                    obj = self.graph.entities.get(rel.object_id)
                    lines.append(
                        f"- [{fmt_date(f.valid_at)}] {subj.name if subj else f.subject} "
                        f"--{rel.predicate}--> {obj.name if obj else f.object}"
                    )
                    neighbor_id = rel.object_id if direction == "out" else rel.subject_id
                    if depth < max_hops and neighbor_id not in seen_nodes:
                        seen_nodes.add(neighbor_id)
                        frontier.append((neighbor_id, depth + 1))
                    if len(lines) >= limit:
                        return "GRAPH PATHS (relation evidence):\n" + "\n".join(lines)
        return "GRAPH PATHS (relation evidence):\n" + "\n".join(lines) if lines else ""

    @staticmethod
    def _evidence_block_title(block: str) -> str:
        return block.split("\n", 1)[0].rstrip(":：")

    @classmethod
    def _evidence_block_kind(cls, block: str) -> str:
        title = cls._evidence_block_title(block)
        if title.startswith("PROVENANCE RAW"):
            return "provenance"
        if title.startswith("RELEVANT CONVERSATIONS"):
            return "raw"
        if title.startswith("GRAPH PATHS"):
            return "graph"
        if title.startswith("AGGREGATION"):
            return "aggregation"
        if title.startswith("DURATION"):
            return "duration"
        if title.startswith("FACT HISTORY"):
            return "history"
        if title.startswith("FACT EVOLUTION"):
            return "evolution"
        if title.startswith("CURRENT STATE"):
            return "current_state"
        if title.startswith("当前偏好记录（结构化）"):
            return "preference"
        if title.startswith("PROCEDURAL MEMORY"):
            return "procedural"
        if title.startswith("FACTS"):
            return "facts"
        if title.startswith("SESSION SUMMARIES"):
            return "summaries"
        if title.startswith("TIMELINE"):
            return "timeline"
        if title.startswith("WORKING MEMORY"):
            return "working"
        if title.startswith("USER PROFILE"):
            return "profile"
        return "other"

    @staticmethod
    def _fit_evidence_block(block: str, cap: int) -> str:
        if cap <= 0:
            return ""
        if len(block) <= cap:
            return block
        title, _, body = block.partition("\n")
        if cap <= len(title):
            return title[:cap]
        return (title + "\n" + body[: cap - len(title) - 1]).rstrip()

    def _evidence_priority(self, kind: str, need) -> int:
        score = {
            "working": 95,
            "facts": 90,
            "current_state": 88,
            "procedural": 86,
            "provenance": 82,
            "raw": 78,
            "graph": 76,
            "history": 75,
            "evolution": 72,
            "timeline": 68,
            "aggregation": 66,
            "duration": 66,
            "preference": 66,
            "summaries": 58,
            "profile": 38,
            "other": 40,
        }.get(kind, 40)
        if need is None:
            return score
        if need.exact_lookup:
            score += {"provenance": 45, "raw": 35, "facts": 20}.get(kind, 0)
        if need.multi_hop:
            score += {"graph": 45, "facts": 25, "provenance": 15, "raw": 12}.get(kind, 0)
        if need.aggregation:
            score += {"aggregation": 55, "raw": 35, "summaries": 30, "facts": 10}.get(kind, 0)
        if need.duration:
            score += {"duration": 55, "raw": 35, "timeline": 25, "summaries": 15}.get(kind, 0)
        if need.history:
            score += {"history": 55, "evolution": 40, "timeline": 20, "facts": 15}.get(kind, 0)
        if getattr(need, "procedural", False):
            score += {"procedural": 55, "facts": 18, "provenance": 15, "raw": 12, "summaries": 8}.get(kind, 0)
        if need.current_state:
            score += {"current_state": 45, "facts": 20, "evolution": 20}.get(kind, 0)
        if need.preference:
            score += {"preference": 45, "facts": 18, "provenance": 12, "raw": 10}.get(kind, 0)
        if need.timeline:
            score += {"timeline": 35, "history": 15, "facts": 10}.get(kind, 0)
        return score

    def _evidence_block_cap(self, kind: str, budget: int, need) -> int:
        ratio = {
            "aggregation": 0.60,
            "duration": 0.55,
            "provenance": 0.55,
            "raw": 0.55,
            "graph": 0.45,
            "history": 0.45,
            "evolution": 0.42,
            "facts": 0.42,
            "timeline": 0.35,
            "summaries": 0.35,
            "current_state": 0.35,
            "procedural": 0.38,
            "preference": 0.35,
            "working": 0.25,
            "profile": 0.18,
            "other": 0.25,
        }.get(kind, 0.25)
        if need is not None and need.exact_lookup and kind in {"provenance", "raw"}:
            ratio = 0.75
        if need is not None and need.aggregation and kind == "aggregation":
            ratio = 0.75
        cap = int(max(96, budget * ratio))
        return min(cap, budget)

    def _fit_blocks_even(self, selected: list[str], budget: int) -> str:
        if not selected or budget <= 0:
            return ""
        sep = "\n\n"
        sep_chars = len(sep) * (len(selected) - 1)
        if budget <= sep_chars:
            return sep.join(block.split("\n", 1)[0] for block in selected)[:budget]
        available = budget - sep_chars
        quota, extra = divmod(available, len(selected))
        fitted: list[str] = []
        for i, block in enumerate(selected):
            cap = quota + (1 if i < extra else 0)
            fitted.append(self._fit_evidence_block(block, cap))
        return sep.join(fitted)[:budget]

    def _fit_blocks_by_evidence_budget(self, blocks: list[str], budget: int, need) -> str:
        """面向精简读取路径的意图感知证据打包器。

        旧回退逻辑把紧张的预算在块间均分，或从顶部直接截断，可能把宝贵的 token 花在 profile
        文本上，却丢掉问题真正需要的 raw/provenance/graph 证据。这里按查询意图对证据类别排序，
        给每类设上限，再按原有的可读顺序输出被选中的证据。
        """
        if not blocks or budget <= 0:
            return ""
        sep = "\n\n"
        ranked = sorted(
            enumerate(blocks),
            key=lambda item: (-self._evidence_priority(self._evidence_block_kind(item[1]), need), item[0]),
        )
        selected: list[tuple[int, str]] = []
        used = 0
        for idx, block in ranked:
            kind = self._evidence_block_kind(block)
            priority = self._evidence_priority(kind, need)
            sep_cost = len(sep) if selected else 0
            remaining = budget - used - sep_cost
            if remaining <= 0:
                break
            if selected and remaining < 120 and priority < 100:
                continue
            cap = min(len(block), self._evidence_block_cap(kind, budget, need), remaining)
            fitted = self._fit_evidence_block(block, cap)
            if not fitted:
                continue
            min_title = self._evidence_block_title(block)
            if selected and len(fitted) < min(len(min_title), max(24, remaining)):
                continue
            selected.append((idx, fitted))
            used += sep_cost + len(fitted)
        if not selected:
            return self._fit_evidence_block(blocks[0], budget)
        return sep.join(block for _, block in sorted(selected, key=lambda item: item[0]))[:budget]

    def lean_context(
        self,
        query: str,
        user_id: str = "default",
        as_of: Optional[float] = None,
        top_k: Optional[int] = None,
        n_summaries: int = 20,
        n_facts: int = 15,
        n_chunks: int = 2,
        persona: bool = True,
        agentic: bool = False,
        cascade: bool = False,  # _S 最优为关；它是 _M/10M 的扩展原语（由粗到细的下钻）
        timeline: bool = False,  # 追加按时间排序的事件时间线（有助于时序/时长问题）
        char_budget: int = 60_000,
        session_id: Optional[str] = None,  # 设置时，在最前面加上本 session 的瞬态工作记忆
        redact_sensitive: bool = False,  # 安全/共享视图：只显示非敏感的结构化 fact
    ) -> str:
        """可扩展的读路径（AGENTS.md Bet A/E）：从检索到的抽象层组装一个小而组织良好的上下文，
        而不是整段历史——
            L3 persona  +  L1 带日期 fact  +  L2 session 摘要  +  少量完整 chunk 提供细节。

        历史增长时 token 保持大致恒定（检索的是固定大小的切片），这是 full-context 做不到的。
        两个让它既精简又准确的设计点：
          * top-`n_chunks` 个 session 完整展示（细节），并从摘要块中排除，因此没有 session 出现
            两次——每个 token 都买到新信息。
          * 摘要块提供宽时间覆盖（默认 20），这正是聚合类问题（"how many trips"、"list
            everything"）所需的；摘要很小，覆盖因此仍然廉价。
        `char_budget` 对组装出的上下文硬性封顶，使其永远不可能接近全文历史的规模。"""
        user = user_id
        blocks: list[str] = []
        need = (
            plan_evidence(
                query,
                aggregation_recall_expansion=self.config.aggregation_recall_expansion,
            )
            if self.config.evidence_planner
            else None
        )
        if need is not None:
            n_facts = max(n_facts, need.n_facts)
            n_summaries = max(n_summaries, need.n_summaries)
            n_chunks = max(n_chunks, need.n_chunks)
            agentic = agentic or need.use_agentic
            cascade = cascade or need.use_cascade
            timeline = timeline or need.timeline
        if redact_sensitive:
            # 自由文本层（persona、摘要、原始 chunk、工作记忆）可能把敏感内容揉进散文。因此
            # 脱敏上下文刻意只含 fact，且在 fact 级敏感性分类之后，使共享/第三方注入有清晰的
            # 隐私边界。
            n_summaries = 0
            n_chunks = 0
            cascade = False

        # Bet B——multi-hop 分解。对关系/聚合类问题，由 LLM 拆成子查询（'who is my colleague'
        # + 'where does <colleague> work'）；然后对每个角度检索 fact 和摘要并取并集，使单个查询
        # 浮不出的二跳证据也被拉进来。这是领域的软肋（multi-session/multi-hop），也是我们的
        # 主攻面。
        queries = [query]
        if need is not None:
            for sq in need.subqueries:
                if sq not in queries:
                    queries.append(sq)
        if agentic and self.llm is not None:
            from .retrieve.agentic import AgenticRetriever
            queries += AgenticRetriever(self, self.llm)._subqueries(query)

        # L3 persona 是自由文本合成，可能揉入敏感 fact；脱敏上下文只允许结构化 fact，因此整个
        # 丢弃。
        if persona and not redact_sensitive:
            p = self._persona_at(user, as_of)
            if p:
                label = "USER PROFILE" if as_of is None else f"USER PROFILE (as of {fmt_date(as_of)})"
                blocks.append(f"{label}:\n{p}")

        # 工作记忆：当前 session 的瞬态状态（"今天我嗓子疼"、本次出行的意图）——浮出以让答案
        # 反映"此刻"，但绝不整合进长期存储或 profile。
        if session_id is not None and not redact_sensitive:
            wm = self.working_memory(user, session_id=session_id, as_of=as_of)
            if wm:
                wl = "\n".join(f"- [{w.kind}] {w.content}"
                               for w in sorted(wm, key=lambda x: x.event_time))
                blocks.append(f"WORKING MEMORY (this session, ephemeral):\n{wl}")

        # L1 fact：对每个（子）查询做 hybrid 检索后取并集；再从查询实体做 n-hop 图扩展。
        fact_map: dict[str, Fact] = {}
        for q in queries:
            ranked = self.retriever.retrieve(q, user, as_of, top_k or n_facts)[0]
            if not ranked:
                ranked = self._retrieve_cold(q, user, as_of, top_k or n_facts)[0]
            for f, _ in ranked:
                fact_map.setdefault(f.id, f)
        for f in self._graph_related_facts(query, user, as_of, limit=n_facts):
            fact_map.setdefault(f.id, f)
        all_facts = list(fact_map.values())
        # 关注项 "mute"：丢弃用户要求在读上下文中抑制的主题相关 fact。
        mute = self.focus.get("mute", [])
        if mute:
            all_facts = [f for f in all_facts if not self._matches(f, mute)]
        # 敏感性脱敏（feature ⑤）：组装共享/导出上下文时排除敏感 fact。
        if redact_sensitive:
            all_facts = [f for f in all_facts if not getattr(f, "sensitive", False)]
        # 可选：对 fact 池做 cross-encoder 重排。与重排整段 session 不同（cross-encoder 会把
        # session 截断到 512 导致误排——一个已知的 _S 回归），fact 很短，因此重排能锐化 fact
        # 选择且无截断损失。
        if self.reranker is not None and len(all_facts) > (top_k or n_facts):
            order = self.reranker.rerank(query, [(str(i), f.text) for i, f in enumerate(all_facts)],
                                         top_k or n_facts)
            all_facts = [all_facts[int(i)] for i, _ in order]
        self.working_set = all_facts  # 工作记忆：本次查询的活动集
        if all_facts:
            for f in all_facts:  # 访问即强化：被浮出的 fact 保持显著（间隔效应）
                reinforce(f, self.config.access_boost)
            by_date = sorted(all_facts, key=lambda f: f.valid_at, reverse=True)  # 最新在前（更新优先）
            fl = "\n".join(f"- [{fmt_date(f.valid_at)}] {f.text}" for f in by_date)
            blocks.append(f"FACTS (current, dated):\n{fl}")
            if need is not None and need.current_state:
                state = self._current_state_block(all_facts)
                if state:
                    blocks.append(state)
            if need is not None and need.preference:
                prefs = self._preference_block(user, query, all_facts)
                if prefs:
                    blocks.append(prefs)
            # TIMELINE：同样的 fact 按旧->新排列并显式留出间隔，使"最早/最近/间隔多久"能直接
            # 从顺序读出，日期算术为模型铺好而不是留给心算（时间类问题的主要失败模式）。
            if timeline:
                chrono = sorted(all_facts, key=lambda f: f.valid_at)
                tl = "\n".join(f"- {fmt_date(f.valid_at)}: {f.text}" for f in chrono)
                blocks.append(f"TIMELINE (oldest to newest — use for ordering and durations):\n{tl}")
            if need is not None and need.multi_hop:
                paths = self._graph_paths_block(query, user, as_of, redact_sensitive=redact_sensitive)
                if paths:
                    blocks.append(paths)
            if self.config.chain_evidence and (need is None or not need.history):
                evo = self._fact_evolution_block(
                    all_facts,
                    user,
                    as_of,
                    query=query,
                    limit=max(8, min(18, n_facts + 3)),
                    redact_sensitive=redact_sensitive,
                )
                if evo:
                    blocks.append(evo)

        provenance_facts = list(all_facts)
        if self.config.chain_evidence and all_facts:
            seen_chain_facts = {f.id for f in provenance_facts}
            for f in self._chain_facts_for_seeds(
                all_facts,
                user,
                as_of,
                limit=max(8, min(18, n_facts + 3)),
                redact_sensitive=redact_sensitive,
            ):
                if f.id not in seen_chain_facts:
                    seen_chain_facts.add(f.id)
                    provenance_facts.append(f)

        if (
            self.config.procedural_memory
            and not redact_sensitive
            and need is not None
            and getattr(need, "procedural", False)
        ):
            proc = self._procedural_memory_block(
                query,
                user,
                as_of,
                limit=getattr(self.config, "procedural_memory_k", 6),
            )
            if proc:
                blocks.append(proc)

        if need is not None and need.history and self.config.temporal_history_queries:
            hist = self._history_block(user, query, as_of, redact_sensitive=redact_sensitive)
            if hist:
                blocks.append(hist)

        # L2 粗层：对每个（子）查询检索 session 摘要并排序。这是由粗到细级联的粗层
        # （AGENTS.md Bet E / OpenViking）：摘要很小，可以廉价地索引并排序大量 session——这是
        # 突破模型窗口（_M / 10M 前沿）的关键。
        summ_ranked: list[Episode] = []
        seen_s: set[str] = set()
        if n_summaries:
            for q in queries:
                for e in self.retrieve_summaries(q, user, n_summaries, as_of=as_of):
                    if e.id not in seen_s:
                        seen_s.add(e.id)
                        summ_ranked.append(e)

        # 细层下钻：级联模式下，细节 chunk 就是排序最高的摘要各自的 session（分数由粗层传播到
        # 细层），因此绝不会对无关 session 的原始轮次做嵌入扫描。非级联时，细节是直接的
        # episode 查找（对小型历史足够）。
        if cascade and summ_ranked:
            detail_eps = summ_ranked[:n_chunks] if n_chunks else []
        else:
            detail_eps = []
            if n_chunks:
                seen_detail: set[str] = set()
                detail_queries = list(need.subqueries) + [query] if need is not None and need.subqueries else queries
                per_query = [
                    self.retrieve_episodes(q, user, max(n_chunks, 1), as_of=as_of)
                    for q in detail_queries
                ]
                max_hits = max((len(eps) for eps in per_query), default=0)
                for rank in range(max_hits):
                    for eps in per_query:
                        if rank >= len(eps):
                            continue
                        ep = eps[rank]
                        if ep.id in seen_detail:
                            continue
                        seen_detail.add(ep.id)
                        detail_eps.append(ep)
                        if len(detail_eps) >= n_chunks:
                            break
                    if len(detail_eps) >= n_chunks:
                        break
        detail_ids = {e.id for e in detail_eps}
        if self.config.provenance_chunk_promotion and n_chunks and not redact_sensitive:
            promoted = self._provenance_detail_chunks(
                provenance_facts,
                " ".join(queries),
                user,
                as_of,
                limit=n_chunks,
                redact_sensitive=redact_sensitive,
            )
            if promoted:
                detail_eps = self._merge_promoted_chunks(promoted, detail_eps, n_chunks)
                detail_ids = {e.id for e in detail_eps}
        summaries = [e for e in summ_ranked if e.id not in detail_ids]

        # 聚合/列举类问题尤其容易受压缩损失影响：正确的 session 可能被检索用于整合，但 LLM 的
        # fact/摘要步骤会丢掉某个低显著度候选。为这类问题保留一张紧凑的原始证据表，让计数/列举
        # 在候选被 System-2 抽象挤压之前就能看到它们。
        aggregation_eps: list[Episode] = []
        if need is not None and need.aggregation and not redact_sensitive:
            seen_agg = set(detail_ids)
            for q in queries:
                for ep in self.retrieve_episodes(q, user, 4, as_of=as_of):
                    if ep.id in seen_agg:
                        continue
                    seen_agg.add(ep.id)
                    aggregation_eps.append(ep)
                    if len(aggregation_eps) >= 12:
                        break
                if len(aggregation_eps) >= 12:
                    break

        if summaries:
            chrono = sorted(summaries, key=lambda e: e.event_time)
            sm = "\n".join(
                f"- [{e.metadata.get('date') or fmt_date(e.event_time)}] {e.summary}" for e in chrono
            )
            blocks.append(f"SESSION SUMMARIES (relevant, chronological):\n{sm}")

        if detail_eps:
            chunks = "\n\n".join(
                f"原始记录时间：{e.metadata.get('date') or fmt_date(e.event_time)}\n"
                f"会话：{e.session_id}\n"
                f"记录内容：{e.content}" for e in detail_eps
            )
            blocks.append(f"RELEVANT CONVERSATIONS (full detail):\n{chunks}")

        if self.config.provenance_evidence:
            raw_prov = self._provenance_raw_block(
                provenance_facts,
                query,
                user,
                as_of,
                exclude_episode_ids=detail_ids,
                redact_sensitive=redact_sensitive,
            )
            if raw_prov:
                blocks.append(raw_prov)

        if need is not None and need.duration:
            dur = self._duration_block(query, all_facts, detail_eps + aggregation_eps)
            if dur:
                blocks.append(dur)

        if need is not None and need.aggregation:
            agg = self._aggregation_block(
                all_facts,
                summaries,
                detail_eps + aggregation_eps,
                query=" ".join(queries),
            )
            if agg:
                blocks.append(agg)

        assembled = "\n\n".join(blocks)
        if char_budget == 60_000:
            full_chars = sum(len(ep.content) for ep in self.episodes_doc.values() if ep.user_id == user)
            if full_chars:
                # 极小的历史仍需要足够空间容纳 profile + fact + 一个 chunk；否则均分块适配器
                # 可能恰好在约束所在处截断持久的项目规则。
                char_budget = min(char_budget, max(2048, full_chars * 3 - 1))
            if len(assembled) > char_budget:
                if self.config.evidence_budgeting:
                    return self._fit_blocks_by_evidence_budget(blocks, char_budget, need)
                return self._fit_blocks_even(blocks, char_budget)
        elif len(assembled) > char_budget and self.config.evidence_budgeting:
            return self._fit_blocks_by_evidence_budget(blocks, char_budget, need)
        return assembled[:char_budget]

    # --- 读路径 ---
    def search(
        self,
        query: str,
        user_id: str = "default",
        as_of: Optional[float] = None,
        top_k: Optional[int] = None,
    ) -> SearchResult:
        user = user_id

        # 1. 先走 multi-hop planner（仅对真正的 >=2 跳关系类问题触发）
        plan = self.planner.plan(query, user, as_of)
        if plan is not None:
            for f in plan.facts:
                reinforce(f, self.config.access_boost)
            return SearchResult(query=query, facts=plan.facts, via="multi-hop", _answer=plan.answer)

        # 2. 基于取代链的自然语言历史查询
        hist = self._temporal_history_result(query, user, as_of)
        if hist is not None:
            return hist

        # 3. 类型化程序性记忆：对规则/how-to 查询，优先使用带来源的程序性视图而非泛化的 fact
        # 答案。这使长期指令可审计，同时普通查询问题仍会向下落到常规检索。
        proc = self._procedural_fallback(query, user, as_of=as_of)
        if proc is not None:
            return proc

        # 4. hybrid 检索
        ranked, diag = self.retriever.retrieve(query, user, as_of, top_k)
        via = "hybrid"
        if not ranked:
            ranked, diag = self._retrieve_cold(query, user, as_of, top_k)
            if ranked:
                via = "cold"
        if not ranked:
            proc = self._procedural_fallback(query, user, as_of=as_of)
            if proc is not None:
                return proc
            summ = self._summary_fallback(query, user_id, as_of=as_of)
            if summ is not None:
                return summ
            return SearchResult(query=query, via="abstain", abstained=True)

        facts = [f for f, _ in ranked]
        scores = [s for _, s in ranked]

        # #2/#3 答案类型对齐：若问题要求结构化的值，则浮出 object 真正长得像该类型的 fact；
        # 若没有，语义命中即为虚假 -> 记忆中没有。
        etype = _expected_answer_type(query)
        type_ok = True
        if etype is not None:
            matched = [f for f in facts if _ANSWER_TYPE_MATCH[etype](f.object or f.text)]
            if matched:
                facts = matched + [f for f in facts if f not in matched]
            else:
                type_ok = False

        if not type_ok or self._should_abstain(query, facts, diag):
            cold_ranked, cold_diag = self._retrieve_cold(query, user, as_of, top_k)
            if cold_ranked:
                facts = [f for f, _ in cold_ranked]
                scores = [s for _, s in cold_ranked]
                diag = cold_diag
                type_ok = True
                if etype is not None:
                    matched = [f for f in facts if _ANSWER_TYPE_MATCH[etype](f.object or f.text)]
                    if matched:
                        facts = matched + [f for f in facts if f not in matched]
                    else:
                        type_ok = False
                if type_ok and not self._should_abstain(query, facts, diag):
                    return SearchResult(query=query, facts=facts, scores=scores, via="cold")
            # #3b：答案可能只存在于 session 摘要中（某个 how-to、规则、安装命令），extractor
            # 从未把它原子化成 fact。拒答之前先回退到最相关的摘要。
            proc = self._procedural_fallback(query, user, as_of=as_of)
            if proc is not None:
                return proc
            summ = self._summary_fallback(query, user_id, as_of=as_of)
            if summ is not None:
                return summ
            return SearchResult(query=query, facts=facts, scores=scores, via="abstain", abstained=True)

        if via != "cold":
            reinforce(facts[0], self.config.access_boost)
        return SearchResult(query=query, facts=facts, scores=scores, via=via)

    def as_of(self, query: str, when: float, user_id: str = "default", top_k: Optional[int] = None) -> SearchResult:
        """回答"在 `when` 时刻我们认为什么是真的？"——双时间轴时间点查询。"""
        return self.search(query, user_id=user_id, as_of=when, top_k=top_k)

    def retrieve_episodes(
        self,
        query: str,
        user_id: str = "default",
        k: int = 5,
        pool: Optional[int] = None,
        as_of: Optional[float] = None,
    ):
        """为查询检索 top-k 条原始 episode（session）：bi-encoder 候选池 → BM25 词法重排（RRF）
        → 可选 cross-encoder 重排。

        BM25 层：当 pool >= episode 总数时（LongMemEval_S：约 54 个 session，pool 至多 100），
        所有 episode 都在候选里，仅凭嵌入排序会漏掉精确词匹配（名字、地点、日期）。BM25 的 RRF
        融合把这些提上来而不取代语义信号。改善偏好类和精确实体类问题——原始文本词项优于嵌入
        相似度的场景。
        """
        user = user_id
        pool = pool or max(k * 5, 25)
        candidates = self.episodes_vec.search(
            self.embedder.embed(query),
            pool,
            where=lambda e: e.user_id == user and (as_of is None or e.event_time <= as_of),
        )
        eps = [ep for _, ep in candidates]

        # 候选数多于要返回的数量时，做 BM25 + 嵌入的 RRF 融合。
        if len(eps) > k:
            bm25 = bm25_scores(query, [(ep.id, ep.content) for ep in eps])
            if bm25:
                bm25_rank = {eid: r for r, (eid, _) in
                             enumerate(sorted(bm25.items(), key=lambda x: x[1], reverse=True))}
                K_RRF = 60  # 标准 RRF 常数——在 [30, 100] 区间内不敏感
                fused_order = sorted(range(len(eps)), key=lambda i: -(
                    1.0 / (K_RRF + i + 1) +  # 嵌入排名贡献
                    1.0 / (K_RRF + bm25_rank.get(eps[i].id, len(eps)) + 1)  # BM25 排名贡献
                ))
                eps = [eps[i] for i in fused_order]

        if self.reranker is not None and len(eps) > k:
            ranked = self.reranker.rerank(query, [(i, ep.content) for i, ep in enumerate(eps)], k)
            return [eps[i] for i, _ in ranked]
        return eps[:k]

    def context_for(
        self,
        query: str,
        user_id: str = "default",
        as_of: Optional[float] = None,
        top_k: Optional[int] = None,
        k_chunks: int = 3,
        agentic: bool = False,
        timeline: bool = False,
        hyde: bool = False,
        graph: bool = False,
        wiki: bool = False,
        summary: bool = False,
        verify: bool = False,
        intent: bool = False,
    ) -> str:
        """组装供 LLM 作答的 hybrid 读上下文（AGENTS.md 「架构概览与代码导航」）：live 且带日期戳的 fact（经冲突
        解决的当前状态）+ top-k 原始 session chunk（细节抽取会丢掉的部分）。每一行都带日期戳，
        正是时间类与知识更新类问题可被回答的关键。

        agentic=True 把单次 chunk 检索换成 LLM 分解的迭代检索（Bet B）。"""
        user = user_id

        # HyDE：用 LLM 写的假想答案扩展查询，以提升检索召回（M2c）。
        search_query = query
        if hyde and self.llm is not None:
            hypo = self.llm.complete(
                f"Write a brief, plausible hypothetical answer (1-2 sentences) to this question, to aid "
                f"retrieval:\n{query}",
                system="You write a short plausible answer. Be concise; no preamble.",
            )
            if hypo.strip():
                search_query = f"{query}\n{hypo.strip()}"

        ranked, _ = self.retriever.retrieve(search_query, user, as_of, top_k)
        # 最新在前：对知识更新类问题，LLM 应在顶部看到最新 fact（如新工作、新城市）——并信任
        # 它甚于列表下方更旧的 fact。
        ranked_by_date = sorted(ranked, key=lambda x: x[0].valid_at, reverse=True)
        ranked_facts = [f for f, _ in ranked_by_date]
        fact_lines = [f"- [{fmt_date(f.valid_at)}] {f.text}" for f in ranked_facts]
        facts_block = "\n".join(fact_lines) or "(none)"

        chunk_block = ""
        episodes: list[Episode] = []
        if k_chunks:
            if agentic and self.llm is not None:
                from .retrieve.agentic import AgenticRetriever

                episodes = AgenticRetriever(self, self.llm).gather_episodes(query, user, k_chunks, as_of=as_of)
            else:
                episodes = self.retrieve_episodes(search_query, user, k_chunks, as_of=as_of)
            if self.config.provenance_chunk_promotion:
                promoted = self._provenance_detail_chunks(ranked_facts, search_query, user, as_of, k_chunks)
                if promoted:
                    episodes = self._merge_promoted_chunks(promoted, episodes, k_chunks)
            parts = []
            for ep in episodes:
                date = ep.metadata.get("date") or fmt_date(ep.event_time)
                parts.append(f"[{date}]\n{ep.content}")
            chunk_block = "\n\n".join(parts)

        result = (
            f"FACTS (current, with dates):\n{facts_block}\n\n"
            f"RELEVANT CONVERSATIONS (with dates):\n{chunk_block}"
        ).strip()
        if self.config.chain_evidence:
            evo = self._fact_evolution_block(ranked_facts, user, as_of, query=search_query)
            if evo:
                result += f"\n\n{evo}"
        if self.config.provenance_evidence:
            raw_prov = self._provenance_raw_block(
                ranked_facts,
                search_query,
                user,
                as_of,
                exclude_episode_ids={ep.id for ep in episodes},
            )
            if raw_prov:
                result += f"\n\n{raw_prov}"
        if timeline:
            # 相关 fact 的显式时间顺序——有助于 "first/after/how long"（M2b）
            ordered = sorted((f for f, _ in ranked), key=lambda f: f.valid_at)
            tl = "\n".join(f"- [{fmt_date(f.valid_at)}] {f.text}" for f in ordered) or "(none)"
            result = f"TIMELINE (oldest to newest):\n{tl}\n\n" + result
        if graph:
            # L2：从查询的锚实体出发遍历实体图，跨 session 拉取相连的 fact
            # （multi-hop / multi-session）。
            related = self._graph_related_facts(search_query, user, as_of)
            if related:
                block = "\n".join(f"- [{fmt_date(f.valid_at)}] {f.text}" for f in related)
                result += f"\n\nRELATED FACTS (graph traversal):\n{block}"
        if wiki:
            # L4：LLM 精编的逐实体笔记（区分当前与过去），查询时合成。
            notes = self._entity_notes(search_query, user, as_of)
            if notes:
                result = "ENTITY NOTES:\n" + "\n".join(f"- {n}" for n in notes) + "\n\n" + result
        if verify and self.llm is not None:
            # 自校验：起草答案，找出最有用的单个缺口，检索之，追加证据。
            extra = self._self_verify(query, result, user, as_of)
            if extra:
                result += f"\n\nADDITIONAL EVIDENCE (self-verify):\n{extra}"
        if summary and self.llm is not None:
            # L5：把相关材料合成为简短忠实的摘要，置于最前。
            syn = self.llm.complete(
                f"Synthesize, in 2-3 faithful sentences, the facts relevant to: {query}\n\n{result}",
                system="You write a concise, strictly faithful synthesis of the given context.",
            )
            if syn.strip():
                result = f"SUMMARY:\n{syn.strip()}\n\n" + result
        if intent and self.llm is not None:
            # L6：前瞻性意图提示。如实说明：不预期对 QA benchmark 有帮助；为完整性和消融实验
            # 而保留。
            hint = self.llm.complete(
                f"In one short phrase, what is the user likely really trying to find out with: {query}",
                system="Reply with a short phrase only.",
            )
            if hint.strip():
                result = f"LIKELY INTENT: {hint.strip()}\n\n" + result
        return result

    def _self_verify(self, query: str, context: str, user: str, as_of: Optional[float]) -> str:
        draft = self.llm.complete(
            f"Using only this context, answer concisely. If something is missing, say what.\n\n{context}\n\nQ: {query}",
            system="Answer from context; note any missing piece.",
        )
        gap = self.llm.complete(
            f"Question: {query}\nDraft answer: {draft}\nWhat ONE short search query would best fill a gap or "
            f"verify this? Reply with the query, or 'none'.",
            system="Reply with one short search query, or exactly 'none'.",
        )
        g = gap.strip().strip(".").lower()
        if not g or g == "none":
            return ""
        more = self.retrieve_episodes(gap.strip(), user, 2, as_of=as_of)
        return "\n\n".join(f"[{ep.metadata.get('date', '?')}]\n{ep.content}" for ep in more)

    def _graph_related_facts(self, query: str, user: str, as_of: Optional[float], limit: int = 8) -> list[Fact]:
        if not self.config.graph_proximity:
            return []
        live = [f for f in self._all_facts() if f.user_id == user and f.is_live(as_of)]
        if not live:
            return []
        graph_scores, _ = self.retriever._graph_scores(query, user, live, as_of)
        related = [f for f in live if graph_scores.get(f.id, 0.0) > 0.0]
        related.sort(key=lambda f: (-graph_scores.get(f.id, 0.0), -f.valid_at, f.text.lower()))
        return related[:limit]

    def _entity_notes(self, query: str, user: str, as_of: Optional[float], max_entities: int = 3) -> list[str]:
        if self.llm is None:
            return []
        notes: list[str] = []
        for eid in list(self.retriever.query_entity_ids(query, user))[:max_entities]:
            ent = self.graph.entities.get(eid)
            if ent is None:
                continue
            facts = [
                f for f in self._all_facts()
                if f.user_id == user and f.subject.lower() == ent.name.lower()
            ]
            if not facts:
                continue
            lines = "\n".join(
                f"[{fmt_date(f.valid_at)}] {f.text}" + ("" if f.is_live(as_of) else " (past)")
                for f in sorted(facts, key=lambda x: x.valid_at)
            )
            note = self.llm.complete(
                f"Summarize what is known about {ent.name} in 2-3 sentences. Note current vs outdated "
                f"facts.\n{lines}",
                system="You write a concise, accurate entity note that resolves current vs past facts.",
            )
            if note.strip():
                notes.append(f"{ent.name}: {note.strip()}")
        return notes

    def history(self, subject: str, predicate: str, user_id: str = "default") -> list[Fact]:
        user = user_id
        return history(self._all_facts(), user, subject, predicate)

    def profile(self, user_id: str = "default") -> dict[str, str]:
        return self.engine.profile(user_id)

    # --- 内部实现 ---
    def _should_abstain(self, query: str, facts: list[Fact], diag: dict) -> bool:
        """当查询的*属性*不在记忆中时拒答——关键在于，仅匹配实体名是不够的（只知道 Gina 在哪
        工作，却问 "Gina's favorite food"）。我们要求 predicate+object 有词法重叠，或语义相似度
        足够强。针对 LongMemEval 的拒答（false-premise 类别）。"""

        def attribute_text(f: Fact) -> str:
            return f.predicate.replace("_", " ") + " " + f.object

        for f in facts:
            if overlap_terms(query, attribute_text(f)) - _GENERIC_ATTR_TERMS:
                return False  # 命中了非泛化的属性词 -> 答案在记忆中
        best_sem = max(diag.get("sem", {}).values(), default=0.0)
        return best_sem < self.config.abstain_threshold

    def _summary_fallback(
        self,
        query: str,
        user_id: str,
        as_of: Optional[float] = None,
    ) -> Optional[SearchResult]:
        """#3b：当原子化 fact 无法作答时，若最相关的 session 摘要与查询确实重叠，则浮出该摘要
        （信息可能只存在于摘要中——某个 how-to、规则、安装命令——extractor 从未把它蒸馏成
        fact）。保守策略：要求非泛化的词法重叠，绝不把模糊相似的摘要当作答案返回。"""
        if not self.config.summary_fallback:
            return None
        candidates: list[tuple[int, int, float, Episode, str]] = []
        k = max(1, int(getattr(self.config, "summary_fallback_k", 6) or 6))
        for ep in self.retrieve_summaries(query, user_id, k=k, as_of=as_of):
            text = (ep.summary or ep.content or "").strip()
            if not text:
                continue
            exact = overlap_terms(query, text) - _GENERIC_ATTR_TERMS
            if exact:
                candidates.append((len(exact), len(overlap_terms(query, text)), ep.event_time, ep, text))
        if candidates:
            _, _, _, ep, text = max(candidates, key=lambda row: (row[0], row[1], row[2]))
            dated = f"[{ep.metadata.get('date') or fmt_date(ep.event_time)}] (session: {ep.session_id}) {text}"
            return SearchResult(query=query, via="summary", _answer=dated)
        return None

    def _procedural_fallback(
        self,
        query: str,
        user: str,
        as_of: Optional[float] = None,
    ) -> Optional[SearchResult]:
        """针对长期指令/how-to fact 的类型化回退。

        当程序性 fact 的 predicate 过于泛化（"procedure"、"rule"）而只有 object 携带有用步骤时，
        hybrid 检索可能拒答。在回退到自由文本摘要之前，先从带来源的程序性视图作答，使持久规则
        保持类型化、可审计。
        """
        rows = self._procedural_candidates(query, user, as_of, limit=1)
        if not rows:
            return None
        f = rows[0]
        reinforce(f, self.config.access_boost)
        source = self._procedural_source_label(f)
        pred = f.predicate.replace("_", " ")
        answer = f"[{fmt_date(f.valid_at)}] ({source}) {f.subject} {pred}: {f.object}"
        return SearchResult(query=query, facts=[f], scores=[1.0], via="procedural", _answer=answer)
