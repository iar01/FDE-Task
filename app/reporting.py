"""Read-only loader for the nightly reporting snapshot (data/reporting_db).

This is the ONLY module that reads data/reporting_db. It opens files read-only and never writes.
Prices are parsed straight from CSV text into Decimal.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from app.clock import Clock
from app.money import parse_decimal

PRODUCTS = "products.csv"
CUSTOMERS = "customers.csv"
CONTRACT_PRICES = "contract_prices.csv"
APPROVERS = "approvers.csv"
METADATA = "export_metadata.json"


class ReportingDataError(ValueError):
    """The snapshot is malformed. Fail loudly at load time, not mid-pricing."""


@dataclass(frozen=True, slots=True)
class Product:
    part_number: str
    description: str
    category: str
    unit: str
    list_price: Decimal
    status: str
    replaced_by: str | None
    primary_warehouse: str


@dataclass(frozen=True, slots=True)
class Customer:
    customer_id: str
    name: str
    region: str
    pricing_tier: str


@dataclass(frozen=True, slots=True)
class ContractPrice:
    customer_id: str
    part_number: str
    contract_price: Decimal
    valid_from: date
    valid_to: date

    def is_valid_on(self, day: date) -> bool:
        return self.valid_from <= day <= self.valid_to


@dataclass(frozen=True, slots=True)
class Approver:
    approver_id: str
    name: str
    role: str
    region: str
    backup_approver_id: str | None
    out_of_office_from: date | None
    out_of_office_to: date | None

    def is_out_on(self, day: date) -> bool:
        """Out-of-office dates are inclusive at both ends (DECISIONS.md A14)."""
        if self.out_of_office_from is None:
            return False
        end = self.out_of_office_to or self.out_of_office_from
        return self.out_of_office_from <= day <= end


@dataclass(frozen=True)
class ReportingSnapshot:
    exported_at: datetime
    products: Mapping[str, Product]
    customers: Mapping[str, Customer]
    contract_prices: Mapping[tuple[str, str], tuple[ContractPrice, ...]]
    approvers: Mapping[str, Approver]
    source: str = field(default="")

    def __post_init__(self) -> None:
        if self.exported_at.tzinfo is None:
            raise ReportingDataError("exported_at must be timezone-aware")
        # Freeze the lookups so nothing downstream can mutate the snapshot.
        for name in ("products", "customers", "contract_prices", "approvers"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    def contracts_for(self, customer_id: str, part_number: str) -> tuple[ContractPrice, ...]:
        return self.contract_prices.get((customer_id, part_number), ())

    def age_hours(self, clock: Clock) -> Decimal:
        seconds = (clock.now() - self.exported_at).total_seconds()
        return Decimal(str(seconds)) / Decimal(3600)


# ---- loading ------------------------------------------------------------------------------------

def _rows(path: Path, required: set[str]) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as fh:  # read-only, always
        reader = csv.DictReader(fh)
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ReportingDataError(f"{path.name}: missing columns {sorted(missing)}")
        return list(reader)


def _opt(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _date(value: str | None, *, where: str) -> date | None:
    value = _opt(value)
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ReportingDataError(f"{where}: bad date {value!r}") from exc


def _unique(items, key, file: str) -> dict:
    out: dict = {}
    for item in items:
        k = key(item)
        if k in out:
            raise ReportingDataError(f"{file}: duplicate key {k!r}")
        out[k] = item
    return out


def load_snapshot(reporting_dir: Path) -> ReportingSnapshot:
    reporting_dir = Path(reporting_dir)

    meta = json.loads((reporting_dir / METADATA).read_text(encoding="utf-8"))
    try:
        exported_at = datetime.fromisoformat(meta["exported_at"])
    except (KeyError, ValueError) as exc:
        raise ReportingDataError(f"{METADATA}: missing or bad exported_at") from exc

    products = []
    for i, r in enumerate(_rows(reporting_dir / PRODUCTS, {
        "part_number", "description", "category", "unit", "list_price", "status", "replaced_by",
        "primary_warehouse"}), start=2):
        try:
            price = parse_decimal(r["list_price"], field=f"{PRODUCTS}:{i} list_price")
        except ValueError as exc:
            raise ReportingDataError(str(exc)) from exc
        if price < 0:
            raise ReportingDataError(f"{PRODUCTS}:{i} negative list_price")
        products.append(Product(
            part_number=r["part_number"].strip(), description=r["description"], category=r["category"],
            unit=r["unit"].strip(), list_price=price, status=r["status"].strip(),
            replaced_by=_opt(r["replaced_by"]), primary_warehouse=r["primary_warehouse"].strip()))

    customers = [
        Customer(customer_id=r["customer_id"].strip(), name=r["name"], region=r["region"].strip(),
                 pricing_tier=r["pricing_tier"].strip())
        for r in _rows(reporting_dir / CUSTOMERS, {"customer_id", "name", "region", "pricing_tier"})
    ]

    contracts: dict[tuple[str, str], list[ContractPrice]] = defaultdict(list)
    for i, r in enumerate(_rows(reporting_dir / CONTRACT_PRICES, {
        "customer_id", "part_number", "contract_price", "valid_from", "valid_to"}), start=2):
        where = f"{CONTRACT_PRICES}:{i}"
        try:
            price = parse_decimal(r["contract_price"], field=f"{where} contract_price")
        except ValueError as exc:
            raise ReportingDataError(str(exc)) from exc
        valid_from = _date(r["valid_from"], where=where)
        valid_to = _date(r["valid_to"], where=where)
        if valid_from is None or valid_to is None or valid_from > valid_to or price < 0:
            raise ReportingDataError(f"{where}: invalid contract row")
        cp = ContractPrice(r["customer_id"].strip(), r["part_number"].strip(), price, valid_from, valid_to)
        contracts[(cp.customer_id, cp.part_number)].append(cp)

    approvers = []
    for i, r in enumerate(_rows(reporting_dir / APPROVERS, {
        "approver_id", "name", "role", "region", "backup_approver_id", "out_of_office_from",
        "out_of_office_to"}), start=2):
        where = f"{APPROVERS}:{i}"
        approvers.append(Approver(
            approver_id=r["approver_id"].strip(), name=r["name"], role=r["role"].strip(),
            region=r["region"].strip(), backup_approver_id=_opt(r["backup_approver_id"]),
            out_of_office_from=_date(r["out_of_office_from"], where=where),
            out_of_office_to=_date(r["out_of_office_to"], where=where)))

    return ReportingSnapshot(
        exported_at=exported_at,
        products=_unique(products, lambda p: p.part_number, PRODUCTS),
        customers=_unique(customers, lambda c: c.customer_id, CUSTOMERS),
        contract_prices={k: tuple(v) for k, v in contracts.items()},
        approvers=_unique(approvers, lambda a: a.approver_id, APPROVERS),
        source=str(meta.get("source", "")),
    )
