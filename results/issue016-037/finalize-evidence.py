"""Validate evidence links and capture recoverable source without staging files."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
metadata_path = OUT / "metadata.json"
base_commit = (json.loads(metadata_path.read_text(encoding="utf-8"))["base_commit"]
               if metadata_path.exists() else None)


def git(*args, allowed=(0, 1)):
    completed = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    if completed.returncode not in allowed:
        raise RuntimeError((completed.stdout + completed.stderr).decode("utf-8", "replace"))
    return completed


docs = [ROOT / "docs" / name for name in (
    "known-issues.zh-CN.md", "mnemotrace-full-architecture-report.zh-CN.md", "architecture-optimization-map.zh-CN.md"
)] + [OUT / "README.md", ROOT / "results/console-validation-index.md"] + list((ROOT / "specs/007-console-error-lifecycle").glob("*.md"))
missing = []
for doc in docs:
    for target in re.findall(r"\]\(([^)]+)\)", doc.read_text(encoding="utf-8")):
        if "://" in target or target.startswith(("#", "mailto:")):
            continue
        relative = target.split("#", 1)[0].strip("<>")
        if relative and not (doc.parent / relative).exists():
            missing.append({"document": doc.relative_to(ROOT).as_posix(), "target": target})
assert all(item["document"] == "results/issue016-037/README.md" and item["target"] in ("metadata.json", "implementation.patch") for item in missing), missing
ledger = docs[0].read_text(encoding="utf-8")
resolved = ledger.index('<a id="resolved-issues">')
for number in ("016", "037"):
    anchor = f'<a id="issue-{number}"></a>'
    assert ledger.count(anchor) == 1
    assert ledger.index(anchor) > resolved
    section = ledger[ledger.index(anchor) + len(anchor):].split('<a id="', 1)[0]
    assert "已修复" in section
for heading, expected in (("### P2：", 18), ("### 已修复（", 9)):
    section = ledger[ledger.index(heading):].split("\n### ", 1)[0]
    assert len(re.findall(r"^\| \[ISSUE-", section, re.MULTILINE)) == expected

files = git("ls-files", "--others", "--exclude-standard").stdout.decode("utf-8").splitlines()
base_commit = base_commit or git("rev-parse", "HEAD").stdout.decode().strip()
changed = git("diff", "--name-only", "--diff-filter=ACM", base_commit).stdout.decode("utf-8").splitlines()
numbered_refs = []
for name in sorted(set(changed + files)):
    if name.startswith("results/") or name == "docs/known-issues.zh-CN.md":
        continue
    source = (ROOT / name).read_text(encoding="utf-8")
    if re.search(r"issue[-_ ]?\d{3}", source, re.IGNORECASE):
        numbered_refs.append(name)
assert not numbered_refs, numbered_refs
sources = [name for name in sorted(set(changed + files))
           if name.endswith((".md", ".cjs", ".mjs", ".py", ".tsx", ".ts", ".json"))
           and (not name.startswith("results/") or name.endswith((".md", ".cjs", ".mjs", ".py")))]
added = [name for name in sources if name in files]
tracked = [name for name in sources if name not in files]
# 空白上下文也是补丁文件的行末空白；零上下文保留可恢复性，同时排除证据自身以免递归嵌入。
patch = git("diff", "--binary", "--unified=0", base_commit, "--", *tracked).stdout if tracked else b""
for name in added:
    patch += git("diff", "--no-index", "--binary", "--unified=0", "--", "/dev/null", name).stdout
(OUT / "implementation.patch").write_bytes(patch)
metadata = {
    "date": datetime.now(timezone.utc).isoformat(),
    "base_commit": base_commit,
    "working_tree_dirty": True,
    "source_patch": "implementation.patch",
    "source_patch_apply": "git apply --unidiff-zero results/issue016-037/implementation.patch at base_commit",
    "patch_sha256": hashlib.sha256(patch).hexdigest(),
    "source_patch_scope": "all tracked modifications and added textual test/spec/evidence source; logs and screenshots supplied separately",
    "screenshots": {p.name: {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                    for p in sorted((OUT / "screenshots").glob("*.jpg"))},
    "evidence_only": True,
}
assert len(metadata["screenshots"]) == 8
(OUT / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
assert all((OUT / item["target"]).exists() for item in missing)
check = git("diff", "--check", base_commit, allowed=(0, 1, 2, 3))
assert check.returncode == 0, check.stdout.decode("utf-8", "replace")
# 普通 git diff 看不到未跟踪文件；逐项检查待加入的证据和源码，不更改暂存区。
for name in files:
    check = git("diff", "--no-index", "--check", "--", "/dev/null", name, allowed=(0, 1, 2, 3))
    # no-index 即使格式正确，也会以 1 表示两份文件不同；--check 的诊断输出才表示格式错误。
    assert check.returncode in (0, 1) and not check.stdout, check.stdout.decode("utf-8", "replace")
record = {"label": "documentation_and_diff_check", "date": metadata["date"], "local_links": "passed", "numbered_issue_refs_outside_evidence": "none", "ledger_counts": {"P2": 18, "resolved": 9}, "anchors": "unique and resolved", "git_diff_check": 0, "untracked_files_checked": len(files), "screenshots": len(metadata["screenshots"]), "patch_sha256": metadata["patch_sha256"]}
with (OUT / "validation.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
print(json.dumps(record, ensure_ascii=False))
