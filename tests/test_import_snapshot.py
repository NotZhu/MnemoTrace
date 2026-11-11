"""导出 -> 导入的可移植性往返测试（忆痕 快照应当被"还原"，而不是被重新写入）：
事实保留双时间轴时间戳 + 取代链，episode 以已整合状态重新进入，整体是幂等的，
且 /v1/import 会把解析失败转成 400 而不是 500。与所有其他测试一样离线运行
（测试嵌入器 + 固定合成模型响应）。"""
from __future__ import annotations


from dataclasses import asdict

import pytest

from tests.model_fixtures import MemoryEngine


def _mk_memory() -> MemoryEngine:
    return MemoryEngine()


class TestSnifferAndMemoryLevel:
    def test_sniff_recognizes_snapshot_and_parse_gives_actionable_error(self):
        from mnemotrace.connectors import parse, sniff
        snap = {"format_version": 3, "facts": [], "episodes": []}
        assert sniff(snap) == "mnemotrace"
        with pytest.raises(ValueError, match="import_snapshot"):
            parse(snap)

    def test_memory_import_snapshot_direct(self):
        src = _mk_memory()
        src.add("I love hiking in the Alps.", user_id="u1")
        src.consolidate()
        facts = [asdict(f) for f in src._all_facts()]
        dst = _mk_memory()
        stats = dst.import_snapshot({"format_version": 3, "facts": facts, "episodes": []},
                                    user_id="u2")
        assert stats["facts_restored"] == len(facts) > 0
        assert stats["malformed"] == 0

    def test_v2_graph_is_rebuilt_from_fact_types(self):
        src = _mk_memory()
        src.add_fact("user", "works_at", "Synthetic Lab", object_kind="entity", text='user works at Synthetic Lab')
        src.add_fact("user", "note", "Synthetic literal", object_kind="literal", text='user note Synthetic literal')
        snap = {
            "format_version": 3, "facts": [asdict(f) for f in src._all_facts()], "episodes": [],
            "graph": {"nodes": [{"name": "Stale alias"}], "edges": []},
        }
        dst = _mk_memory()
        dst.import_snapshot(snap)
        dst.import_snapshot(snap)
        graph = dst.graph_data()
        assert {node["name"] for node in graph["nodes"]} == {"user", "Synthetic Lab"}
        assert len(graph["edges"]) == 1
        assert len(dst._all_facts()) == 2

    def test_malformed_rows_reject_the_entire_snapshot(self):
        dst = _mk_memory()
        with pytest.raises(ValueError, match="snapshot.facts"):
            dst.import_snapshot({
                "format_version": 3,
                "facts": [{"subject": "a"}, "not-a-dict"],
                "episodes": [{"no_content": True}, "junk"],
            })
        assert dst.fact_store.values() == dst.episodes_doc.values() == []



def test_reimport_preserves_target_edit_and_explicit_copy_restores_source():
    source = _mk_memory()
    fact = source.add_fact("user", "project_code", "Ab1", valid_at=10.01, text='user project code Ab1')
    fact.text = "The synthetic code is Ab1."
    snapshot = {"format_version": 3, "facts": [asdict(fact)], "episodes": []}
    target = _mk_memory()
    assert target.import_snapshot(snapshot)["facts_restored"] == 1
    restored, = target._all_facts()
    assert restored.text == fact.text and restored.valid_at == 10.01
    target.update_fact(restored.id, object="Ab2", subject=restored.subject, predicate=restored.predicate, text=' '.join((restored.subject, restored.predicate.replace('_', ' '), 'Ab2')))
    assert target.import_snapshot(snapshot)["facts_skipped"] == 1
    assert target.fact_store.get(restored.id).object == "Ab2"
    assert target.import_snapshot(snapshot, dedupe=False)["facts_restored"] == 1
    assert {fact.object for fact in target._all_facts()} == {"Ab1", "Ab2"}
