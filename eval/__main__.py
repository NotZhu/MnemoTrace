"""从仓库根目录运行评测预检、执行、校验和清理。"""

import argparse
import json
import sqlite3
from pathlib import Path

import httpx
from filelock import Timeout
from pydantic import ValidationError

from .artifacts import Store
from .config import load_config
from .datasets import Dataset, load_dataset
from .http import EvaluationError
from .report import build_report
from .runner import (
    cleanup,
    execute,
    make_manifest,
    preflight,
    reader_for,
    resume_dataset,
)
from .scoring import Reader

_RUNS = Path(__file__).resolve().parents[1] / ".artifacts" / "eval"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "run"):
        command = sub.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
    for name in ("resume", "validate", "report", "cleanup"):
        command = sub.add_parser(name)
        command.add_argument("--run-dir", required=True, type=Path)
        if name == "resume":
            command.add_argument("--from-stage", choices=("answer", "score"))
            command.add_argument("--config", type=Path)
        if name == "validate":
            command.add_argument(
                "--for-publication",
                action="store_true",
                help="要求同次三组完整通过全量评测",
            )
        if name == "cleanup":
            command.add_argument(
                "--confirm-settled",
                action="store_true",
                help="确认服务端请求已经结束后，清理仍待确认的资源",
            )
    return parser.parse_args()


def _print_report(result: dict[str, object], *, for_publication: bool = False) -> int:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    required = "publishable" if for_publication else "complete"
    return 0 if result[required] else 1


def _execute(
    store: Store,
    dataset: Dataset,
    reader: Reader,
    client: httpx.Client,
) -> int:
    try:
        execute(store, dataset, reader, client)
    finally:
        store.export()
        result = build_report(store)
        store.write_json("report.json", result)
    return _print_report(result)


def _new_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    dataset = load_dataset(config.benchmark, config.data_dir)
    reader = reader_for(config)
    cases = preflight(config, dataset, reader)
    if args.command == "prepare":
        print(
            json.dumps(
                {
                    "benchmark": config.benchmark,
                    "revision": dataset.release.revision,
                    "questions": len(cases),
                    "dataset_questions": dataset.release.question_count,
                    "question_ids": [case.id for case in cases],
                    "backends": list(config.backends),
                },
                ensure_ascii=False,
            )
        )
        return 0
    with Store.create(_RUNS, make_manifest(config, dataset, cases), cases) as store:
        print(f"run_dir={store.directory}", flush=True)
        with httpx.Client(timeout=config.timeout_seconds, trust_env=False) as client:
            return _execute(store, dataset, reader, client)


def _existing_run(args: argparse.Namespace) -> int:
    directory = args.run_dir.resolve()
    if directory.parent != _RUNS.resolve():
        raise ValueError("运行目录须位于本仓库的 .artifacts/eval 下")
    with Store.open(directory) as store:
        if args.command in {"report", "validate"}:
            result = build_report(store)
            if args.command == "report":
                store.export()
                store.write_json("report.json", result)
            return _print_report(
                result,
                for_publication=args.command == "validate" and args.for_publication,
            )
        config = store.manifest.config
        with httpx.Client(timeout=config.timeout_seconds, trust_env=False) as client:
            if args.command == "cleanup":
                cleanup(store, client, confirm_uncertain=args.confirm_settled)
                store.export()
                pending = len(store.active())
                print(f"remaining_resources={pending}")
                return int(bool(pending))
            supplied = load_config(args.config) if args.config is not None else config
            dataset = resume_dataset(store, supplied)
            reader = reader_for(config)
            preflight(config, dataset, reader)
            if args.from_stage is not None:
                store.rewind(args.from_stage)
            return _execute(store, dataset, reader, client)


def main() -> int:
    """执行单个 CLI 子命令；预检、校验和报告不访问模型接口。"""
    args = _arguments()
    try:
        return (
            _new_run(args)
            if args.command in {"prepare", "run"}
            else _existing_run(args)
        )
    except ValidationError as error:
        fields = {str(item["loc"]) for item in error.errors(include_input=False)}
        print("评测输入格式无效，字段 " + ", ".join(sorted(fields)))
    except Timeout:
        print("另一个进程正在访问此运行")
    except (EvaluationError, OSError, SyntaxError, ValueError, sqlite3.Error) as error:
        print(f"评测命令失败 ({type(error).__name__})：{error}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
