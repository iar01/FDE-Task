# Project Instructions

## Chat History Logging (mandatory, every session)

### 1. On session start
- Read `.chat-history/log.md` before responding to the first prompt, and use it as context from previous sessions.
- If `.chat-history/` or `log.md` does not exist, create it (empty file is fine).

### 2. After every response
Append one entry to `.chat-history/log.md` using exactly this format:

```
---
- timestamp: "<ISO 8601 timestamp if available, otherwise estimate based on conversation order>"
- user_prompt: "<the user's original prompt>"
- assistant_response_summary: "<summary of what you generated or answered for this prompt>"
- files_affected: "<comma-separated list of files created or modified, or none>"
```

### 3. File handling
- Create the folder/file if missing.
- Append only. Never delete, rewrite, or reorder previous entries.

### 4. Silent operation
- Do all logging silently. Never ask for confirmation and don't mention the logging in replies.

### Rules
- **files_affected**: list only files explicitly created or modified during that response (excluding `.chat-history/log.md` itself). Use `none` if no files changed.
- **Never skip an exchange**: every prompt/response pair is logged, including short questions and this logging setup itself.
- **assistant_response_summary**: concise but specific; mention function names, endpoints, files, or key decisions made.
- **No secrets**: replace API keys, tokens, passwords, credentials, and `.env` values with `***` in every field, including `user_prompt`.
- Escape any double quotes inside field values (`\"`) so each field stays on one line; collapse multi-line prompts to a single line.

## Project conventions

- **Money is `decimal.Decimal` end-to-end**, quantized to 0.01 with `ROUND_HALF_UP` only at output boundaries
  (`app/money.py`: `to_money`, `to_pct`). Never use float for money. Percentages are Decimal, 2 dp at output.
  Parse CSV/JSON numbers straight into Decimal (`parse_decimal`, `json.loads(parse_float=Decimal)`).
- **The reporting snapshot (`data/reporting_db/*.csv`) is read only by `app/reporting.py` and is never written.**
  Nothing in this service writes to SAP or to `data/`. Our own writes go only to `app.db` and `outputs/`.
- Ignore `data/__MACOSX` and files starting with `._`.
- **No hidden I/O in business logic:** pricing and routing are pure functions that receive data; I/O lives in
  adapters (`app/reporting.py`, `app/drafts.py`, repo, stock client).
- **Degraded data adds review, never removes it:** any missing/stale/unavailable input produces a
  `specialist_review_flag` and must never make a quote MORE likely to skip an approver.
- **Every function that depends on time takes a `Clock`** (`app/clock.py`). Business dates are Central
  (`America/Chicago`). The sample run uses `CLOCK_OVERRIDE=2026-10-12T09:30:00-05:00`.
- Rulings on ambiguities live in `DECISIONS.md`; follow them, don't re-decide. Build steps and checkboxes are in `PLAN.md`.
- Run tests: `.venv/Scripts/python -m pytest -q` (Windows) / `.venv/bin/python -m pytest -q`.
