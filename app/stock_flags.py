"""Pure: turn stock results into specialist-review flags (approval_rules.md rules 9-10, DECISIONS.md A5).

The quote always continues to routing; stock problems add review, they never block or guess.
"""
from __future__ import annotations

from typing import Mapping

from app.models import DraftQuote, Flag
from app.stock_client import StockResult


def stock_flags(draft: DraftQuote, stock: Mapping[str, StockResult]) -> tuple[Flag, ...]:
    flags: list[Flag] = []
    unavailable_attempts: list[int] = []
    for dl in draft.lines:
        r = stock.get(dl.part_number)
        if r is None or r.status == "unavailable":
            unavailable_attempts.append(r.attempts if r else 0)
            continue
        if r.status == "not_found":
            flags.append(Flag(line=dl.line, code="stock_not_found", reason="part not found in stock API"))
            continue
        total = r.total_on_hand or 0
        if total == 0:
            flags.append(Flag(line=dl.line, code="out_of_stock", reason="out of stock in all warehouses"))
        elif total < dl.quantity:
            # A5 (proposal, not in policy): informational; adds review, never removes it.
            flags.append(Flag(line=dl.line, code="partial_stock",
                              reason=f"partial stock: have {total}, need {dl.quantity}"))
        if r.status == "stale":
            flags.append(Flag(line=dl.line, code="stock_stale",
                              reason=f"stock reading {r.age_hours:.1f} h old (as_of {r.as_of.isoformat()})"))
    if unavailable_attempts:
        n = max(unavailable_attempts)
        reason = f"stock unavailable after {n} attempts" if n else "stock not checked"
        flags.insert(0, Flag(line=None, code="stock_unavailable", reason=reason))
    return tuple(flags)
