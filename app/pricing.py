"""Pure pricing (approval_rules.md rules 3-6, 10; DECISIONS.md A3, A6-A8, A12, A15, A16).

No I/O: receives the draft, the reporting snapshot and a clock; returns a PricedQuote.
Nothing is rounded here; rounding happens only at output boundaries.
"""
from __future__ import annotations

from decimal import Decimal

from zoneinfo import ZoneInfo

from app.clock import BUSINESS_TZ, Clock, business_date
from app.models import DraftQuote, Flag, PricedLine, PricedQuote
from app.money import HUNDRED
from app.reporting import ContractPrice, ReportingSnapshot


def price_quote(
    draft: DraftQuote,
    snapshot: ReportingSnapshot,
    clock: Clock,
    *,
    reporting_max_age_hours: Decimal,
    tz: ZoneInfo = BUSINESS_TZ,  # the pipeline passes rules.timezone
) -> PricedQuote:
    today = business_date(clock, tz)
    discount_factor = 1 - draft.proposed_discount_pct / HUNDRED
    flags: list[Flag] = []

    customer = snapshot.customers.get(draft.customer_id)
    if customer is None:
        # A8: price at list, region unknown; never guess a region.
        flags.append(Flag(line=None, code="customer_not_found",
                          reason=f"customer {draft.customer_id} not found in reporting data; "
                                 "list prices used, region unknown"))

    priced: list[PricedLine] = []
    for dl in draft.lines:
        product = snapshot.products.get(dl.part_number)
        if product is None:
            # A7: leave the line out of the totals and say so.
            flags.append(Flag(line=dl.line, code="product_not_found",
                              reason=f"part {dl.part_number} not found in reporting data; line not priced"))
            priced.append(PricedLine(line=dl.line, part_number=dl.part_number, quantity=dl.quantity,
                                     extraction_confidence=dl.extraction_confidence,
                                     price_source="unpriced", list_price=None, unit_price=None,
                                     list_extended=None, line_net=None))
            continue

        if product.status == "discontinued":
            # A6: flag, keep the requested part and price; a person decides on substitution.
            flags.append(Flag(line=dl.line, code="part_discontinued",
                              reason=f"part discontinued; replaced by {product.replaced_by or 'none listed'}"))

        contracts = snapshot.contracts_for(customer.customer_id, dl.part_number) if customer else ()
        contract, contract_flag = _select_contract(contracts, today, dl.line)
        if contract_flag is not None:
            flags.append(contract_flag)

        if contract is not None:
            unit_price, source = contract.contract_price, "contract"
        else:
            unit_price, source = product.list_price, "list"

        priced.append(PricedLine(
            line=dl.line, part_number=dl.part_number, quantity=dl.quantity, price_source=source,
            extraction_confidence=dl.extraction_confidence,
            list_price=product.list_price, unit_price=unit_price,
            list_extended=product.list_price * dl.quantity,
            line_net=unit_price * dl.quantity * discount_factor,
        ))

    unpriced = [p.line for p in priced if p.price_source == "unpriced"]
    if unpriced:
        flags.append(Flag(line=None, code="pricing_incomplete",
                          reason=f"pricing incomplete: line(s) {', '.join(map(str, unpriced))} not priced"))

    list_total = sum((p.list_extended for p in priced if p.list_extended is not None), Decimal(0))
    net_total = sum((p.line_net for p in priced if p.line_net is not None), Decimal(0))
    # Rule 6: measured against LIST price, so contract savings count toward the discount.
    effective = (1 - net_total / list_total) * HUNDRED if list_total > 0 else None

    age = snapshot.age_hours(clock)
    if age > reporting_max_age_hours:
        flags.append(Flag(line=None, code="reporting_stale",
                          reason=f"pricing data is {age:.1f} h old (exported {snapshot.exported_at.isoformat()})"))

    return PricedQuote(
        draft_id=draft.draft_id,
        rfq_id=draft.rfq_id,
        customer_id=draft.customer_id,
        prepared_by=draft.prepared_by,
        created_at=draft.created_at,
        region=customer.region if customer else None,
        proposed_discount_pct=draft.proposed_discount_pct,
        lines=tuple(priced),
        list_total=list_total,
        net_total=net_total,
        effective_discount_pct=effective,
        flags=tuple(flags),
        reporting_exported_at=snapshot.exported_at,
        reporting_age_hours=age,
    )


def _select_contract(
    contracts: tuple[ContractPrice, ...], today, line: int
) -> tuple[ContractPrice | None, Flag | None]:
    """Rule 3: a contract valid today wins. Rule 4: expired-only -> list price + flag.

    A16: several valid rows -> latest valid_from, and flag the conflict for the specialist.
    """
    valid = [c for c in contracts if c.is_valid_on(today)]
    if len(valid) == 1:
        return valid[0], None
    if len(valid) > 1:
        chosen = max(valid, key=lambda c: c.valid_from)
        return chosen, Flag(line=line, code="contract_conflict",
                            reason=f"{len(valid)} contract prices valid today; used the one from "
                                   f"{chosen.valid_from.isoformat()}")
    expired = [c for c in contracts if c.valid_to < today]
    if expired:
        last = max(c.valid_to for c in expired)
        return None, Flag(line=line, code="contract_expired",
                          reason=f"contract price expired {last.isoformat()}; list price used")
    return None, None
