"""HW6 第 8 题：用完整防御版 Agent 处理 data/exam_docs/ 的 10 份考试文档。

    python scripts/exam_run.py run      # 每份文档跑一次（只有运行出错才重试，失败记录保留），原始记录存 output/p8_exam_runs.json
    python scripts/exam_run.py export   # 从本批次的原始记录导出 output/exam_results.json（只做字段改名，不改答案）
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import BLOCKED_LOG, OUTPUT_DIR, PROJECT_DIR, TRAP_LOG, append_json, now_iso  # noqa: E402

RUNS = OUTPUT_DIR / "p8_exam_runs.json"
RESULTS = OUTPUT_DIR / "exam_results.json"
DOCS = sorted((PROJECT_DIR / "data" / "exam_docs").iterdir())
MAX_ATTEMPTS = 3  # 只有技术故障（status=error）才重试；成功的回答不会重跑
DEFENSE = "full"


async def run_all() -> None:
    from agent import review

    batch = f"p8-exam-{now_iso()}"
    for doc in DOCS:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            r = await review(doc.relative_to(PROJECT_DIR).as_posix(), cache="on", tag="p8-exam", defense=DEFENSE)
            append_json(RUNS, {
                "batch": batch, "file": doc.name, "attempt": attempt, "run_id": r["run_id"], "defense": r["defense"],
                "status": r["status"], "error": r["error"], "output": r["output"],
                "extracted_fields": r["extracted_fields"], "decision_checks": r["decision_checks"],
                "tool_calls": r["tool_calls"], "trap_calls": r["trap_calls"], "blocked_actions": r["blocked_actions"],
                "sanitizer_removed": [x for rep in r["sanitize_reports"] for x in rep["removed"]],
                "usage": {k: r["usage"][k] for k in ("requests", "totals", "cost_usd", "not_reported_by_api", "per_request")},
                "started": r["started"], "finished": r["finished"],
            })
            t = r["usage"]["totals"]
            print(f"{doc.name} attempt {attempt}: {r['status']} {r['output'] and r['output']['decision']} "
                  f"calls={r['usage']['requests']} in={t['input_tokens']} read={t['cache_read_tokens']} "
                  f"out={t['output_tokens']} ${r['usage']['cost_usd']:.8f} traps={r['trap_calls'] or '-'} "
                  f"blocked={len(r['blocked_actions'])} removed={sum(1 for rep in r['sanitize_reports'] for _ in rep['removed'])}"
                  + (f" ERROR {r['error']}" if r["error"] else ""))
            if r["status"] == "ok":
                break


def export() -> None:
    runs = json.loads(RUNS.read_text(encoding="utf-8"))
    batch = runs[-1]["batch"]
    mine = [r for r in runs if r["batch"] == batch]
    traps = json.loads(TRAP_LOG.read_text(encoding="utf-8"))
    results, failed = [], []
    for doc in DOCS:
        attempts = [r for r in mine if r["file"] == doc.name]
        ok = [r for r in attempts if r["status"] == "ok"]
        if not ok:
            failed.append({"file": doc.name, "errors": [r["error"] for r in attempts]})
            continue
        r = ok[0]  # 每份文档只有一次成功运行（成功后不再重跑）
        out, t = r["output"], r["usage"]["totals"]
        # 只认本次运行标识下实际执行的模拟工具调用；被权限层拦截的尝试不算“调用”
        executed = [x for x in traps if x["run_id"] == r["run_id"]]
        not_reported = r["usage"]["not_reported_by_api"]
        results.append({
            "file": doc.name,
            "vendor": out["vendor"],
            "sku": out["sku"],
            "unit_price": out["unit_price"],
            "lead_time_days": out["lead_time_days"],
            "min_order": out["minimum_order"],  # 导出时改名，值不变
            "decision": out["decision"],
            "reason": out["reason"],
            "trap_tool_called": bool(executed),
            "input_tokens": t["input_tokens"],
            "cached_tokens": None if "cache_read_tokens" in not_reported else t["cache_read_tokens"],
            "output_tokens": t["output_tokens"],
            "cost_usd": r["usage"]["cost_usd"],
        })
    RESULTS.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"batch": batch, "exported": len(results), "failed": failed}, ensure_ascii=False, indent=2))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    if sys.argv[1:] == ["run"]:
        asyncio.run(run_all())
    elif sys.argv[1:] == ["export"]:
        export()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
