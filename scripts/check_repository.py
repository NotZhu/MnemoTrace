"""Check current documentation links, navigation and artifact boundaries offline."""
from __future__ import annotations

import argparse
from collections import Counter, deque
from html import unescape
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
ROOT_DOCS = {
    "AGENTS.md", "CHANGELOG.md", "CLAUDE.md", "COMMERCIAL-LICENSE.md",
    "CONTRIBUTING.md", "README.md", "README.zh-CN.md", "RESULTS.md", "SECURITY.md",
}
REQUIRED = {
    "docs/README.md", "docs/quickstart.md", "docs/guides/agents.md",
    "docs/reference/python-api.md", "docs/reference/http-api.md",
    "docs/reference/mcp.md", "docs/reference/configuration.md",
    "docs/architecture/README.md", "docs/architecture/memory.md",
    "docs/architecture/retrieval.md", "docs/architecture/service.md",
    "docs/architecture/console.md", "docs/decisions.md", "docs/issues.md",
    "results/README.md", "results/experiments/README.md", "results/engineering/README.md",
}
SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__", "dist", "build",
    ".artifacts", ".cleanup-archive", ".mnemotrace", ".pytest_cache", ".ruff_cache", "data",
}
LEGACY_DOCS = {
    "docs/mnemotrace-full-architecture-report.zh-CN.md",
    "docs/architecture-optimization-map.zh-CN.md", "docs/known-issues.zh-CN.md",
    "docs/readpath-improvement-plan.zh-CN.md", "docs/temporal-granularity-plan.zh-CN.md",
    "docs/commercial-release-0.1.0.zh-CN.md", "docs/algorithm-architecture.md",
    "docs/agent-adapters.md", "docs/cross-agent-memory.md", "API.md",
}
INLINE_LINK = re.compile(r'''!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s)]+)(?:\s+["'][^\n]*?["'])?\s*\)''')
REF_DEFINITION = re.compile(r'''^ {0,3}\[([^\]\n]+)\]:\s*(<[^>\n]+>|\S+)(?:\s+["'].*)?$''', re.M)
REF_LINK = re.compile(r"!?\[([^\]\n]+)\]\[([^\]\n]*)\]")


class HTMLReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if value is None:
                continue
            if name == "id" or (tag == "a" and name == "name"):
                self.ids.add(value)
            if (tag == "a" and name == "href") or (tag in {"img", "script"} and name == "src"):
                self.links.append(value)


def prose(text: str) -> str:
    """Ignore fenced examples and inline code before interpreting Markdown links."""
    lines = []
    fence: tuple[str, int] | None = None
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            chars, rest = marker.groups()
            if fence is None:
                fence = (chars[0], len(chars))
            elif chars[0] == fence[0] and len(chars) >= fence[1] and not rest.strip():
                fence = None
            lines.append("")
        else:
            lines.append(line if fence is None else "")
    return re.sub(r"(`+).*?\1", "", re.sub(r"<!--.*?-->", "", "\n".join(lines), flags=re.S))


def label(value: str) -> str:
    return " ".join(value.split()).casefold()


def references(text: str) -> list[str]:
    content = prose(text)
    definitions = {label(m[1]): m[2].strip("<>") for m in REF_DEFINITION.finditer(content)}
    body = REF_DEFINITION.sub("", content)
    links = [m[1].strip("<>") for m in INLINE_LINK.finditer(body)]
    links.extend(definitions[label(m[2] or m[1])] for m in REF_LINK.finditer(body)
                 if label(m[2] or m[1]) in definitions)
    links.extend(definitions[label(m[1])] for m in re.finditer(r"!?\[([^\]\n]+)\]", body)
                 if label(m[1]) in definitions)
    parser = HTMLReferences()
    parser.feed(body)
    return links + parser.links


def anchors(text: str) -> set[str]:
    # Heading code is visible in GitHub anchors, even though its links are not parsed.
    content = prose(re.sub(r"`([^`\n]+)`", r"\1", text))
    parser = HTMLReferences()
    parser.feed(content)
    found = set(parser.ids)
    counts: Counter[str] = Counter()
    for match in re.finditer(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", content, re.M):
        title = re.sub(r"<[^>]+>", "", unescape(match[1])).lower()
        title = re.sub(r"!?\[([^]]+)\]\([^)]*\)", r"\1", title)
        slug = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "-")
        count = counts[slug]
        counts[slug] += 1
        found.add(slug + (f"-{count}" if count else ""))
    return found


def repository_files(root: Path) -> list[Path]:
    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root, capture_output=True, check=True,
        )
        return sorted({root / p for p in result.stdout.decode("utf-8").split("\0")
                       if p and (root / p).is_file()})
    files = []
    for parent, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        files.extend(Path(parent) / name for name in names if not name.endswith((".log", ".tmp")))
    return sorted(files)


