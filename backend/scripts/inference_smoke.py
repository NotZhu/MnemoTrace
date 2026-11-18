"""用部署方显式配置的端点验证合成补全与嵌入。"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence

from mnemotrace_next.adapters.inference import InferenceRequestError, open_inference
from mnemotrace_next.ports.inference import (
    CompletionRefused,
    CompletionTruncated,
    InvalidStructuredOutput,
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "details": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["note", "task"]},
                "due": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            },
            "required": ["kind", "due"],
            "additionalProperties": False,
        },
    },
    "required": ["title", "details"],
    "additionalProperties": False,
}
_EXPECTED = {"title": "茶", "details": {"kind": "note", "due": None}}


async def _check(env: Mapping[str, str], *, llm: bool, embed: bool) -> None:
    async with open_inference(env, llm=llm, embed=embed) as clients:
        if clients.llm is not None:
            completion = await clients.llm.complete(
                system="请用任意非空文本回复。", prompt="这是合成测试。"
            )
            if not completion.text.strip():
                raise ValueError("合成文本补全为空")
            record = await clients.llm.complete_json(
                system="提取合成记录。备忘对应 note，任务对应 task，缺失的到期编号设为 null。",
                prompt="备忘标题为茶，无到期编号。",
                schema=_SCHEMA,
            )
            if record != _EXPECTED:
                raise ValueError("合成记录的返回字段值不符合预期")
            temperature = env.get("MNEMOTRACE_NEXT_LLM_TEMPERATURE")
            print(
                json.dumps(
                    {
                        "role": "llm",
                        "status": "passed",
                        "base_url": env["MNEMOTRACE_NEXT_LLM_BASE_URL"].strip(),
                        "model": env["MNEMOTRACE_NEXT_LLM_MODEL"].strip(),
                        "json_mode": env["MNEMOTRACE_NEXT_LLM_JSON_MODE"].strip(),
                        "temperature": float(temperature)
                        if temperature is not None
                        else None,
                    },
                    ensure_ascii=False,
                )
            )
        if clients.embedder is not None:
            vectors = await clients.embedder.embed(("合成文本：茶", "合成文本：云"))
            dim = clients.embedder.dim
            if len(vectors) != 2 or any(len(vector) != dim for vector in vectors):
                raise ValueError("合成嵌入批次的数量或维度不符合预期")
            print(
                json.dumps(
                    {
                        "role": "embed",
                        "status": "passed",
                        "base_url": env["MNEMOTRACE_NEXT_EMBED_BASE_URL"].strip(),
                        "model": env["MNEMOTRACE_NEXT_EMBED_MODEL"].strip(),
                        "revision": env["MNEMOTRACE_NEXT_EMBED_REVISION"].strip(),
                        "dim": dim,
                        "embedding_space": clients.embedder.embedding_space,
                    },
                    ensure_ascii=False,
                )
            )


def main(argv: Sequence[str] | None = None) -> int:
    """按显式指定的角色运行端点冒烟。

    Args:
        argv: 命令行参数，缺省时读取进程参数。

    Returns:
        所有检查通过返回 0，配置或请求失败返回 1。

    Raises:
        SystemExit: 参数缺失或请求帮助时由 argparse 退出。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm", action="store_true", help="验证文本和结构化补全")
    parser.add_argument("--embed", action="store_true", help="验证批量嵌入")
    args = parser.parse_args(argv)
    if not (args.llm or args.embed):
        parser.error("请指定 --llm 或 --embed")

    try:
        asyncio.run(_check(os.environ, llm=args.llm, embed=args.embed))
    except TimeoutError:
        print("推理冒烟失败：请求超时", file=sys.stderr)
        return 1
    except (
        ValueError,
        InferenceRequestError,
        CompletionRefused,
        CompletionTruncated,
        InvalidStructuredOutput,
    ) as exc:
        print(f"推理冒烟失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
