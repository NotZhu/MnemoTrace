from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_durable_persistence_contract_matches_committed_prefix_semantics():
    contract = (ROOT / "specs/001-durable-persistence/contracts/on-disk-format.md").read_text(
        encoding="utf-8"
    )
    assert "manifest 声明的已提交前缀" in contract
    assert "前缀**内部**缺失记录或 JSON 格式错误，抛出 `StoreFormatError`" in contract
    assert "manifest 数量**之后**" in contract
    assert "未提交的撕裂尾部并忽略" in contract


def test_durable_persistence_quickstart_documents_both_crash_edges():
    quickstart = (ROOT / "specs/001-durable-persistence/quickstart.md").read_text(encoding="utf-8")
    assert "manifest 数量之后的撕裂字节只会导致未提交尾部被丢弃" in quickstart
    assert "manifest 已提交前缀内部缺失记录或格式错误时，抛出 `StoreFormatError`" in quickstart


def test_durable_persistence_docs_cover_embedder_id_mismatch():
    contract = (ROOT / "specs/001-durable-persistence/contracts/on-disk-format.md").read_text(
        encoding="utf-8"
    )
    model = (ROOT / "specs/001-durable-persistence/data-model.md").read_text(encoding="utf-8")
    quickstart = (ROOT / "specs/001-durable-persistence/quickstart.md").read_text(encoding="utf-8")
    assert "EmbedderMismatchError" in contract
    assert "错误向量空间的近邻" in contract
    assert "维度相同但模型不同的嵌入" in model
    assert "嵌入维度 / 模型 id 不匹配时明确报错" in quickstart
