"""Injectable time source. Every function that depends on time takes a Clock."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

# Ridgeline's business calendar (contract validity, out-of-office days) is Central time.
BUSINESS_TZ = ZoneInfo("America/Chicago")

# approval_rules.md: "Assume the current time is Monday, October 12, 2026, 09:30 Central".
SAMPLE_RUN_TIME = datetime.fromisoformat("2026-10-12T09:30:00-05:00")


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class FixedClock:
    at: datetime

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")

    def now(self) -> datetime:
        return self.at


def business_date(clock: Clock, tz: ZoneInfo = BUSINESS_TZ):
    """Today's date on Ridgeline's business calendar (rules.timezone; Central by default)."""
    return clock.now().astimezone(tz).date()
