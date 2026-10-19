"""把一份记忆数据集批量灌入 忆痕 服务，然后抽样验证召回。仅依赖标准库 ——
把这个脚本加一个 JSON 数据文件交给任何人，他们就能灌入自己（已脱敏的）数据并测试。

数据文件 = JSON 数组（或 {"records": [...]}）。每条记录是以下二者之一：
  • 一段原始对话  -> POST /v1/remember（服务端自动抽取 fact）
      {"content": "用户：我在字节做后端，最爱周杰伦。\n助手：好的", "session_id": "s1", "scope": "long"}
      scope: "auto"（默认，临时状态路由到 working 记忆）| "long"（强制长期）| "working"
  • 一条结构化 fact -> POST /v1/facts（权威写入，不会被自动覆盖）
      {"subject": "user", "predicate": "works_at", "object": "字节跳动"}

用法：
  python examples/ingest_client.py --base http://127.0.0.1:8000 --key demo-test \
      --data your_data.json [--reset] [--scope long] [--probe "我喜欢哪个歌手"]
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="service base URL, e.g. http://127.0.0.1:8000")
    ap.add_argument("--key", required=True, help="your API key = your isolated memory namespace")
    ap.add_argument("--data", required=True, help="path to the JSON dataset")
    ap.add_argument("--reset", action="store_true", help="wipe this key's memory before loading")
    ap.add_argument("--scope", default="", help="override scope for ALL conversations (long|working|auto)")
    ap.add_argument("--probe", action="append", default=[], help="a query to test recall after loading (repeatable)")
    ap.add_argument("--no-proxy", action="store_true", help="bypass any HTTP(S)_PROXY env for direct connect")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if args.no_proxy \
        else urllib.request.build_opener()

    def call(path: str, body=None, method="POST"):
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"}, method=method)
        with opener.open(req, timeout=120) as r:
            return json.loads(r.read())

    with open(args.data, encoding="utf-8") as fh:
        data = json.load(fh)
    records = data.get("records", data) if isinstance(data, dict) else data
    if not isinstance(records, list):
        raise SystemExit("data must be a JSON array, or an object with a 'records' array")

    if args.reset:
        call("/v1/forget", {})
        print(f"reset namespace '{args.key}'")

    convos = facts = 0
    for i, rec in enumerate(records, 1):
        try:
            if "content" in rec:
                body = {"content": rec["content"], "session_id": rec.get("session_id", f"s{i}"),
                        "scope": args.scope or rec.get("scope", "auto")}
                r = call("/v1/remember", body)
                convos += 1
                tag = r.get("scope", "long")
            elif "predicate" in rec:
                call("/v1/facts", {"subject": rec.get("subject", "user"),
                                   "predicate": rec["predicate"], "object": rec["object"]})
                facts += 1
                tag = "fact"
            else:
                print(f"  [{i}] skipped (no 'content' or 'predicate')")
                continue
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}] ERROR: {type(exc).__name__}: {exc}")
            continue
        if i % 20 == 0 or i == len(records):
            print(f"  {i}/{len(records)}  (last={tag})")
        time.sleep(0.05)

    mem = call("/v1/memories", method="GET")
    c = mem["counts"]
    print(f"\nloaded: {convos} conversations + {facts} facts -> "
          f"{c['facts_live']} live facts / {c['episodes']} episodes / {c['summaries']} summaries")

    for q in args.probe:
        r = call("/v1/recall", {"query": q, "n_chunks": 4})
        save = (r.get("full_tokens", 0) / max(1, r.get("tokens_est", 1)))
        print(f"\nQ: {q}\nA: {r.get('answer', '')}\n   (精炼 {r.get('tokens_est')} / 全量 {r.get('full_tokens')} tokens"
              f" · 省 {save:.1f}×)")
    print(f"\n控制台可视化查看: {base}/ui/  (key: {args.key})")


if __name__ == "__main__":
    main()
