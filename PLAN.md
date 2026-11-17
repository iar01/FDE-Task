# Ridgeline Quote Approvals: Build Plan

## Architecture decisions
- Python 3.11, FastAPI, Pydantic v2, httpx, pytest, PyYAML. Our own state (quotes, approvals, audit)
  in SQLite app.db.
- The reporting snapshot is data/reporting_db/*.csv, loaded read-only. Its age comes from
  export_metadata.json. Nothing ever writes to data/ or to SAP.
- Stock comes only from the stock API (mock_stock_api.py locally), through a retrying client.
- Money is decimal.Decimal; pricing and routing are pure functions; time comes from an injectable Clock.
  For the sample run the clock is 2026-10-12T09:30:00-05:00, as approval_rules.md specifies.
- Policy (thresholds, tiers, low-confidence threshold, escalation SLA) lives in
  rules/approval_rules.yaml. People (approvers, regions, backups, availability) come from the data
  files (approvers.csv and/or approval_rules.md). Do not duplicate them into the YAML.
- Missing, stale or unavailable data adds specialist review; it never removes a required approver.
- Nothing reaches "approved" without a human actor. We never send quotes; "approved" = ready to send.

(Step 1 = this planning step: PLAN.md + DECISIONS.md.)

## Build steps

### [x] Step 2  Scaffold, CLAUDE.md conventions, reporting loader, pricing
- **Files:** `pyproject.toml` (or `requirements.txt`), `app/__init__.py`, `app/config.py` (env settings),
  `app/clock.py` (Clock protocol, SystemClock, FixedClock), `app/models.py` (Pydantic: DraftQuote, DraftLine,
  Product, Customer, ContractPrice, Approver, PricedLine, PricedQuote), `app/reporting.py` (read-only CSV loader
  into an immutable ReportingSnapshot with `exported_at` and `age(clock)`), `app/pricing.py` (pure
  `price_quote(draft, snapshot, today) -> PricedQuote`). Add a "Conventions" section to the existing
  `CLAUDE.md`, keeping the logging rules already there.
- **Reads:** reporting_db/products.csv, customers.csv, contract_prices.csv, export_metadata.json; draft_quotes.json.
- **Acceptance:**
  - Contract price used only when valid_from <= today <= valid_to; expired contract -> list price + line flag
    (AC: "Pricing comes from the reporting database").
  - Effective discount = 1 - net/list, Decimal throughout; net_total 2 dp, effective_discount_pct 2 dp
    (AC: "routing_results.json follows the format in Deliverable 1").
  - Loader opens files read-only; no write path to data/ (AC: "nothing writes to SAP").
- **Main risk:** float leakage or rounding in the wrong place. A boundary quote (15.00 vs 15.004) could then
  route to the wrong tier.

### [x] Step 3  Stock client with timeout, retries, backoff; stale vs unavailable
- **Files:** `app/stock_client.py` (httpx client: per-attempt timeout, max attempts, exponential backoff + jitter,
  honours body `retry_after_seconds`, injectable sleep; uses batch `GET /v1/stock?part_numbers=` in chunks of 50).
  Returns a per-part `StockResult` with status `ok | out_of_stock | not_found | stale | unavailable`.
- **Reads:** stock_api.md (contract); mock_stock_api.py + stock_fixture.json at runtime only.
- **Acceptance:**
  - 503 / timeout / connection error retried; 404 / not_found not retried
    (AC: "stock API calls are retried before being marked unavailable").
  - Retries exhausted -> quote-level flag "stock unavailable after retries", quote still routed, never blocked or
    guessed (AC: "Failures of the stock API or stale data are handled visibly").
  - `as_of` older than 24 h -> stale flag; all warehouses 0 -> out of stock flag; not in API -> not-found flag
    (AC: "stock comes from the API").
- **Main risk:** long total latency or hung requests. Bound it with timeout × attempts plus a cap on backoff.
  The mock fails 10% of calls at random by default, so the sample run is nondeterministic unless
  MOCK_FAILURE_RATE=0.

### [x] Step 4  Rules config + validation, pure router, backup chain, SLA escalation
- **Files:** `rules/approval_rules.yaml` (tiers with explicit bounds, net-total threshold 25000,
  low-confidence threshold 0.85, stock max age 24 h, reporting max age 36 h, duplicate window 24 h,
  escalation SLA marked `proposal: true`), `app/rules.py` (Pydantic schema + validation: tiers contiguous,
  non-overlapping, roles exist in approvers.csv), `app/router.py` (pure
  `route(priced, flags, rules, people, today) -> RoutingDecision`; backup chain with cycle detection;
  OOO dates inclusive), `app/escalation.py` (pure `escalation_due(pending_since, now, sla)`).
- **Reads:** reporting_db/approvers.csv, customers.csv (region); approval_rules.md (source of the YAML values).
- **Acceptance:**
  - Each tier routes correctly, including boundaries (AC: "Approval rules are configurable and covered by tests").
  - Out-of-office approver -> backup_approver_id (AC: "...the backup approver").
  - Changing the YAML changes routing with no code change (Part 4.3).
  - Flags never lower the tier (architecture rule).
- **Main risk:** misreading the bounds ("15% or less" vs "over 15%"). Also, an approver with no backup
  (U05) silently produces approver_id = null.

### [x] Step 5  Pipeline, duplicate detection, store, hash-chained audit, outputs/routing_results.json
- **Files:** `app/duplicates.py` (same rfq_id + same multiset of (part_number, quantity, unit) within 24 h of
  an earlier draft), `app/store.py` (SQLite app.db: quotes, quote_versions, approvals, audit_events),
  `app/audit.py` (append-only, `hash = sha256(prev_hash + canonical_json(event))`, `verify_chain()`),
  `app/pipeline.py` (load -> dedupe -> price -> stock -> flags -> route -> persist -> audit),
  `app/process.py` (CLI `python -m app.process` writes `outputs/routing_results.json`).
- **Reads:** draft_quotes.json, all reporting_db files, stock API.
- **Acceptance:**
  - 10 objects in draft order with exactly the Deliverable 1 keys (AC: "routing_results.json follows the format").
  - DQ-24110 -> status duplicate, duplicate_of DQ-24102, no approval request (AC: "...and a duplicate").
  - Every ingest/route event is audited with actor (`system:pipeline`), before/after status, timestamp
    (AC: "Every action is recorded in an audit log").
  - Re-running is idempotent (no second approval request).
- **Main risk:** output schema drift from the brief, e.g. Decimal serialized as a string or extra keys.

### [x] Step 6  Review API, roles, state machine, version checks (quote editing = 501 stub)
- **Files:** `app/api.py` (FastAPI): `GET /quotes`, `GET /quotes/{id}`, `POST /quotes/{id}/approve`,
  `POST /quotes/{id}/reject`, `PATCH /quotes/{id}` -> 501 with explanation, `GET /quotes/{id}/audit`,
  `GET /audit/verify`. `app/auth.py` (X-User-Id header -> approvers.csv role). `app/state.py` (state machine).
- **States:** `pending_specialist` -> (`specialist_only`: `approved`) | (`approver_required`: `pending_approver`
  -> `approved`) ; any pending -> `rejected`. Duplicates are terminal `duplicate`.
- **Reads:** approvers.csv; app.db.
- **Acceptance:**
  - Only a quote specialist can do the specialist step; only the routed approver (after backup) can do the
    approver step; others get 403 (Part 3.3).
  - Approve/reject require `expected_version`; a stale version -> 409, so the same step can't be approved twice
    (Part 3.2 stub).
  - `approved` reachable only through a human action (AC: "No quote reaches a customer without human approval").
  - Every transition audited with before/after (AC: "Every action is recorded in an audit log").
- **Main risk:** role bypass, e.g. the approver also signing as specialist, or the header trusted without a
  lookup in approvers.csv.

### [x] Step 7  Reliability demo (stock down, stale data, duplicate, roles)
- **Files:** `scripts/demo.sh` or `scripts/demo.py`, `docs/demo.md` (commands + expected output).
- **Scenarios:** (a) `MOCK_FAILURE_RATE=1` -> "stock unavailable after retries" flags, quotes still routed;
  (b) stale data: stock RL107944 (as_of 2026-10-10) already stale; reporting staleness shown by advancing the
  Clock (e.g. `CLOCK_OVERRIDE=2026-10-12T15:00:00-05:00` -> export 37 h old), never by editing data/;
  (c) duplicate DQ-24110; (d) roles: wrong user 403, specialist then manager approves, stale version 409.
- **Reads:** everything, via the service.
- **Acceptance:** AC "Failures of the stock API or stale data are handled visibly"; Part 3.1-3.3 shown.
- **Main risk:** a flaky or slow demo eats recording time. Script it and dry-run it first.

### [x] Step 8  Tests
- **Files:** `tests/test_pricing.py`, `tests/test_router.py` (each tier + boundaries 15.00/15.01/25.00/25.01 and
  net 24999.99/25000.00), `tests/test_backup.py` (U02 OOO -> U04; chain; VP with no backup),
  `tests/test_duplicates.py`, `tests/test_stock_client.py` (httpx.MockTransport: 503×N then 200; exhausted ->
  unavailable; timeout; 404 not retried), `tests/test_api_roles.py`, `tests/test_audit_chain.py`,
  `tests/test_pipeline_golden.py` (sample -> expected routing_results with a deterministic fake stock client).
- **Reads:** sample data (read-only) + fixtures.
- **Acceptance:** `pytest` green; covers each tier, backup, duplicate (AC: "covered by tests (at least each tier,
  the backup approver and a duplicate)").
- **Main risk:** tests depending on the random mock. Use MockTransport or a fake client, never a live mock.

### [x] Step 9  README (exact headings) + Mermaid production diagram + .env.example
- **Files:** `README.md` (exactly: `# Ridgeline Quote Approvals`, `## Repository`, `## How to Run`, `## Approach`,
  `## Reliability`, `## Production`), Mermaid diagram inside Production, `.env.example` (names only:
  STOCK_API_URL, STOCK_TIMEOUT_SECONDS, STOCK_MAX_ATTEMPTS, STOCK_BACKOFF_BASE_SECONDS, APP_DB_PATH,
  REPORTING_DIR, RULES_PATH, CLOCK_OVERRIDE), `.gitignore` check (.env, app.db).
- **Reads:** nothing new.
- **Acceptance:** AC "A production diagram is shown"; AC "zip contains every path ... and no .env";
  Deliverable 3 headings exact; Part 4.1-4.4 bullets.
- **Main risk:** heading text or order drift, or a secret committed.

## Brief coverage
| Brief item | Step |
|---|---|
| Part 1: say plan + stubs before coding | USER ACTION – pending (recording; script: PLAN.md + DECISIONS.md) |
| 2.1 Contract pricing from reporting DB | 2 |
| 2.1 Stock check through API | 3 |
| 2.1 Handle stale/unavailable data clearly | 3, 5 |
| 2.2 Configurable rules | 4 |
| 2.2 Specialist-only under threshold; regional manager / VP above | 4 |
| 2.2 Backup approver when out | 4 |
| 2.2 Low-confidence lines -> specialist first | 4, 5 |
| 2.3 API: list quotes, approve/reject (specialist or manager) | 6 |
| 2.3 Editing stub + explain reprice/reroute | 6 (501), 9 |
| 2.4 Audit who/what/when/before/after | 5, 6 |
| 2 "Explain what you'd use in production" for store | 9 |
| 3.1 Stock down / data a day old | 3, 7 |
| 3.1 Retries with timeout + backoff; exhausted -> unavailable | 3 |
| 3.2 Duplicate detection (`duplicate_of`) | 5 |
| 3.2 Double approvals / lost quotes (stub, explain) | 6 (version check), 9 |
| 3.3 Role-based access via header; explain production | 6, 9 |
| 3.4 Tests: each tier, backup, duplicate | 8 |
| 4.1 Deployment + connection without writing to SAP | 9 |
| 4.2 Monitoring incl. time from draft to approval | 9 (metric derivable from audit, Step 5/6) |
| 4.3 Change rules without code release | 4 (YAML), 9 |
| 4.4 Two more weeks | 9 |
| Part 5 demo on sample drafts | 5, 7 |
| Deliverable README.md (exact headings) | 9 |
| Deliverable .chat-history/ | ongoing (CLAUDE.md logging); verified by `scripts/check_submission.py --zip` |
| Deliverable outputs/routing_results.json | 5 |
| Deliverable tests + command in How to Run | 8, 9 |
| .env.example (names only) | 9 |
| Repository: public GitHub link | USER ACTION – pending (create repo, push, fill README `## Repository`) |
| Zip ridgeline-quote-approvals.zip, no .env | `scripts/check_submission.py --zip` builds + verifies it; USER ACTION – pending (run it after the final commit) |
| AC: recording opens with plan, ends with demo | USER ACTION – pending (recording; demo script: docs/demo.md + scripts/demo.py) |
| AC: rules configurable + tested | 4, 8 |
| AC: pricing from reporting DB, stock from API, no SAP writes | 2, 3 |
| AC: every action audited | 5, 6 |
| AC: stock failure / stale handled visibly, retried | 3, 7 |
| AC: every stub explained in recording | USER ACTION – pending (recording; Stubs table below is the script) |
| AC: production diagram shown | 9 |
| AC: no quote reaches customer without human approval | 6 |
| AC: routing_results.json format | 5, 8 |
| AC: zip has all deliverables, no .env | 9 + `scripts/check_submission.py --zip` |

## Submission checklist (user actions)
- [ ] Record: plan first (Part 1), stubs explained, demo last (`docs/demo.md`, `scripts/demo.py`, `python -m app.process`)
- [ ] Create the public GitHub repo and fill in README `## Repository`
- [x] README "The one decision that matters most" written
- [ ] Re-run `python -m app.process --reset` with the mock running; `outputs/routing_results.json` has no "stock unavailable" flags
- [ ] Commit and push (`git branch -m main` first if the remote expects `main`)
- [ ] `python scripts/check_submission.py --zip` -> no FAIL; submit `ridgeline-quote-approvals.zip` + MP4 link

## Stubs
| Stubbed | Why | Production version |
|---|---|---|
| Quote editing (`PATCH` -> 501) | Brief allows it; time | Edit creates new quote_version, reprices via `price_quote`, re-runs stock + router, resets approvals that no longer apply, audits field-level before/after |
| Sending quotes | Out of scope; "approved" = ready to send | Outbox table -> CRM/email integration, idempotent send, still no SAP write |
| Auth (X-User-Id header) | Brief allows simple check | SSO/OIDC (Entra ID), JWT roles from IdP groups, approvers.csv reconciled from HR/SAP |
| Stock API (local mock) | Real API owned by Ridgeline IT | Real hourly API via private network, circuit breaker, cached last reading with as_of |
| Reporting DB (CSV files) | Data pack format | Read-only replica / warehouse connection with read-only credentials; freshness from export metadata |
| Escalation (computed + audited, no notification) | SLA not in policy (proposal) | Scheduler + Teams/email notifications, escalation to backup |
| Double-approval / lost-quote prevention | Brief allows explanation | Optimistic version check (built) + DB unique constraints, transactional outbox, queue with DLQ, reconciliation job |
| Rule changes | File edit + reload | Rules in DB/config service with admin UI, validation, four-eyes approval, versioned + audited, effective dates |
| SQLite app.db | Simple store allowed | Managed Postgres, backups, PITR |
| Audit hash chain in SQLite | Tamper-evidence demo | Append-only/WORM store (e.g. immutable object storage or ledger table), periodic anchoring |
