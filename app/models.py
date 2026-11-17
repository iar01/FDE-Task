"""Domain models. Money and percentages are Decimal and stay unrounded inside the service."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.money import to_money, to_pct


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---- input from the extraction team -------------------------------------------------------------

class DraftLine(_Frozen):
    line: int = Field(ge=1)
    part_number: str
    description: str
    quantity: int = Field(gt=0)
    unit: str
    extraction_confidence: Decimal = Field(ge=0, le=1)


class DraftQuote(_Frozen):
    draft_id: str
    rfq_id: str
    customer_id: str
    created_at: datetime
    prepared_by: str
    proposed_discount_pct: Decimal = Field(ge=0, le=100)
    lines: tuple[DraftLine, ...] = Field(min_length=1)
    note: str | None = None


# ---- pricing ------------------------------------------------------------------------------------

class Flag(_Frozen):
    """A reason the specialist must look at the quote. line=None means the whole quote."""
    line: int | None
    reason: str
    code: str  # machine-readable, e.g. "contract_expired"; not part of routing_results.json


PriceSource = Literal["contract", "list", "unpriced"]


class PricedLine(_Frozen):
    line: int
    part_number: str
    quantity: int
    extraction_confidence: Decimal
    price_source: PriceSource
    list_price: Decimal | None      # None only when the part is unknown
    unit_price: Decimal | None      # contract or list price before the proposed discount
    list_extended: Decimal | None   # list_price * quantity
    line_net: Decimal | None        # unit_price * quantity * (1 - discount), unrounded


class PricedQuote(_Frozen):
    draft_id: str
    rfq_id: str
    customer_id: str
    prepared_by: str
    created_at: datetime
    region: str | None
    proposed_discount_pct: Decimal
    lines: tuple[PricedLine, ...]
    list_total: Decimal
    net_total: Decimal
    # Unrounded; tiers compare this value (DECISIONS.md A3). None when list_total is 0.
    effective_discount_pct: Decimal | None
    flags: tuple[Flag, ...]
    reporting_exported_at: datetime
    reporting_age_hours: Decimal

    @property
    def net_total_out(self) -> Decimal:
        return to_money(self.net_total)

    @property
    def list_total_out(self) -> Decimal:
        return to_money(self.list_total)

    @property
    def effective_discount_pct_out(self) -> Decimal | None:
        return None if self.effective_discount_pct is None else to_pct(self.effective_discount_pct)


# ---- routing (filled in by Step 4) --------------------------------------------------------------

RoutingStatus = Literal["specialist_only", "approver_required", "duplicate"]


class RoutingDecision(_Frozen):
    draft_id: str
    status: RoutingStatus
    tier: str | None                # rules tier name, e.g. "regional_manager"
    approver_id: str | None         # after backup substitution
    routed_via_backup_of: str | None = None
    specialist_id: str | None       # who does specialist review / final sign-off
    specialist_review_flags: tuple[Flag, ...] = ()
    duplicate_of: str | None = None
    rules_version: str | None = None
    rules_hash: str | None = None
    trace: tuple[str, ...] = ()
