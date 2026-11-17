"""Output boundary: outputs/routing_results.json (exact Deliverable 1 format) + routing_explanations.json.

Money/percentages are rounded here and only here, and written as JSON numbers with 2 decimals.
"""
from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

import jsonschema

from app.money import to_money, to_pct
from app.paths import PROTECTED_DIRS, ensure_writable

SCHEMA_PATH = Path(__file__).parent / "schemas" / "routing_results.schema.json"
RESULT_KEYS = ("draft_id", "status", "net_total", "effective_discount_pct", "approver_id",
               "specialist_review_flags", "duplicate_of")

_DEC = "@@DECIMAL@@"
_DEC_RE = re.compile(rf'"{_DEC}(-?\d+(?:\.\d+)?)"')


class OutputValidationError(ValueError):
    pass


def render_json(obj: Any) -> str:
    """json.dumps, but Decimal is written as a bare number that keeps its digits (47652.00, not 47652.0)."""
    def default(o):
        if isinstance(o, Decimal):
            return f"{_DEC}{o}"
        raise TypeError(f"not JSON serializable: {type(o).__name__}")
    return _DEC_RE.sub(r"\1", json.dumps(obj, indent=2, ensure_ascii=False, default=default)) + "\n"


def routing_result(r) -> dict[str, Any]:
    """One Deliverable 1 object from a pipeline ProcessResult. Exactly RESULT_KEYS, in that order."""
    return {
        "draft_id": r.draft_id,
        "status": r.status,
        "net_total": None if r.net_total is None else to_money(r.net_total),
        "effective_discount_pct": None if r.effective_discount_pct is None else to_pct(r.effective_discount_pct),
        "approver_id": r.approver_id,
        "specialist_review_flags": [{"line": f.line, "reason": f.reason} for f in r.flags],
        "duplicate_of": r.duplicate_of,
    }


def validate_routing_results(text: str, expected_draft_ids: Sequence[str]) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"), parse_float=Decimal)
    data = json.loads(text, parse_float=Decimal)  # Decimal: exact multipleOf 0.01 checks
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    if errors:
        raise OutputValidationError("routing_results.json does not match schema:\n" + "\n".join(
            f"  - at {'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors))
    got = [o["draft_id"] for o in data]
    if got != list(expected_draft_ids):
        raise OutputValidationError(f"routing_results.json must list every draft in input order: "
                                    f"expected {list(expected_draft_ids)}, got {got}")
    for o in data:
        if tuple(o) != RESULT_KEYS:
            raise OutputValidationError(f"{o['draft_id']}: keys must be exactly {RESULT_KEYS}")


def _atomic_write(path: Path, text: str, validate=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    try:
        if validate:
            validate(tmp.read_text(encoding="utf-8"))  # validate what was actually written
        os.replace(tmp, path)  # atomic on Windows and POSIX
    finally:
        tmp.unlink(missing_ok=True)


def write_outputs(results: Sequence, out_dir: Path, explanations: list[dict[str, Any]],
                  protected: tuple[Path, ...] = PROTECTED_DIRS) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    ensure_writable(out_dir, protected, what="write outputs")  # before any mkdir or file write
    ids = [r.draft_id for r in results]
    results_path = out_dir / "routing_results.json"
    _atomic_write(results_path, render_json([routing_result(r) for r in results]),
                  validate=lambda text: validate_routing_results(text, ids))
    explanations_path = out_dir / "routing_explanations.json"
    _atomic_write(explanations_path, render_json(explanations))
    return results_path, explanations_path
