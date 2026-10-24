"""在 benchmark JSONL 日志成为公开发布证据之前进行校验。

这是 ``eval/report.py`` 的失败导向孪生兄弟。``report.py`` 概括一次运行里有什么；本模块回答的
是"一份日志是否完整到可以引用"。刻意保持零依赖，贡献者在任何有 benchmark 日志的地方都能跑。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable


BENCH_REQUIRED_RESULT_KEYS = {"ok", "tok", "lat", "err"}
PERSONAMEM_REQUIRED_RESULT_KEYS = {"ok", "tok", "lat", "pick"}
SCHEMAS = {"bench", "personamem"}


def _load_rows(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [], [f"{path}: cannot read file: {exc}"]
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{path}:{lineno}: invalid JSON: {exc}")
            continue
        if not isinstance(row, dict):
            errors.append(f"{path}:{lineno}: row must be a JSON object")
            continue
        rows.append(row)
    return rows, errors


def validate_bench_log(
    path: str | Path,
    *,
    expected_rows: int | None = None,
    require_complete: bool = False,
    required_systems: Iterable[str] | None = None,
    schema: str = "bench",
) -> list[str]:
    """返回一份 ``bench.py`` JSONL 日志的校验错误。

    ``require_complete`` 是发布/DONE 证据的标准。默认情况下，发现的每个 system 在每一行都必须
    有已打分、无错误的结果，且 prediction 和 gold 都在。只引用多 system 日志中指定 system 时，
    传 ``required_systems``。
    """
    if schema not in SCHEMAS:
        return [f"{path}: unknown schema {schema!r}; expected one of {sorted(SCHEMAS)}"]
    path = Path(path)
    rows, errors = _load_rows(path)
    if errors:
        return errors
    if not rows:
        return [f"{path}: no rows"]
    if expected_rows is not None and len(rows) != expected_rows:
        errors.append(f"{path}: expected {expected_rows} rows, got {len(rows)}")

    qids: list[str] = []
    systems: set[str] = set()
    for idx, row in enumerate(rows, start=1):
        prefix = f"{path}:{idx}"
        qid = row.get("qid")
        if not isinstance(qid, str) or not qid:
            errors.append(f"{prefix}: missing qid")
        else:
            qids.append(qid)
        if schema == "bench":
            cat = row.get("cat")
            if not isinstance(cat, str) or not cat:
                errors.append(f"{prefix}: missing cat")
        else:
            pref_type = row.get("pref_type")
            if not isinstance(pref_type, str) or not pref_type:
                errors.append(f"{prefix}: missing pref_type")
        sys_obj = row.get("sys")
        if not isinstance(sys_obj, dict) or not sys_obj:
            errors.append(f"{prefix}: missing sys")
            continue
        for system, result in sys_obj.items():
            if not isinstance(system, str) or not system:
                errors.append(f"{prefix}: empty system name")
                continue
            systems.add(system)
            _validate_result(prefix, system, result, require_complete, schema, errors)

    if len(qids) != len(set(qids)):
        errors.append(f"{path}: duplicate qids")
    if require_complete:
        systems_to_check = set(required_systems) if required_systems is not None else systems
        if not systems_to_check:
            errors.append(f"{path}: no systems")
        for system in sorted(systems_to_check):
            if system not in systems:
                errors.append(f"{path}:{system}: missing from log")
                continue
            scored = 0
            errored = 0
            missing = 0
            for row in rows:
                result = row.get("sys", {}).get(system) if isinstance(row.get("sys"), dict) else None
                if result is None:
                    missing += 1
                    continue
                if schema == "bench" and result.get("err"):
                    errored += 1
                elif result.get("ok") is not None:
                    scored += 1
            if missing:
                errors.append(f"{path}:{system}: missing {missing} row(s)")
            if errored:
                errors.append(f"{path}:{system}: has {errored} error row(s)")
            if scored != len(rows):
                errors.append(f"{path}:{system}: scored {scored}/{len(rows)} rows")
    return errors


def _validate_result(
    prefix: str,
    system: str,
    result: Any,
    require_complete: bool,
    schema: str,
    errors: list[str],
) -> None:
    if not isinstance(result, dict):
        errors.append(f"{prefix}: {system} result must be object")
        return
    required = BENCH_REQUIRED_RESULT_KEYS if schema == "bench" else PERSONAMEM_REQUIRED_RESULT_KEYS
    missing = required - set(result)
    if missing:
        errors.append(f"{prefix}: {system} missing keys {sorted(missing)}")
        return
    if result["ok"] is not None and not isinstance(result["ok"], bool):
        errors.append(f"{prefix}: {system} ok must be bool or null")
    if not isinstance(result["tok"], int) or result["tok"] < 0:
        errors.append(f"{prefix}: {system} tok must be a non-negative int")
    if not isinstance(result["lat"], (int, float)) or result["lat"] < 0:
        errors.append(f"{prefix}: {system} lat must be a non-negative number")
    if schema == "personamem":
        if not isinstance(result["pick"], int) or not -1 <= result["pick"] <= 3:
            errors.append(f"{prefix}: {system} pick must be an int in [-1, 3]")
        return
    if result["err"] is not None and not isinstance(result["err"], str):
        errors.append(f"{prefix}: {system} err must be string or null")
    if result["err"] is None and result["ok"] is not None:
        for key in ("pred", "gold"):
            if key not in result:
                errors.append(f"{prefix}: {system} missing {key}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate MnemoTrace benchmark JSONL logs.")
    ap.add_argument("logs", nargs="+", help="JSONL files to validate (select the matching schema)")
    ap.add_argument(
        "--schema",
        choices=sorted(SCHEMAS),
        default="bench",
        help="log schema to validate: bench.py JSONL or eval/personamem.py JSONL",
    )
    ap.add_argument("--expected-rows", type=int, default=None, help="require an exact row count")
    ap.add_argument(
        "--require-complete",
        action="store_true",
        help="require every system to be scored with no errors on every row",
    )
    ap.add_argument(
        "--system",
        action="append",
        dest="systems",
        default=None,
        help="when --require-complete is set, only require this system to be complete; repeatable",
    )
    args = ap.parse_args(argv)

    all_errors: list[str] = []
    for log in args.logs:
        errors = validate_bench_log(
            log,
            expected_rows=args.expected_rows,
            require_complete=args.require_complete,
            required_systems=args.systems,
            schema=args.schema,
        )
        if errors:
            all_errors.extend(errors)
        else:
            print(f"OK {log}")
    if all_errors:
        for err in all_errors:
            print(err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
