"""Pure review workflow: states, legal transitions and who may perform each step.

approval_rules.md rule 1 ("A quote specialist gives final approval on every quote") + the table
"Who must approve (after specialist review)":

  approver_required: pending_specialist_review -> pending_approver -> pending_final_signoff -> approved
  specialist_only:   pending_specialist_review -> pending_final_signoff -> approved
  any pending state -> rejected (reason required)
  duplicate: no approval request exists; every action is illegal.

Nothing reaches "approved" except through a human's final sign-off. "approved" = ready to send; we never send.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.directory import Directory
from app.models import RoutingDecision
from app.reporting import Approver

PENDING_REVIEW = "pending_specialist_review"
PENDING_APPROVER = "pending_approver"
PENDING_SIGNOFF = "pending_final_signoff"
APPROVED, REJECTED, DUPLICATE = "approved", "rejected", "duplicate"
PENDING = (PENDING_REVIEW, PENDING_APPROVER, PENDING_SIGNOFF)

SPECIALIST_ROLE = "quote_specialist"

Step = Literal["specialist_review", "approve", "final_signoff", "reject"]

# step -> (allowed from-states, audit action)
STEPS: dict[str, tuple[tuple[str, ...], str]] = {
    "specialist_review": ((PENDING_REVIEW,), "specialist_reviewed"),
    "approve": ((PENDING_APPROVER,), "approver_approved"),
    "final_signoff": ((PENDING_SIGNOFF,), "final_signoff"),
    "reject": (PENDING, "rejected"),
}


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str


def next_state(step: str, current: str, routing: RoutingDecision | None) -> str | None:
    """The state after `step`, or None if the transition is illegal from `current`."""
    allowed, _ = STEPS[step]
    if routing is None or current not in allowed:  # routing None = duplicate: no approval request
        return None
    if step == "specialist_review":
        return PENDING_APPROVER if routing.status == "approver_required" else PENDING_SIGNOFF
    if step == "approve":
        return PENDING_SIGNOFF
    if step == "final_signoff":
        return APPROVED
    return REJECTED


def may_do_specialist_step(user: Approver, routing: RoutingDecision, directory: Directory, today: date) -> Verdict:
    """DECISIONS.md A1: the assigned specialist (prepared_by, or backup if out); any specialist if they're unavailable."""
    if user.role != SPECIALIST_ROLE:
        return Verdict(False, f"separation of duties: {user.approver_id} ({user.role}) cannot perform "
                              "specialist steps; only a quote_specialist can")
    assigned = routing.specialist_id
    if assigned is None or user.approver_id == assigned:
        return Verdict(True, "assigned specialist" if assigned else "no specialist assigned; any specialist may act")
    person = directory.approvers.get(assigned)
    if person is None or person.is_out_on(today):
        return Verdict(True, f"assigned specialist {assigned} is unavailable; any specialist may act (A1)")
    return Verdict(False, f"quote is assigned to specialist {assigned}; another specialist may act only "
                          f"while {assigned} is out of office (DECISIONS.md A1)")


def may_do_approver_step(user: Approver, routing: RoutingDecision) -> Verdict:
    """Only the routed approver_id (backup already applied at routing time)."""
    if user.role == SPECIALIST_ROLE:
        return Verdict(False, f"separation of duties: specialist {user.approver_id} cannot perform the approver step")
    if routing.approver_id is None:
        return Verdict(False, "no approver is assigned to this quote; a specialist must resolve it")
    if user.approver_id != routing.approver_id:
        why = (f"; {user.approver_id} is out of office, so it was routed to backup {routing.approver_id}"
               if routing.routed_via_backup_of == user.approver_id else "")
        return Verdict(False, f"only the routed approver {routing.approver_id} may approve{why}")
    return Verdict(True, "routed approver")


def may_reject(user: Approver, routing: RoutingDecision, directory: Directory, today: date) -> Verdict:
    if user.role == SPECIALIST_ROLE:
        return may_do_specialist_step(user, routing, directory, today)
    return may_do_approver_step(user, routing)


def check(step: str, user: Approver, routing: RoutingDecision, directory: Directory, today: date) -> Verdict:
    if step == "approve":
        return may_do_approver_step(user, routing)
    if step == "reject":
        return may_reject(user, routing, directory, today)
    return may_do_specialist_step(user, routing, directory, today)
