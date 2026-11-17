"""Step 8 acceptance tests for the approval rules (CHALLENGE.md: each tier, backup approver, duplicate).

Router tests are table-driven with a fixed Clock and the in-memory POLICY from tests/factories.py;
test_real_yaml_matches_in_memory_policy is the one test that loads rules/approval_rules.yaml.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.directory import Directory
from app.drafts import load_drafts
from app.models import DraftQuote
from app.pipeline import Pipeline
from app.pricing import price_quote
from app.process import main as process_main
from app.reporting import load_snapshot
from app.router import route
from app.rules import RulesError, load_rules, validate_against_directory
from app.stock_client import StockClient
from app.stock_flags import stock_flags
from app.store import Store
from tests.factories import CLOCK, OOO, POLICY, ROOT, RULES, A, fixture_stock, flag_codes, people, priced
from tests.test_api import client, step, version  # noqa: F401  (client is a pytest fixture)

DRAFTS = ROOT / "data" / "draft_quotes.json"


@pytest.fixture(scope="module")
def snapshot():
    return load_snapshot(ROOT / "data" / "reporting_db")


@pytest.fixture(scope="module")
def directory(snapshot):
    return Directory.from_snapshot(snapshot)


@pytest.fixture(scope="module")
def sample(snapshot):
    return {d.draft_id: d for d in load_drafts(DRAFTS)}


def go(quote, directory=None, clock=CLOCK):
    return route(quote, RULES, directory or people(), clock)


def test_real_yaml_matches_in_memory_policy(directory):
    """The one test that loads the real YAML: it must encode exactly the policy the tests use."""
    loaded = load_rules(ROOT / "rules" / "approval_rules.yaml", directory)
    assert loaded.rules.model_copy(update={"version": POLICY.version}) == POLICY
    assert loaded.version == "2026-10-12.1" and len(loaded.content_hash) == 64


# ---- tiers -------------------------------------------------------------------------------------

@pytest.mark.parametrize("eff, net", [("0", "0.01"), ("8.00", "435.23"), ("14.99", "24999.99"), ("15", "1000")])
def test_tier_specialist_only(eff, net):
    d = go(priced(eff=eff, net=net))
    assert (d.status, d.tier, d.approver_id, d.specialist_id) == ("specialist_only", "specialist_only", None, "S1")


@pytest.mark.parametrize("eff, region, approver", [
    ("15.01", "West", "RW"), ("18.00", "West", "RW"), ("18.36", "Central", "RC"), ("25", "Central", "RC"),
])
def test_tier_regional_manager(eff, region, approver):
    d = go(priced(eff=eff, region=region))
    assert (d.status, d.tier, d.approver_id) == ("approver_required", "regional_manager", approver)


@pytest.mark.parametrize("eff, net", [("25.01", "100"), ("26.22", "728.80"), ("30", "90000")])
def test_tier_vp(eff, net):
    d = go(priced(eff=eff, net=net))
    assert (d.status, d.tier, d.approver_id) == ("approver_required", "vp", "VP")


@pytest.mark.parametrize("eff, tier", [
    ("15.00", "specialist_only"),        # "15% or less"
    ("15.00001", "regional_manager"),    # unrounded comparison (DECISIONS.md A3)
    ("25.00", "regional_manager"),       # "over 15% up to 25%"
    ("25.00001", "vp"),                  # "over 25%"
])
def test_boundary_exactly_at_threshold(eff, tier):
    assert go(priced(eff=eff, net="1000")).tier == tier


@pytest.mark.parametrize("net, tier", [
    ("24999.99", "specialist_only"), ("25000.00", "regional_manager"), ("47652.00", "regional_manager"),
])
def test_manager_required_by_net_total(net, tier):
    d = go(priced(eff="5", net=net, region="Central"))
    assert d.tier == tier
    if tier == "regional_manager":
        assert d.approver_id == "RC" and d.trace[0] == f"net {net} >= 25000 -> tier regional_manager (Central) -> RC"


def test_discount_measured_against_list_price(snapshot, directory, sample):
    pq = price_quote(sample["DQ-24103"], snapshot, CLOCK, reporting_max_age_hours=Decimal(36))
    assert pq.proposed_discount_pct == 22
    assert [l.price_source for l in pq.lines if l.price_source == "contract"] == ["contract", "contract"]
    assert pq.effective_discount_pct > 25 and pq.effective_discount_pct_out == Decimal("26.22")
    d = route(pq, RULES, directory, CLOCK)
    assert (d.tier, d.approver_id) == ("vp", "U05")


def test_expired_contract_uses_list_price_and_flags(snapshot, sample):
    pq = price_quote(sample["DQ-24107"], snapshot, CLOCK, reporting_max_age_hours=Decimal(36))
    line1 = pq.lines[0]
    assert (line1.part_number, line1.price_source, line1.unit_price) == ("RL171482", "list", Decimal("8.40"))
    assert [(f.line, f.reason) for f in pq.flags] == [(1, "contract price expired 2025-12-31; list price used")]


# ---- backup approver ---------------------------------------------------------------------------

def test_backup_when_approver_out_of_office():
    d = go(priced(eff="18", region="Central"), people(RC=A("RC", "regional_manager", "Central", "DIR", OOO)))
    assert (d.approver_id, d.routed_via_backup_of) == ("DIR", "RC")
    assert "RC out of office 2026-10-12..2026-10-16 -> backup DIR" in d.trace
    assert d.specialist_review_flags == ()


def test_backup_chain_when_backup_also_out():
    d = go(priced(eff="18", region="Central"), people(
        RC=A("RC", "regional_manager", "Central", "DIR", OOO),
        DIR=A("DIR", "sales_operations_director", "ALL", "VP", OOO)))
    assert (d.approver_id, d.routed_via_backup_of) == ("VP", "RC")
    assert d.trace[1:3] == ("RC out of office 2026-10-12..2026-10-16 -> backup DIR",
                            "DIR out of office 2026-10-12..2026-10-16 -> backup VP")


def test_backup_cycle_detected():
    looping = people(RC=A("RC", "regional_manager", "Central", "DIR", OOO),
                     DIR=A("DIR", "sales_operations_director", "ALL", "RC", OOO))
    # Refused at start-up ...
    with pytest.raises(RulesError, match="backup cycle"):
        validate_against_directory(POLICY, looping)
    # ... and if it slips through, routing stops instead of looping, and flags it.
    d = go(priced(eff="18", region="Central"), looping)
    assert d.approver_id == "DIR"
    assert flag_codes(d.specialist_review_flags) == [(None, "no_backup_available")]


def test_vp_out_no_backup_keeps_vp_and_flags():
    d = go(priced(eff="30"), people(VP=A("VP", "vp_sales", ooo=OOO)))
    assert (d.status, d.approver_id) == ("approver_required", "VP")
    assert [(f.line, f.reason) for f in d.specialist_review_flags] == [
        (None, "required approver VP out of office, no backup available")]


# ---- specialist flags --------------------------------------------------------------------------

@pytest.mark.parametrize("confidence, flagged", [("0.62", True), ("0.84", True), ("0.85", False), ("0.97", False)])
def test_low_confidence_line_flags_specialist(confidence, flagged):
    d = go(priced(eff="8", confidences=("0.97", confidence)))
    assert d.status == "specialist_only"  # a flag adds review; it never changes the tier
    expected = [(2, f"extraction confidence {confidence}")] if flagged else []
    assert [(f.line, f.reason) for f in d.specialist_review_flags] == expected


# ---- duplicates (rule 11), through the pipeline ------------------------------------------------

def _draft(draft_id, created, lines, rfq="RFQ-9001"):
    return DraftQuote.model_validate({
        "draft_id": draft_id, "rfq_id": rfq, "customer_id": "C1002", "created_at": created, "prepared_by": "S01",
        "proposed_discount_pct": 8,
        "lines": [{"line": i, "part_number": pn, "description": "x", "quantity": q, "unit": "EA",
                   "extraction_confidence": "0.97"} for i, (pn, q) in enumerate(lines, start=1)]})


@pytest.fixture
def pipeline(snapshot, directory, tmp_path):
    with Store(tmp_path / "app.db", audit_clock=CLOCK) as store, fixture_stock() as stock:
        yield Pipeline(snapshot, directory, RULES, stock, store, CLOCK)


ORIGINAL = _draft("DQ-A", "2026-10-12T08:00:00-05:00", [("RL104165", 30), ("RL134671", 8)])


def test_duplicate_detected_with_duplicate_of(pipeline):
    resubmitted = _draft("DQ-B", "2026-10-12T09:15:00-05:00", [("rl134671", 8), ("RL104165", 30)])
    first, dup = pipeline.process([ORIGINAL, resubmitted])
    assert (dup.status, dup.duplicate_of, dup.approver_id, dup.flags) == ("duplicate", "DQ-A", None, ())
    assert (dup.net_total, dup.effective_discount_pct) == (first.net_total, first.effective_discount_pct)
    assert pipeline.store.get_quote("DQ-B").status == "duplicate"


def test_same_rfq_different_lines_not_duplicate(pipeline):
    other = _draft("DQ-B", "2026-10-12T09:15:00-05:00", [("RL104165", 30), ("RL134671", 9)])
    _, res = pipeline.process([ORIGINAL, other])
    assert res.status != "duplicate" and res.duplicate_of is None


def test_same_lines_after_24h_not_duplicate(pipeline):
    later = _draft("DQ-B", "2026-10-13T08:00:01-05:00", [("RL104165", 30), ("RL134671", 8)])
    _, res = pipeline.process([ORIGINAL, later])
    assert res.status != "duplicate" and res.duplicate_of is None


# ---- stock API ---------------------------------------------------------------------------------

def _client(responses):
    sleeps, requests = [], []

    def handler(request):
        requests.append(request)
        code, body = responses.pop(0)
        return httpx.Response(code, json=body)
    c = StockClient("http://stock.test", CLOCK, max_attempts=4, max_age_hours=Decimal(24),
                    sleep=sleeps.append, rand=lambda a, b: pytest.fail("jitter not expected"),
                    transport=httpx.MockTransport(handler))
    return c, sleeps, requests


BUSY = (503, {"error": "service_unavailable", "retry_after_seconds": 2})
LINE = _draft("DQ-S", "2026-10-12T08:00:00-05:00", [("RL1", 1)])


def item(pn, dal=5, as_of="2026-10-12T08:00:00-05:00"):
    return {"part_number": pn, "warehouses": {"DAL": dal, "PHX": 0, "ATL": 0}, "as_of": as_of}


def test_stock_retries_then_unavailable():
    c, sleeps, requests = _client([BUSY] * 4)
    started = time.perf_counter()
    res = c.fetch(["RL1"])
    assert time.perf_counter() - started < 1.0          # 3 x 2 s were recorded, not slept
    assert len(requests) == 4 and res["RL1"].attempts == 4 and res["RL1"].status == "unavailable"
    assert sleeps == [2.0, 2.0, 2.0]                      # retry_after_seconds from the 503 body
    assert [(f.line, f.reason) for f in stock_flags(LINE, res)] == [(None, "stock unavailable after 4 attempts")]


def test_stock_recovers_after_one_503():
    c, sleeps, requests = _client([BUSY, (200, {"items": [item("RL1")], "not_found": []})])
    r = c.fetch(["RL1"])["RL1"]
    assert (r.status, r.attempts, r.total_on_hand, sleeps, len(requests)) == ("ok", 2, 5, [2.0], 2)
    assert stock_flags(LINE, {"RL1": r}) == ()


def test_stock_404_not_retried():
    c, sleeps, requests = _client([(404, {"error": "not_found", "part_number": "RL1"})])
    assert c.fetch(["RL1"])["RL1"].status == "unavailable"
    assert len(requests) == 1 and sleeps == []
    # A part the API reports as not found is final too: one call, line flagged.
    c, sleeps, requests = _client([(200, {"items": [], "not_found": ["RL1"]})])
    res = c.fetch(["RL1"])
    assert (res["RL1"].status, len(requests), sleeps) == ("not_found", 1, [])
    assert [(f.line, f.reason) for f in stock_flags(LINE, res)] == [(1, "part not found in stock API")]


@pytest.mark.parametrize("as_of, stale", [
    ("2026-10-10T08:00:00-05:00", True),   # 49.5 h (RL107944 in the fixture)
    ("2026-10-11T09:29:00-05:00", True),   # 24 h 1 min
    ("2026-10-11T09:30:00-05:00", False),  # exactly 24 h: "older than 24 hours" is strict
])
def test_stale_stock_over_24h_flags_line(as_of, stale):
    c, _, _ = _client([(200, {"items": [item("RL1", as_of=as_of)], "not_found": []})])
    res = c.fetch(["RL1"])
    reasons = [f.reason for f in stock_flags(LINE, res)]
    assert (res["RL1"].status == "stale") is stale
    assert any(r.startswith("stock reading ") and f"(as_of {as_of})" in r for r in reasons) is stale


def test_out_of_stock_all_warehouses_flags_line():
    c, _, _ = _client([(200, {"items": [item("RL1", dal=0)], "not_found": []})])
    assert [(f.line, f.reason) for f in stock_flags(LINE, c.fetch(["RL1"]))] == [
        (1, "out of stock in all warehouses")]


# ---- degraded pricing data ---------------------------------------------------------------------

def test_stale_pricing_adds_flag_never_removes_approver(snapshot, directory, sample):
    fresh = CLOCK                                                            # export 31.5 h old
    stale = type(CLOCK)(datetime.fromisoformat("2026-10-12T15:00:00-05:00"))  # 37 h old, same day
    with fixture_stock(fresh) as s1, fixture_stock(stale) as s2:
        stock_fresh = s1.fetch(l.part_number for d in sample.values() for l in d.lines)
        stock_stale = s2.fetch(l.part_number for d in sample.values() for l in d.lines)
    for d in sample.values():
        a = route(price_quote(d, snapshot, fresh, reporting_max_age_hours=Decimal(36)), RULES, directory,
                  fresh, extra_flags=stock_flags(d, stock_fresh))
        b = route(price_quote(d, snapshot, stale, reporting_max_age_hours=Decimal(36)), RULES, directory,
                  stale, extra_flags=stock_flags(d, stock_stale))
        assert (b.status, b.tier, b.approver_id) == (a.status, a.tier, a.approver_id), d.draft_id
        assert "reporting_stale" not in {f.code for f in a.specialist_review_flags}
        stale_flag = [f for f in b.specialist_review_flags if f.code == "reporting_stale"]
        assert len(stale_flag) == 1 and stale_flag[0].line is None
        assert stale_flag[0].reason.startswith("pricing data is 37.0 h old (exported 2026-10-11T02:00:00-05:00")


# ---- review API --------------------------------------------------------------------------------

def test_wrong_approver_gets_403(client):
    step(client, "DQ-24106", "specialist-review", "S03")
    for wrong in ("U02", "U01", "U05", "S03"):  # out-of-office original, other region, VP, specialist
        r = step(client, "DQ-24106", "approve", wrong)
        assert r.status_code == 403, wrong
    assert step(client, "DQ-24106", "approve", "U04").status_code == 200


def test_stale_version_gets_409(client):
    seen = version(client, "DQ-24102")
    assert step(client, "DQ-24102", "specialist-review", "S02", v=seen).status_code == 200
    r = step(client, "DQ-24102", "approve", "U01", v=seen)
    assert r.status_code == 409 and r.json()["error"] == "stale_version"
    assert client.store.get_quote("DQ-24102").status == "pending_approver"


def test_no_double_approval(client):
    step(client, "DQ-24102", "specialist-review", "S02")
    v = version(client, "DQ-24102")
    assert step(client, "DQ-24102", "approve", "U01", v=v).status_code == 200
    assert step(client, "DQ-24102", "approve", "U01", v=v).status_code == 409          # same click twice
    assert step(client, "DQ-24102", "approve", "U01").status_code == 409               # wrong state now
    row = client.store.get_quote("DQ-24102")
    assert [a["step"] for a in client.store.approvals(row.id)].count("approve") == 1
    with pytest.raises(sqlite3.IntegrityError):  # and the database itself refuses a second row
        client.store.insert_approval(quote_id=row.id, quote_version=v, content_hash=row.content_hash,
                                     actor_id="U01", role="regional_manager", step="approve",
                                     decision="approve", comment=None)


# ---- audit -------------------------------------------------------------------------------------

def test_audit_chain_detects_tampering(tmp_path):
    with Store(tmp_path / "app.db", audit_clock=CLOCK) as store:
        for i in range(4):
            store.append_audit(actor_id="U01", action="approver_approved", entity="quote", entity_id=f"Q{i}",
                               before_status="pending_approver", after_status="pending_final_signoff",
                               details={"comment": f"ok {i}"})
        assert store.verify_audit_chain().ok
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store._db.execute("UPDATE audit_log SET actor_id = 'U05' WHERE seq = 2")
        store._db.execute("DROP TRIGGER audit_log_no_update")   # an attacker with DB access
        store._db.execute("UPDATE audit_log SET actor_id = 'U05' WHERE seq = 2")
        chk = store.verify_audit_chain()
        assert (chk.ok, chk.first_bad_seq) == (False, 2) and "tampered" in chk.problem


# ---- end to end --------------------------------------------------------------------------------

GOLDEN = ROOT / "tests" / "golden" / "routing_expected.json"


def test_routing_results_matches_golden(snapshot, directory, tmp_path):
    if not GOLDEN.exists():
        pytest.fail(f"{GOLDEN.relative_to(ROOT)} is missing (expected to be provided, not generated)")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"), parse_float=Decimal)
    rules = load_rules(ROOT / "rules" / "approval_rules.yaml", directory)
    with Store(tmp_path / "app.db", audit_clock=CLOCK) as store, fixture_stock() as stock:
        results = Pipeline(snapshot, directory, rules, stock, store, CLOCK).process(load_drafts(DRAFTS))

    def q(v, places):
        return None if v is None else Decimal(v).quantize(Decimal(places))

    got = [{"draft_id": r.draft_id, "status": r.status, "net_total": q(r.net_total, "0.01"),
            "effective_discount_pct": q(r.effective_discount_pct, "0.01"), "approver_id": r.approver_id,
            "flagged_lines": [f.line for f in r.flags if f.line is not None], "duplicate_of": r.duplicate_of}
           for r in results]
    want = [{**e, "net_total": q(e["net_total"], "0.01"),
             "effective_discount_pct": q(e["effective_discount_pct"], "0.01")} for e in expected]
    assert [g["draft_id"] for g in got] == [w["draft_id"] for w in want]
    for g, w in zip(got, want):
        assert g == {k: w[k] for k in g}, g["draft_id"]


def test_invalid_rules_file_refused(tmp_path, directory, monkeypatch, capsys):
    bad = tmp_path / "approval_rules.yaml"
    bad.write_text((ROOT / "rules" / "approval_rules.yaml").read_text(encoding="utf-8")
                   .replace("    when: {}", "    when: {discount_pct_gt: 0}"), encoding="utf-8")
    with pytest.raises(RulesError, match="last tier must be a catch-all"):
        load_rules(bad, directory)
    # The CLI refuses to start (exit 2) before touching the database or the stock API.
    monkeypatch.setenv("RULES_PATH", str(bad))
    db = tmp_path / "app.db"
    assert process_main(["--db", str(db), "--out-dir", str(tmp_path / "out")]) == 2
    assert "refusing to start" in capsys.readouterr().err and not db.exists()
