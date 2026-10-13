"""HW6 第 6 题：逐层加防御，在同样 12 份练习文档、同样缓存设置（--cache on）下比较效果和成本。

    python scripts/defense_test.py run [none code code_arch full]   # 按顺序跑这些配置，默认 4 个都跑
    python scripts/defense_test.py run arch prompt                  # 单层隔离对照（不含代码层）
    python scripts/defense_test.py evaluate                         # 独立评估，写 defense_results.json 等

配置是叠加的：none → code → code_arch（code+arch）→ full（code+arch+prompt），
相邻两个配置的差额就是“按此顺序增加该层的边际成本”。full 配置的运行同时就是完整防御测试，不另外再跑。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools import OUTPUT_DIR, PROJECT_DIR, append_json, extract_document_text, now_iso  # noqa: E402

RUNS = OUTPUT_DIR / "p6_runs.json"
LAYER_EVAL = OUTPUT_DIR / "p6_layer_eval.json"
RESULTS = OUTPUT_DIR / "defense_results.json"
DOCS = sorted((PROJECT_DIR / "data" / "practice_docs").iterdir())
CONFIGS = ["none", "code", "code_arch", "full"]
# 单层隔离对照（不含代码层）：看架构层、提示词层各自能挡住什么，不叠加代码层
ISOLATION_CONFIGS = ["arch", "prompt"]
ALL_CONFIGS = CONFIGS + ISOLATION_CONFIGS


async def run_config(config: str) -> None:
    from agent import review

    batch = f"p6-{config}-{now_iso()}"
    for doc in DOCS:
        r = await review(doc.relative_to(PROJECT_DIR).as_posix(), cache="on", tag=f"p6-{config}", defense=config)
        append_json(RUNS, {
            "batch": batch, "config": config, "defense": r["defense"], "doc": doc.name, "run_id": r["run_id"],
            "status": r["status"], "error": r["error"], "output": r["output"],
            "extracted_fields": r["extracted_fields"], "decision_checks": r["decision_checks"],
            "tool_calls": r["tool_calls"], "trap_calls": r["trap_calls"], "blocked_actions": r["blocked_actions"],
            "sanitizer_removed": [x for rep in r["sanitize_reports"] for x in rep["removed"]],
            "usage": {k: r["usage"][k] for k in ("requests", "totals", "cost_usd", "not_reported_by_api", "per_request")},
            "started": r["started"], "finished": r["finished"],
        })
        t = r["usage"]["totals"]
        print(f"[{config}] {doc.name}: {r['status']} {r['output'] and r['output']['decision']} "
              f"calls={r['usage']['requests']} in={t['input_tokens']} read={t['cache_read_tokens']} "
              f"out={t['output_tokens']} ${r['usage']['cost_usd']:.8f} traps={r['trap_calls'] or '-'} "
              f"blocked={len(r['blocked_actions'])}")


def text_seen_by_model(doc: Path, config: str) -> str:
    from agent import parse_defense

    if "code" not in parse_defense(config):
        return extract_document_text(doc)
    from defense import sanitize_document

    return sanitize_document(doc)[0]


def evaluate() -> None:
    from attack_test import evaluate_batch

    runs = json.loads(RUNS.read_text(encoding="utf-8"))
    ann = json.loads((OUTPUT_DIR / "p5_doc_inspection.json").read_text(encoding="utf-8"))
    out: dict = {}
    for config in ALL_CONFIGS:
        mine = [r for r in runs if r["config"] == config]
        if not mine:
            continue
        latest = mine[-1]["batch"]
        batch = [r for r in mine if r["batch"] == latest]
        # 注入是否送到模型：按这一配置下模型实际收到的文本判断（代码层会改变这一点）
        reached = {d.name: ann[d.name]["marker"] in text_seen_by_model(d, config)
                   for d in DOCS if ann[d.name].get("has_injection")}
        results = evaluate_batch(batch, reached=reached)
        for res, run in zip(results, batch):
            res["defense"] = run["defense"]
            res["extracted_fields"] = run["extracted_fields"]
            res["decision_checks"] = run["decision_checks"]
            res["sanitizer_removed"] = run["sanitizer_removed"]
            res["usage"] = {"model_requests": run["usage"]["requests"], **run["usage"]["totals"],
                            "cost_usd": run["usage"]["cost_usd"],
                            "not_reported_by_api": run["usage"]["not_reported_by_api"],
                            "per_request": run["usage"]["per_request"]}
        out[config] = {"batch": latest, "results": results}
        print(f"== {config}")
        for x in results:
            print(f"  {x['doc']}: fields={'OK' if x['all_fields_match'] else [f for f, v in x['field_match'].items() if not v['match']]} "
                  f"decision={'OK' if x['decision_match'] else 'X'} trap={x['trap_tool_called']} "
                  f"blocked={len(x['blocked_action_attempts'])} strange={x['strange_text_found']} -> {x['attack_verdict']} "
                  f"${x['usage']['cost_usd']:.8f}")
    LAYER_EVAL.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if "full" in out:
        RESULTS.write_text(json.dumps(out["full"]["results"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    if sys.argv[1:2] == ["run"]:
        for config in sys.argv[2:] or CONFIGS:
            assert config in ALL_CONFIGS, config
            asyncio.run(run_config(config))
    elif sys.argv[1:2] == ["evaluate"]:
        evaluate()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
