"""Offline regressions for document navigation and artifact ownership."""
from __future__ import annotations

from pathlib import Path
import runpy

import pytest


ROOT = Path(__file__).resolve().parents[1]
checker = runpy.run_path(str(ROOT / "scripts/check_repository.py"))


def document(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def test_links_support_reference_forms_html_and_unicode_anchors(tmp_path):
    source = document(tmp_path, "docs/README.md", """# Docs
[inline](guide.md#中文-api)
[reference][guide]
[guide][]
[guide]
<a href="guide.md#explicit">section</a>
![image](<my image.svg>)
[guide]: guide.md#中文-api-1
""")
    document(tmp_path, "docs/guide.md", "# 中文 `API`\n# 中文 API\n<a id=\"explicit\"></a>\n")
    document(tmp_path, "docs/my image.svg", "<svg/>")
    assert checker["check_links"](tmp_path, [source]) == []


def test_checker_detects_missing_target_anchor_and_reference(tmp_path):
    source = document(tmp_path, "README.md", "[missing](missing.md)\n[anchor](guide.md#absent)\n[unknown][ref]\n")
    document(tmp_path, "guide.md", "# Present\n")
    errors = checker["check_links"](tmp_path, [source])
    assert any("missing target: missing.md" in error for error in errors)
    assert any("missing anchor: guide.md#absent" in error for error in errors)
    assert any("undefined link reference: ref" in error for error in errors)


def test_examples_and_historical_urls_are_not_current_paths(tmp_path):
    source = document(tmp_path, "README.md", """# Docs
```markdown
[example](missing.md)
```
~~~text
[example][undefined]
~~~
`[literal](missing.md)`
[history](https://github.com/example/repo/blob/commit/old.md)
""")
    assert checker["check_links"](tmp_path, [source]) == []


def test_percent_encoded_fragment_and_directory_link(tmp_path):
    source = document(tmp_path, "README.md", "[guide](docs/guide.md#%E4%B8%AD%E6%96%87)\n[dir](docs/)\n")
    document(tmp_path, "docs/guide.md", "# 中文\n")
    assert checker["check_links"](tmp_path, [source]) == []


def test_link_cannot_escape_repository(tmp_path):
    source = document(tmp_path, "README.md", "[outside](../outside.md)\n")
    assert "link escapes repository" in checker["check_links"](tmp_path, [source])[0]


def fixture_repository(root):
    files = []
    for name in sorted(checker["REQUIRED"]):
        files.append(document(root, name, "# Entry\n"))
    nav = root / "docs/README.md"
    nav.write_text("# Docs\n" + "\n".join(
        f"[entry]({p.relative_to(root / 'docs').as_posix()})"
        for p in files if p.is_relative_to(root / "docs") and p != nav
    ), encoding="utf-8")
    return files


def test_navigation_is_the_document_registry(tmp_path):
    files = fixture_repository(tmp_path)
    assert checker["check_structure"](tmp_path, files) == []
    orphan = document(tmp_path, "docs/orphan.md", "# Orphan\n")
    errors = checker["check_structure"](tmp_path, files + [orphan])
    assert errors == ["document absent from navigation: docs/orphan.md"]


@pytest.mark.parametrize("path,diagnostic", [
    ("REPORT.md", "unowned root document"),
    (".artifacts/report.md", "local artifact must not be tracked"),
])
def test_artifact_boundaries(tmp_path, path, diagnostic):
    files = fixture_repository(tmp_path)
    extra = document(tmp_path, path, "content\n")
    assert any(diagnostic in error for error in checker["check_structure"](tmp_path, files + [extra]))


def test_documentation_check_needs_no_historical_artifacts(tmp_path):
    files = fixture_repository(tmp_path)
    assert checker["check_structure"](tmp_path, files) == []
    for name in ("RESULTS.md", "results", "paper", ".specify", "specs"):
        assert not (tmp_path / name).exists()
    assert checker["main"](["--root", str(tmp_path)]) == 0


def test_repository_check_is_read_only_and_passes():
    assert checker["main"]([]) == 0
