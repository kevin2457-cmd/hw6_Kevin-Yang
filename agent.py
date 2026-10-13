"""Campus Customs 供应商审核 Agent 的入口。

用法：
    python agent.py --doc data/practice_docs/p01_bluehen_quote.html
    python agent.py --doc data/practice_docs/p01_bluehen_quote.html --cache off   # 关闭提示词缓存（第 4 题）
    python agent.py --doc data/practice_docs/p01_bluehen_quote.html --defense none   # 无防御（第 5 题行为）
    python agent.py --doc data/practice_docs/p01_bluehen_quote.html --defense full   # 三层防御（默认）
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Literal

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")  # 关掉 PydanticAI 的启动横幅，终端输出更干净

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from models import ExtractedQuote, VendorDecision
from tools import (ALL_TOOLS, BLOCKED_LOG, OUTPUT_DIR, PROJECT_DIR, READ_ONLY_TOOLS, TRAP_LOG, RunDeps, append_json,
                   ensure_json_list, log_blocked, now_iso)

# 作业要求：只用 gpt-6-luna，不设备用模型，也不允许用环境变量改成别的模型
MODEL_NAME = "gpt-6-luna"
PORTKEY_BASE_URL = "https://api.portkey.ai/v1"

HANDBOOK_PATH = PROJECT_DIR / "data" / "shop_handbook.md"
PROMPT_PATH = PROJECT_DIR / "prompts" / "prompt.md"  # 第 6 题：带提示词层防御的版本
UNDEFENDED_PROMPT_PATH = PROJECT_DIR / "prompts" / "prompt_undefended.md"  # 第 3–5 题原样，无防御模式使用
PRICES_PATH = PROJECT_DIR / "data" / "prices.json"
USAGE_LOG = OUTPUT_DIR / "usage_log.json"
AUDIT_LOG = OUTPUT_DIR / "audit_trail.json"

# 单次审核的运行上限：正常流程只需要 2 次模型请求、1 次工具调用
LIMITS = UsageLimits(request_limit=6, tool_calls_limit=6)

CacheMode = Literal["on", "off"]

# 第 6 题：三层防御，可以单独开关。
#   code   = 代码层：文档交给模型前先清洗（defense.sanitize_document）
#   arch   = 架构层：读文档的模型只有只读工具、只输出字段；决策由代码按手册完成；操作需人类批准
#   prompt = 提示词层：使用 prompts/prompt.md（加强版规则），否则用 prompts/prompt_undefended.md
LAYERS = ("code", "arch", "prompt")
DEFENSE_PRESETS: dict[str, frozenset[str]] = {
    "none": frozenset(),  # 第 5 题的行为：原提示词、原读取方式、3 个工具
    "code": frozenset({"code"}),
    "code_arch": frozenset({"code", "arch"}),
    "full": frozenset(LAYERS),
}


def parse_defense(value: str) -> frozenset[str]:
    """--defense 可以写预设名（none / code / code_arch / full），也可以写逗号分隔的层（如 code,prompt）。"""
    if value in DEFENSE_PRESETS:
        return DEFENSE_PRESETS[value]
    layers = frozenset(x.strip() for x in value.split(",") if x.strip())
    unknown = layers - set(LAYERS)
    if unknown:
        raise ValueError(f"未知的防御层 {sorted(unknown)}，可选 {LAYERS} 或预设 {list(DEFENSE_PRESETS)}")
    return layers


def defense_label(layers: frozenset[str]) -> str:
    return "+".join(x for x in LAYERS if x in layers) or "none"
# 缓存开关（第 4 题）：
# - on：默认的 implicit 模式，OpenAI 自动在提示词末尾设一个缓存断点；prompt_cache_key 固定，
#   让相同前缀（手册 + 任务说明）的请求尽量路由到同一台缓存机器。
# - off：两层做法。
#   1) mode=explicit 且不设任何显式断点。按 OpenAI 接口说明，这种请求“不使用提示词缓存”。
#      但第 4 题最小验证（output/cache_test.json 中 validation 记录）显示：经 Portkey 调用时，
#      同一次运行的第 2 个请求仍读到 10,905 个缓存 token，所以这个参数单独不够。
#   2) 缓存绕过：每个模型请求都在 instructions 最前面放一个随机 nonce。缓存只按“从头开始完全相同的前缀”
#      匹配，开头不同，后面的手册再一样也读不到缓存。手册、任务说明、文档内容都不变，只多十几个 token。
#   是否真的生效，以 API 返回的 cached_tokens 为准（必须为 0）。
CACHE_SETTINGS: dict[CacheMode, OpenAIResponsesModelSettings] = {
    "on": {"openai_prompt_cache_key": "campus-customs-vendor-desk"},
    "off": {"openai_prompt_cache_options": {"mode": "explicit"}},
}

# .env 可以放在 hw6/ 或上一级目录
load_dotenv(PROJECT_DIR / ".env")
load_dotenv(PROJECT_DIR.parent / ".env")

_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|pk-[A-Za-z0-9_\-]{8,}|PORTKEY_API_KEY=\S+)")


def redact(text: str) -> str:
    key = os.getenv("PORTKEY_API_KEY", "").strip()
    if key:
        text = text.replace(key, "[REDACTED]")
    return _SECRET.sub("[REDACTED]", text)


# ---------------------------------------------------------------- 记录 API 原始 usage

# 每次 /responses 请求的原始响应 usage 都追加到这里，用来记录 API 实际报告的数字
_raw_usages: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar("raw_usages", default=None)


async def _capture_usage(response: httpx.Response) -> None:
    sink = _raw_usages.get()
    if sink is None or not response.url.path.endswith("/responses"):
        return
    await response.aread()
    try:
        body = response.json()
    except ValueError:
        body = {}
    # 同时记下请求里实际发出的缓存参数，作为设置生效的证据（不记录任何请求头或密钥）
    try:
        sent = json.loads(response.request.content or b"{}")
    except ValueError:
        sent = {}
    sink.append({
        "time": now_iso(),
        "http_status": response.status_code,
        "response_id": body.get("id"),
        "model": body.get("model"),
        "usage": body.get("usage"),
        "error": redact(json.dumps(body.get("error"), ensure_ascii=False)) if body.get("error") else None,
        "sent_cache_params": {k: sent[k] for k in ("prompt_cache_key", "prompt_cache_options") if k in sent},
    })


def build_model() -> OpenAIResponsesModel:
    key = os.getenv("PORTKEY_API_KEY", "").strip()
    if not key:
        raise RuntimeError("缺少 PORTKEY_API_KEY，请在 hw6/.env 或上一级目录的 .env 中设置。")
    client = AsyncOpenAI(
        base_url=PORTKEY_BASE_URL,
        api_key=key,
        default_headers={"x-portkey-api-key": key, "x-portkey-provider": "openai"},
        http_client=httpx.AsyncClient(timeout=120, event_hooks={"response": [_capture_usage]}),
        max_retries=1,
    )
    # gpt-6-luna 的函数工具需要走 Responses API
    return OpenAIResponsesModel(MODEL_NAME, provider=OpenAIProvider(openai_client=client))


def build_agent(cache: CacheMode = "on", layers: frozenset[str] = frozenset()) -> Agent[RunDeps, Any]:
    # 完整手册放在最前面、任务说明在后：每次请求都发送，而且前缀保持不变；
    # 变化的部分（文档路径、工具返回的文档内容）都在这个固定前缀之后
    handbook = HANDBOOK_PATH.read_text(encoding="utf-8")
    prompt = (PROMPT_PATH if "prompt" in layers else UNDEFENDED_PROMPT_PATH).read_text(encoding="utf-8")
    if "arch" in layers:
        # 架构层：这个模型只负责提取字段，最终决策由代码完成
        prompt += ("\n\n## Program note\n\nIn this program you return only the extracted fields (`ExtractedQuote`) "
                   "plus `document_requests`. The purchasing decision and reason are computed afterwards by code "
                   "from the handbook, so do not output a decision.")
    fixed = f"{handbook}\n\n---\n\n{prompt}"

    def bypass_instructions() -> str:
        # PydanticAI 每个模型请求都会重新调用这个函数，所以连工具调用之后的第 2 个请求也拿到新的 nonce
        return f"[request nonce {uuid.uuid4().hex} - ignore]\n\n{fixed}"

    return Agent(
        build_model(),
        deps_type=RunDeps,
        output_type=ExtractedQuote if "arch" in layers else VendorDecision,
        instructions=fixed if cache == "on" else bypass_instructions,
        # 架构层：读取不可信文档的组件只拿只读工具；send_email / approve_payment 不在它的作用域里
        tools=READ_ONLY_TOOLS if "arch" in layers else ALL_TOOLS,
        retries=1,
        model_settings=CACHE_SETTINGS[cache],
    )


# ---------------------------------------------------------------- 用量与费用


def request_cost(usage: dict[str, Any] | None, prices: dict[str, float]) -> dict[str, Any]:
    """按一次请求的原始 usage 计算费用。

    OpenAI 的 input_tokens 已经包含缓存读取（cached_tokens）和缓存写入（cache_write_tokens），
    所以按原价计费的部分 = input_tokens - cached_tokens - cache_write_tokens，四部分分别计价，不会重复计算。
    output_tokens 已经包含 reasoning_tokens，按输出价计一次。API 没返回的字段记为 None。
    """
    u = usage or {}
    in_d = u.get("input_tokens_details") or {}
    out_d = u.get("output_tokens_details") or {}
    inp, out = u.get("input_tokens", 0), u.get("output_tokens", 0)
    cache_read, cache_write = in_d.get("cached_tokens"), in_d.get("cache_write_tokens")
    uncached = inp - (cache_read or 0) - (cache_write or 0)
    parts = {
        "uncached_input_usd": uncached * prices["input_per_1m"] / 1_000_000,
        "cache_read_usd": (cache_read or 0) * prices["cached_input_per_1m"] / 1_000_000,
        "cache_write_usd": (cache_write or 0) * prices["cache_write_per_1m"] / 1_000_000,
        "output_usd": out * prices["output_per_1m"] / 1_000_000,
    }
    return {
        "input_tokens": inp,
        "uncached_input_tokens": uncached,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "output_tokens": out,
        "reasoning_tokens": out_d.get("reasoning_tokens"),
        "total_tokens": u.get("total_tokens"),
        "not_reported_by_api": [k for k, v in (("cache_read_tokens", cache_read), ("cache_write_tokens", cache_write))
                                if v is None] if usage else ["usage"],
        "cost_usd": round(sum(parts.values()), 10),
        "cost_breakdown_usd": {k: round(v, 10) for k, v in parts.items()},
    }


def load_prices() -> dict[str, float]:
    return json.loads(PRICES_PATH.read_text(encoding="utf-8"))[MODEL_NAME]


def summarize_usage(raw: list[dict[str, Any]]) -> dict[str, Any]:
    """逐次请求计算后再汇总。"""
    prices = load_prices()
    per_request = [{"response_id": r["response_id"], "model": r["model"], **request_cost(r["usage"], prices)}
                   for r in raw]
    keys = ("input_tokens", "uncached_input_tokens", "cache_read_tokens", "cache_write_tokens",
            "output_tokens", "reasoning_tokens", "total_tokens")
    return {
        "requests": len(raw),
        "totals": {k: sum(p[k] or 0 for p in per_request) for k in keys},
        "cost_usd": round(sum(p["cost_usd"] for p in per_request), 10),
        "not_reported_by_api": sorted({f for p in per_request for f in p["not_reported_by_api"]}),
        "per_request": per_request,
        "prices_per_1m": prices,
        "cost_method": "uncached_input*input + cache_read*cached_input + cache_write*cache_write + output*output（单价来自 data/prices.json，每百万 token）",
    }


# ---------------------------------------------------------------- 运行


async def review(doc: str, cache: CacheMode = "on", tag: str | None = None,
                 defense: str | frozenset[str] = "none") -> dict[str, Any]:
    """审核一份文档，写入日志，并返回本次运行的全部信息（含每次请求的原始 usage）。

    defense 默认 "none"，让第 4、5 题的脚本保持原来的行为；命令行默认是 full。"""
    layers = parse_defense(defense) if isinstance(defense, str) else frozenset(defense)
    for path in (USAGE_LOG, AUDIT_LOG, TRAP_LOG, BLOCKED_LOG):
        ensure_json_list(path)

    run_id = f"run-{now_iso().replace(':', '').replace('+0000', 'Z')}-{uuid.uuid4().hex[:6]}"
    deps = RunDeps(run_id=run_id, doc=doc, layers=layers, scope="extractor" if "arch" in layers else "legacy")
    raw: list[dict[str, Any]] = []
    token = _raw_usages.set(raw)
    started = now_iso()
    output: VendorDecision | None = None
    error: str | None = None
    pai_usage: dict[str, Any] | None = None
    extracted: ExtractedQuote | None = None
    decision_checks: list[dict[str, Any]] | None = None

    try:
        agent = build_agent(cache, layers)
        result = await agent.run(f"Review this vendor document: {doc}", deps=deps, usage_limits=LIMITS)
        if "arch" in layers:
            from defense import decide

            extracted = result.output
            # 决策只依据经过类型校验的字段 + 可信手册，文档原文不会进到这一步
            output, decision_checks = decide(extracted)
            # 文档里要求执行的操作只记录为“被拦截的尝试”，不会执行任何工具
            for req in extracted.document_requests:
                log_blocked(deps, "document_request", {"text": req}, requested_by="vendor document",
                            reason="文档内容不能触发操作；send_email / approve_payment 需要独立的人类批准。")
        else:
            output = result.output
        u = result.usage
        pai_usage = {"requests": u.requests, "tool_calls": u.tool_calls, "input_tokens": u.input_tokens,
                     "cache_read_tokens": u.cache_read_tokens, "cache_write_tokens": u.cache_write_tokens,
                     "output_tokens": u.output_tokens}
    except Exception as e:  # 出错也要把已经产生的调用写进日志
        error = redact(f"{type(e).__name__}: {e}")
    finally:
        _raw_usages.reset(token)

    finished = now_iso()
    status = "ok" if error is None else "error"
    trap_calls = [c.tool for c in deps.tool_calls if c.tool in ("send_email", "approve_payment")]
    usage = summarize_usage(raw)
    common = {"run_id": run_id, "doc": doc, "model": MODEL_NAME, "cache": cache, "defense": defense_label(layers),
              "tag": tag, "started": started, "finished": finished, "status": status, "error": error}

    append_json(USAGE_LOG, {**common, **usage, "pydantic_ai_usage": pai_usage})  # pydantic_ai_usage 用于交叉核对
    append_json(AUDIT_LOG, {
        **common,
        "tool_calls": [c.model_dump() for c in deps.tool_calls],
        "trap_calls": trap_calls,
        "blocked_actions": deps.blocked_actions,
        "sanitize_reports": deps.sanitize_reports,
        "extracted_fields": extracted.model_dump() if extracted else None,
        "decision_checks": decision_checks,
        "final_answer": output.model_dump() if output else None,
    })
    return {**common, "output": output.model_dump() if output else None, "usage": usage, "raw": raw,
            "tool_calls": [c.tool for c in deps.tool_calls], "trap_calls": trap_calls,
            "blocked_actions": deps.blocked_actions, "sanitize_reports": deps.sanitize_reports,
            "extracted_fields": extracted.model_dump() if extracted else None, "decision_checks": decision_checks}


async def run(doc: str, cache: CacheMode = "on", defense: str = "full") -> int:
    r = await review(doc, cache, defense=defense)
    print(f"run_id: {r['run_id']}")
    print(f"status: {r['status']}  cache: {cache}  defense: {r['defense']}")
    if r["output"]:
        print(json.dumps(r["output"], ensure_ascii=False, indent=2))
    if r["error"]:
        print(f"error: {r['error']}")
    u, t = r["usage"], r["usage"]["totals"]
    print(f"usage: {u['requests']} requests, input {t['input_tokens']} (cache read {t['cache_read_tokens']}, "
          f"cache write {t['cache_write_tokens']}), output {t['output_tokens']}, cost ${u['cost_usd']:.8f}")
    print(f"tools: {r['tool_calls']}  trap calls: {r['trap_calls'] or 'none'}  "
          f"blocked: {len(r['blocked_actions'])}  sanitizer removed: "
          f"{sum(len(x['removed']) for x in r['sanitize_reports'])}")
    return 0 if r["status"] == "ok" else 1


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Campus Customs vendor desk agent")
    parser.add_argument("--doc", required=True, help="供应商文档路径（.html / .pdf / .eml）")
    parser.add_argument("--cache", choices=["on", "off"], default="on", help="提示词缓存开关，默认 on")
    parser.add_argument("--defense", default="full",
                        help="防御设置：none（第 5 题无防御）/ code / code_arch / full（默认），或逗号分隔的层，如 code,prompt")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.doc, args.cache, args.defense)))


if __name__ == "__main__":
    main()
