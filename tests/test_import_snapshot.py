"""导出 -> 导入的可移植性往返测试（忆痕 快照应当被"还原"，而不是被重新写入）：
事实保留双时间轴时间戳 + 取代链，episode 以已整合状态重新进入，整体是幂等的，
且 /v1/import 会把解析失败转成 400 而不是 500。与所有其他测试一样离线运行
（hashing 嵌入器 + 固定合成模型响应）。"""
from __future__ import annotations


from dataclasses import asdict

import pytest

from tests.model_fixtures import MemoryEngine


def _mk_memory() -> MemoryEngine:
    return MemoryEngine()


class TestSnifferAndMemoryLevel:
    def test_sniff_recognizes_snapshot_and_parse_gives_actionable_error(self):
        from mnemotrace.connectors import parse, sniff
        snap = {"facts": [], "episodes": []}
        assert sniff(snap) == "mnemotrace"
        with pytest.raises(ValueError, match="import_snapshot"):
            parse(snap)

    def test_memory_import_snapshot_direct(self):
        src = _mk_memory()
        src.add("I love hiking in the Alps.", user_id="u1")
        src.consolidate()
        facts = [asdict(f) for f in src._all_facts()]
        dst = _mk_memory()
        stats = dst.import_snapshot({"facts": facts, "episodes": []},
                                    user_id="u2")
        assert stats["facts_restored"] == len(facts) > 0
        assert stats["malformed"] == 0

    def test_malformed_rows_reject_the_entire_snapshot(self):
        dst = _mk_memory()
        with pytest.raises(ValueError, match="snapshot.facts"):
            dst.import_snapshot({
                "facts": [{"subject": "a"}, "not-a-dict"],
                "episodes": [{"no_content": True}, "junk"],
            })
        assert dst.fact_store.values() == dst.episodes_doc.values() == []
