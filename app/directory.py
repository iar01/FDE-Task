"""People lookups (approvers, regions, backups, out-of-office), built from the reporting snapshot.

Approvers come only from data/reporting_db/approvers.csv (loaded by app/reporting.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from app.reporting import Approver, ReportingSnapshot


@dataclass(frozen=True)
class Directory:
    approvers: Mapping[str, Approver]
    customer_regions: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "approvers", MappingProxyType(dict(self.approvers)))

    @classmethod
    def from_snapshot(cls, snapshot: ReportingSnapshot) -> "Directory":
        return cls(approvers=snapshot.approvers,
                   customer_regions=frozenset(c.region for c in snapshot.customers.values()))

    def with_role(self, role: str, region: str | None = None) -> list[Approver]:
        return [a for a in self.approvers.values()
                if a.role == role and (region is None or a.region == region)]
