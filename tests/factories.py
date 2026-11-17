"""Shared test builders: in-memory rules (not the YAML), people directory, priced quotes, fake stock."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx

from app.clock import SAMPLE_RUN_TIME, FixedClock
from app.directory import Directory
from app.models import Flag, PricedLine, PricedQuote
from app.reporting import Approver
from app.rules import (ApprovalRules, ApproverSpec, Condition, Escalation, LoadedRules, SpecialistSignoff,
                       Tier)
from app.stock_client import StockClient

ROOT = Path(__file__).resolve().parents[1]
CLOCK = FixedClock(SAMPLE_RUN_TIME)
OOO = (date(2026, 10, 12), date(2026, 10, 16))

# The policy from data/approval_rules.md, built in code so router tests don't depend on the YAML file.
POLICY = ApprovalRules(
    version="in-memory-test",
    timezone="America/Chicago",
    low_confidence_threshold=Decimal("0.85"),
    stock_max_age_hours=Decimal(24),
    reporting_max_age_hours=Decimal(36),
    duplicate_window_hours=Decimal(24),
    tiers=(
        Tier(name="vp", when=Condition(discount_pct_gt=Decimal(25)), approver=ApproverSpec(role="vp_sales")),
        Tier(name="regional_manager",
             when_any=(Condition(discount_pct_gt=Decimal(15)), Condition(net_total_gte=Decimal(25000))),
             approver=ApproverSpec(role="regional_manager", match_region="customer")),
        Tier(name="specialist_only", when=Condition(), approver=None),
    ),
    specialist_signoff=SpecialistSignoff(role="quote_specialist", assign="prepared_by"),
    escalation=Escalation(enabled=False, pending_sla_minutes=120),
)
RULES = LoadedRules(POLICY, content_hash="in-memory")


def A(id, role, region="ALL", backup=None, ooo=None) -> Approver:
    return Approver(id, id, role, region, backup, *(ooo or (None, None)))


def people(**override: Approver) -> Directory:
    """Shaped like approvers.csv: RMs back up to DIR, DIR to VP, VP has no backup."""
    base = {
        "RW": A("RW", "regional_manager", "West", "DIR"),
        "RC": A("RC", "regional_manager", "Central", "DIR"),
        "DIR": A("DIR", "sales_operations_director", "ALL", "VP"),
        "VP": A("VP", "vp_sales"),
        "S1": A("S1", "quote_specialist", backup="S2"),
        "S2": A("S2", "quote_specialist", backup="S1"),
    }
    base.update(override)
    return Directory(base, frozenset({"West", "Central"}))


def priced(eff="10", net="1000", region="West", prepared_by="S1", confidences=("0.97",), flags=()) -> PricedQuote:
    lines = tuple(PricedLine(line=i, part_number=f"P{i}", quantity=1, extraction_confidence=Decimal(c),
                             price_source="list", list_price=Decimal(1), unit_price=Decimal(1),
                             list_extended=Decimal(1), line_net=Decimal(1))
                  for i, c in enumerate(confidences, start=1))
    return PricedQuote(
        draft_id="DQ-T", rfq_id="R", customer_id="C", prepared_by=prepared_by, created_at=SAMPLE_RUN_TIME,
        region=region, proposed_discount_pct=Decimal(0), lines=lines, list_total=Decimal(net),
        net_total=Decimal(net), effective_discount_pct=None if eff is None else Decimal(eff),
        flags=tuple(flags), reporting_exported_at=SAMPLE_RUN_TIME, reporting_age_hours=Decimal(1))


def fixture_stock(clock=CLOCK, max_age_hours=Decimal(24)) -> StockClient:
    """A StockClient whose transport serves data/stock_fixture.json: no network, deterministic."""
    fixture = json.loads((ROOT / "data" / "stock_fixture.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        pns = request.url.params["part_numbers"].split(",")
        return httpx.Response(200, json={"items": [fixture[p] for p in pns if p in fixture],
                                         "not_found": [p for p in pns if p not in fixture]})
    return StockClient("http://stock.test", clock, max_age_hours=max_age_hours,
                       transport=httpx.MockTransport(handler), sleep=lambda s: None)


def flag_codes(flags: tuple[Flag, ...]) -> list[tuple[int | None, str]]:
    return [(f.line, f.code) for f in flags]
