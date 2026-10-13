"""HW6 第 4 题：同一个 Agent 在 with_cache / without_cache 两种设置下审核全部练习文档，逐次记录每个模型请求。

用法（在 hw6/ 目录下）：
    python scripts/cache_test.py validate      # 最小验证：p01 各跑一次，不纳入正式统计
    python scripts/cache_test.py full          # 正式实验：12 份文档 × 2 种设置
    python scripts/cache_test.py summary       # 汇总 output/cache_test.json 中纳入正式比较的数据

每个模型请求一条记录，追加到 output/cache_test.json（JSON 列表，不覆盖历史记录）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import load_prices, request_cost, review  # noqa: E402
from tools import OUTPUT_DIR, PROJECT_DIR, append_json  # noqa: E402

CACHE_TEST = OUTPUT_DIR / "cache_test.json"
DOCS = sorted((PROJECT_DIR / "data" / "practice_docs").iterdir())
SETUPS = {"without_cache": "off", "with_cache": "on"}
MAX_ATTEMPTS = 3  # without_cache 出现缓存命中或运行失败时，最多重跑到第 3 次


def check_call(setup: str, rec: dict) -> tuple[bool, str]:
    """检查单次请求是否符合本组设置。"""
    if rec["http_status"] != 200 or rec["usage"] is None:
        return False, f"请求失败（HTTP {rec['http_status']}）或没有 usage"
    details = rec["usage"].get("input_tokens_details") or {}
    if setup == "without_cache":
        if "cached_tokens" not in details:
            return False, "API 没有返回 cached_tokens，无法证明没有缓存读取"
        if details["cached_tokens"] != 0:
            return False, f"出现缓存读取 {details['cached_tokens']} tokens，不符合无缓存设置"
        return True, "cached_tokens = 0"
    if "cached_tokens" not in details:
        return False, "API 没有返回 cached_tokens，无法判断缓存是否命中"
    return True, f"cached_tokens = {details['cached_tokens']}"


async def run_one(setup: str, doc: Path, phase: str, attempt: int) -> bool:
    """跑一份文档，把每个模型请求写进 cache_test.json。返回这次运行是否合格。"""
    rel = doc.relative_to(PROJECT_DIR).as_posix()
    r = await review(rel, cache=SETUPS[setup], tag=f"p4-{phase}-{setup}")
    prices = load_prices()
    checks = [check_call(setup, raw) for raw in r["raw"]]
    run_ok = r["status"] == "ok" and bool(r["raw"]) and all(ok for ok, _ in checks)
    for i, (raw, (ok, why)) in enumerate(zip(r["raw"], checks), 1):
        append_json(CACHE_TEST, {
            "setup": setup,
            "phase": phase,  # validation = 最小验证；formal = 正式实验
            "doc": doc.name,
            "run_id": r["run_id"],
            "attempt": attempt,
            "call_index": i,
            "calls_in_run": len(r["raw"]),
            "time": raw["time"],
            "model": raw["model"],
            "response_id": raw["response_id"],
            "http_status": raw["http_status"],
            "call_success": raw["http_status"] == 200 and raw["usage"] is not None,
            "setup_check_passed": ok,
            "setup_check": why,
            "run_status": r["status"],
            "run_error": r["error"],
            "included_in_comparison": phase == "formal" and run_ok,
            "sent_cache_params": raw["sent_cache_params"],
            "raw_usage": raw["usage"],
            **request_cost(raw["usage"], prices),
            "decision": (r["output"] or {}).get("decision"),
            "trap_calls": r["trap_calls"],
        })
    if not r["raw"]:  # 一次模型请求都没发出去，也留一条失败记录
        append_json(CACHE_TEST, {"setup": setup, "phase": phase, "doc": doc.name, "run_id": r["run_id"],
                                 "attempt": attempt, "call_index": None, "call_success": False,
                                 "run_status": r["status"], "run_error": r["error"],
                                 "included_in_comparison": False})
    t = r["usage"]["totals"]
    print(f"[{phase}/{setup}] {doc.name} attempt {attempt}: {r['status']}, {len(r['raw'])} calls, "
          f"read {t['cache_read_tokens']} write {t['cache_write_tokens']}, ${r['usage']['cost_usd']:.8f}, "
          f"{'OK' if run_ok else 'NOT OK: ' + '; '.join(w for ok, w in checks if not ok) + (r['error'] or '')}")
    return run_ok


async def run_setup(setup: str, docs: list[Path], phase: str) -> None:
    for doc in docs:
        # 最小验证只跑一次：不合格说明设置本身有问题，要先修设置
        for attempt in range(1, (1 if phase == "validation" else MAX_ATTEMPTS) + 1):
            if await run_one(setup, doc, phase, attempt):
                break


def summary() -> None:
    calls = json.loads(CACHE_TEST.read_text(encoding="utf-8"))
    formal = [c for c in calls if c.get("included_in_comparison")]
    out: dict = {"all_records": len(calls), "formal_records": len(formal), "setups": {}}
    for setup in SETUPS:
        cs = [c for c in formal if c["setup"] == setup]
        per_doc: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0, "input": 0, "read": 0,
                                                        "write": 0, "output": 0})
        for c in cs:
            d = per_doc[c["doc"]]
            d["calls"] += 1
            d["cost_usd"] += c["cost_usd"]
            d["input"] += c["input_tokens"]
            d["read"] += c["cache_read_tokens"] or 0
            d["write"] += c["cache_write_tokens"] or 0
            d["output"] += c["output_tokens"]
        parts = defaultdict(float)
        for c in cs:
            for k, v in c["cost_breakdown_usd"].items():
                parts[k] += v
        total = sum(d["cost_usd"] for d in per_doc.values())
        out["setups"][setup] = {
            "docs": len(per_doc), "calls": len(cs), "total_usd": total,
            "avg_per_doc_usd": total / len(per_doc) if per_doc else None,
            "tokens": {k: sum(d[k] for d in per_doc.values()) for k in ("input", "read", "write", "output")},
            "cost_breakdown_usd": dict(parts),
            "per_doc": dict(sorted(per_doc.items())),
        }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if cmd == "validate":
        p01 = [d for d in DOCS if d.name.startswith("p01")]
        for setup in SETUPS:
            asyncio.run(run_setup(setup, p01, "validation"))
    elif cmd == "full":
        for setup in SETUPS:  # 先跑完无缓存组，再连续跑有缓存组
            asyncio.run(run_setup(setup, DOCS, "formal"))
    else:
        summary()


if __name__ == "__main__":
    main()
