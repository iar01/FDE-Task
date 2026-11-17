"""Load and validate rules/approval_rules.yaml. Invalid rules -> refuse to start with a clear error."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.directory import Directory


class RulesError(ValueError):
    """The rules file is invalid or inconsistent with approvers.csv. The service must not start."""


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Condition(_Strict):
    discount_pct_gt: Decimal | None = None
    net_total_gte: Decimal | None = None

    @property
    def is_empty(self) -> bool:
        return self.discount_pct_gt is None and self.net_total_gte is None


class ApproverSpec(_Strict):
    role: str
    match_region: Literal["customer"] | None = None


class Tier(_Strict):
    name: str
    when: Condition | None = None
    when_any: tuple[Condition, ...] | None = None
    approver: ApproverSpec | None

    @model_validator(mode="after")
    def _one_condition_form(self) -> "Tier":
        if (self.when is None) == (self.when_any is None):
            raise ValueError(f"tier {self.name!r}: exactly one of 'when' or 'when_any' is required")
        if self.when_any is not None and (not self.when_any or any(c.is_empty for c in self.when_any)):
            raise ValueError(f"tier {self.name!r}: 'when_any' needs non-empty conditions")
        return self

    @property
    def is_catch_all(self) -> bool:
        return self.when is not None and self.when.is_empty


class SpecialistSignoff(_Strict):
    role: str
    assign: Literal["prepared_by"]


class Escalation(_Strict):
    enabled: bool = False
    pending_sla_minutes: int = Field(gt=0)


class ApprovalRules(_Strict):
    version: str
    timezone: str
    low_confidence_threshold: Decimal = Field(ge=0, le=1)
    stock_max_age_hours: Decimal = Field(gt=0)
    reporting_max_age_hours: Decimal = Field(gt=0)
    duplicate_window_hours: Decimal = Field(gt=0)
    tiers: tuple[Tier, ...] = Field(min_length=1)
    specialist_signoff: SpecialistSignoff
    escalation: Escalation

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v

    @model_validator(mode="after")
    def _tiers(self) -> "ApprovalRules":
        names = [t.name for t in self.tiers]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate tier names: {names}")
        if not self.tiers[-1].is_catch_all:
            raise ValueError("the last tier must be a catch-all (when: {})")
        early = [t.name for t in self.tiers[:-1] if t.is_catch_all]
        if early:
            raise ValueError(f"catch-all tier(s) {early} before the last tier make later tiers unreachable")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class LoadedRules:
    rules: ApprovalRules
    content_hash: str  # sha256 of the file bytes; recorded with every routing decision

    @property
    def version(self) -> str:
        return self.rules.version


def parse_rules(text: str) -> LoadedRules:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RulesError(f"approval rules: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise RulesError("approval rules: top level must be a mapping")
    try:
        rules = ApprovalRules.model_validate(raw)
    except ValidationError as exc:
        raise RulesError(f"approval rules invalid:\n{exc}") from exc
    return LoadedRules(rules, hashlib.sha256(text.encode("utf-8")).hexdigest())


def validate_against_directory(rules: ApprovalRules, directory: Directory) -> None:
    """Every role a tier needs exists, backups exist, no backup cycles. Raises RulesError."""
    problems: list[str] = []
    for tier in rules.tiers:
        spec = tier.approver
        if spec is None:
            continue
        if spec.match_region == "customer":
            for region in sorted(directory.customer_regions):
                found = directory.with_role(spec.role, region)
                if len(found) != 1:
                    problems.append(f"tier {tier.name!r}: need exactly one {spec.role} for region "
                                    f"{region!r}, found {[a.approver_id for a in found]}")
        else:
            found = directory.with_role(spec.role)
            if len(found) != 1:
                problems.append(f"tier {tier.name!r}: need exactly one {spec.role}, "
                                f"found {[a.approver_id for a in found]}")
    if not directory.with_role(rules.specialist_signoff.role):
        problems.append(f"specialist_signoff: no approver with role {rules.specialist_signoff.role!r}")

    for a in directory.approvers.values():
        if a.backup_approver_id and a.backup_approver_id not in directory.approvers:
            problems.append(f"{a.approver_id}: backup_approver_id {a.backup_approver_id!r} does not exist")
    # Cycles are refused for chains that start at a tier approver (a manager/VP request could loop).
    # The quote_specialist pool in approvers.csv is a deliberate rotation (S01 -> S02 -> S03 -> S01);
    # it is allowed, and the router detects the loop at runtime if every specialist is out.
    tier_roles = {t.approver.role for t in rules.tiers if t.approver}
    for start in (a.approver_id for a in directory.approvers.values() if a.role in tier_roles):
        seen, cur = [start], directory.approvers[start].backup_approver_id
        while cur and cur in directory.approvers:
            if cur in seen:
                problems.append(f"backup cycle: {' -> '.join(seen + [cur])}")
                break
            seen.append(cur)
            cur = directory.approvers[cur].backup_approver_id
    if problems:
        raise RulesError("approval rules inconsistent with approvers.csv:\n  - " + "\n  - ".join(problems))


def load_rules(path: Path, directory: Directory) -> LoadedRules:
    loaded = parse_rules(Path(path).read_text(encoding="utf-8"))
    validate_against_directory(loaded.rules, directory)
    return loaded
