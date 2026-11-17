"""Duplicates (rule 11), store + hash-chained audit, output format, end-to-end pipeline."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.clock import SAMPLE_RUN_TIME, FixedClock
from app.directory import Directory
from app.drafts import load_drafts
from app.duplicates import Original, find_original, fingerprint
from app.models import DraftQuote
from app.outputs import OutputValidationError, render_json, validate_routing_results, write_outputs
from app.pipeline import Pipeline, explanation
from app.reporting import load_snapshot
from app.rules import load_rules
from app.stock_client import StockClient
from app.paths import ProtectedPathError, ensure_writable
from app.process import main as process_main
from app.store import Store, StoreLocationError, VersionConflict, reset_database

ROOT = Path(__file__).resolve().parents[1]
CLOCK = FixedClock(SAMPLE_RUN_TIME)
DAY = timedelta(hours=24)


def draft(draft_id="DQ-1", rfq="RFQ-1", lines=(("RL1", 3), ("RL2", 5)), created="2026-10-12T08:00:00-05:00"):
    return DraftQuote.model_validate({
        "draft_id": draft_id, "rfq_id": rfq, "customer_id": "C1002", "created_at": created, "prepared_by": "S01",
        "proposed_discount_pct": 0,
        "lines": [{"line": i, "part_number": pn, "description": "x", "quantity": q, "unit": "EA",
                   "extraction_confidence": "0.97"} for i, (pn, q) in enumerate(lines, start=1)]})


# ---- duplicate detection (pure) ----------------------------------------------------------------

def test_fingerprint_ignores_line_order_and_part_case():
    a = draft(lines=(("RL1", 3), ("RL2", 5)))
    b = draft(lines=(("rl2", 5), ("RL1", 3)))
    assert fingerprint(a) == fingerprint(b)


@pytest.mark.parametrize("other", [
    draft(rfq="RFQ-2"),                              # different rfq_id
    draft(lines=(("RL1", 3), ("RL2", 6))),           # different quantity
    draft(lines=(("RL1", 3),)),                      # fewer lines
])
def test_fingerprint_differs_when_rfq_or_lines_differ(other):
    assert fingerprint(other) != fingerprint(draft())


def test_duplicate_within_window():
    orig = Original("DQ-1", fingerprint(draft()), draft().created_at)
    dup = draft("DQ-2", created="2026-10-12T09:14:00-05:00")
    assert find_original(dup, fingerprint(dup), [orig], DAY).draft_id == "DQ-1"


def test_exactly_24h_later_is_still_duplicate_but_not_after():
    orig = Original("DQ-1", fingerprint(draft()), draft().created_at)
    at_24h = draft("DQ-2", created="2026-10-13T08:00:00-05:00")
    after = draft("DQ-3", created="2026-10-13T08:00:01-05:00")
    assert find_original(at_24h, fingerprint(at_24h), [orig], DAY) is not None
    assert find_original(after, fingerprint(after), [orig], DAY) is None


def test_earlier_draft_is_never_a_duplicate_of_a_later_one():
    later = Original("DQ-9", fingerprint(draft()), datetime.fromisoformat("2026-10-12T10:00:00-05:00"))
    assert find_original(draft(), fingerprint(draft()), [later], DAY) is None


def test_most_recent_original_wins():
    old = Original("DQ-OLD", fingerprint(draft()), datetime.fromisoformat("2026-10-12T01:00:00-05:00"))
    new = Original("DQ-NEW", fingerprint(draft()), datetime.fromisoformat("2026-10-12T07:00:00-05:00"))
    assert find_original(draft("DQ-X"), fingerprint(draft()), [old, new], DAY).draft_id == "DQ-NEW"


# ---- store -------------------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "app.db", audit_clock=CLOCK) as s:
        yield s


def _audit(store, n):
    for i in range(n):
        store.append_audit(actor_id="system", action="a", entity="quote", entity_id=f"Q{i}",
                           before_status=None, after_status="x", details={"i": i})


def test_audit_chain_verifies(store):
    _audit(store, 5)
    chk = store.verify_audit_chain()
    assert chk.ok and chk.entries == 5
    rows = store.audit_entries()
    assert rows[0]["prev_hash"] == "0" * 64 and rows[1]["prev_hash"] == rows[0]["hash"]


def test_audit_log_rejects_update_and_delete(store):
    _audit(store, 1)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._db.execute("UPDATE audit_log SET actor_id = 'mallory'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._db.execute("DELETE FROM audit_log")


def test_tampering_is_detected_even_if_triggers_are_bypassed(store):
    _audit(store, 3)
    store._db.execute("DROP TRIGGER audit_log_no_update")
    store._db.execute("UPDATE audit_log SET actor_id = 'mallory' WHERE seq = 2")
    chk = store.verify_audit_chain()
    assert not chk.ok and chk.first_bad_seq == 2 and "tampered" in chk.problem


def test_optimistic_concurrency(store):
    with store.transaction():
        store.insert_quote(draft_id="DQ-1", status="pending_specialist", content_hash="h", fingerprint="f",
                           draft_created_at=SAMPLE_RUN_TIME, draft_json="{}", priced_json=None,
                           routing_json=None, duplicate_of=None)
    v2 = store.transition("DQ-1", expected_version=1, from_status="pending_specialist",
                          to_status="pending_approver", actor_id="S01", action="specialist_approved")
    assert v2 == 2
    with pytest.raises(VersionConflict, match="expected version 1"):
        store.transition("DQ-1", expected_version=1, from_status="pending_specialist",
                         to_status="approved", actor_id="S02", action="specialist_approved")
    assert store.get_quote("DQ-1").status == "pending_approver"
    assert [e["action"] for e in store.audit_entries("DQ-1")] == ["specialist_approved"]  # loser not audited


def test_store_refuses_data_dir(tmp_path):
    with pytest.raises(StoreLocationError):
        Store(tmp_path / "data" / "app.db", forbidden_dirs=(tmp_path / "data",))


def test_reset_refuses_protected_dir_before_deleting_anything(tmp_path):
    protected = tmp_path / "data"
    protected.mkdir()
    victim = protected / "app.db"
    victim.write_text("precious")
    with pytest.raises(ProtectedPathError):
        reset_database(victim, protected=(protected,))
    assert victim.read_text() == "precious"


def test_write_outputs_refuses_protected_dir(tmp_path):
    protected = tmp_path / "data"
    with pytest.raises(ProtectedPathError):
        write_outputs([], protected / "out", [], protected=(protected,))
    assert not protected.exists()  # not even the directory was created


def test_default_guard_covers_the_real_data_dir():
    with pytest.raises(ProtectedPathError):
        ensure_writable(ROOT / "data" / "reporting_db" / "products.csv")
    ensure_writable(ROOT / "outputs" / "routing_results.json")  # allowed


@pytest.mark.parametrize("args", [
    ["--db", str(ROOT / "data" / "__guard_probe__.db"), "--reset"],
    ["--out-dir", str(ROOT / "data" / "__guard_probe_out__")],
])
def test_cli_refuses_to_write_under_data(args, capsys):
    before = sorted(p.name for p in (ROOT / "data").rglob("*"))
    assert process_main(args) == 2
    assert "refusing to start" in capsys.readouterr().err
    assert sorted(p.name for p in (ROOT / "data").rglob("*")) == before  # nothing created or deleted


# ---- output format -----------------------------------------------------------------------------

GOOD = [{"draft_id": "DQ-1", "status": "duplicate", "net_total": Decimal("47652.00"),
         "effective_discount_pct": Decimal("5.00"), "approver_id": None, "specialist_review_flags": [],
         "duplicate_of": "DQ-0"}]


def test_render_keeps_two_decimals_as_json_numbers():
    text = render_json(GOOD)
    assert '"net_total": 47652.00,' in text and '"effective_discount_pct": 5.00,' in text
    validate_routing_results(text, ["DQ-1"])


@pytest.mark.parametrize("mutate, message", [
    (lambda o: o.update(extra=1), "Additional properties"),
    (lambda o: o.update(net_total=Decimal("1.234")), "multiple of"),
    (lambda o: o.update(net_total="1.00"), "not of type"),
    (lambda o: o.update(status="approved"), "is not one of"),
    (lambda o: o.update(duplicate_of=None), "duplicate_of"),
    (lambda o: o.update(specialist_review_flags=[{"line": 0, "reason": "x"}]), "minimum"),
])
def test_schema_rejects_bad_output(mutate, message):
    bad = [dict(GOOD[0])]
    mutate(bad[0])
    with pytest.raises(OutputValidationError, match=message):
        validate_routing_results(render_json(bad), ["DQ-1"])


def test_output_order_must_match_input():
    with pytest.raises(OutputValidationError, match="input order"):
        validate_routing_results(render_json(GOOD), ["DQ-2"])


# ---- end-to-end on the sample drafts -----------------------------------------------------------

@pytest.fixture(scope="module")
def env():
    snapshot = load_snapshot(ROOT / "data" / "reporting_db")
    directory = Directory.from_snapshot(snapshot)
    rules = load_rules(ROOT / "rules" / "approval_rules.yaml", directory)
    return snapshot, directory, rules


def fixture_stock(rules, fail=False):
    fixture = json.loads((ROOT / "data" / "stock_fixture.json").read_text(encoding="utf-8"))

    def handler(request):
        if fail:
            return httpx.Response(503, json={"error": "service_unavailable", "retry_after_seconds": 2})
        pns = request.url.params["part_numbers"].split(",")
        return httpx.Response(200, json={"items": [fixture[p] for p in pns if p in fixture],
                                         "not_found": [p for p in pns if p not in fixture]})
    return StockClient("http://stock.test", CLOCK, max_age_hours=rules.rules.stock_max_age_hours,
                       transport=httpx.MockTransport(handler), sleep=lambda s: None)


def run(env, store, drafts, fail=False):
    snapshot, directory, rules = env
    with fixture_stock(rules, fail) as stock:
        return Pipeline(snapshot, directory, rules, stock, store, CLOCK).process(drafts)


EXPECTED = ROOT / "tests" / "golden_routing_results.json"


def test_sample_pipeline_matches_golden_output(env, store, tmp_path):
    drafts = load_drafts(ROOT / "data" / "draft_quotes.json")
    results = run(env, store, drafts)
    path, _ = write_outputs(results, tmp_path, [explanation(r, "x") for r in results])
    assert path.read_text(encoding="utf-8") == EXPECTED.read_text(encoding="utf-8")
    assert store.verify_audit_chain().ok


def test_duplicate_gets_original_values_and_no_approval_request(env, store):
    results = {r.draft_id: r for r in run(env, store, load_drafts(ROOT / "data" / "draft_quotes.json"))}
    dup, orig = results["DQ-24110"], results["DQ-24102"]
    assert (dup.status, dup.duplicate_of, dup.approver_id, dup.flags) == ("duplicate", "DQ-24102", None, ())
    assert (dup.net_total, dup.effective_discount_pct) == (orig.net_total, orig.effective_discount_pct)
    assert dup.priced is None and dup.routing is None  # not priced, not routed
    row = store.get_quote("DQ-24110")
    assert row.status == "duplicate" and row.routing_json is None
    assert [e["action"] for e in store.audit_entries("DQ-24110")] == ["draft_received", "duplicate_detected"]
    assert store._db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


def test_rerun_is_idempotent(env, store):
    drafts = load_drafts(ROOT / "data" / "draft_quotes.json")
    first = run(env, store, drafts)
    n_audit = len(store.audit_entries())
    second = run(env, store, drafts)
    assert [(r.status, r.approver_id, r.net_total) for r in first] == \
           [(r.status, r.approver_id, r.net_total) for r in second]
    assert all(r.reused for r in second)
    assert len(store.audit_entries()) == n_audit and len(store.list_quotes()) == 10


def test_duplicate_detected_against_store_from_an_earlier_run(env, store):
    drafts = {d.draft_id: d for d in load_drafts(ROOT / "data" / "draft_quotes.json")}
    run(env, store, [drafts["DQ-24102"]])
    (res,) = run(env, store, [drafts["DQ-24110"]])
    assert res.status == "duplicate" and res.duplicate_of == "DQ-24102"
    assert res.net_total == Decimal("132.4628")


def test_stock_down_still_routes_every_quote(env, store):
    results = run(env, store, load_drafts(ROOT / "data" / "draft_quotes.json"), fail=True)
    routed = [r for r in results if r.status != "duplicate"]
    assert all(any(f.reason == "stock unavailable after 4 attempts" for f in r.flags) for r in routed)
    # Stock outage adds review but changes no approver (same as the golden routing).
    assert [r.approver_id for r in routed] == [None, "U01", "U05", "U05", None, "U04", "U01", None, "U04"]
