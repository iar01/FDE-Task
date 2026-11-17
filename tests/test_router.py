"""Approval rules: every tier, boundaries, backup chain, specialist sign-off, rules validation, escalation."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.clock import SAMPLE_RUN_TIME, FixedClock
from app.directory import Directory
from app.drafts import load_drafts
from app.models import Flag, PricedLine, PricedQuote
from app.pricing import price_quote
from app.reporting import Approver, load_snapshot
from app.router import PendingApproval, escalate_overdue, route
from app.rules import RulesError, load_rules, parse_rules, validate_against_directory
from app.stock_client import StockClient
from app.stock_flags import stock_flags
from tests.factories import RULES as IN_MEMORY_RULES

ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "rules" / "approval_rules.yaml"
RULES_TEXT = RULES_PATH.read_text(encoding="utf-8")
CLOCK = FixedClock(SAMPLE_RUN_TIME)
OOO = (date(2026, 10, 12), date(2026, 10, 16))


@pytest.fixture(scope="module")
def snapshot():
    return load_snapshot(ROOT / "data" / "reporting_db")


@pytest.fixture(scope="module")
def directory(snapshot):
    return Directory.from_snapshot(snapshot)


@pytest.fixture(scope="module")
def rules(directory):
    return load_rules(RULES_PATH, directory)


def A(id, role, region="ALL", backup=None, ooo=None):
    return Approver(id, id, role, region, backup, *(ooo or (None, None)))


def people(*extra, **override):
    """Small directory shaped like approvers.csv, with per-test overrides."""
    base = {
        "RW": A("RW", "regional_manager", "West", "DIR"),
        "RC": A("RC", "regional_manager", "Central", "DIR"),
        "DIR": A("DIR", "sales_operations_director", "ALL", "VP"),
        "VP": A("VP", "vp_sales"),
        "S1": A("S1", "quote_specialist", backup="S2"),
        "S2": A("S2", "quote_specialist", backup="S1"),
    }
    base.update(override)
    for a in extra:
        base[a.approver_id] = a
    return Directory(base, frozenset({"West", "Central"}))


def pq(eff="10", net="1000", region="West", prepared_by="S1", confidences=("0.97",), flags=()):
    lines = tuple(PricedLine(line=i, part_number=f"P{i}", quantity=1, extraction_confidence=Decimal(c),
                             price_source="list", list_price=Decimal(1), unit_price=Decimal(1),
                             list_extended=Decimal(1), line_net=Decimal(1))
                  for i, c in enumerate(confidences, start=1))
    return PricedQuote(
        draft_id="DQ-T", rfq_id="R", customer_id="C", prepared_by=prepared_by, created_at=SAMPLE_RUN_TIME,
        region=region, proposed_discount_pct=Decimal(0), lines=lines, list_total=Decimal(net),
        net_total=Decimal(net), effective_discount_pct=None if eff is None else Decimal(eff),
        flags=tuple(flags), reporting_exported_at=SAMPLE_RUN_TIME, reporting_age_hours=Decimal(1))


def go(quote, directory=None, rules_text=None, clock=CLOCK):
    """In-memory policy by default (tests/factories.py); pass rules_text only to test the YAML itself."""
    rules = parse_rules(rules_text) if rules_text is not None else IN_MEMORY_RULES
    return route(quote, rules, directory or people(), clock)


# ---- one test per tier -------------------------------------------------------------------------

def test_tier_specialist_only():
    d = go(pq(eff="8", net="435.23"))
    assert (d.status, d.tier, d.approver_id, d.specialist_id) == ("specialist_only", "specialist_only", None, "S1")


def test_tier_regional_manager_by_discount_matches_customer_region():
    d = go(pq(eff="18", region="Central"))
    assert (d.status, d.tier, d.approver_id) == ("approver_required", "regional_manager", "RC")
    assert d.trace[0] == "effective discount 18.00% > 15 -> tier regional_manager (Central) -> RC"


def test_tier_regional_manager_by_net_total():
    d = go(pq(eff="5", net="47652.00"))
    assert (d.tier, d.approver_id) == ("regional_manager", "RW")
    assert d.trace[0] == "net 47652.00 >= 25000 -> tier regional_manager (West) -> RW"


def test_tier_vp():
    d = go(pq(eff="26.22"))
    assert (d.tier, d.approver_id) == ("vp", "VP")
    assert d.trace[0] == "effective discount 26.22% > 25 -> tier vp -> VP"


def test_vp_wins_over_net_threshold():  # A13
    assert go(pq(eff="30", net="90000")).tier == "vp"


# ---- boundaries (approval_rules.md: "15% or less", "over 15% up to 25%", "over 25%", "$25,000 or more")

@pytest.mark.parametrize("eff, net, tier", [
    ("15", "1000", "specialist_only"),
    ("15.00001", "1000", "regional_manager"),   # A3: compare unrounded (displays as 15.00)
    ("25", "1000", "regional_manager"),
    ("25.00001", "1000", "vp"),
    ("0", "24999.99", "specialist_only"),
    ("0", "25000.00", "regional_manager"),
])
def test_boundaries(eff, net, tier):
    assert go(pq(eff=eff, net=net)).tier == tier


def test_unknown_discount_never_skips_an_approver():
    assert go(pq(eff=None, net="0")).tier == "vp"


# ---- backup approver ---------------------------------------------------------------------------

def test_backup_when_approver_out_of_office():
    d = go(pq(eff="18", region="Central"), people(RC=A("RC", "regional_manager", "Central", "DIR", OOO)))
    assert (d.approver_id, d.routed_via_backup_of) == ("DIR", "RC")
    assert d.trace[1] == "RC out of office 2026-10-12..2026-10-16 -> backup DIR"
    assert d.specialist_review_flags == ()


def test_out_of_office_dates_inclusive():
    for day in ("2026-10-12T09:30:00-05:00", "2026-10-16T23:59:00-05:00"):
        d = go(pq(eff="18", region="Central"), people(RC=A("RC", "regional_manager", "Central", "DIR", OOO)),
               clock=FixedClock(datetime.fromisoformat(day)))
        assert d.approver_id == "DIR"
    d = go(pq(eff="18", region="Central"), people(RC=A("RC", "regional_manager", "Central", "DIR", OOO)),
           clock=FixedClock(datetime.fromisoformat("2026-10-17T00:01:00-05:00")))
    assert d.approver_id == "RC"


def test_ooo_uses_central_date_not_utc():
    # 2026-10-17 03:00 UTC is still 2026-10-16 in Chicago -> still out.
    d = go(pq(eff="18", region="Central"), people(RC=A("RC", "regional_manager", "Central", "DIR", OOO)),
           clock=FixedClock(datetime.fromisoformat("2026-10-17T03:00:00+00:00")))
    assert d.approver_id == "DIR"


def test_backup_chain_followed_when_backup_also_out():
    d = go(pq(eff="18", region="Central"), people(
        RC=A("RC", "regional_manager", "Central", "DIR", OOO), DIR=A("DIR", "sales_operations_director", "ALL", "VP", OOO)))
    assert d.approver_id == "VP"
    assert d.trace[1:3] == ("RC out of office 2026-10-12..2026-10-16 -> backup DIR",
                            "DIR out of office 2026-10-12..2026-10-16 -> backup VP")


def test_vp_out_with_no_backup_keeps_vp_and_flags():  # A2
    d = go(pq(eff="30"), people(VP=A("VP", "vp_sales", ooo=OOO)))
    assert d.approver_id == "VP" and d.status == "approver_required"
    assert [(f.line, f.reason) for f in d.specialist_review_flags] == [
        (None, "required approver VP out of office, no backup available")]


def test_runtime_cycle_is_detected_not_looped():
    d = go(pq(eff="18", region="Central"), people(
        RC=A("RC", "regional_manager", "Central", "DIR", OOO), DIR=A("DIR", "sales_operations_director", "ALL", "RC", OOO)))
    assert d.approver_id == "DIR"
    assert "loops back to RC" in d.trace[-2]
    assert d.specialist_review_flags[0].code == "no_backup_available"


# ---- specialist sign-off (rule 1, A1) ----------------------------------------------------------

def test_specialist_is_prepared_by_with_backup_if_out():
    d = go(pq(prepared_by="S1"), people(S1=A("S1", "quote_specialist", backup="S2", ooo=OOO)))
    assert d.specialist_id == "S2"


def test_unknown_prepared_by_is_flagged_and_does_not_change_tier():
    d = go(pq(eff="18", prepared_by="ZZ"))
    assert d.specialist_id is None and d.tier == "regional_manager"
    assert d.specialist_review_flags[0].code == "specialist_unknown"


# ---- flags -------------------------------------------------------------------------------------

def test_low_confidence_threshold_is_strict():
    d = go(pq(confidences=("0.97", "0.84", "0.85")))
    assert [(f.line, f.reason) for f in d.specialist_review_flags] == [(2, "extraction confidence 0.84")]


def test_flags_never_lower_the_tier():
    noisy = [Flag(line=None, code="stock_unavailable", reason="stock unavailable after 4 attempts")]
    for eff, tier in (("8", "specialist_only"), ("18", "regional_manager"), ("30", "vp")):
        assert go(pq(eff=eff, flags=noisy)).tier == tier


def test_unknown_region_flags_and_assigns_nobody():  # A8
    d = go(pq(eff="18", region=None))
    assert d.status == "approver_required" and d.approver_id is None
    assert d.specialist_review_flags[0].code == "approver_unknown"


def test_flags_sorted_lines_first_then_quote_level():
    flags = [Flag(line=None, code="x", reason="q"), Flag(line=3, code="y", reason="l3")]
    d = go(pq(confidences=("0.5",), flags=flags))
    assert [f.line for f in d.specialist_review_flags] == [1, 3, None]


# ---- configurability ---------------------------------------------------------------------------

def test_changing_yaml_changes_routing_without_code():
    stricter = RULES_TEXT.replace("{discount_pct_gt: 15}", "{discount_pct_gt: 10}")
    assert go(pq(eff="12"), rules_text=RULES_TEXT).tier == "specialist_only"
    assert go(pq(eff="12"), rules_text=stricter).tier == "regional_manager"


def test_rules_version_and_hash_recorded(rules):
    d = route(pq(), rules, people(), CLOCK)
    assert d.rules_version == "2026-10-12.1" and len(d.rules_hash) == 64


@pytest.mark.parametrize("mutate, message", [
    (lambda t: t.replace("    when: {}                              #", "    when: {discount_pct_gt: 0} #"),
     "last tier must be a catch-all"),
    (lambda t: t.replace("{role: vp_sales}", "{role: chief_pricing_officer}"), "need exactly one chief_pricing_officer"),
    (lambda t: t.replace("low_confidence_threshold: 0.85", "low_confidence_threshold: 0.85\nsurprise: 1"), "surprise"),
    (lambda t: t.replace("timezone: America/Chicago", "timezone: Mars/Olympus"), "unknown timezone"),
    (lambda t: t.replace("discount_pct_gt: 25}", "discount_pct_gt: 25, net_total_lte: 5}"), "net_total_lte"),
])
def test_invalid_rules_refuse_to_start(directory, mutate, message):
    with pytest.raises(RulesError, match=message):
        validate_against_directory(parse_rules(mutate(RULES_TEXT)).rules, directory)


def test_region_without_manager_refuses_to_start():
    d = Directory(dict(people().approvers), frozenset({"West", "Central", "East"}))
    with pytest.raises(RulesError, match="regional_manager for region 'East'"):
        validate_against_directory(parse_rules(RULES_TEXT).rules, d)


def test_missing_backup_refuses_to_start():
    with pytest.raises(RulesError, match="backup_approver_id 'NOPE' does not exist"):
        validate_against_directory(parse_rules(RULES_TEXT).rules,
                                   people(RW=A("RW", "regional_manager", "West", "NOPE")))


def test_approver_backup_cycle_refuses_to_start():
    with pytest.raises(RulesError, match="backup cycle: VP -> DIR -> VP"):
        validate_against_directory(parse_rules(RULES_TEXT).rules, people(VP=A("VP", "vp_sales", backup="DIR")))


def test_specialist_rotation_cycle_in_approvers_csv_is_allowed(directory):
    # approvers.csv: S01 -> S02 -> S03 -> S01. A rotation, not an error.
    validate_against_directory(parse_rules(RULES_TEXT).rules, directory)


# ---- escalation (proposal, off by default) -----------------------------------------------------

ENABLED = RULES_TEXT.replace("enabled: false", "enabled: true")


def _pending(minutes, approver="RW"):
    return PendingApproval("DQ-T", approver, SAMPLE_RUN_TIME - timedelta(minutes=minutes))


def test_escalation_off_by_default(rules, directory):
    assert escalate_overdue([_pending(999, "U01")], rules, directory, CLOCK) == ()


def test_escalation_reassigns_overdue_to_backup():
    out = escalate_overdue([_pending(121), _pending(120)], parse_rules(ENABLED), people(), CLOCK)
    assert len(out) == 1 and (out[0].to_approver_id, out[0].overdue_minutes) == ("DIR", 121)


def test_escalation_with_no_backup_alerts_instead():
    out = escalate_overdue([_pending(500, "VP")], parse_rules(ENABLED), people(), CLOCK)
    assert out[0].to_approver_id is None and "alert operations" in out[0].reason


# ---- golden: the 10 sample drafts --------------------------------------------------------------

GOLDEN = {
    "DQ-24101": ("specialist_only", None), "DQ-24102": ("approver_required", "U01"),
    "DQ-24103": ("approver_required", "U05"), "DQ-24104": ("approver_required", "U05"),
    "DQ-24105": ("specialist_only", None), "DQ-24106": ("approver_required", "U04"),
    "DQ-24107": ("approver_required", "U01"), "DQ-24108": ("specialist_only", None),
    "DQ-24109": ("approver_required", "U04"),
}


def _fixture_stock(rules):
    fixture = json.loads((ROOT / "data" / "stock_fixture.json").read_text(encoding="utf-8"))

    def handler(request):
        pns = request.url.params["part_numbers"].split(",")
        return httpx.Response(200, json={"items": [fixture[p] for p in pns if p in fixture],
                                         "not_found": [p for p in pns if p not in fixture]})
    return StockClient("http://stock.test", CLOCK, max_age_hours=rules.rules.stock_max_age_hours,
                       transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_sample_drafts_route_as_expected(snapshot, directory, rules):
    drafts = load_drafts(ROOT / "data" / "draft_quotes.json")
    with _fixture_stock(rules) as client:
        stock = client.fetch(l.part_number for d in drafts for l in d.lines)
    got = {}
    for d in drafts:
        priced = price_quote(d, snapshot, CLOCK, reporting_max_age_hours=rules.rules.reporting_max_age_hours)
        dec = route(priced, rules, directory, CLOCK, extra_flags=stock_flags(d, stock))
        got[d.draft_id] = dec
    for draft_id, (status, approver) in GOLDEN.items():
        assert (got[draft_id].status, got[draft_id].approver_id) == (status, approver), draft_id
    assert got["DQ-24106"].routed_via_backup_of == "U02" and got["DQ-24109"].routed_via_backup_of == "U02"
    codes = {k: [(f.line, f.code) for f in v.specialist_review_flags] for k, v in got.items()}
    assert codes["DQ-24103"] == [(3, "stock_not_found")]
    assert codes["DQ-24104"] == [(1, "stock_stale")]
    assert codes["DQ-24105"] == [(3, "low_confidence")]
    assert codes["DQ-24107"] == [(1, "contract_expired")]
    assert codes["DQ-24108"] == [(1, "out_of_stock")]
    assert codes["DQ-24109"] == [(3, "low_confidence"), (6, "stock_stale")]
    assert all(v.specialist_id == d.prepared_by for d in drafts for v in [got[d.draft_id]])
