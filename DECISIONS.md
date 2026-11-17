# Decisions and data notes

## 1. Data summary

### reporting_db (read-only nightly export from SAP)
| File | Columns |
|---|---|
| products.csv (200 rows) | part_number, description, category, unit, list_price, status (`active`/`discontinued`), replaced_by, primary_warehouse (DAL/PHX/ATL) |
| customers.csv (10) | customer_id, name, region (West/Central/East), pricing_tier (`contract`/`standard`) |
| contract_prices.csv (128) | customer_id, part_number, contract_price, valid_from, valid_to (dates) |
| approvers.csv (8) | approver_id, name, role (`regional_manager`, `sales_operations_director`, `vp_sales`, `quote_specialist`), region (or `ALL`), backup_approver_id, out_of_office_from, out_of_office_to |

export_metadata.json: `source`, `exported_at` = **2026-10-11T02:00:00-05:00**, `tables` (the 4 CSVs),
`note` ("Read-only. Refreshed nightly at 02:00 Central."). At the sample clock (2026-10-12T09:30-05:00) the
snapshot is **31.5 h old**: under the 36 h threshold, so not stale, but the 2026-10-12 02:00 refresh is missing.

Notable rows: RL150432 is `discontinued`, replaced_by RL120374. C1003/RL171482 contract is expired
(2025-01-01..2025-12-31). U02 (Central RM) is OOO 2026-10-12..2026-10-16, backup U04. U05 (VP) has no backup.

### draft_quotes.json
`{"drafts": [...]}`, 10 drafts. Draft: draft_id, rfq_id, customer_id, created_at (ISO, -05:00), prepared_by
(S01-S03), proposed_discount_pct (integer percent), lines[], optional `note`. Line: line, part_number,
description, quantity (int), unit, **extraction_confidence** (0-1, per line; there is no quote-level
confidence). Low values: DQ-24105 L3 0.62, DQ-24109 L3 0.79, DQ-24103 L4 0.88 (above 0.85), DQ-24105 L2 0.91.

### Stock API (stock_api.md + mock_stock_api.py)
- `GET /v1/health` -> `{"status":"ok"}`
- `GET /v1/stock/{part_number}` -> 200 `{"part_number","warehouses":{"DAL","PHX","ATL"},"as_of"}`; 404 `{"error":"not_found","part_number"}`
- `GET /v1/stock?part_numbers=A,B` (max 50; mock silently truncates beyond 50) -> 200 `{"items":[...],"not_found":[...]}`; 400 if empty
- Any endpoint: 503 `{"error":"service_unavailable","retry_after_seconds":2}` (in the body only, no Retry-After header). Unknown route 404 `{"error":"unknown_route"}`.
- `as_of` = when the reading was taken (can be stale). Fixture: all 08:00 2026-10-12 except **RL107944 as_of 2026-10-10T08:00** (49.5 h old). **RL120374 is absent** (404/not_found). **RL110322 is 0/0/0**.
- Run: `python data/mock_stock_api.py` (port 8080). Env: `PORT`, `MOCK_FAILURE_RATE` (default 0.1; 1 = always down,
  0 = never), `MOCK_LATENCY_MS` (default 150; set above the client timeout to simulate a slow API). No seed and no
  per-part failure control.

## 2. Approval policy
| Tier | Discount range | Net total | Approver role | How chosen | Backup / OOO | Source |
|---|---|---|---|---|---|---|
| Specialist only | 0 <= eff <= 15% (15 inclusive) | AND net < $25,000 (exclusive) | quote_specialist | the draft's specialist (see A1) | specialist backup_approver_id | "Effective discount 15% or less **and** net total under $25,000 \| Quote specialist only" |
| Regional manager | 15% < eff <= 25% (15 exclusive, 25 inclusive) | OR net >= $25,000 (inclusive) | regional_manager | customers.region = approvers.region | out today -> backup_approver_id (U01/U02/U03 -> U04) | "Effective discount over 15% up to 25%, **or** net total $25,000 or more \| Regional manager for the customer's region" |
| VP of Sales | eff > 25% (exclusive) | any | vp_sales (U05) | single, region ALL | no backup listed (see A2) | "Effective discount over 25% \| VP of Sales" |
| All quotes | n/a | n/a | quote specialist final sign-off | | | "A quote specialist gives final approval on every quote, even when no manager approval is needed." |
| Backup rule | n/a | n/a | n/a | | | "If the required approver is out of office today, route to their `backup_approver_id` (see `reporting_db/approvers.csv`)." |

