"""Generated landing pages retain per-deployment content and local assets."""
import json
import re
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
builder = runpy.run_path(str(ROOT / "scripts/build_landing_pages.py"))


def test_committed_landing_pages_match_shared_source():
    for path, expected in builder["build_pages"]().items():
        assert path.read_text(encoding="utf-8") == expected
        assert path.with_name("assets").is_dir()
        for source in re.findall(r'(?:src|href)="(assets/[^"?#]+)', expected):
            assert (path.parent / source).is_file(), source


def test_page_variants_keep_their_content_and_share_benchmark_evidence():
    pages = {path.parent.name: html for path, html in builder["build_pages"]().items()}
    assert "<title>MnemoTrace · 忆痕</title>" in pages["demo"]
    assert "给 AI 一个会记事的大脑" in pages["docs"]
    assert "margin-left:auto" in pages["demo"]
    for html in pages.values():
        assert "84.4%" in html and "78.8%" in html
        assert "run500_v3_main_deepseekjudge.jsonl" in html
        assert "<!-- landing:" not in html


@pytest.mark.parametrize("slots", [{}, {"title": "ok", "extra": "bad"}, {"title": 1}])
def test_renderer_rejects_missing_extra_or_nontext_slots(slots):
    with pytest.raises(ValueError):
        builder["render_page"]("<!DOCTYPE html>\n<!-- landing:title -->\n", slots)


def test_check_detects_stale_output_without_overwriting(monkeypatch, tmp_path):
    template = tmp_path / "landing"
    template.mkdir()
    (template / "index.html.in").write_text("<!DOCTYPE html>\n<!-- landing:title -->\n", encoding="utf-8")
    (template / "pages.json").write_text(json.dumps({"demo": {"title": "demo"}, "docs": {"title": "docs"}}), encoding="utf-8")
    for page in ("demo", "docs"):
        (tmp_path / page).mkdir()
        (tmp_path / page / "index.html").write_text("stale", encoding="utf-8")
    main = builder["main"]
    monkeypatch.setitem(main.__globals__, "ROOT", tmp_path)
    monkeypatch.setitem(main.__globals__, "build_pages", lambda: builder["build_pages"](tmp_path))
    monkeypatch.setitem(main.__globals__, "build_assets", lambda root: {})
    assert main(["--check"]) == 1
    assert (tmp_path / "demo/index.html").read_text(encoding="utf-8") == "stale"
    assert main([]) == 0
    assert main(["--check"]) == 0


def test_shared_assets_are_checked_without_overwriting(tmp_path, monkeypatch):
    main = builder["main"]
    source = tmp_path / "demo/assets/shared.bin"
    copy = tmp_path / "docs/assets/shared.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source\x00\xff")
    copy.parent.mkdir(parents=True)
    copy.write_bytes(b"stale")
    monkeypatch.setitem(main.__globals__, "ROOT", tmp_path)
    monkeypatch.setitem(main.__globals__, "build_pages", lambda: {})
    asset_builder = builder["build_assets"]
    monkeypatch.setitem(asset_builder.__globals__, "SHARED_ASSETS", ("shared.bin",))
    monkeypatch.setitem(main.__globals__, "build_assets", asset_builder)
    assert main(["--check"]) == 1
    assert copy.read_bytes() == b"stale"
    assert main([]) == 0
    assert copy.read_bytes() == source.read_bytes()
    assert main(["--check"]) == 0


def test_check_accepts_git_crlf_html_without_rewriting(tmp_path, monkeypatch):
    main = builder["main"]
    page = tmp_path / "demo/index.html"
    page.parent.mkdir(parents=True)
    checked_out = b"<html>\r\n</html>\r\n"
    page.write_bytes(checked_out)
    monkeypatch.setitem(main.__globals__, "ROOT", tmp_path)
    monkeypatch.setitem(main.__globals__, "build_pages", lambda: {page: "<html>\n</html>\n"})
    monkeypatch.setitem(main.__globals__, "build_assets", lambda root: {})
    assert main(["--check"]) == 0
    assert page.read_bytes() == checked_out
