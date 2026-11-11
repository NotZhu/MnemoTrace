"""统一评测台（AGENTS.md Bet D）—— 公平、不作弊的 memory 系统对比方式。

每个被测系统都是一个黑盒，只暴露一个方法：context(item) -> str（摄取该问题的各会话，返回用于作答的
context）。固定的 answerer + 固定的 judge + 固定的 embedder/extractor 以完全相同的方式作用于每个系统，
因此唯一的变量就是 memory 层本身。我们在这里亲自跑竞品（Mem0、Zep），而不是引用它们在其他 harness 上
自报的数字。

    python eval/bench.py --data s --limit 40 --systems engram,full_context,rag \
        --answerer univibe:gemini-2.5-flash --judge univibe:gpt-5.5 --extractor deepseek

标准配置（默认）：answerer=gemini-2.5-flash, judge=gpt-5.5, extractor(内部)=deepseek,
embedder=bge-small。可以改，但必须对所有系统一视同仁。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

# 关闭 chromadb/posthog 遥测 —— 它会尝试外联上报，在代理环境下失败并卡死整个运行。
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("POSTHOG_DISABLED", "1")
# BGE embedder 的 HF tokenizer 会被工作线程调用；HuggingFace tokenizers 警告 fork 之后使用可能死锁，
# 并会因此关闭自身并行。这里显式设置，避免长时并行运行被 fork/死锁路径中途杀掉（曾观察到 exit 144）。
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemotrace import Config, MemoryEngine  # noqa: E402
from mnemotrace.llm.providers import load_dotenv, make_embedder, make_llm, make_reranker  # noqa: E402
from eval.longmemeval import (  # noqa: E402
    ANSWER_SYSTEM,
    ANSWER_TEMPLATE,
    FC_CHAR_BUDGET,
    REASONING_SYSTEM,
    all_text,
    answer_self_consistency,
    answer_two_stage_pref,
    build_persona,
    build_session_map,
    extract_answer,
    ingest,
    is_abstention,
    judge_correct,
    load_data,
    looks_like_abstention,
    rank_raw_sessions,
    needs_self_consistency,
    needs_two_stage_pref,
    sessions_of,
)
def _maybe_pinned(llm, cache_dir: str, model_tag: str):
    """给出 --pin-extraction 时固定（缓存）extractor 的回复。

    只包装 extractor。固定 answerer 或 judge 会掩盖被评测对象的真实方差；固定 extractor 则是消除
    被控变量自身的方差。
    """
    if not cache_dir:
        return llm
    from mnemotrace.llm.cache import CachedLLM

    print(f"  extraction PINNED -> {cache_dir} (same prompt => same facts across runs)")
    return CachedLLM(llm, cache_dir, model_tag=model_tag)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[idx]


@dataclass
class Rig:
    embedder: object
    extractor_llm: object
    answerer_llm: object
    judge_llm: object
    reranker: object = None
    topk: int = 10
    chunks: int = 5
    reasoning: bool = False
    strategies: bool = False
    sc_on: bool = True
    sc_k: int = 5
    persona: bool = False
    session_map: bool = False
    n_digests: int = 12  # engram_lean: 取多少条会话摘要进入 lean context
    agentic: bool = False
    timeline: bool = False
    hyde: bool = False
    graph: bool = False
    wiki: bool = False
    summary: bool = False
    verify: bool = False
    verify_retry: bool = False  # engram_lean: 遇到 "I don't know" 时扩大切片并重试一次
    intent: bool = False
    ablations: tuple[str, ...] = ()


_ABLATION_FLAGS = {
    "digest_fallback": ("digest_fallback", "digest", "derived_digest"),
    "chain_evidence": ("chain",),
    "graph_proximity": ("graph", "graph_proximity"),
    "graph_relation_awareness": ("graph_relation", "relation_graph", "graph_relation_awareness"),
    "graph_path_reinforcement": ("graph_reinforcement", "path_reinforcement", "graph_path_reinforcement"),
}
_KNOWN_ABLATIONS = frozenset(flag for flags in _ABLATION_FLAGS.values() for flag in flags)


def mnemotrace_config(ablations: tuple[str, ...] = ()) -> Config:
    """供评测台内消融实验使用的 Config 辅助函数。

    A/B 系统之间只能差在指定的 memory 干预点上，不能有隐藏的配置漂移。这些开关默认是 MnemoTrace 的
    正常行为，让统一 harness 可以一次只关掉一个算法。
    """
    disabled = set(ablations)
    unknown = disabled - _KNOWN_ABLATIONS
    if unknown:
        raise ValueError("Unknown ablation option(s): " + ", ".join(sorted(unknown))
                         + ". See --ablate in --help for supported options.")
    return Config(**{name: disabled.isdisjoint(flags) for name, flags in _ABLATION_FLAGS.items()})


# ---------------- 系统适配器（各自实现 context(item) -> str） ----------------
class MnemoTraceSystem:
    name = "engram"

    def __init__(self, rig: Rig):
        self.rig = rig

    def context(self, item: dict) -> str:
        rig, qid, q = self.rig, item["question_id"], item["question"]
        mem = MemoryEngine(
            config=mnemotrace_config(ablations=rig.ablations),
            embedder=rig.embedder,
            llm=rig.extractor_llm,
            reranker=rig.reranker,
        )
        ingest(mem, item, qid)
        mem.consolidate_full()
        return mem.context_for(q, user_id=qid, top_k=rig.topk, k_digests=rig.n_digests,
                               agentic=rig.agentic, timeline=rig.timeline, hyde=rig.hyde,
                               graph=rig.graph, wiki=rig.wiki,
                               summary=rig.summary, verify=rig.verify, intent=rig.intent)


class FullContextSystem:
    name = "full_context"

    def __init__(self, rig: Rig):
        self.rig = rig

    def context(self, item: dict) -> str:
        return all_text(item)[:FC_CHAR_BUDGET]


class RAGSystem:
    """纯检索基线：对原始会话做嵌入，返回 top-k chunk。没有抽取、没有 fact、没有双时间轴 ——
    用来精确隔离 MnemoTrace 整合层相对朴素 RAG 的增量。"""

    name = "rag"

    def __init__(self, rig: Rig):
        self.rig = rig

    def context(self, item: dict) -> str:
        rig = self.rig
        documents = rank_raw_sessions(item, rig.embedder, rig.chunks, reranker=rig.reranker, hybrid=True)
        return "\n\n".join(f"[{date}]\n{text}" for _, _, date, text in documents)



class Mem0System:
    """竞品：Mem0，配置与 MnemoTrace 相同的内部 LLM（deepseek）和 embedder，保证对比公平。懒加载；
    未安装 mem0 时评测台会跳过它。"""

    name = "mem0"

    def __init__(self, rig: Rig):
        self.rig = rig
        from mem0 import Memory as Mem0Memory

        self._Mem0 = Mem0Memory
        # Mem0 的内部抽取 LLM 指向可用的 ARK（火山）账号（doubao-flash）：与 MnemoTrace 用的是同一个
        # 内部模型（保证对比公平），也是还有余额的账号。
        self._llm_key = os.environ.get("ARK_API_KEY")
        self._llm_base = os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
        os.environ.setdefault("OPENAI_API_KEY", self._llm_key or "")

    def context(self, item: dict) -> str:
        # 公平配置：与 MnemoTrace 相同的内部 LLM（doubao-flash）+ embedder（bge-small）。每个问题独立的
        # chroma 路径 = 隔离 + 线程安全（每个问题的 memory 相互独立，LongMemEval 要求如此）。
        qid, q = item["question_id"], item["question"]
        safe = qid.replace("/", "_")
        cfg = {
            "llm": {"provider": "openai", "config": {"model": "doubao-seed-1-6-flash-250615",
                    "openai_base_url": self._llm_base, "api_key": self._llm_key, "temperature": 0.0}},
            "embedder": {"provider": "huggingface", "config": {"model": "BAAI/bge-small-en-v1.5"}},
            "vector_store": {"provider": "chroma", "config": {"collection_name": "mem0_lme", "path": f"data/mem0c/{safe}"}},
            # SQLite history 也按问题隔离 —— 否则并行 worker 下共享默认库会锁住。
            "history_db_path": f"data/mem0c/{safe}/history.db",
        }
        m = self._Mem0.from_config(cfg)
        for sid, turns in sessions_of(item):
            msgs = [{"role": t.get("role", "user"), "content": t.get("content", "")} for t in turns if t.get("content")]
            if msgs:
                m.add(messages=msgs, user_id=qid)
        hits = m.search(query=q, user_id=qid, limit=self.rig.topk)
        results = hits.get("results", hits) if isinstance(hits, dict) else hits
        return "FACTS:\n" + "\n".join(f"- {r.get('memory', r)}" for r in results)


class ZepSystem:
    """竞品：Zep / Graphiti —— 有时间感知（双时间轴）的知识图谱 memory，与 MnemoTrace 设计最接近。懒加载；
    需要 `pip install graphiti-core`、运行中的 Neo4j/FalkorDB（NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD）、
    以及给 Graphiti 自己的抽取 LLM 用的 OpenAI 兼容 key（OPENAI_API_KEY [+ OPENAI_BASE_URL]）。按问题
    使用 `group_id` 隔离每个 LongMemEval 问题。

    注意：按公开的 graphiti_core API 编写（async；签名随版本变化）。这是脚手架代码，只在第一次带
    key、连着真实图数据库的运行里验证过，未在离线 CI 环境验证。"""

    name = "zep"

    def __init__(self, rig: Rig):
        self.rig = rig
        import graphiti_core  # noqa: F401  (--systems 包含 zep 时才校验)

        self._Graphiti = graphiti_core.Graphiti

    def context(self, item: dict) -> str:
        import asyncio
        from datetime import datetime, timedelta, timezone

        qid, q = item["question_id"], item["question"]

        async def run() -> str:
            g = self._Graphiti(
                os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
                os.environ.get("NEO4J_USER", "neo4j"),
                os.environ.get("NEO4J_PASSWORD", "password"),
            )
            try:
                await g.build_indices_and_constraints()
                base = datetime.now(timezone.utc)
                for i, (sid, turns) in enumerate(sessions_of(item)):
                    body = "\n".join(
                        f"{t.get('role', 'user')}: {t.get('content', '')}" for t in turns if t.get("content")
                    )
                    if not body:
                        continue
                    await g.add_episode(
                        name=f"{qid}-{sid}",
                        episode_body=body,
                        source_description="longmemeval session",
                        reference_time=base + timedelta(seconds=i),
                        group_id=qid,
                    )
                results = await g.search(q, group_id=qid, num_results=self.rig.topk)
                return "FACTS:\n" + "\n".join(f"- {getattr(r, 'fact', r)}" for r in results)
            finally:
                await g.close()

        return asyncio.run(run())


class HippoRAGSystem:
    """竞品：HippoRAG（知识图谱 + Personalized PageRank 多跳检索）。懒加载；需要 `pip install hipporag`
    以及给其 OpenIE + 嵌入用的 OpenAI 兼容 key（LLM 名称用 HIPPORAG_LLM 设置）。按问题使用 `save_dir`
    隔离每个问题的索引。

    注意：按公开的 hipporag 2.x API 编写（index/retrieve 签名随版本变化）；脚手架只在第一次带 key 的
    运行里验证过，未在离线 CI 环境验证。"""

    name = "hipporag"

    def __init__(self, rig: Rig):
        self.rig = rig
        from hipporag import HippoRAG  # noqa: F401  (--systems 包含 hipporag 时才校验)

        self._HippoRAG = HippoRAG

    def context(self, item: dict) -> str:
        qid, q = item["question_id"], item["question"]
        safe = qid.replace("/", "_")
        hr = self._HippoRAG(
            save_dir=f"data/hipporag/{safe}",
            llm_model_name=os.environ.get("HIPPORAG_LLM", "gpt-4o-mini"),
            embedding_model_name="BAAI/bge-small-en-v1.5",
        )
        docs = [t.get("content", "") for _, turns in sessions_of(item) for t in turns if t.get("content")]
        hr.index(docs=docs)
        res = hr.retrieve(queries=[q], num_to_retrieve=self.rig.topk)[0]
        passages = getattr(res, "docs", res)
        return "PASSAGES:\n" + "\n".join(f"- {p}" for p in passages)


class MnemoTraceFullSystem:
    """把 MnemoTrace 抽取/消解过冲突的 fact 作为"记忆索引"前缀，拼接在完整对话历史之前。结合 100% 会话
    召回（full-context）与 MnemoTrace 的双时间轴结构（fact 在前，最新的排最前）。如果它达到 OMEGA 级别的
    分数，就证明 memory 层在 full-context 之上还有增量价值。最强单系统运行用 --systems engram_full。"""

    name = "engram_full"

    def __init__(self, rig: Rig):
        self.rig = rig

    def context(self, item: dict) -> str:
        rig, qid = self.rig, item["question_id"]
        mem = MemoryEngine(embedder=rig.embedder, llm=rig.extractor_llm, reranker=rig.reranker)
        ingest(mem, item, qid)
        mem.consolidate_full()
        # 组装：抽取出的 fact（最新在前）作为索引，完整历史附在下面
        from mnemotrace.util import fmt_date
        facts = [f for f in mem.fact_store.values() if f.user_id == qid and f.is_live()]
        facts.sort(key=lambda f: f.valid_at, reverse=True)
        fact_lines = "\n".join(f"- [{fmt_date(f.valid_at)}] {f.text}" for f in facts) or "(none extracted)"
        full_hist = all_text(item)[:FC_CHAR_BUDGET]

        # L2/L3 抽象层（用便宜的 extractor LLM 在完整历史上一次性构建）。
        blocks = []
        if rig.persona:  # L3: 用户画像 —— 为偏好/推荐类回答提供依据
            persona = build_persona(rig.extractor_llm, full_hist)
            if persona:
                blocks.append(f"USER PROFILE (preferences, habits, possessions):\n{persona}")
        blocks.append(f"MEMORY INDEX (extracted facts, most recent first):\n{fact_lines}")
        if rig.session_map:  # L2: 逐会话完整摘要 —— 辅助多会话聚合
            smap = build_session_map(rig.extractor_llm, full_hist)
            if smap:
                blocks.append(f"SESSION MAP (every conversation, chronological):\n{smap}")
        blocks.append(f"FULL CONVERSATION HISTORY:\n{full_hist}")
        return "\n\n".join(blocks)


class MnemoTraceLeanSystem:
    """可扩展的 memory 系统（AGENTS.md Bet A/E）：整合时构建 L1 facts + L2 会话摘要 + L3 用户画像，
    然后从精简（lean）的检索切片作答 —— 而不是完整历史。这才是真正的赢面：用一小部分 token 在准确率
    上击败 full-context，从而在历史远超模型窗口时依然可用。与 engram_full 不同，原始 haystack 永远
    不进入 prompt，进入的只是一小段有组织的、检索出来的 context。"""

    name = "engram_lean"
    ablations: tuple[str, ...] = ()

    def __init__(self, rig: Rig):
        self.rig = rig
        self._tl = threading.local()  # 每线程缓存 1 个 item 的 mem，让 verify 重试复用已构建的 mem

    def _mem_for(self, item: dict):
        rig, qid = self.rig, item["question_id"]
        if getattr(self._tl, "qid", None) == qid and getattr(self._tl, "mem", None) is not None:
            return self._tl.mem  # verify 重试时复用（不再重新摄取/重新摘要）
        mem = MemoryEngine(
            config=mnemotrace_config(
                ablations=tuple(self.ablations) + tuple(rig.ablations),
            ),
            embedder=rig.embedder,
            llm=rig.extractor_llm,
            reranker=rig.reranker,
        )
        ingest(mem, item, qid)
        mem.consolidate_full()
        self._tl.mem, self._tl.qid = mem, qid
        return mem

    def context(self, item: dict, expand: int = 0) -> str:
        rig, qid, q = self.rig, item["question_id"], item["question"]
        mem = self._mem_for(item)
        # expand>0（验证重试）：加宽切片 —— 更多完整细节会话、时间线、多跳分解 —— 把第一轮 lean 切片
        # 漏掉的证据找出来（针对 "I don't know" 的修复）。
        return mem.lean_context(
            q, user_id=qid, n_digests=rig.n_digests + expand * 5, n_facts=rig.topk,
            agentic=rig.agentic or expand > 0,
            timeline=rig.timeline or expand > 0,
        )


class MnemoTraceLeanNoChainSystem(MnemoTraceLeanSystem):
    """A/B 基线：lean 系统，但不含检索到的 supersedes/演化链上下文。"""

    name = "engram_lean_no_chain"
    ablations = ("chain",)


class MnemoTraceLeanNoDigestFallbackSystem(MnemoTraceLeanSystem):
    """A/B 基线：lean 系统，但搜索落空时没有派生摘要回退。"""

    name = "engram_lean_no_digest_fallback"
    ablations = ("digest_fallback",)


class MnemoTraceLeanNoGraphSystem(MnemoTraceLeanSystem):
    """A/B 基线：lean 系统，但关闭 n-hop 图邻近打分/遍历。"""

    name = "engram_lean_no_graph"
    ablations = ("graph",)


class MnemoTraceLeanNoGraphRelationSystem(MnemoTraceLeanSystem):
    """A/B 基线：lean 系统，但图邻近内部不做按查询条件的关系加权。"""

    name = "engram_lean_no_graph_relation"
    ablations = ("graph_relation",)


class MnemoTraceLeanNoGraphReinforcementSystem(MnemoTraceLeanSystem):
    """A/B 基线：lean 系统，但关闭多路径的图支持强化。"""

    name = "engram_lean_no_graph_reinforcement"
    ablations = ("graph_reinforcement",)


class MnemoTraceLeanCoreSystem(MnemoTraceLeanSystem):
    """A/B 基线：lean 系统，同时关闭最新的证据增强项。"""

    name = "engram_lean_core"
    ablations = ("chain", "graph")


SYSTEMS = {"engram": MnemoTraceSystem, "full_context": FullContextSystem, "rag": RAGSystem,
           "mem0": Mem0System, "zep": ZepSystem, "hipporag": HippoRAGSystem,
           "engram_full": MnemoTraceFullSystem, "engram_lean": MnemoTraceLeanSystem,
           "engram_lean_no_chain": MnemoTraceLeanNoChainSystem,
           "engram_lean_no_digest_fallback": MnemoTraceLeanNoDigestFallbackSystem,
           "engram_lean_no_graph": MnemoTraceLeanNoGraphSystem,
           "engram_lean_no_graph_relation": MnemoTraceLeanNoGraphRelationSystem,
           "engram_lean_no_graph_reinforcement": MnemoTraceLeanNoGraphReinforcementSystem,
           "engram_lean_core": MnemoTraceLeanCoreSystem}


def failed_qids(path: str, system: str, limit: int = 0) -> set[str]:
    """某次历史运行中 `system` 答错的 QID 集合。

    用于回归重放：用当前代码重跑那些确切的旧错题，既不人工挑选题目，也不让检索过程偷看 benchmark
    类别。
    """
    qids: list[str] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            res = row.get("sys", {}).get(system)
            if res and res.get("ok") is False:
                qids.append(row["qid"])
                if limit and len(qids) >= limit:
                    break
    return set(qids)


def eval_item(item, systems, rig):
    qid, cat, q = item["question_id"], item.get("question_type", "?"), item["question"]
    qdate = item.get("question_date", "")
    out = {"qid": qid, "cat": cat, "sys": {}}
    sys_prompt = REASONING_SYSTEM if rig.reasoning else ANSWER_SYSTEM
    for sysobj in systems:
        try:
            t0 = time.perf_counter()
            ctx = sysobj.context(item)
            # 策略路由（rig.strategies）：依据问题文本选择作答方式（可泛化；绝不偷看 benchmark 的类别
            # 标签）。计数/时长 -> 自一致性投票；推荐/偏好 -> 两阶段（先列出用户历史，再据此作答）。
            if rig.strategies and rig.sc_on and rig.reasoning and needs_self_consistency(q):
                prompt = ANSWER_TEMPLATE.format(qdate=qdate, context=ctx, question=q)
                pred_raw = answer_self_consistency(rig.answerer_llm, prompt, sys_prompt, k=rig.sc_k)
            elif rig.strategies and rig.reasoning and needs_two_stage_pref(q):
                pred_raw = answer_two_stage_pref(rig.answerer_llm, ctx, q, qdate, sys_prompt)
            else:
                pred_raw = rig.answerer_llm.complete(
                    ANSWER_TEMPLATE.format(qdate=qdate, context=ctx, question=q), system=sys_prompt
                )
            # reasoning 模式：judge 和拒答检查只作用于抽取出的最终 ANSWER 行，而非思维链文本，
            # 避免推理文字误触发拒答标记。
            pred = extract_answer(pred_raw) if rig.reasoning else pred_raw
            # 验证 / 重检索循环：首要失败模式是模型答 "I don't know"，因为证据落在第一轮 lean 切片之外。
            # 若出现拒答（且问题本身可答），就加宽切片（更多完整会话 + 时间线 + 多跳）并重试一次；
            # 只有重试答案不再是拒答时才采纳。
            if (rig.verify_retry and looks_like_abstention(pred) and not is_abstention(item)
                    and sysobj.name == "engram_lean"):
                ctx2 = sysobj.context(item, expand=1)
                raw2 = rig.answerer_llm.complete(
                    ANSWER_TEMPLATE.format(qdate=qdate, context=ctx2, question=q), system=sys_prompt)
                pred2 = extract_answer(raw2) if rig.reasoning else raw2
                if not looks_like_abstention(pred2):
                    pred, ctx = pred2, ctx2
            lat = (time.perf_counter() - t0) * 1000.0
            ok = judge_correct(item, pred, rig.judge_llm)
            # 保存（截断的）预测和金标准答案，跑完后无需重跑即可做错误分析
            out["sys"][sysobj.name] = {"ok": ok, "tok": len(ctx.split()), "lat": lat, "err": None,
                                       "pred": pred[:300], "gold": str(item.get("answer", ""))[:200]}
        except Exception as e:  # noqa: BLE001
            out["sys"][sysobj.name] = {"ok": None, "tok": 0, "lat": 0, "err": f"{type(e).__name__}: {str(e)[:80]}"}
    return out


def emit_item(item, systems, rig):
    """生成每个系统的假设答案（不打分 —— 打分是官方 harness 的职责）。"""
    q, qdate = item["question"], item.get("question_date", "")
    hyps = {}
    for sysobj in systems:
        try:
            ctx = sysobj.context(item)
            pred = rig.answerer_llm.complete(
                ANSWER_TEMPLATE.format(qdate=qdate, context=ctx, question=q), system=ANSWER_SYSTEM
            )
            hyps[sysobj.name] = pred.strip()
        except Exception as e:  # noqa: BLE001
            hyps[sysobj.name] = f"[ERROR {type(e).__name__}]"
    return {"qid": item["question_id"], "hyps": hyps}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="s")
    ap.add_argument("--qid", action="append", default=None,
                    help="run only the specified question_id; can be passed multiple times")
    ap.add_argument("--category", default=None,
                    help="filter to one question_type (e.g. knowledge-update) BEFORE --limit, to A/B a "
                         "category-specific change without paying for the whole set")
    ap.add_argument("--failures-from", default=None,
                    help="filter to QIDs that --failure-system got wrong in a previous bench JSONL")
    ap.add_argument("--failure-system", default="engram_lean",
                    help="system name to read from --failures-from (default: engram_lean)")
    ap.add_argument("--failure-limit", type=int, default=0,
                    help="max number of prior failures to replay (0 = all)")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--systems", default="engram,full_context,rag")
    ap.add_argument("--answerer", default="univibe:gemini-2.5-flash")
    ap.add_argument("--judge", default="univibe:gpt-5.5")
    ap.add_argument("--extractor", default="deepseek")
    ap.add_argument("--pin-extraction", default="", metavar="DIR",
                    help="cache extractor replies in DIR so two runs build the SAME facts. Required for "
                         "an honest read-path A/B: without it the extractor's own run-to-run variance "
                         "swamps the code change being measured (see mnemotrace/llm/cache.py).")
    ap.add_argument("--embedder", default="bge-small")
    ap.add_argument("--reranker", default="none",
                    help="none | bge-reranker | bge-reranker-large | bge-reranker-v2 (cross-encoder over chunk pool)")
    ap.add_argument("--topk", type=int, default=10)
    ap.add_argument("--chunks", type=int, default=5, help="Raw session count for the RAG baseline")
    ap.add_argument("--reasoning", action="store_true",
                    help="answer with explicit EVIDENCE+REASON+ANSWER chain-of-thought (Phase 1: lifts "
                         "multi-evidence categories — temporal, multi-session, knowledge-update — which "
                         "fail because single-shot answering can't aggregate)")
    ap.add_argument("--strategies", action="store_true",
                    help="route by question text: self-consistency vote on counting/duration questions, "
                         "two-stage (surface user history -> answer) on preference questions (needs --reasoning)")
    ap.add_argument("--sc-k", type=int, default=5, dest="sc_k", help="self-consistency vote count (default 5)")
    ap.add_argument("--no-self-consistency", action="store_false", dest="sc_on",
                    help="with --strategies: keep two-stage preference but DISABLE self-consistency voting")
    ap.add_argument("--persona", action="store_true",
                    help="L3 user-profile (preferences/habits) — prepended in engram_full")
    ap.add_argument("--session-map", action="store_true", dest="session_map",
                    help="engram_full: insert an L2 complete session-by-session digest (multi-session aggregation)")
    ap.add_argument("--n-digests", type=int, default=12, dest="n_digests",
                    help="engram_lean: number of session digests to pull into the lean context (default 12)")
    ap.add_argument("--agentic", action="store_true", help="engram: LLM-decomposed iterative retrieval (M2a)")
    ap.add_argument("--timeline", action="store_true", help="engram: prepend a chronological timeline (M2b)")
    ap.add_argument("--hyde", action="store_true", help="engram: HyDE query expansion for recall (M2c)")
    ap.add_argument("--graph", action="store_true", help="engram: entity-graph traversal (L2)")
    ap.add_argument("--wiki", action="store_true", help="engram: LLM-curated entity notes (L4)")
    ap.add_argument("--summary", action="store_true", help="engram: L5 synthesis summary")
    ap.add_argument("--verify", action="store_true", help="engram: self-verify -> re-retrieve gap")
    ap.add_argument("--verify-retry", action="store_true", dest="verify_retry",
                    help="engram_lean: on an 'I don't know', widen the retrieved slice + timeline and retry once")
    ap.add_argument("--intent", action="store_true", help="engram: L6 intent hint (benchmark-neutral)")
    ap.add_argument("--ablate", default="",
                    help="comma-separated MnemoTrace algorithm switches to disable for all MnemoTrace systems in this run: "
                         "digest_fallback, chain, "
                         "graph/graph_proximity, graph_relation, "
                         "graph_reinforcement")
    ap.add_argument("--full", action="store_true",
                    help="engram: turn ON ALL differentiators (agentic+timeline+hyde+graph+wiki+summary+verify+intent)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--answerer-timeout", type=int, default=90, dest="answerer_timeout",
                    help="per-call timeout (s) for the answerer. Raise to ~180 for gemini-2.5-pro on the "
                         "124k-token _S full context — pro is slow and 90s times out under load.")
    ap.add_argument("--out", default=None)
    ap.add_argument("--shuffle", action="store_true",
                    help="deterministically shuffle items (seeded) so any partial run is category-balanced")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--resume", action="store_true",
                    help="skip items already present in --out and append (survives a killed run; relaunch to continue)")
    ap.add_argument("--emit", default=None,
                    help="emit {question_id,hypothesis} JSONL per system (to <emit>.<system>.jsonl) for the "
                         "OFFICIAL LongMemEval evaluate_qa.py judge — instead of self-judging here")
    args = ap.parse_args()
    ablations = tuple(flag.strip() for flag in args.ablate.split(",") if flag.strip())
    try:
        mnemotrace_config(ablations)
    except ValueError as exc:
        ap.error(str(exc))
    if args.full:  # 一个开关打开全部 engram 差异化特性
        args.agentic = args.timeline = args.hyde = args.graph = args.wiki = True
        args.summary = args.verify = args.intent = True

    load_dotenv()
    items = load_data(args.data)
    if args.qid:
        wanted = set(args.qid)
        items = [it for it in items if it["question_id"] in wanted]
        print(f"  QID filter: {len(items)} of {len(wanted)} requested items")
    if args.category:
        items = [it for it in items if it.get("question_type") == args.category]
        print(f"  CATEGORY filter: {len(items)} '{args.category}' items")
    if args.failures_from:
        qids = failed_qids(args.failures_from, args.failure_system, args.failure_limit)
        n_before = len(items)
        items = [it for it in items if it["question_id"] in qids]
        print(f"  FAILURE replay: {len(items)} of {n_before} items from {args.failures_from} "
              f"where {args.failure_system} was wrong")
    if args.limit and args.limit < len(items):
        stride = max(1, len(items) // args.limit)
        items = items[::stride][: args.limit]
    if args.shuffle:
        import random

        random.Random(args.seed).shuffle(items)  # 确定性：部分运行也保持类别均衡
    # resume：跳过 --out 中已打分的 item，被杀掉的运行可重启续跑。
    done_qids = set()
    if args.out and args.resume and os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            for line in fh:
                try:
                    done_qids.add(json.loads(line)["qid"])
                except Exception:  # noqa: BLE001
                    pass
        n_before = len(items)
        items = [it for it in items if it["question_id"] not in done_qids]
        print(f"  RESUME: {len(done_qids)} already done; {len(items)} of {n_before} remaining")

    # reasoning 模式会生成 EVIDENCE+REASON+ANSWER；推理型骨干模型（doubao-seed、deepseek-r1）还会输出
    # 思考轨迹，所以留足余量，避免在 ANSWER 行之前被截断。
    ans_max_tok = 1500 if args.reasoning else 256
    rig = Rig(
        embedder=make_embedder(args.embedder),
        extractor_llm=_maybe_pinned(
            make_llm(args.extractor, max_tokens=512, num_retries=3, timeout=60),
            args.pin_extraction, args.extractor),
        answerer_llm=make_llm(args.answerer, max_tokens=ans_max_tok, num_retries=3, timeout=args.answerer_timeout),
        judge_llm=make_llm(args.judge, max_tokens=8, num_retries=3, timeout=60),
        reranker=make_reranker(args.reranker),
        topk=args.topk, chunks=args.chunks,
        reasoning=args.reasoning, strategies=args.strategies, sc_on=args.sc_on, sc_k=args.sc_k,
        persona=args.persona, session_map=args.session_map,
        n_digests=args.n_digests,
        agentic=args.agentic, timeline=args.timeline, hyde=args.hyde, graph=args.graph, wiki=args.wiki,
        summary=args.summary, verify=args.verify, verify_retry=args.verify_retry, intent=args.intent,
        ablations=ablations,
    )

    systems = []
    for name in args.systems.split(","):
        name = name.strip()
        if not name:
            continue
        try:
            systems.append(SYSTEMS[name](rig))
        except Exception as e:  # noqa: BLE001
            print(f"  (skipping system '{name}': {type(e).__name__}: {str(e)[:80]})")
    sys_names = [s.name for s in systems]

    print(f"UNIFIED RIG | {len(items)} items from _{args.data} | systems={sys_names}")
    print(f"  answerer={args.answerer}  judge={args.judge}  extractor={args.extractor}  embedder={args.embedder}")
    print(f"  topk={args.topk} chunks={args.chunks} n_digests={args.n_digests} workers={args.workers}\n")
    if rig.ablations:
        print(f"  global ablations={list(rig.ablations)}\n")

    if args.emit:
        files = {s: open(f"{args.emit}.{s}.jsonl", "w", encoding="utf-8") for s in sys_names}
        lock = threading.Lock()
        done = {"n": 0}

        def handle_emit(r):
            with lock:
                done["n"] += 1
                for name, hyp in r["hyps"].items():
                    files[name].write(json.dumps({"question_id": r["qid"], "hypothesis": hyp}, ensure_ascii=False) + "\n")
                    files[name].flush()
                if done["n"] % 20 == 0 or done["n"] <= 3:
                    print(f"  emitted {done['n']}/{len(items)}", flush=True)

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for fut in as_completed([ex.submit(emit_item, it, systems, rig) for it in items]):
                handle_emit(fut.result())
        for f in files.values():
            f.close()
        print("\nhypotheses written:")
        for s in sys_names:
            print(f"  {s}: {args.emit}.{s}.jsonl")
        print("\nNow score with the OFFICIAL judge, e.g.:")
        print("  OPENAI_API_KEY=$UNIVIBE_API_KEY OPENAI_BASE_URL=https://api.univibe.cc/openai/v1 \\")
        print(f"  python external/LongMemEval/src/evaluation/evaluate_qa.py gpt-5.5 {args.emit}.{sys_names[0]}.jsonl <ref.json>")
        return

    hits = {s: defaultdict(list) for s in sys_names}
    toks = {s: [] for s in sys_names}
    lats = {s: [] for s in sys_names}
    errs = {s: 0 for s in sys_names}
    lock = threading.Lock()
    outfh = open(args.out, "a" if args.resume else "w", encoding="utf-8") if args.out else None
    done = {"n": 0}

    def handle(r):
        with lock:
            done["n"] += 1
            for name, res in r["sys"].items():
                if res["err"]:
                    errs[name] += 1
                elif res["ok"] is not None:
                    hits[name][r["cat"]].append(res["ok"])
                    toks[name].append(res["tok"])
                    lats[name].append(res["lat"])
            if done["n"] <= 3 or done["n"] % 20 == 0:
                snap = {n: f"{100*sum(v for vs in hits[n].values() for v in vs)/max(1,sum(len(v) for v in hits[n].values())):.0f}%" for n in sys_names}
                print(f"  [{done['n']}/{len(items)}] {snap}", flush=True)
            if outfh:
                outfh.write(json.dumps(r, ensure_ascii=False) + "\n")
                outfh.flush()

    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for fut in as_completed([ex.submit(eval_item, it, systems, rig) for it in items]):
                handle(fut.result())
    else:
        for it in items:
            handle(eval_item(it, systems, rig))
    if outfh:
        outfh.close()

    cats = sorted({c for n in sys_names for c in hits[n]})
    print("\n=== ACCURACY BY SYSTEM (same rig, same items, same answerer+judge) ===")
    header = "  " + "category".ljust(26) + "".join(n.ljust(16) for n in sys_names)
    print(header)
    for c in cats:
        row = "  " + c.ljust(26)
        for n in sys_names:
            v = hits[n][c]
            row += (f"{100*sum(v)/len(v):.1f}% ({len(v)})").ljust(16) if v else "-".ljust(16)
        print(row)
    print("  " + "-" * (26 + 16 * len(sys_names)))
    row = "  " + "OVERALL".ljust(26)
    for n in sys_names:
        flat = [v for vs in hits[n].values() for v in vs]
        row += (f"{100*sum(flat)/len(flat):.1f}%" if flat else "-").ljust(16)
    print(row)
    row = "  " + "avg context tokens".ljust(26)
    for n in sys_names:
        row += (f"{sum(toks[n])/len(toks[n]):.0f}" if toks[n] else "-").ljust(16)
    print(row)
    row = "  " + "p50 latency ms".ljust(26)
    for n in sys_names:
        row += (f"{percentile(lats[n], 50):.0f}" if lats[n] else "-").ljust(16)
    print(row)
    row = "  " + "p95 latency ms".ljust(26)
    for n in sys_names:
        row += (f"{percentile(lats[n], 95):.0f}" if lats[n] else "-").ljust(16)
    print(row)
    if any(errs.values()):
        print("  errors:", {n: errs[n] for n in sys_names if errs[n]})


if __name__ == "__main__":
    main()