Effective discount: "Effective discount for approval purposes = 1 - (quote net total / quote total at list price)."
Specialist-first triggers: "Any line with `extraction_confidence` below 0.85" (strict <); "Any part that is out of
stock in all warehouses, or not found in the stock API"; "Any stock reading older than 24 hours, or a reporting
database export older than 36 hours"; "If a contract price exists but has expired, use the list price and flag the line".
Duplicates: "same `rfq_id` and the same lines as a draft received in the last 24 hours ... Do not create a second approval request."

## 3. Ambiguities
All rulings below: **RESOLVED 2026-11-17. The user accepted the suggested default for every item.**
Items marked *(proposal)* are not in the policy. They are our additions and must be presented that way.

| # | Question | Options | Ruling (RESOLVED) | Reason |
|---|---|---|---|---|
| A1 | Which specialist reviews and gives final sign-off? | (a) prepared_by; (b) a different specialist (four-eyes); (c) any specialist | **RESOLVED:** prepared_by does review + final sign-off, falling back to their backup if OOO; any specialist may act if prepared_by is unavailable | All prepared_by IDs are quote_specialists with backups; policy names no separation-of-duties rule |
| A2 | VP (U05) out with no backup | (a) keep U05 + flag "no backup available" and escalate to ops; (b) route to U04; (c) approver_id null | **RESOLVED:** (a) keep U05, add quote-level flag, escalate | Never route to lower authority; never drop a required approver (architecture rule) |
| A3 | Rounding | per-line round vs round once; tiers on rounded vs unrounded | **RESOLVED:** exact Decimal line totals; apply discount and round net once (HALF_UP, cents); compare tiers on the **unrounded** effective discount; display 2 dp | Conservative: a 15.004% quote can't show as specialist-only. No sample draft changes under either choice (checked) |
| A4 | net_total / effective_discount_pct for a duplicate | null vs computed (= original's) | **RESOLVED:** computed values; approver_id null; flags [] | Brief: null only "when a value doesn't apply"; no approval request is created. Reaffirmed 2026-11-17 after the golden-file conflict: DQ-24110 = 132.46 / 18.00 |
| A5 | Stock below quantity but not zero | no flag (policy) vs flag total < qty vs flag per-warehouse shortfall | **RESOLVED** *(proposal)*: flag only when total across warehouses < quantity; no flag for a per-warehouse split | Only adds review. No sample draft has total < qty (DQ-24101/24105 RL104165: 30 needed, max single warehouse 28, total 53) |
| A6 | Discontinued part requested | treat as normal; flag + suggest replaced_by; auto-substitute | **RESOLVED:** flag line "discontinued, replaced by X", price as listed, no auto-substitution | Never change a price or part without a person. Not in the sample: DQ-24103 already asks for the replacement RL120374 |
| A7 | Product missing from products.csv | drop line; price 0; flag | **RESOLVED:** flag the line, leave it out of totals, add quote-level "pricing incomplete"; routing still computed and not lowered | Missing data adds review only |
| A8 | Customer missing | reject; flag | **RESOLVED:** flag the quote "customer not found": list pricing, region unknown; if a regional manager is required, approver_id null + flag | No region means we can't pick a manager; don't guess |
| A9 | Escalation when an approver doesn't respond | none (policy silent); SLA -> backup; SLA -> notify | **RESOLVED** *(proposal)*: `escalation_sla_hours` in YAML (e.g. 2 h, below the 4-6 h pain point) -> audit event + reassign to backup_approver_id; disabled-by-default switch | Not in policy; labeled as a proposal |
| A10 | Backup of a backup (U04 also out) | one hop only vs follow chain | **RESOLVED:** follow chain with cycle detection; never land on a lower-authority role; flag when exhausted | Policy says "route to their backup", silent on chains |
| A11 | Duplicate "same lines" definition and 24 h reference point | include description/confidence? order? | **RESOLVED:** multiset of (part_number, quantity, unit); window measured between the two drafts' created_at; earliest is the original | Extraction text/confidence can differ on resubmission |
| A12 | Reporting export 31.5 h old, nightly refresh missed | flag vs not | **RESOLVED:** no flag (under 36 h), but show `reporting_age_hours` in logs/API | Policy threshold is explicit |
| A13 | Discount over 25% AND net >= $25k | RM or VP | **RESOLVED:** VP (highest required tier wins) | Higher authority covers lower |
| A14 | Out-of-office dates | inclusive? | **RESOLVED:** inclusive both ends, compared on the Central calendar date | Natural reading |
| A15 | customers.pricing_tier "standard" vs contract rows | ignore tier vs gate contracts | **RESOLVED:** contract_prices rows decide; tier informational | Pricing rule mentions only contract rows |
| A16 | Multiple valid contract rows for one customer/part | lowest vs latest | **RESOLVED:** latest valid_from + flag conflict (none in sample) | Avoid silently choosing the cheaper price |
| A17 | U04 (sales_operations_director) not in policy | n/a | **RESOLVED:** treated as a valid approver only as a backup | Comes from approvers.csv |

## 4. Expected outcome per draft (unverified)
Clock 2026-10-12T09:30-05:00, mock failure rate 0.
| Draft | Cust/region | List total | Net total | Eff % | Status | Approver | Flags | duplicate_of |
|---|---|---|---|---|---|---|---|---|
| DQ-24101 | C1002 West | 473.08 | 435.23 | 8.00 | specialist_only | null | [] | null |
| DQ-24102 | C1008 West | 161.54 | 132.46 | 18.00 | approver_required | U01 | [] | null |
| DQ-24103 | C1007 Central | 987.82 | 728.80 | 26.22 | approver_required | U05 | L3 RL120374 not found in stock API | null |
| DQ-24104 | C1005 East | 3130.00 | 2191.00 | 30.00 | approver_required | U05 | L1 RL107944 stock reading 49.5 h old | null |
| DQ-24105 | C1004 Central | 496.00 | 471.20 | 5.00 | specialist_only | null | L3 confidence 0.62 | null |
| DQ-24106 | C1001 Central | 50160.00 | 47652.00 | 5.00 | approver_required (net >= 25k) | U04 (U02 OOO) | [] | null |
| DQ-24107 | C1003 West | 539.50 | 456.54 | 15.38 | approver_required | U01 | L1 RL171482 contract expired 2025-12-31, list used | null |
| DQ-24108 | C1009 East | 1686.00 | 1517.40 | 10.00 | specialist_only | null | L1 RL110322 out of stock in all warehouses | null |
| DQ-24109 | C1010 Central | 1727.00 | 1409.86 | 18.36 | approver_required | U04 (U02 OOO) | L3 confidence 0.79; L6 RL107944 stock 49.5 h old | null |
| DQ-24110 | C1008 West | 161.54 | 132.46 | 18.00 | duplicate | null | [] | DQ-24102 |

Contract prices used: DQ-24103 L1 RL178907 9.53, L4 RL194646 31.45; DQ-24107 L3 RL159429 39.10;
DQ-24109 L2 RL179507 22.14. Because of contract prices, DQ-24103 (proposed 22%) and DQ-24107 (proposed 12%)
land in higher tiers than their proposed discount suggests.
