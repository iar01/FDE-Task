from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.clock import SAMPLE_RUN_TIME, FixedClock
from app.drafts import load_drafts
from app.models import DraftQuote
from app.pricing import price_quote
from app.reporting import ContractPrice, Customer, Product, ReportingSnapshot, load_snapshot

ROOT = Path(__file__).resolve().parents[1]
MAX_AGE = Decimal("36")
SAMPLE_CLOCK = FixedClock(SAMPLE_RUN_TIME)


@pytest.fixture(scope="module")
def snapshot() -> ReportingSnapshot:
    return load_snapshot(ROOT / "data" / "reporting_db")


@pytest.fixture(scope="module")
def drafts() -> dict[str, DraftQuote]:
    return {d.draft_id: d for d in load_drafts(ROOT / "data" / "draft_quotes.json")}


def _price(draft, snap, clock=SAMPLE_CLOCK):
    return price_quote(draft, snap, clock, reporting_max_age_hours=MAX_AGE)


# ---- sample data: expected values from DECISIONS.md section 4 ----------------------------------

@pytest.mark.parametrize("draft_id, list_total, net_total, eff", [
    ("DQ-24101", "473.08", "435.23", "8.00"),
    ("DQ-24102", "161.54", "132.46", "18.00"),
    ("DQ-24103", "987.82", "728.80", "26.22"),
    ("DQ-24104", "3130.00", "2191.00", "30.00"),
    ("DQ-24105", "496.00", "471.20", "5.00"),
    ("DQ-24106", "50160.00", "47652.00", "5.00"),
    ("DQ-24107", "539.50", "456.54", "15.38"),
    ("DQ-24108", "1686.00", "1517.40", "10.00"),
    ("DQ-24109", "1727.00", "1409.86", "18.36"),
    ("DQ-24110", "161.54", "132.46", "18.00"),
])
def test_sample_totals(snapshot, drafts, draft_id, list_total, net_total, eff):
    pq = _price(drafts[draft_id], snapshot)
    assert pq.list_total_out == Decimal(list_total)
    assert pq.net_total_out == Decimal(net_total)
    assert pq.effective_discount_pct_out == Decimal(eff)


def test_contract_savings_count_toward_effective_discount(snapshot, drafts):
    pq = _price(drafts["DQ-24103"], snapshot)  # proposed 22%
    assert pq.effective_discount_pct > Decimal(25)
    sources = {pl.line: pl.price_source for pl in pq.lines}
    assert sources == {1: "contract", 2: "list", 3: "list", 4: "contract", 5: "list", 6: "list"}


def test_expired_contract_uses_list_and_flags_line(snapshot, drafts):
    pq = _price(drafts["DQ-24107"], snapshot)
    line1 = pq.lines[0]
    assert line1.price_source == "list" and line1.unit_price == Decimal("8.40")
    assert [(f.line, f.code) for f in pq.flags] == [(1, "contract_expired")]
    assert pq.flags[0].reason == "contract price expired 2025-12-31; list price used"


def test_sample_reporting_export_is_fresh_at_sample_clock(snapshot, drafts):
    pq = _price(drafts["DQ-24101"], snapshot)
    assert pq.reporting_age_hours == Decimal("31.5")
    assert pq.flags == ()


def test_values_are_decimal_not_float(snapshot, drafts):
    pq = _price(drafts["DQ-24109"], snapshot)
    for value in (pq.list_total, pq.net_total, pq.effective_discount_pct, *(pl.line_net for pl in pq.lines)):
        assert isinstance(value, Decimal)


def test_no_rounding_inside_pricing(snapshot, drafts):
    pq = _price(drafts["DQ-24101"], snapshot)
    assert pq.net_total == Decimal("435.2336")  # rounded only by net_total_out


# ---- synthetic snapshots for edge cases -------------------------------------------------------

def _snap(*, contracts=(), products=None, exported_at=SAMPLE_RUN_TIME - timedelta(hours=1)):
    products = products or [
        Product("P1", "widget", "Cat", "EA", Decimal("10.00"), "active", None, "DAL"),
        Product("P2", "old widget", "Cat", "EA", Decimal("20.00"), "discontinued", "P1", "DAL"),
    ]
    by_key: dict = {}
    for c in contracts:
        by_key.setdefault((c.customer_id, c.part_number), []).append(c)
    return ReportingSnapshot(
        exported_at=exported_at,
        products={p.part_number: p for p in products},
        customers={"C1": Customer("C1", "Cust", "West", "contract")},
        contract_prices={k: tuple(v) for k, v in by_key.items()},
        approvers={},
    )


