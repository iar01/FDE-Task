"""End-to-end: drafts -> duplicate check -> pricing -> stock -> routing -> store + audit.

Business logic stays in pure functions (duplicates, pricing, stock_flags, router); this module wires
the adapters (store, stock client) around them. Nothing here sends a quote or writes to SAP/data/.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Sequence

from app.clock import Clock
from app.directory import Directory
from app.duplicates import Original, find_original, fingerprint
from app.models import DraftQuote, Flag, PricedQuote, RoutingDecision
from app.money import to_money, to_pct
from app.pricing import price_quote
from app.reporting import ReportingSnapshot
from app.router import route
from app.rules import LoadedRules
from app.stock_client import StockClient
from app.stock_flags import stock_flags
from app.store import Store

log = logging.getLogger("app.pipeline")

SYSTEM = "system"
# Workflow states (Step 6 adds the human transitions out of these).
RECEIVED, PENDING_SPECIALIST, DUPLICATE = "received", "pending_specialist_review", "duplicate"


@dataclass(frozen=True)
class ProcessResult:
    draft_id: str
    status: str                              # specialist_only | approver_required | duplicate
    net_total: Decimal | None
    effective_discount_pct: Decimal | None
    approver_id: str | None
    flags: tuple[Flag, ...]
    duplicate_of: str | None
    priced: PricedQuote | None = None
    routing: RoutingDecision | None = None
    trace: tuple[str, ...] = ()
    reused: bool = False                     # already in the store from an earlier run
    workflow_status: str | None = None


def content_hash(draft: DraftQuote) -> str:
    payload = json.dumps(draft.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Pipeline:
    snapshot: ReportingSnapshot
    directory: Directory
    rules: LoadedRules
    stock: StockClient
    store: Store
    clock: Clock                             # business "as of" time (OOO, staleness, contract dates)
    _batch: dict[str, ProcessResult] = field(default_factory=dict, init=False)

    def process(self, drafts: Sequence[DraftQuote]) -> list[ProcessResult]:
        r = self.rules.rules
        window = timedelta(hours=float(r.duplicate_window_hours))
        self._batch = {}

        # Pass 1: classify (already stored / duplicate / new), so duplicates never hit pricing or stock.
        plan: list[tuple[str, DraftQuote, Any]] = []
        batch_originals: list[Original] = []
        seen: set[str] = set()
        for d in drafts:
            if d.draft_id in seen or self.store.get_quote(d.draft_id):
                plan.append(("existing", d, None))
                seen.add(d.draft_id)
                continue
            seen.add(d.draft_id)
            fp = fingerprint(d)
            original = find_original(d, fp, [*self.store.originals(fp), *batch_originals], window)
            if original:
                plan.append(("duplicate", d, (fp, original)))
            else:
                batch_originals.append(Original(d.draft_id, fp, d.created_at))
                plan.append(("new", d, fp))

        # Pass 2: one bulk stock lookup (chunked by the client) for every new draft's parts.
        new_parts = [l.part_number for kind, d, _ in plan if kind == "new" for l in d.lines]
        stock = self.stock.fetch(new_parts) if new_parts else {}

        # Pass 3: price, route, persist + audit, in input order.
        results: list[ProcessResult] = []
        for kind, d, extra in plan:
            if kind == "new":
                res = self._new(d, extra, stock)
            elif kind == "duplicate":
                res = self._duplicate(d, *extra)
            else:
                res = self._existing(d)
            self._batch.setdefault(d.draft_id, res)
            results.append(res)
        return results

    # ---- new draft ------------------------------------------------------------------------------

    def _new(self, d: DraftQuote, fp: str, stock) -> ProcessResult:
        r = self.rules.rules
        priced = price_quote(d, self.snapshot, self.clock,
                             reporting_max_age_hours=r.reporting_max_age_hours, tz=r.tz)
        decision = route(priced, self.rules, self.directory, self.clock, extra_flags=stock_flags(d, stock))
        chash = content_hash(d)
        with self.store.transaction():
            self.store.insert_quote(
                draft_id=d.draft_id, status=RECEIVED, content_hash=chash, fingerprint=fp,
                draft_created_at=d.created_at, draft_json=d.model_dump_json(),
                priced_json=priced.model_dump_json(), routing_json=decision.model_dump_json(), duplicate_of=None)
            self.store.append_audit(
                actor_id=SYSTEM, action="draft_received", entity="quote", entity_id=d.draft_id,
                before_status=None, after_status=RECEIVED,
                details={"rfq_id": d.rfq_id, "customer_id": d.customer_id, "prepared_by": d.prepared_by,
                         "lines": len(d.lines), "content_hash": chash, "fingerprint": fp})
            # A human still has to act: every routed quote starts with the specialist (rule 1).
            self.store.transition(
                d.draft_id, expected_version=1, from_status=RECEIVED, to_status=PENDING_SPECIALIST,
                actor_id=SYSTEM, action="quote_routed", details=self._routing_details(priced, decision))
        return self._from_decision(priced, decision, workflow_status=PENDING_SPECIALIST)

    def _routing_details(self, priced: PricedQuote, dec: RoutingDecision) -> dict[str, Any]:
        return {
            "routing_status": dec.status, "tier": dec.tier, "approver_id": dec.approver_id,
            "routed_via_backup_of": dec.routed_via_backup_of, "specialist_id": dec.specialist_id,
            "net_total": str(to_money(priced.net_total)),
            "effective_discount_pct": None if priced.effective_discount_pct is None
            else str(to_pct(priced.effective_discount_pct)),
            "flags": [f.model_dump() for f in dec.specialist_review_flags],
            "rules_version": dec.rules_version, "rules_hash": dec.rules_hash,
            "as_of": self.clock.now().isoformat(), "trace": list(dec.trace),
        }

    @staticmethod
    def _from_decision(priced: PricedQuote, dec: RoutingDecision, *, workflow_status: str | None,
                       reused: bool = False) -> ProcessResult:
        return ProcessResult(
            draft_id=dec.draft_id, status=dec.status, net_total=priced.net_total,
            effective_discount_pct=priced.effective_discount_pct, approver_id=dec.approver_id,
            flags=dec.specialist_review_flags, duplicate_of=None, priced=priced, routing=dec,
            trace=dec.trace, reused=reused, workflow_status=workflow_status)

    # ---- duplicate ------------------------------------------------------------------------------

    def _duplicate(self, d: DraftQuote, fp: str, original: Original) -> ProcessResult:
        """Rule 11: no pricing, no routing, no approval request. Values = the original's (DECISIONS.md A4)."""
        orig = self._original_values(original.draft_id)
        minutes = int((d.created_at - original.created_at).total_seconds() // 60)
        trace = (f"same rfq_id {d.rfq_id} and same lines as {original.draft_id}, received {minutes} min "
                 f"earlier (window {self.rules.rules.duplicate_window_hours} h) -> duplicate, "
                 "no approval request",)
        chash = content_hash(d)
        with self.store.transaction():
            self.store.insert_quote(
                draft_id=d.draft_id, status=RECEIVED, content_hash=chash, fingerprint=fp,
                draft_created_at=d.created_at, draft_json=d.model_dump_json(), priced_json=None,
                routing_json=None, duplicate_of=original.draft_id)
            self.store.append_audit(
                actor_id=SYSTEM, action="draft_received", entity="quote", entity_id=d.draft_id,
                before_status=None, after_status=RECEIVED,
                details={"rfq_id": d.rfq_id, "customer_id": d.customer_id, "content_hash": chash,
                         "fingerprint": fp, "note": d.note})
            self.store.transition(
                d.draft_id, expected_version=1, from_status=RECEIVED, to_status=DUPLICATE,
                actor_id=SYSTEM, action="duplicate_detected",
                details={"duplicate_of": original.draft_id, "minutes_after_original": minutes,
                         "window_hours": str(self.rules.rules.duplicate_window_hours), "trace": list(trace)})
        return ProcessResult(
            draft_id=d.draft_id, status="duplicate", net_total=orig[0], effective_discount_pct=orig[1],
            approver_id=None, flags=(), duplicate_of=original.draft_id, trace=trace,
            workflow_status=DUPLICATE)

    def _original_values(self, original_id: str) -> tuple[Decimal | None, Decimal | None]:
        if original_id in self._batch and self._batch[original_id].priced:
            p = self._batch[original_id].priced
        else:
            row = self.store.get_quote(original_id)
            if row is None or row.priced_json is None:
                return None, None
            p = PricedQuote.model_validate_json(row.priced_json)
        return p.net_total, p.effective_discount_pct

    # ---- already stored (idempotent re-run) -----------------------------------------------------

    def _existing(self, d: DraftQuote) -> ProcessResult:
        if d.draft_id in self._batch:  # same draft_id twice in one batch
            return self._batch[d.draft_id]
        row = self.store.get_quote(d.draft_id)
        if row.content_hash != content_hash(d):
            # Same id, different content: that is an edit, which is stubbed. Keep the stored version.
            log.warning("draft %s resubmitted with different content; stored version kept", d.draft_id)
            self.store.append_audit(
                actor_id=SYSTEM, action="draft_conflict_ignored", entity="quote", entity_id=d.draft_id,
                before_status=row.status, after_status=row.status,
                details={"stored_content_hash": row.content_hash, "received_content_hash": content_hash(d)})
        if row.duplicate_of:
            net, eff = self._original_values(row.duplicate_of)
            detected = [e for e in self.store.audit_entries(d.draft_id) if e["action"] == "duplicate_detected"]
            trace = tuple(json.loads(detected[0]["details_json"]).get("trace", ())) if detected else ()
            return ProcessResult(
                draft_id=d.draft_id, status="duplicate", net_total=net, effective_discount_pct=eff,
                approver_id=None, flags=(), duplicate_of=row.duplicate_of,
                trace=trace or (f"already stored as duplicate of {row.duplicate_of}",), reused=True,
                workflow_status=row.status)
        priced = PricedQuote.model_validate_json(row.priced_json)
        dec = RoutingDecision.model_validate_json(row.routing_json)
        return self._from_decision(priced, dec, workflow_status=row.status, reused=True)


def explanation(r: ProcessResult, as_of: str) -> dict[str, Any]:
    """Human-readable record for outputs/routing_explanations.json (not part of Deliverable 1)."""
    out: dict[str, Any] = {"draft_id": r.draft_id, "status": r.status, "workflow_status": r.workflow_status,
                           "as_of": as_of, "reused_from_store": r.reused}
    if r.routing:
        out.update(tier=r.routing.tier, approver_id=r.routing.approver_id,
                   routed_via_backup_of=r.routing.routed_via_backup_of, specialist_id=r.routing.specialist_id,
                   rules_version=r.routing.rules_version, rules_hash=r.routing.rules_hash)
    if r.priced:
        out.update(list_total=to_money(r.priced.list_total), net_total=to_money(r.priced.net_total),
                   effective_discount_pct=None if r.priced.effective_discount_pct is None
                   else to_pct(r.priced.effective_discount_pct),
                   reporting_age_hours=r.priced.reporting_age_hours.quantize(Decimal("0.1")),
                   lines=[{"line": l.line, "part_number": l.part_number, "quantity": l.quantity,
                           "price_source": l.price_source, "unit_price": l.unit_price} for l in r.priced.lines])
    out.update(duplicate_of=r.duplicate_of,
               flags=[{"line": f.line, "code": f.code, "reason": f.reason} for f in r.flags],
               trace=list(r.trace))
    return out
