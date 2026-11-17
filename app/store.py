"""Our own state: SQLite app.db (quotes, approvals, hash-chained audit log).

Never anything under data/ (the reporting snapshot is read-only, and SAP is never written).
Production: managed Postgres; audit log in an append-only / WORM store.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.clock import Clock, SystemClock
from app.duplicates import Original

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    id               INTEGER PRIMARY KEY,
    draft_id         TEXT NOT NULL UNIQUE,
    version          INTEGER NOT NULL,
    status           TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    fingerprint      TEXT NOT NULL,          -- rule 11 duplicate key
    draft_created_at TEXT NOT NULL,          -- UTC ISO, sortable
    draft_json       TEXT NOT NULL,          -- what the extraction team sent (for re-pricing on edit)
    priced_json      TEXT,
    routing_json     TEXT,
    duplicate_of     TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quotes_fingerprint ON quotes (fingerprint, draft_created_at);

CREATE TABLE IF NOT EXISTS approvals (
    id            INTEGER PRIMARY KEY,
    quote_id      INTEGER NOT NULL REFERENCES quotes (id),
    quote_version INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,             -- the exact content that was approved
    actor_id      TEXT NOT NULL,
    role          TEXT NOT NULL,             -- the actor's role from approvers.csv
    step          TEXT NOT NULL,             -- specialist_review | approve | final_signoff | reject
    decision      TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
    comment       TEXT,
    at            TEXT NOT NULL,
    UNIQUE (quote_id, quote_version, step)   -- one decision per step per version: no double approvals
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq           INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    actor_id      TEXT NOT NULL,
    action        TEXT NOT NULL,
    entity        TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    before_status TEXT,
    after_status  TEXT,
    details_json  TEXT NOT NULL,
    prev_hash     TEXT NOT NULL,
    hash          TEXT NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


class VersionConflict(RuntimeError):
    """The quote changed since the caller read it (optimistic concurrency)."""


class StoreLocationError(ValueError):
    """Refusing to put our database somewhere we must never write (e.g. data/)."""


@dataclass(frozen=True)
class QuoteRow:
    id: int
    draft_id: str
    version: int
    status: str
    content_hash: str
    fingerprint: str
    draft_created_at: str
    draft_json: str
    priced_json: str | None
    routing_json: str | None
    duplicate_of: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ChainCheck:
    ok: bool
    entries: int
    first_bad_seq: int | None = None
    problem: str | None = None


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _row_hash(prev_hash: str, row: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical(row)).encode("utf-8")).hexdigest()


def reset_database(path: Path) -> None:
    """Drop app.db (and SQLite side files). Pure Python, so it works on Windows."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


