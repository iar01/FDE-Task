# Reliability demo (CHALLENGE.md Part 3)

Every scenario uses a separate database and output folder (`--db demo.db --out-dir demo_out`, both gitignored),
so it never touches the real `app.db` or the deliverable `outputs/routing_results.json`.
All output below was captured from real runs on 2026-11-17.

**Windows gotcha: one mock per port.** Python's `HTTPServer` on Windows lets a second process bind a port that is
already in use, without any error. A "failing" mock started on 8080 while a healthy one is still running there
can silently lose to the healthy one. Use a separate port per mock (below: 8090 down, 8091 healthy) and point the
service at it with `STOCK_API_URL`. Always use `127.0.0.1`, not `localhost`.

**PowerShell `$env:` variables persist for the whole session.** After the demo, run
`Remove-Item Env:MOCK_FAILURE_RATE, Env:PORT, Env:STOCK_API_URL, Env:APP_DB_PATH -ErrorAction SilentlyContinue`
or open a new terminal, or the next "normal" run will still point at the failing mock or `demo.db`.

## 1. Stock API down: retries, then "unavailable", quote still routed

```powershell
# terminal 1
$env:MOCK_FAILURE_RATE="1"; $env:PORT="8090"; python data/mock_stock_api.py
# terminal 2
$env:STOCK_API_URL="http://127.0.0.1:8090"
python -m app.process data/draft_quotes.json --reset --db demo.db --out-dir demo_out -v
```

```bash
MOCK_FAILURE_RATE=1 PORT=8090 python data/mock_stock_api.py &
STOCK_API_URL=http://127.0.0.1:8090 python -m app.process data/draft_quotes.json --reset --db demo.db --out-dir demo_out -v
```

```
WARNING app.stock_client {"event": "stock_attempt", "parts": 30, "first_part": "RL104165", "attempt": 1, "max_attempts": 4, "outcome": 503, "delay_s": 2.0}
WARNING app.stock_client {"event": "stock_attempt", "parts": 30, "first_part": "RL104165", "attempt": 2, "max_attempts": 4, "outcome": 503, "delay_s": 2.0}
WARNING app.stock_client {"event": "stock_attempt", "parts": 30, "first_part": "RL104165", "attempt": 3, "max_attempts": 4, "outcome": 503, "delay_s": 2.0}
WARNING app.stock_client {"event": "stock_attempt", "parts": 30, "first_part": "RL104165", "attempt": 4, "max_attempts": 4, "outcome": 503, "delay_s": null, "final": "retries exhausted"}
processed 10 drafts (10 new, 0 already stored)
  specialist_only    3
  approver_required  6
  duplicate          1
```

`demo_out/routing_results.json` (approvers are the same as the normal run; every routed quote is flagged):

```
DQ-24101 specialist_only    None  ['stock unavailable after 4 attempts']
DQ-24103 approver_required  U05   ['stock unavailable after 4 attempts']
DQ-24105 specialist_only    None  ['extraction confidence 0.62', 'stock unavailable after 4 attempts']
DQ-24106 approver_required  U04   ['stock unavailable after 4 attempts']
DQ-24109 approver_required  U04   ['extraction confidence 0.79', 'stock unavailable after 4 attempts']
DQ-24110 duplicate          None  []
```

What to say: the delay is the 503 body's `retry_after_seconds` (2 s, capped at 5 s). After 4 attempts the
client never raises; each part is `unavailable`, and the quote is flagged and still routed, never blocked or guessed.
With no server at all, the attempts show `ConnectTimeout` and random (full-jitter) delays instead.

## 2. Reporting data a day old (stale vs fresh)

The export is from 2026-10-11 02:00. At the policy's sample time (10-12 09:30) it is 31.5 h old, under the 36 h
limit. Move the business clock to 15:00 the same day (so out-of-office is unchanged):

```bash
# healthy mock on its own port: MOCK_FAILURE_RATE=0 PORT=8091 python data/mock_stock_api.py
STOCK_API_URL=http://127.0.0.1:8091 python -m app.process data/draft_quotes.json --reset --db demo.db --out-dir demo_out --as-of 2026-10-12T15:00:00-05:00
```

Quote-level flags in `demo_out/routing_results.json`:

