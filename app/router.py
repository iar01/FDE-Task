"""Pure approval routing (approval_rules.md rules 1, 6-8; DECISIONS.md A1-A3, A8, A10, A13, A14).

No I/O: receives the priced quote, validated rules, the people directory and a clock.
Flags only ever add review; nothing here can lower a tier or drop a required approver.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence

from app.clock import Clock
from app.directory import Directory
from app.models import Flag, PricedQuote, RoutingDecision
from app.money import to_money, to_pct
from app.rules import Condition, LoadedRules, Tier


def route(
    priced: PricedQuote,
    rules: LoadedRules,
    directory: Directory,
    clock: Clock,
    *,
    extra_flags: Iterable[Flag] = (),
) -> RoutingDecision:
    """extra_flags: flags from other adapters (e.g. stock) that the specialist must also see."""
    r = rules.rules
    today = clock.now().astimezone(r.tz).date()
    flags: list[Flag] = [*priced.flags, *extra_flags]
    trace: list[str] = []

    # Rule 8: low-confidence lines always go to the specialist first.
    for pl in priced.lines:
        if pl.extraction_confidence < r.low_confidence_threshold:
            flags.append(Flag(line=pl.line, code="low_confidence",
                              reason=f"extraction confidence {pl.extraction_confidence}"))
            trace.append(f"line {pl.line} extraction confidence {pl.extraction_confidence} "
                         f"< {r.low_confidence_threshold} -> specialist review")

    # Rules table: tier from effective discount AND net total.
    tier, why = _select_tier(priced, r.tiers)
    approver_id = backup_of = None
    if tier.approver is None:
        trace.append(f"{why} -> tier {tier.name}")
    else:
        spec = tier.approver
        if spec.match_region == "customer" and priced.region is None:
            flags.append(Flag(line=None, code="approver_unknown",
                              reason=f"customer region unknown; {spec.role} cannot be determined"))
            trace.append(f"{why} -> tier {tier.name} -> region unknown, no approver assigned")
        else:
            region = priced.region if spec.match_region == "customer" else None
            candidates = directory.with_role(spec.role, region)
            label = f"tier {tier.name}" + (f" ({region})" if region else "")
            if len(candidates) != 1:  # validation prevents this; never guess
                flags.append(Flag(line=None, code="approver_unknown",
                                  reason=f"no unique {spec.role}{' for ' + region if region else ''}"))
                trace.append(f"{why} -> {label} -> no unique approver")
            else:
                required = candidates[0].approver_id
                trace.append(f"{why} -> {label} -> {required}")
                approver_id, steps, flag = _first_available(required, directory, today, "required approver")
                trace.extend(steps)
                if flag:
                    flags.append(flag)
                backup_of = required if approver_id != required else None

    # Rule 1: a specialist signs off on every quote (DECISIONS.md A1: prepared_by, backup if out).
    specialist_id = None
    signoff = r.specialist_signoff
    prepared = directory.approvers.get(priced.prepared_by)
    if prepared is None or prepared.role != signoff.role:
        flags.append(Flag(line=None, code="specialist_unknown",
                          reason=f"prepared_by {priced.prepared_by} is not a known {signoff.role}"))
        trace.append(f"specialist sign-off: prepared_by {priced.prepared_by} not a {signoff.role} -> unassigned")
    else:
        specialist_id, steps, flag = _first_available(prepared.approver_id, directory, today, "specialist")
        trace.append(f"specialist sign-off: {prepared.approver_id} (prepared_by)")
        trace.extend(steps)
        if flag:
            flags.append(flag)

    return RoutingDecision(
        draft_id=priced.draft_id,
        status="specialist_only" if tier.approver is None else "approver_required",
        tier=tier.name,
        approver_id=approver_id,
        routed_via_backup_of=backup_of,
        specialist_id=specialist_id,
        specialist_review_flags=tuple(sorted(flags, key=lambda f: (f.line is None, f.line or 0))),
        rules_version=rules.version,
        rules_hash=rules.content_hash,
        trace=tuple(trace),
    )


# ---- tiers --------------------------------------------------------------------------------------

def _select_tier(priced: PricedQuote, tiers: Sequence[Tier]) -> tuple[Tier, str]:
    for tier in tiers:
        if tier.is_catch_all:
            return tier, f"no approver tier matched ({_facts(priced)})"
        conds = (tier.when,) if tier.when is not None else tier.when_any
        matched = [c for c in conds if _holds(c, priced)]
        if matched and (tier.when_any is not None or len(matched) == len(conds)):
            return tier, " and ".join(_describe(c, priced) for c in matched)
    raise AssertionError("validated rules always end with a catch-all tier")


def _holds(c: Condition, q: PricedQuote) -> bool:
    ok = True
    if c.discount_pct_gt is not None:
        # Unknown discount (nothing priceable) is treated as exceeding: never skip an approver.
        ok &= q.effective_discount_pct is None or q.effective_discount_pct > c.discount_pct_gt
    if c.net_total_gte is not None:
        ok &= q.net_total >= c.net_total_gte
    return ok


def _describe(c: Condition, q: PricedQuote) -> str:
    parts = []
    if c.discount_pct_gt is not None:
        parts.append("effective discount unknown (treated as exceeding " f"{c.discount_pct_gt})"
                     if q.effective_discount_pct is None
                     else f"effective discount {to_pct(q.effective_discount_pct)}% > {c.discount_pct_gt}")
    if c.net_total_gte is not None:
        parts.append(f"net {to_money(q.net_total)} >= {c.net_total_gte}")
    return " and ".join(parts)


def _facts(q: PricedQuote) -> str:
    d = "unknown" if q.effective_discount_pct is None else f"{to_pct(q.effective_discount_pct)}%"
    return f"effective discount {d}, net {to_money(q.net_total)}"


# ---- availability / backup chain ----------------------------------------------------------------

def _first_available(start: str, directory: Directory, today: date, label: str
                     ) -> tuple[str, list[str], Flag | None]:
    """Follow backup_approver_id while the person is out today (OOO dates inclusive, A14).

    Chain ends with nobody available -> keep the last person and flag it (A2). Cycles are detected.
    """
    steps: list[str] = []
    seen = [start]
    cur = start
    while True:
        person = directory.approvers[cur]
        if not person.is_out_on(today):
            return cur, steps, None
        ooo = (f"{cur} out of office {person.out_of_office_from.isoformat()}.."
               f"{(person.out_of_office_to or person.out_of_office_from).isoformat()}")
        nxt = person.backup_approver_id
        if nxt is None or nxt not in directory.approvers or nxt in seen:
            why = "no backup" if nxt is None else (f"backup {nxt} unknown" if nxt not in directory.approvers
                                                   else f"backup chain loops back to {nxt}")
            steps.append(f"{ooo}, {why} -> stays with {cur}")
            return cur, steps, Flag(line=None, code="no_backup_available",
                                    reason=f"{label} {cur} out of office, no backup available")
        steps.append(f"{ooo} -> backup {nxt}")
        seen.append(nxt)
        cur = nxt


# ---- escalation (proposal; off unless rules.escalation.enabled) ---------------------------------

@dataclass(frozen=True)
class PendingApproval:
    draft_id: str
    approver_id: str
    pending_since: datetime  # reset by the caller whenever the item is (re)assigned


@dataclass(frozen=True)
class Reassignment:
    draft_id: str
    from_approver_id: str
    to_approver_id: str | None  # None: nobody else available; alert a human instead
    overdue_minutes: int
    reason: str


def escalate_overdue(
    pending: Iterable[PendingApproval],
    rules: LoadedRules,
    directory: Directory,
    clock: Clock,
) -> tuple[Reassignment, ...]:
    """Pure. A scheduler would call this and apply/audit the reassignments it returns."""
    esc = rules.rules.escalation
    if not esc.enabled:
        return ()
    now = clock.now()
    today = now.astimezone(rules.rules.tz).date()
    sla = timedelta(minutes=esc.pending_sla_minutes)
    out: list[Reassignment] = []
    for p in pending:
        waited = now - p.pending_since
        if waited <= sla:
            continue
        minutes = int(waited.total_seconds() // 60)
        person = directory.approvers.get(p.approver_id)
        backup = person.backup_approver_id if person else None
        target = None
        if backup and backup in directory.approvers:
            target, _, flag = _first_available(backup, directory, today, "backup")
            if flag or target == p.approver_id:
                target = None
        reason = (f"pending {minutes} min > SLA {esc.pending_sla_minutes} min -> "
                  + (f"reassigned to backup {target}" if target else "no available backup; alert operations"))
        out.append(Reassignment(p.draft_id, p.approver_id, target, minutes, reason))
    return tuple(out)