class Store:
    def __init__(self, path: Path, *, audit_clock: Clock | None = None,
                 forbidden_dirs: tuple[Path, ...] = (Path("data"),)) -> None:
        path = Path(path)
        resolved = path.resolve()
        for d in forbidden_dirs:
            if resolved.is_relative_to(Path(d).resolve()):
                raise StoreLocationError(f"refusing to create the app database under {d}/: {path}")
        # Audit timestamps are real wall-clock time, even when business logic runs "as of" another time.
        self._clock = audit_clock or SystemClock()
        # check_same_thread=False: the API serialises access with its own lock (FastAPI threadpool).
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)
        self._depth = 0

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def now(self) -> str:
        return utc_iso(self._clock.now())

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """BEGIN IMMEDIATE: one writer at a time, so audit seq/prev_hash can't fork."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._db.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        finally:
            self._depth = 0

    # ---- quotes ---------------------------------------------------------------------------------

    def get_quote(self, draft_id: str) -> QuoteRow | None:
        row = self._db.execute("SELECT * FROM quotes WHERE draft_id = ?", (draft_id,)).fetchone()
        return QuoteRow(**dict(row)) if row else None

    def list_quotes(self, status: str | None = None) -> list[QuoteRow]:
        sql, args = "SELECT * FROM quotes", ()
        if status:
            sql, args = sql + " WHERE status = ?", (status,)
        return [QuoteRow(**dict(r)) for r in self._db.execute(sql + " ORDER BY id", args)]

    def originals(self, fingerprint: str) -> list[Original]:
        rows = self._db.execute(
            "SELECT draft_id, fingerprint, draft_created_at FROM quotes "
            "WHERE fingerprint = ? AND status != 'duplicate'", (fingerprint,))
        return [Original(r["draft_id"], r["fingerprint"],
                         datetime.fromisoformat(r["draft_created_at"].replace("Z", "+00:00"))) for r in rows]

    def insert_quote(self, *, draft_id: str, status: str, content_hash: str, fingerprint: str,
                     draft_created_at: datetime, draft_json: str, priced_json: str | None,
                     routing_json: str | None, duplicate_of: str | None) -> int:
        now = self.now()
        cur = self._db.execute(
            "INSERT INTO quotes (draft_id, version, status, content_hash, fingerprint, draft_created_at, "
            "draft_json, priced_json, routing_json, duplicate_of, created_at, updated_at) "
            "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (draft_id, status, content_hash, fingerprint, utc_iso(draft_created_at), draft_json,
             priced_json, routing_json, duplicate_of, now, now))
        return int(cur.lastrowid)

    def transition(self, draft_id: str, *, expected_version: int, from_status: str, to_status: str,
                   actor_id: str, action: str, details: dict[str, Any] | None = None) -> int:
        """Change status only if nobody else changed the quote first. Audited in the same transaction."""
        with self.transaction():
            cur = self._db.execute(
                "UPDATE quotes SET status = ?, version = version + 1, updated_at = ? "
                "WHERE draft_id = ? AND version = ? AND status = ?",
                (to_status, self.now(), draft_id, expected_version, from_status))
            if cur.rowcount != 1:
                current = self.get_quote(draft_id)
                raise VersionConflict(
                    f"{draft_id}: expected version {expected_version} in status {from_status!r}, found "
                    + (f"version {current.version} in status {current.status!r}" if current else "no quote"))
            self.append_audit(actor_id=actor_id, action=action, entity="quote", entity_id=draft_id,
                              before_status=from_status, after_status=to_status, details=details or {})
            return expected_version + 1

    def insert_approval(self, *, quote_id: int, quote_version: int, content_hash: str, actor_id: str,
                        role: str, step: str, decision: str, comment: str | None) -> int:
        cur = self._db.execute(
            "INSERT INTO approvals (quote_id, quote_version, content_hash, actor_id, role, step, decision, "
            "comment, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (quote_id, quote_version, content_hash, actor_id, role, step, decision, comment, self.now()))
        return int(cur.lastrowid)

    def approvals(self, quote_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self._db.execute(
            "SELECT * FROM approvals WHERE quote_id = ? ORDER BY id", (quote_id,))]

    # ---- audit ----------------------------------------------------------------------------------

    def append_audit(self, *, actor_id: str, action: str, entity: str, entity_id: str,
                     before_status: str | None, after_status: str | None, details: dict[str, Any]) -> int:
        with self.transaction():
            last = self._db.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
            seq = (last["seq"] + 1) if last else 1
            prev = last["hash"] if last else GENESIS
            row = {"seq": seq, "at": self.now(), "actor_id": actor_id, "action": action, "entity": entity,
                   "entity_id": entity_id, "before_status": before_status, "after_status": after_status,
                   "details_json": canonical(details)}
            h = _row_hash(prev, row)
            self._db.execute(
                "INSERT INTO audit_log (seq, at, actor_id, action, entity, entity_id, before_status, "
                "after_status, details_json, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*row.values(), prev, h))
            return seq

    def audit_entries(self, entity_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM audit_log", ()
        if entity_id:
            sql, args = sql + " WHERE entity_id = ?", (entity_id,)
        return [dict(r) for r in self._db.execute(sql + " ORDER BY seq", args)]

    def verify_audit_chain(self) -> ChainCheck:
        prev, n = GENESIS, 0
        for r in self._db.execute("SELECT * FROM audit_log ORDER BY seq"):
            n += 1
            row = {k: r[k] for k in ("seq", "at", "actor_id", "action", "entity", "entity_id",
                                     "before_status", "after_status", "details_json")}
            if r["seq"] != n:
                return ChainCheck(False, n, r["seq"], f"gap in sequence: expected {n}")
            if r["prev_hash"] != prev:
                return ChainCheck(False, n, r["seq"], "prev_hash does not match the previous entry")
            if _row_hash(prev, row) != r["hash"]:
                return ChainCheck(False, n, r["seq"], "hash does not match row content (tampered)")
            prev = r["hash"]
        return ChainCheck(True, n)
