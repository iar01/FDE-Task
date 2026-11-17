"""Decimal helpers. Money and percentages are Decimal end-to-end; rounding happens only at output."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
HUNDRED = Decimal(100)


def parse_decimal(raw: str, *, field: str) -> Decimal:
    """Parse a CSV/text value straight into Decimal (never via float)."""
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"{field}: not a decimal: {raw!r}") from exc
    if not value.is_finite():
        raise ValueError(f"{field}: not a finite decimal: {raw!r}")
    return value


def to_money(value: Decimal) -> Decimal:
    """Output boundary only: USD to 2 dp, ROUND_HALF_UP."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def to_pct(value: Decimal) -> Decimal:
    """Output boundary only: percentage to 2 dp, ROUND_HALF_UP."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)
