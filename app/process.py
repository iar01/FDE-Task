"""CLI: python -m app.process [data/draft_quotes.json] [--reset] [--as-of ISO|now]

Processes the drafts, persists quotes + audit to app.db, writes outputs/routing_results.json
(validated against its JSON schema) and outputs/routing_explanations.json.
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from app.clock import SAMPLE_RUN_TIME, Clock, FixedClock, SystemClock
from app.config import Settings
from app.directory import Directory
from app.drafts import load_drafts
from app.outputs import OutputValidationError, write_outputs
from app.pipeline import Pipeline, explanation
from app.reporting import load_snapshot
from app.rules import RulesError, load_rules
from app.stock_client import StockClient
from app.store import Store, StoreLocationError, reset_database


def _clock(as_of: str) -> Clock:
    if as_of == "now":
        return SystemClock()
    dt = datetime.fromisoformat(as_of)
    if dt.tzinfo is None:
        raise argparse.ArgumentTypeError("--as-of needs a UTC offset, e.g. 2026-10-12T09:30:00-05:00")
    return FixedClock(dt)


def main(argv: list[str] | None = None) -> int:
    settings = Settings()
    p = argparse.ArgumentParser(prog="python -m app.process", description=__doc__.splitlines()[0])
    p.add_argument("drafts", nargs="?", type=Path, default=settings.drafts_path)
    p.add_argument("--reset", action="store_true", help="drop and recreate app.db first")
    p.add_argument("--as-of", default=SAMPLE_RUN_TIME.isoformat(),
                   help="business time for out-of-office and staleness (ISO with offset, or 'now'); "
                        "default: the policy's sample time 2026-10-12T09:30:00-05:00")
    p.add_argument("--db", type=Path, default=settings.app_db_path)
    p.add_argument("--out-dir", type=Path, default=settings.outputs_dir)
    p.add_argument("-v", "--verbose", action="store_true", help="log every stock API attempt")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)-7s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        clock = _clock(args.as_of)
    except (ValueError, argparse.ArgumentTypeError) as exc:
        p.error(str(exc))

    snapshot = load_snapshot(settings.reporting_dir)
    directory = Directory.from_snapshot(snapshot)
    try:
        rules = load_rules(settings.rules_path, directory)
    except RulesError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    if args.reset:
        reset_database(args.db)
    drafts = load_drafts(args.drafts)
    try:
        store = Store(args.db)
    except StoreLocationError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    with store, StockClient(settings.stock_api_url, clock, timeout_seconds=settings.stock_timeout_seconds,
                            max_attempts=settings.stock_max_attempts,
                            backoff_base_seconds=settings.stock_backoff_base_seconds,
                            max_backoff_seconds=settings.stock_max_backoff_seconds,
                            max_age_hours=rules.rules.stock_max_age_hours) as stock:
        results = Pipeline(snapshot, directory, rules, stock, store, clock).process(drafts)
        as_of = clock.now().isoformat()
        try:
            results_path, expl_path = write_outputs(results, args.out_dir,
                                                    [explanation(r, as_of) for r in results])
        except OutputValidationError as exc:
            print(f"output invalid, not written: {exc}", file=sys.stderr)
            return 3
        chain = store.verify_audit_chain()

    counts = Counter(r.status for r in results)
    reused = sum(r.reused for r in results)
    print(f"as of {as_of} | rules {rules.version} ({rules.content_hash[:12]}) | db {args.db}")
    print(f"processed {len(results)} drafts ({len(results) - reused} new, {reused} already stored)")
    for status in ("specialist_only", "approver_required", "duplicate"):
        print(f"  {status:<18} {counts.get(status, 0)}")
    print(f"audit chain: {'OK' if chain.ok else 'BROKEN at seq ' + str(chain.first_bad_seq)} "
          f"({chain.entries} entries)")
    print(f"wrote {results_path} (schema-validated) and {expl_path}")
    return 0 if chain.ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