def _draft(lines, customer_id="C1", discount=0):
    return DraftQuote.model_validate({
        "draft_id": "DQ-T", "rfq_id": "RFQ-T", "customer_id": customer_id,
        "created_at": "2026-10-12T08:00:00-05:00", "prepared_by": "S01",
        "proposed_discount_pct": discount,
        "lines": [{"line": i, "part_number": pn, "description": "x", "quantity": q, "unit": "EA",
                   "extraction_confidence": "0.97"} for i, (pn, q) in enumerate(lines, start=1)],
    })


def test_contract_valid_on_last_day_inclusive():
    snap = _snap(contracts=[ContractPrice("C1", "P1", Decimal("8.00"), date(2026, 1, 1), date(2026, 10, 12))])
    pq = _price(_draft([("P1", 1)]), snap)
    assert pq.lines[0].price_source == "contract" and pq.flags == ()


def test_contract_expired_yesterday_flags():
    snap = _snap(contracts=[ContractPrice("C1", "P1", Decimal("8.00"), date(2026, 1, 1), date(2026, 10, 11))])
    pq = _price(_draft([("P1", 1)]), snap)
    assert pq.lines[0].price_source == "list"
    assert [f.code for f in pq.flags] == ["contract_expired"]


def test_future_contract_is_not_used_and_not_flagged():
    snap = _snap(contracts=[ContractPrice("C1", "P1", Decimal("8.00"), date(2026, 11, 1), date(2026, 12, 31))])
    pq = _price(_draft([("P1", 1)]), snap)
    assert pq.lines[0].price_source == "list" and pq.flags == ()


def test_today_is_the_central_calendar_date():
    # 2027-01-01 03:00 UTC is still 2026-12-31 in Chicago, so the 2026 contract still applies.
    snap = _snap(contracts=[ContractPrice("C1", "P1", Decimal("8.00"), date(2026, 1, 1), date(2026, 12, 31))],
                 exported_at=datetime.fromisoformat("2026-12-31T02:00:00-06:00"))
    clock = FixedClock(datetime.fromisoformat("2027-01-01T03:00:00+00:00"))
    pq = _price(_draft([("P1", 1)]), snap, clock)
    assert pq.lines[0].price_source == "contract"


def test_multiple_valid_contracts_use_latest_and_flag():
    snap = _snap(contracts=[
        ContractPrice("C1", "P1", Decimal("7.00"), date(2026, 1, 1), date(2026, 12, 31)),
        ContractPrice("C1", "P1", Decimal("9.00"), date(2026, 6, 1), date(2026, 12, 31)),
    ])
    pq = _price(_draft([("P1", 1)]), snap)
    assert pq.lines[0].unit_price == Decimal("9.00")
    assert [f.code for f in pq.flags] == ["contract_conflict"]


def test_discontinued_part_flags_with_replacement():
    pq = _price(_draft([("P2", 1)]), _snap())
    assert pq.flags[0].line == 1 and pq.flags[0].reason == "part discontinued; replaced by P1"
    assert pq.lines[0].unit_price == Decimal("20.00")  # never auto-substituted


def test_unknown_product_does_not_crash_and_is_excluded():
    pq = _price(_draft([("P1", 2), ("NOPE", 5)], discount=10), _snap())
    assert pq.list_total == Decimal("20.00")
    assert pq.net_total == Decimal("18.00")
    assert [(f.line, f.code) for f in pq.flags] == [(2, "product_not_found"), (None, "pricing_incomplete")]


def test_all_lines_unknown_gives_no_effective_discount():
    pq = _price(_draft([("NOPE", 1)]), _snap())
    assert pq.list_total == 0 and pq.effective_discount_pct is None


def test_unknown_customer_prices_at_list_and_flags():
    snap = _snap(contracts=[ContractPrice("C9", "P1", Decimal("1.00"), date(2026, 1, 1), date(2026, 12, 31))])
    pq = _price(_draft([("P1", 1)], customer_id="C9"), snap)
    assert pq.region is None and pq.lines[0].price_source == "list"
    assert [(f.line, f.code) for f in pq.flags] == [(None, "customer_not_found")]


def test_reporting_exactly_36h_old_is_not_stale():
    snap = _snap(exported_at=SAMPLE_RUN_TIME - timedelta(hours=36))
    assert _price(_draft([("P1", 1)]), snap).flags == ()


def test_reporting_older_than_36h_flags_quote():
    snap = _snap(exported_at=SAMPLE_RUN_TIME - timedelta(hours=37))
    pq = _price(_draft([("P1", 1)]), snap)
    assert pq.flags[0].line is None and pq.flags[0].code == "reporting_stale"
    assert pq.flags[0].reason.startswith("pricing data is 37.0 h old (exported ")


def test_snapshot_is_immutable(snapshot):
    with pytest.raises(TypeError):
        snapshot.products["X"] = None  # type: ignore[index]