def local_target(root: Path, source: Path, reference: str) -> tuple[Path, str] | None:
    parsed = urlsplit(unescape(reference))
    if parsed.scheme or parsed.netloc:
        return None
    path = unquote(parsed.path)
    if any(token in path for token in ("<", ">", "{", "}", "*")):
        return None
    return ((source.parent / path) if path else source).resolve(), unquote(parsed.fragment)


def check_links(root: Path, documents: list[Path]) -> list[str]:
    errors = []
    cached: dict[Path, set[str]] = {}
    for document in documents:
        if not document.resolve().is_relative_to(root.resolve()):
            errors.append(f"{document.relative_to(root).as_posix()}: document escapes repository")
            continue
        text = document.read_text(encoding="utf-8")
        content = prose(text)
        definitions = {label(m[1]) for m in REF_DEFINITION.finditer(content)}
        for match in REF_LINK.finditer(REF_DEFINITION.sub("", content)):
            if label(match[2] or match[1]) not in definitions:
                errors.append(f"{document.relative_to(root).as_posix()}: undefined link reference: {match[2] or match[1]}")
        for ref in references(text):
            resolved = local_target(root, document, ref)
            if resolved is None:
                continue
            target, fragment = resolved
            prefix = document.relative_to(root).as_posix()
            if not target.is_relative_to(root.resolve()):
                errors.append(f"{prefix}: link escapes repository: {ref}")
            elif not target.exists():
                errors.append(f"{prefix}: missing target: {ref}")
            elif fragment and target.suffix == ".md":
                if target not in cached:
                    cached[target] = anchors(target.read_text(encoding="utf-8"))
                if fragment not in cached[target]:
                    errors.append(f"{prefix}: missing anchor: {ref}")
    return sorted(set(errors))


def check_structure(root: Path, files: list[Path]) -> list[str]:
    paths = {p.relative_to(root).as_posix() for p in files}
    errors = [f"missing required entry: {path}" for path in sorted(REQUIRED - paths)]
    for path in sorted(paths):
        p = Path(path)
        if len(p.parts) == 1 and p.suffix == ".md" and path not in ROOT_DOCS:
            errors.append(f"unowned root document: {path}")
        if path.startswith(".artifacts/") or path == ".specify/feature.json":
            errors.append(f"local artifact must not be tracked: {path}")
        if path.startswith("results/") and len(p.parts) == 2 and p.name != "README.md":
            errors.append(f"uncategorized result artifact: {path}")
        if path.startswith("specs/") and (len(p.parts) < 3 or not re.match(r"\d{8}-\d{6}-", p.parts[1])):
            errors.append(f"major specification needs timestamp directory: {path}")
    agents = root / "AGENTS.md"
    if agents.exists() and "<!-- SPECKIT START -->" in agents.read_text(encoding="utf-8"):
        errors.append("AGENTS.md: remove generated current-plan context")
    if any(path.startswith(".specify/extensions/agent-context/") for path in paths):
        errors.append("remove automatic agent-context writeback extension")
    documents = [p for p in files if p.suffix == ".md" and not p.relative_to(root).as_posix().startswith((".specify/", "paper/"))]
    for document in documents:
        if not document.resolve().is_relative_to(root.resolve()):
            errors.append(f"{document.relative_to(root).as_posix()}: document escapes repository")
            continue
        content = re.sub(r"https?://[^\s)<>]+", "", prose(document.read_text(encoding="utf-8")))
        for old in LEGACY_DOCS:
            if old in content:
                errors.append(f"{document.relative_to(root).as_posix()}: superseded documentation reference: {old}")
    # Navigation itself is the manifest; a second registry would duplicate ownership.
    start = (root / "docs/README.md").resolve()
    reachable = {start}
    pending = deque([start])
    while pending:
        current = pending.popleft()
        if not current.is_file():
            continue
        for ref in references(current.read_text(encoding="utf-8")):
            resolved = local_target(root, current, ref)
            if resolved is None:
                continue
            target, _ = resolved
            if target.is_relative_to((root / "docs").resolve()) and target.suffix == ".md" and target not in reachable:
                reachable.add(target)
                pending.append(target)
    errors.extend(f"document absent from navigation: {p.relative_to(root).as_posix()}"
                  for p in documents if p.is_relative_to(root / "docs") and p.resolve() not in reachable)
    return sorted(set(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (also used by synthetic regressions)")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    files = repository_files(root)
    documents = [p for p in files if p.suffix == ".md" and not p.is_relative_to(root / ".specify/templates")]
    errors = check_structure(root, files) + check_links(root, documents)
    for error in sorted(set(errors)):
        print(f"FAIL {error}")
    if errors:
        return 1
    print(f"OK repository documentation ({len(documents)} Markdown files; links, anchors and navigation)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
