"""Review API: roles, state machine, version checks, denied attempts audited, rules simulate/reload."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import Services, create_app
from app.clock import SAMPLE_RUN_TIME, FixedClock
from app.config import Settings
from app.directory import Directory
from app.drafts import load_drafts
from app.pipeline import Pipeline
from app.reporting import load_snapshot
from app.rules import load_rules
from app.store import Store
from tests.test_pipeline import fixture_stock

ROOT = Path(__file__).resolve().parents[1]
CLOCK = FixedClock(SAMPLE_RUN_TIME)


@pytest.fixture
def client(tmp_path):
    snapshot = load_snapshot(ROOT / "data" / "reporting_db")
    directory = Directory.from_snapshot(snapshot)
    rules = load_rules(ROOT / "rules" / "approval_rules.yaml", directory)
    store = Store(tmp_path / "app.db", audit_clock=CLOCK)
    with fixture_stock(rules) as stock:
        Pipeline(snapshot, directory, rules, stock, store, CLOCK).process(
            load_drafts(ROOT / "data" / "draft_quotes.json"))
    settings = Settings(rules_path=ROOT / "rules" / "approval_rules.yaml")
    svc = Services(store, snapshot, directory, rules, CLOCK, settings, threading.RLock())
    with TestClient(create_app(svc)) as c:
        c.store = store
        yield c
    store.close()


def as_(user):
    return {"X-User-Id": user} if user else {}


def version(c, draft_id, user="S01"):
    return c.get(f"/quotes/{draft_id}", headers=as_(user)).json()["version"]


def step(c, draft_id, path, user, v=None, **body):
    return c.post(f"/quotes/{draft_id}/{path}", headers=as_(user),
                  json={"version": version(c, draft_id) if v is None else v, **body})


def last_action(c, draft_id):
    return c.store.audit_entries(draft_id)[-1]


# ---- auth stub ---------------------------------------------------------------------------------

@pytest.mark.parametrize("user", [None, "", "X99"])
def test_missing_or_unknown_user_is_401_and_audited(client, user):
    r = client.get("/quotes", headers=as_(user))
    assert r.status_code == 401 and r.json()["error"] == "unauthenticated"
    assert client.store.audit_entries("*")[-1]["action"] == "list_quotes_denied"


# ---- visibility --------------------------------------------------------------------------------

def test_specialists_see_all_managers_see_their_own(client):
    assert client.get("/quotes", headers=as_("S02")).json()["count"] == 10
    u04 = client.get("/quotes", headers=as_("U04")).json()
    assert sorted(q["draft_id"] for q in u04["quotes"]) == ["DQ-24106", "DQ-24109"]
    assert client.get("/quotes", headers=as_("U02")).json()["count"] == 0  # out of office; routed to U04
    assert client.get("/quotes/DQ-24101", headers=as_("U01")).status_code == 403


def test_filters(client):
    r = client.get("/quotes", params={"status": "duplicate"}, headers=as_("S01")).json()
    assert [q["draft_id"] for q in r["quotes"]] == ["DQ-24110"]
    r = client.get("/quotes", params={"assigned_to": "U05"}, headers=as_("S01")).json()
    assert [q["draft_id"] for q in r["quotes"]] == ["DQ-24103", "DQ-24104"]


def test_detail_has_trace_flags_and_audit(client):
    d = client.get("/quotes/DQ-24109", headers=as_("S03")).json()
    assert d["status"] == "pending_specialist_review" and d["approver_id"] == "U04"
    assert [f["code"] for f in d["flags"]] == ["low_confidence", "stock_stale"]
    assert "U02 out of office 2026-10-12..2026-10-16 -> backup U04" in d["trace"]
    assert [a["action"] for a in d["audit"]] == ["draft_received", "quote_routed"]


# ---- happy paths -------------------------------------------------------------------------------

def test_specialist_only_path(client):
    r = step(client, "DQ-24101", "specialist-review", "S01", comment="checked")
    assert r.status_code == 200 and r.json()["status"] == "pending_final_signoff"
    assert r.json()["flags_cleared_by"] == "S01"
    r = step(client, "DQ-24101", "final-signoff", "S01")
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert [a["step"] for a in r.json()["approvals"]] == ["specialist_review", "final_signoff"]


def test_approver_required_path_with_backup(client):
    assert step(client, "DQ-24106", "specialist-review", "S03").json()["status"] == "pending_approver"
    r = step(client, "DQ-24106", "approve", "U04", comment="as U02's backup")
    assert r.status_code == 200 and r.json()["status"] == "pending_final_signoff"
    r = step(client, "DQ-24106", "final-signoff", "S03")
    assert r.json()["status"] == "approved"
    actions = [e["action"] for e in client.store.audit_entries("DQ-24106")]
    assert actions == ["draft_received", "quote_routed", "specialist_reviewed", "approver_approved", "final_signoff"]
    assert client.get("/audit/verify", headers=as_("S01")).json()["ok"]


# ---- denials -----------------------------------------------------------------------------------

def test_original_manager_out_of_office_cannot_approve(client):
    step(client, "DQ-24106", "specialist-review", "S03")
    r = step(client, "DQ-24106", "approve", "U02")
    assert r.status_code == 403 and "routed to backup U04" in r.json()["message"]
    entry = last_action(client, "DQ-24106")
    assert (entry["actor_id"], entry["action"], entry["before_status"]) == ("U02", "approve_denied", "pending_approver")


def test_wrong_region_manager_cannot_approve(client):
    step(client, "DQ-24106", "specialist-review", "S03")
    assert step(client, "DQ-24106", "approve", "U01").status_code == 403


@pytest.mark.parametrize("draft_id, path, user, needle", [
    ("DQ-24106", "specialist-review", "U04", "separation of duties"),   # approver on specialist step
    ("DQ-24101", "final-signoff", "U05", "separation of duties"),
])
def test_separation_of_duties_specialist_steps(client, draft_id, path, user, needle):
    r = step(client, draft_id, path, user)
    assert r.status_code == 403 and needle in r.json()["message"]


def test_specialist_cannot_do_approver_step(client):
    step(client, "DQ-24106", "specialist-review", "S03")
    r = step(client, "DQ-24106", "approve", "S03")
    assert r.status_code == 403 and "separation of duties" in r.json()["message"]


def test_other_specialist_blocked_while_assigned_is_available(client):
    r = step(client, "DQ-24101", "specialist-review", "S02")  # assigned S01 is in office
    assert r.status_code == 403 and "assigned to specialist S01" in r.json()["message"]


def test_stale_version_is_409_and_audited(client):
    v = version(client, "DQ-24106")
    step(client, "DQ-24106", "specialist-review", "S03", v=v)
    r = step(client, "DQ-24106", "approve", "U04", v=v)  # U04 still holds the old version
    assert r.status_code == 409 and r.json()["error"] == "stale_version"
    assert last_action(client, "DQ-24106")["action"] == "approve_denied"


def test_illegal_transitions_are_409(client):
    assert step(client, "DQ-24106", "approve", "U04").json()["error"] == "illegal_transition"  # review first
    assert step(client, "DQ-24101", "final-signoff", "S01").status_code == 409
    step(client, "DQ-24101", "specialist-review", "S01")
    assert step(client, "DQ-24101", "approve", "U01").status_code == 403  # specialist_only: no approver step


def test_duplicate_has_no_approval_request(client):
    r = step(client, "DQ-24110", "specialist-review", "S02")
    assert r.status_code == 409 and "duplicate of DQ-24102" in r.json()["message"]


def test_cannot_approve_twice(client):
    step(client, "DQ-24101", "specialist-review", "S01")
    step(client, "DQ-24101", "final-signoff", "S01")
    assert step(client, "DQ-24101", "final-signoff", "S01").status_code == 409


def test_missing_version_is_422(client):
    r = client.post("/quotes/DQ-24101/specialist-review", headers=as_("S01"), json={})
    assert r.status_code == 422 and r.json()["error"] == "version_required"


# ---- reject ------------------------------------------------------------------------------------

def test_reject_needs_reason(client):
    r = step(client, "DQ-24103", "reject", "S01", reason="  ")
    assert r.status_code == 422 and r.json()["error"] == "reason_required"


def test_routed_approver_can_reject(client):
    step(client, "DQ-24103", "specialist-review", "S01")
    r = step(client, "DQ-24103", "reject", "U05", reason="26% is too deep for this customer")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert step(client, "DQ-24103", "approve", "U05").status_code == 409  # terminal


def test_nothing_reaches_approved_without_final_signoff(client):
    step(client, "DQ-24102", "specialist-review", "S02")
    assert step(client, "DQ-24102", "approve", "U01").json()["status"] == "pending_final_signoff"


# ---- edit stub, audit, rules -------------------------------------------------------------------

def test_edit_is_a_501_stub(client):
    r = client.patch("/quotes/DQ-24101", headers=as_("S01"), json={})
    assert r.status_code == 501 and any("reprice" in s for s in r.json()["production_flow"])
    assert last_action(client, "DQ-24101")["action"] == "edit_attempted_stub"


def test_audit_endpoint(client):
    r = client.get("/audit", params={"entity_id": "DQ-24110"}, headers=as_("S01")).json()
    assert [e["action"] for e in r["entries"]] == ["draft_received", "duplicate_detected"]


RULES_TEXT = (ROOT / "rules" / "approval_rules.yaml").read_text(encoding="utf-8")


def test_simulate_shows_diff_without_saving(client):
    candidate = RULES_TEXT.replace("{discount_pct_gt: 15}", "{discount_pct_gt: 10}")
    r = client.post("/rules/simulate", headers=as_("S01"), json={"rules_yaml": candidate}).json()
    assert r["saved"] is False and r["open_quotes_compared"] == 9
    # Specialist-only quotes are at 8%, 5% and 10% (10 is not > 10), so nothing changes tier.
    assert r["changed"] == 0
    assert version(client, "DQ-24101") == 2  # nothing written to quotes


def test_simulate_with_lower_threshold_changes_specialist_only_quotes(client):
    candidate = RULES_TEXT.replace("{discount_pct_gt: 15}", "{discount_pct_gt: 7}")
    r = client.post("/rules/simulate", headers=as_("S01"), json={"rules_yaml": candidate}).json()
    changed = {c["draft_id"]: c["changes"] for c in r["changes"]}
    assert set(changed) == {"DQ-24101", "DQ-24108"}
    assert changed["DQ-24108"]["approver_id"] == {"current": None, "candidate": "U03"}


def test_simulate_rejects_invalid_rules(client):
    r = client.post("/rules/simulate", headers=as_("S01"), json={"rules_yaml": "tiers: []"})
    assert r.status_code == 422


def test_rules_reload_is_restricted_and_audited(client):
    assert client.post("/rules/reload", headers=as_("S01")).status_code == 403
    r = client.post("/rules/reload", headers=as_("U04")).json()
    assert r["active"]["version"] == "2026-10-12.1" and r["changed"] is False
    assert client.store.audit_entries("approval_rules")[-1]["action"] == "rules_reloaded"
