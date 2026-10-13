# HW6 — Campus Customs Vendor Desk (Tokenomics + Prompt Injection)

**Author:** Chirong Kevin Yang · MGT 409 (AI Foundations for Managers), Fall 2026

This project is a PydanticAI agent that reviews one vendor document at a time: an HTML quote, a PDF quote or a `.eml` email. It extracts vendor, SKU, unit price, lead time and minimum order, then decides `recommend` / `do_not_recommend` using the rules in the Campus Customs Shop Handbook. The homework covers:

- what sending the full handbook on every call costs, with and without prompt caching;
- how hidden text in vendor documents can hijack the agent;
- three defence layers and what each one costs;
- two attack documents of my own (red team), the 10 exam documents, and a one-page results report.

- **Model:** every model call uses **`gpt-6-luna`** through Portkey's OpenAI-compatible Responses API. The model name is fixed in `agent.py` and there is no fallback model.
- **Simulated tools:** `send_email` and `approve_payment` are **trap tools**. They never send email or move money. They only append a record to `output/trap_calls.json`. With defences on, the document-reading part of the agent cannot call them at all: attempts are blocked and logged to `output/blocked_actions.json`, and a real run would need a human sign-off typed at a terminal.

## Setup

Python **3.11+** (developed on Python 3.14).

```bash
cd hw6
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Copy the environment template and add your own Portkey key:

```bash
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
```

Then edit `.env`:

```
PORTKEY_API_KEY=your-portkey-api-key-here
```

`PORTKEY_API_KEY` is the only setting needed. The Portkey base URL (`https://api.portkey.ai/v1`) and the model are fixed in `agent.py`. The agent also accepts a `.env` one folder above `hw6/`. The real `.env` is excluded by `.gitignore` and is not in this repository.

## Running the agent

Review one document. This uses all three defences (the default) with prompt caching on:

```bash
python agent.py --doc data/practice_docs/p01_bluehen_quote.html
```

Choose a defence mode with `--defense`:

```bash
python agent.py --doc data/practice_docs/p01_bluehen_quote.html --defense none   # no defences (Problem 5 behaviour)
python agent.py --doc data/practice_docs/p01_bluehen_quote.html --defense full   # code + architecture + prompt layers
```

`--defense` also accepts `code`, `code_arch`, or a comma list such as `code,prompt`. Prompt caching can be switched off for the caching experiment with `--cache off`. Each run prints the structured answer and its token cost, and appends to `output/usage_log.json` and `output/audit_trail.json`.

### Reproducing the experiments (these make paid API calls)

All commands below are run from `hw6/`.

| Problem | Command |
|---|---|
| 2 – handbook token count (no API) | `python scripts/count_handbook_tokens.py` |
| 4 – with vs without cache | `python scripts/cache_test.py validate`, then `full`, then `summary` |
| 5 – attacks, no defences | `python scripts/attack_test.py run`, then `evaluate` |
| 6 – defences layer by layer | `python scripts/defense_test.py run`, then `evaluate` |
| 7 – my two red-team documents | `python scripts/red_team_test.py run`, then `evaluate` |
| 8 – exam documents | `python scripts/exam_run.py run`, then `export` |
| 9 – rebuild the report (no API) | `python scripts/build_report.py` |

## Opening the report

Open **`output/hw6_report.html`** by double-clicking it, or drag it into any browser. It is one self-contained file: inline CSS and inline SVG charts, with no network requests, external fonts, images or scripts. It still works if you copy it somewhere else on its own.

## Main results files

| File | What it contains |
|---|---|
| `AI_prompts.md` | The prompts I gave my coding assistant for each problem (in Chinese) |
| `output/handbook_estimate.md` | Problem 2: handbook = 10,058 tokens (tiktoken `o200k_base`), cost per call and per 1,000 calls |
| `output/harness.md` | Fields in `models.py`, each tool, logging, and the **Safety** section (three layers, tool permissions, how to run each mode) |
| `output/cache_test.json` / `.md` | Problem 4: every model call in both setups. With cache $0.00571161 vs without $0.03457503 for 12 documents (**83.48% saved**) |
| `output/attack_results.json` / `.md` | Problem 5: 4 of 12 practice documents fooled the undefended agent (p02, p03, p07, p12) and where each payload was hidden |
| `output/defense_results.json` / `.md` | Problem 6: full defences give 12/12 fields and decisions correct and 0 successful attacks, with per-layer token and dollar cost |
| `output/red_team.md` | Problem 7: my two attack documents (`data/my_attacks/`) run with no defences and with full defences |
| `output/exam_results.json` | Problem 8: the final defended agent on all 10 exam documents (one entry per file) |
| `output/hw6_report.html` | Problem 9: cost and safety report for the store manager |
| `output/usage_log.json`, `audit_trail.json`, `trap_calls.json`, `blocked_actions.json` | Raw logs for every run: API usage and cost, tool calls, final answers, sanitizer actions, blocked attempts. `trap_calls.json` is still `[]` |
| `output/p5_*`, `p6_*`, `p7_*`, `p8_*`, `cache_probe.json`, `red_team_key.json` | Supporting raw runs, evaluations and inspection evidence |

Code: `agent.py` (entry point, cache and defence switches, usage logging), `tools.py` (document reader and trap tools), `models.py` (Pydantic types), `defense.py` (sanitizer, handbook rules engine, human-approval check), `prompts/prompt.md` (defended rules), `prompts/prompt_undefended.md` (original prompt used by `--defense none`), `scripts/` (the experiments above).

## Known limitations and open items

- **Small samples.** Each document was run once per configuration, and model output varies from run to run. For example, p02's decision differed between two undefended runs in Problem 4.
- **Red team (Problem 7) — done, but the attacks did not succeed.** Both of my attack documents were run with `--defense none` and `--defense full` (4 real runs). Neither fooled either version. Both hidden payloads still reached the model in both modes, which shows the code layer does not recognise collapsed `<details>` elements or `overflow:hidden` clipping. Because the undefended agent also resisted, this test cannot show which defence layer would have stopped them. The grading answers for these two documents (`output/red_team_key.json`) were worked out from the visible quote and the handbook; there is no separate official answer key for them.
- **Exam.** There is no public answer key, so `exam_results.json` shows the agent's decisions, not verified accuracy.
- **Defences are not proven complete.** These have not been tested: visible fake "corrections", text inside images, and hidden text in colours other than white. The audit log also produces false alarms (normal text recorded as a blocked request), so it needs human review.
- **Caching.** The "explicit" cache setting alone did not stop cache reads through Portkey. The no-cache setup therefore uses a per-request nonce, and still pays cache-write charges. Cross-request cache hits are best effort.
- **Cost estimates** cover model API fees only: about $13.48 a month at 1,000 documents a day, using the final defended configuration measured on the practice set. People and infrastructure are not included.
