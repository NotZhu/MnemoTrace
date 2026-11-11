"""Generated clients must agree with the Python classification and profile schema."""
from pathlib import Path
import runpy

from mnemotrace.profile import SECTIONS, SYSTEM

ROOT = Path(__file__).resolve().parents[1]
builder = runpy.run_path(str(ROOT / "scripts/build_memory_contract.py"))


def test_generated_memory_contracts_are_current():
    assert builder["main"](["--check"]) == 0


def test_check_reports_missing_and_stale_files_without_writing(tmp_path, monkeypatch, capsys):
    main = builder["main"]
    monkeypatch.setitem(main.__globals__, "ROOT", tmp_path)
    target = tmp_path / builder["TARGETS"][0]
    target.parent.mkdir(parents=True)
    target.write_text("stale", encoding="utf-8")
    assert main(["--check"]) == 1
    output = capsys.readouterr().out
    assert all(path in output for path in builder["TARGETS"])
    assert target.read_text(encoding="utf-8") == "stale"
    assert not (tmp_path / builder["TARGETS"][1]).exists()
    assert main([]) == 0
    assert main(["--check"]) == 0

    monkeypatch.setitem(main.__globals__, "CATEGORIES", (*builder["CATEGORIES"], "synthetic_category"))
    monkeypatch.setitem(main.__globals__, "SECTIONS", (*SECTIONS, "synthetic_section"))
    assert main(["--check"]) == 1
    assert "synthetic_category" not in target.read_text(encoding="utf-8")
    assert main([]) == 0
    assert main(["--check"]) == 0
    assert '"synthetic_category"' in target.read_text(encoding="utf-8")
    assert '"synthetic_section"' in target.read_text(encoding="utf-8")


def test_check_accepts_crlf_without_rewriting(tmp_path, monkeypatch):
    main = builder["main"]
    monkeypatch.setitem(main.__globals__, "ROOT", tmp_path)
    checked_out = builder["render"]().replace("\n", "\r\n").encode("utf-8")
    for target in builder["TARGETS"]:
        path = tmp_path / target
        path.parent.mkdir(parents=True)
        path.write_bytes(checked_out)
    assert main(["--check"]) == 0
    assert all((tmp_path / target).read_bytes() == checked_out for target in builder["TARGETS"])


def test_profile_prompt_uses_the_validated_section_names():
    declared = SYSTEM.split("array fields:\n", 1)[1].split(".\n", 1)[0]
    assert tuple(declared.split(", ")) == SECTIONS
