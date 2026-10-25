"""Current public copy follows the project wording rules."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_public_copy_avoids_unmeasured_scale_or_latency_claims():
    forbidden = (
        "cost stays flat",
        "sub-second",
        "holds up as history grows",
        "历史再长也不崩",
        "<50ms",
        "&lt;50ms",
        "<100ms",
        "&lt;100ms",
        "under 50ms",
        "~50ms",
    )
    for rel in ("README.md", "README.zh-CN.md", "docs/index.html", "demo/index.html"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        lowered = text.lower()
        for phrase in forbidden:
            assert phrase.lower() not in lowered, f"{phrase!r} found in {rel}"


def test_public_copy_avoids_competitor_leaderboard_positioning():
    forbidden = (
        "OMEGA",
        "Mem0-2026",
        "Hunyuan",
        "SOTA",
        "Public LongMemEval_S SOTA",
        "WE BEAT THIS",
        "对标腾讯",
    )
    public_surfaces = (
        "README.md",
        "README.zh-CN.md",
        "docs/index.html",
        "demo/index.html",
        "docs/guides/usage.md",
        "eval/report.py",
    )
    for rel in public_surfaces:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for phrase in forbidden:
            assert phrase not in text, f"{phrase!r} found in {rel}"
