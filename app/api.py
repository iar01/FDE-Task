"""Review API (FastAPI). Run: uvicorn app.api:app

AUTH IS A STUB: the caller is whoever the X-User-Id header says (looked up in approvers.csv).
See app/auth.py for the production design.

Every state change needs the quote `version` the caller saw. Every action, including denied
attempts (401/403/404/409/422), is written to the hash-chained audit log.
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.auth import USER_HEADER, identify
from app.clock import Clock
from app.config import Settings
from app.directory import Directory
from app.models import Flag, PricedQuote, RoutingDecision
from app.money import to_money, to_pct
from app.reporting import Approver, ReportingSnapshot, load_snapshot
from app.router import route
from app.rules import LoadedRules, RulesError, load_rules, parse_rules, validate_against_directory
from app.store import Store, VersionConflict
from app import workflow as wf

STOCK_FLAG_CODES = {"stock_unavailable", "stock_not_found", "out_of_stock", "partial_stock", "stock_stale"}
# STUB: there is no rules-admin role in approvers.csv; these two roles may reload rules.
RULES_ADMIN_ROLES = {"sales_operations_director", "vp_sales"}


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int | None = None
    comment: str | None = None


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int | None = None
    reason: str | None = None


class SimulateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules_yaml: str


@dataclass
class Services:
    store: Store
    snapshot: ReportingSnapshot
    directory: Directory
    rules: LoadedRules
    clock: Clock
    settings: Settings
    lock: threading.RLock


class ApiError(Exception):
    def __init__(self, status: int, error: str, message: str):
        self.status, self.error, self.message = status, error, message


def create_app(services: Services | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if services is None:
            settings = Settings()
            snapshot = load_snapshot(settings.reporting_dir)
            directory = Directory.from_snapshot(snapshot)
            rules = load_rules(settings.rules_path, directory)  # RulesError -> refuse to start
            store = Store(settings.app_db_path)
            app.state.svc = Services(store, snapshot, directory, rules, settings.clock(), settings, threading.RLock())
            yield
            store.close()
        else:
            app.state.svc = services
            yield

    app = FastAPI(title="Ridgeline Quote Approvals", lifespan=lifespan,
                  description="Review API. **Auth is a stub**: identity comes from the X-User-Id header.")

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content={"error": exc.error, "message": exc.message})

    def svc(request: Request) -> Services:
        return request.app.state.svc

    # ---- helpers --------------------------------------------------------------------------------

    def audit(s: Services, actor: str, action: str, entity_id: str, before: str | None, after: str | None,
              details: dict[str, Any], entity: str = "quote") -> None:
        s.store.append_audit(actor_id=actor, action=action, entity=entity, entity_id=entity_id,
                             before_status=before, after_status=after, details=details)

    def deny(s: Services, status: int, error: str, message: str, *, actor: str | None, action: str,
             entity_id: str, current: str | None = None, entity: str = "quote") -> None:
        """Audit the denied attempt, then fail the request."""
        audit(s, actor or "anonymous", f"{action}_denied", entity_id, current, current,
              {"http_status": status, "error": error, "message": message}, entity)
        raise ApiError(status, error, message)

    def user_or_401(s: Services, user_id: str | None, action: str, entity_id: str, entity="quote") -> Approver:
        user = identify(user_id, s.directory)
        if user is None:
            deny(s, 401, "unauthenticated",
                 f"missing or unknown {USER_HEADER} (auth stub: must be an approver_id in approvers.csv)",
                 actor=user_id, action=action, entity_id=entity_id, entity=entity)
        return user

    def load(s: Services, draft_id: str, user: Approver, action: str):
        row = s.store.get_quote(draft_id)
        if row is None:
            deny(s, 404, "not_found", f"no quote {draft_id}", actor=user.approver_id, action=action,
                 entity_id=draft_id)
        routing = RoutingDecision.model_validate_json(row.routing_json) if row.routing_json else None
        return row, routing

    def visible(user: Approver, routing: RoutingDecision | None) -> bool:
        """Specialists see all quotes; approvers see quotes routed to them."""
        return user.role == wf.SPECIALIST_ROLE or (routing is not None and routing.approver_id == user.approver_id)

    def today(s: Services):
        return s.clock.now().astimezone(s.rules.rules.tz).date()

    def money(v: Decimal | None) -> str | None:
        return None if v is None else str(to_money(v))

    def summary(row, routing: RoutingDecision | None) -> dict[str, Any]:
        priced = PricedQuote.model_validate_json(row.priced_json) if row.priced_json else None
        return {
            "draft_id": row.draft_id, "version": row.version, "status": row.status,
            "routing_status": routing.status if routing else "duplicate",
            "approver_id": routing.approver_id if routing else None,
            "specialist_id": routing.specialist_id if routing else None,
            "net_total": money(priced.net_total) if priced else None,
            "effective_discount_pct": str(to_pct(priced.effective_discount_pct))
            if priced and priced.effective_discount_pct is not None else None,
            "flag_count": len(routing.specialist_review_flags) if routing else 0,
            "duplicate_of": row.duplicate_of,
        }

    def detail(s: Services, row, routing: RoutingDecision | None) -> dict[str, Any]:
        out = summary(row, routing)
        history = s.store.audit_entries(row.draft_id)
        reviewed = next((e for e in history if e["action"] == "specialist_reviewed"), None)
        out.update(
            tier=routing.tier if routing else None,
            routed_via_backup_of=routing.routed_via_backup_of if routing else None,
            rules_version=routing.rules_version if routing else None,
            flags=[{"line": f.line, "code": f.code, "reason": f.reason} for f in routing.specialist_review_flags]
            if routing else [],
            flags_cleared_by=reviewed["actor_id"] if reviewed else None,
            flags_cleared_at=reviewed["at"] if reviewed else None,
            trace=list(routing.trace) if routing else [],
            approvals=[{k: a[k] for k in ("quote_version", "actor_id", "role", "step", "decision", "comment", "at")}
                       for a in s.store.approvals(row.id)],
            audit=[{k: e[k] for k in ("seq", "at", "actor_id", "action", "before_status", "after_status")}
                   for e in history],
        )
        return out

    # ---- reads ----------------------------------------------------------------------------------

    @app.get("/quotes")
    def list_quotes(request: Request, status: str | None = None, assigned_to: str | None = None,
                    x_user_id: str | None = Header(default=None)):
        s = svc(request)
        with s.lock:
            user = user_or_401(s, x_user_id, "list_quotes", "*")
            out = []
            for row in s.store.list_quotes(status):
                routing = RoutingDecision.model_validate_json(row.routing_json) if row.routing_json else None
                if not visible(user, routing):
                    continue
                if assigned_to and not (routing and assigned_to in (routing.approver_id, routing.specialist_id)):
                    continue
                out.append(summary(row, routing))
            return {"user": user.approver_id, "role": user.role, "count": len(out), "quotes": out}

    @app.get("/quotes/{draft_id}")
    def get_quote(draft_id: str, request: Request, x_user_id: str | None = Header(default=None)):
        s = svc(request)
        with s.lock:
            user = user_or_401(s, x_user_id, "view_quote", draft_id)
            row, routing = load(s, draft_id, user, "view_quote")
            if not visible(user, routing):
                deny(s, 403, "forbidden", f"{draft_id} is not routed to {user.approver_id}",
                     actor=user.approver_id, action="view_quote", entity_id=draft_id, current=row.status)
            return detail(s, row, routing)

    # ---- state changes --------------------------------------------------------------------------

    def act(request: Request, draft_id: str, step: str, x_user_id: str | None, version: int | None,
            text: str | None) -> dict[str, Any]:
        s = svc(request)
        _, action = wf.STEPS[step]
        with s.lock:
            user = user_or_401(s, x_user_id, step, draft_id)
            row, routing = load(s, draft_id, user, step)
            who = dict(actor=user.approver_id, action=step, entity_id=draft_id, current=row.status)
            if routing is None:
                deny(s, 409, "illegal_transition", f"{draft_id} is a duplicate of {row.duplicate_of}; "
                     "it has no approval request", **who)
            verdict = wf.check(step, user, routing, s.directory, today(s))
            if not verdict.ok:
                deny(s, 403, "forbidden", verdict.reason, **who)
            if version is None:
                deny(s, 422, "version_required", "body must include the quote version you reviewed", **who)
            if version != row.version:
                deny(s, 409, "stale_version", f"you saw version {version}; current is {row.version} "
                     f"(status {row.status}). Reload and review again.", **who)
            if step == "reject" and not (text or "").strip():
                deny(s, 422, "reason_required", "a rejection needs a reason", **who)
            to = wf.next_state(step, row.status, routing)
            if to is None:
                deny(s, 409, "illegal_transition", f"cannot {step} a quote in status {row.status}", **who)
            details: dict[str, Any] = {"step": step, "version_seen": version, "permission": verdict.reason,
                                       "content_hash": row.content_hash}
            details["reason" if step == "reject" else "comment"] = text
            if step == "specialist_review":
                details["flags_cleared"] = [f.model_dump() for f in routing.specialist_review_flags]
            try:
                with s.store.transaction():
                    s.store.transition(draft_id, expected_version=row.version, from_status=row.status,
                                       to_status=to, actor_id=user.approver_id, action=action, details=details)
                    s.store.insert_approval(quote_id=row.id, quote_version=row.version,
                                            content_hash=row.content_hash, actor_id=user.approver_id,
                                            role=user.role, step=step,
                                            decision="reject" if step == "reject" else "approve", comment=text)
            except VersionConflict as exc:  # lost a race with another reviewer
                deny(s, 409, "stale_version", str(exc), **who)
            row = s.store.get_quote(draft_id)
            return detail(s, row, routing)

    @app.post("/quotes/{draft_id}/specialist-review")
    def specialist_review(draft_id: str, body: ActionBody, request: Request,
                          x_user_id: str | None = Header(default=None)):
        return act(request, draft_id, "specialist_review", x_user_id, body.version, body.comment)

    @app.post("/quotes/{draft_id}/approve")
    def approve(draft_id: str, body: ActionBody, request: Request, x_user_id: str | None = Header(default=None)):
        return act(request, draft_id, "approve", x_user_id, body.version, body.comment)

    @app.post("/quotes/{draft_id}/final-signoff")
    def final_signoff(draft_id: str, body: ActionBody, request: Request,
                      x_user_id: str | None = Header(default=None)):
        return act(request, draft_id, "final_signoff", x_user_id, body.version, body.comment)

    @app.post("/quotes/{draft_id}/reject")
    def reject(draft_id: str, body: RejectBody, request: Request, x_user_id: str | None = Header(default=None)):
        return act(request, draft_id, "reject", x_user_id, body.version, body.reason)

    @app.patch("/quotes/{draft_id}")
    def edit_quote(draft_id: str, request: Request, x_user_id: str | None = Header(default=None)):
        """STUB: editing is not implemented (the brief allows a stub)."""
        s = svc(request)
        with s.lock:
            user = user_or_401(s, x_user_id, "edit", draft_id)
            row = s.store.get_quote(draft_id)
            audit(s, user.approver_id, "edit_attempted_stub", draft_id, row.status if row else None,
                  row.status if row else None, {"http_status": 501})
        return JSONResponse(status_code=501, content={
            "error": "not_implemented",
            "message": "Quote editing is stubbed. The production flow:",
            "production_flow": [
                "1. specialist submits changed lines/discount with the version they saw (409 if stale)",
                "2. store a new quote_version (old version kept; audit before/after of every edited field)",
                "3. reprice with price_quote() against the current reporting snapshot",
                "4. re-check stock and re-run route() with the current rules (tier/approver may change)",
                "5. invalidate approvals for older versions: approvals bind to quote_version + content_hash",
                "6. status returns to pending_specialist_review; the new routing is audited",
            ]})

    # ---- audit ----------------------------------------------------------------------------------

    @app.get("/audit")
    def get_audit(request: Request, entity_id: str | None = None, x_user_id: str | None = Header(default=None)):
        s = svc(request)
        with s.lock:
            user_or_401(s, x_user_id, "view_audit", entity_id or "*", entity="audit")
            return {"entries": s.store.audit_entries(entity_id)}

    @app.get("/audit/verify")
    def verify_audit(request: Request, x_user_id: str | None = Header(default=None)):
        s = svc(request)
        with s.lock:
            user_or_401(s, x_user_id, "verify_audit", "*", entity="audit")
            chk = s.store.verify_audit_chain()
            return {"ok": chk.ok, "entries": chk.entries, "first_bad_seq": chk.first_bad_seq, "problem": chk.problem}

    # ---- rules ----------------------------------------------------------------------------------

    @app.post("/rules/reload")
    def reload_rules(request: Request, x_user_id: str | None = Header(default=None)):
        """Validate and activate rules/approval_rules.yaml without a code release.

        New routing uses the new rules; open quotes keep the routing (and rules_hash) they were given.
        Use /rules/simulate first to see the impact.
        """
        s = svc(request)
        with s.lock:
            user = user_or_401(s, x_user_id, "rules_reload", "approval_rules", entity="rules")
            if user.role not in RULES_ADMIN_ROLES:
                deny(s, 403, "forbidden", f"rules reload needs one of {sorted(RULES_ADMIN_ROLES)} (stub policy)",
                     actor=user.approver_id, action="rules_reload", entity_id="approval_rules", entity="rules")
            old = s.rules
            try:
                new = load_rules(s.settings.rules_path, s.directory)
            except RulesError as exc:
                deny(s, 422, "invalid_rules", str(exc), actor=user.approver_id, action="rules_reload",
                     entity_id="approval_rules", entity="rules")
            s.rules = new
            audit(s, user.approver_id, "rules_reloaded", "approval_rules", old.version, new.version,
                  {"old_hash": old.content_hash, "new_hash": new.content_hash}, entity="rules")
            return {"previous": {"version": old.version, "hash": old.content_hash},
                    "active": {"version": new.version, "hash": new.content_hash},
                    "changed": old.content_hash != new.content_hash}

    @app.post("/rules/simulate")
    def simulate_rules(body: SimulateBody, request: Request, x_user_id: str | None = Header(default=None)):
        """Route every open quote against candidate rules and return the diff. Nothing is saved."""
        s = svc(request)
        with s.lock:
            user = user_or_401(s, x_user_id, "rules_simulate", "candidate", entity="rules")
            try:
                cand = parse_rules(body.rules_yaml)
                validate_against_directory(cand.rules, s.directory)
            except RulesError as exc:
                deny(s, 422, "invalid_rules", str(exc), actor=user.approver_id, action="rules_simulate",
                     entity_id="candidate", entity="rules")
            changes, compared = [], 0
            for row in s.store.list_quotes():
                if row.status not in wf.PENDING or not row.priced_json:
                    continue
                compared += 1
                priced = PricedQuote.model_validate_json(row.priced_json)
                current = RoutingDecision.model_validate_json(row.routing_json)
                stock = [f for f in current.specialist_review_flags if f.code in STOCK_FLAG_CODES]
                new = route(priced, cand, s.directory, s.clock, extra_flags=stock)
                diff = {k: {"current": getattr(current, k), "candidate": getattr(new, k)}
                        for k in ("status", "tier", "approver_id") if getattr(current, k) != getattr(new, k)}
                cur_f = [f.reason for f in current.specialist_review_flags]
                new_f = [f.reason for f in new.specialist_review_flags]
                if cur_f != new_f:
                    diff["flags"] = {"current": cur_f, "candidate": new_f}
                if diff:
                    changes.append({"draft_id": row.draft_id, "status": row.status, "changes": diff,
                                    "candidate_trace": list(new.trace)})
            audit(s, user.approver_id, "rules_simulated", "candidate", None, None,
                  {"candidate_version": cand.version, "candidate_hash": cand.content_hash,
                   "compared": compared, "changed": len(changes)}, entity="rules")
            return {"active_version": s.rules.version, "candidate_version": cand.version,
                    "candidate_hash": cand.content_hash, "open_quotes_compared": compared,
                    "changed": len(changes), "changes": changes, "saved": False}

    return app


app = create_app()