```
DQ-24101 specialist_only    None  ['pricing data is 37.0 h old (exported 2026-10-11T02:00:00-05:00)']
DQ-24103 approver_required  U05   ['pricing data is 37.0 h old (exported 2026-10-11T02:00:00-05:00)']
DQ-24106 approver_required  U04   ['pricing data is 37.0 h old (exported 2026-10-11T02:00:00-05:00)']
... (all 9 routed quotes; approvers unchanged)
DQ-24110 duplicate          None  []
```

Stale stock already shows in the normal run: RL107944 `as_of` 2026-10-10 is 49.5 h old, so DQ-24104 line 1 and
DQ-24109 line 6 get "stock reading 49.5 h old". Stale = we have a reading and flag it; unavailable = we have none.
Degraded data adds review but never removes an approver (`test_stale_pricing_adds_flag_never_removes_approver`).

## 3. Duplicate draft

DQ-24110 was resubmitted by the extraction service after a timeout. From `demo_out/routing_explanations.json`:

```
DQ-24110 trace: ['same rfq_id RFQ-1010 and same lines as DQ-24102, received 74 min earlier (window 24 h) -> duplicate, no approval request']
```

In `routing_results.json` it is `"status": "duplicate", "duplicate_of": "DQ-24102"`, with no approval request,
and its audit trail is `draft_received -> duplicate_detected`.

## 4. Roles, separation of duties, versions

```powershell
python -m app.process --reset --db demo.db --out-dir demo_out          # fresh state
$env:APP_DB_PATH="demo.db"; $env:CLOCK_OVERRIDE="2026-10-12T09:30:00-05:00"; uvicorn app.api:app --port 8001
python scripts/demo.py --base http://127.0.0.1:8001                     # another terminal
```

```
 U02 POST  /quotes/DQ-24106/approve {"version": 3}
      403 only the routed approver U04 may approve; U02 is out of office, so it was routed to backup U04
 U01 POST  /quotes/DQ-24106/approve {"version": 3}
      403 only the routed approver U04 may approve
 S03 POST  /quotes/DQ-24106/approve {"version": 3}
      403 separation of duties: specialist S03 cannot perform the approver step
 U04 POST  /quotes/DQ-24106/approve {"version": 2}
      409 you saw version 2; current is 3 (status pending_approver). Reload and review again.
 S02 POST  /quotes/DQ-24110/specialist-review {"version": 2}
      409 DQ-24110 is a duplicate of DQ-24102; it has no approval request
 U04 POST  /quotes/DQ-24106/approve {"version": 3, "comment": "approved as backup for U02"}
      200 status=pending_final_signoff version=4 approver=U04 specialist=S03
 S03 POST  /quotes/DQ-24106/final-signoff {"version": 4}
      200 status=approved version=5 approver=U04 specialist=S03
```

A malformed request is a denied attempt too, and it is audited without storing the raw input:

```
POST /quotes/DQ-24102/approve  X-User-Id: U01  {"version":"abc"}
-> 422 {"error":"invalid_request", ... "loc":["body","version"], "type":"int_parsing"}
audit #34 U01 approve_denied {"error":"invalid_request","errors":[...],"http_status":422}
```

## 5. Audit trail and tamper evidence

```
GET /audit?entity_id=DQ-24106
#11  system  draft_received       None                       -> received
#12  system  quote_routed         received                   -> pending_specialist_review
#25  S03     specialist_reviewed  pending_specialist_review  -> pending_approver
#26  U02     approve_denied       pending_approver           -> pending_approver
#27  U01     approve_denied       pending_approver           -> pending_approver
#28  S03     approve_denied       pending_approver           -> pending_approver
#29  U04     approve_denied       pending_approver           -> pending_approver
#31  U04     approver_approved    pending_approver           -> pending_final_signoff
#32  S03     final_signoff        pending_final_signoff      -> approved
#33  S03     edit_attempted_stub  approved                   -> approved

GET /audit/verify -> {"ok":true,"entries":34,"first_bad_seq":null,"problem":null}
```

Tampering: `pytest -q -k tampering` (2 passed). UPDATE/DELETE on `audit_log` are rejected by triggers; if an
attacker drops them and edits a row, `verify_audit_chain()` reports the exact `seq`.

Clean up afterwards: delete `demo.db` and `demo_out/` (both gitignored).
