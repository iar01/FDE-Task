"""Pure duplicate detection (approval_rules.md rule 11).

"A draft with the same rfq_id and the same lines as a draft received in the last 24 hours is a
duplicate. Do not create a second approval request."
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from app.models import DraftQuote


def fingerprint(draft: DraftQuote) -> str:
    """sha256 of rfq_id + sorted (UPPERCASED part_number, quantity) pairs.

    Line order, descriptions and extraction confidence are ignored: they can differ on resubmission.
    """
    lines = sorted((l.part_number.strip().upper(), l.quantity) for l in draft.lines)
    canonical = json.dumps([draft.rfq_id.strip(), lines], separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Original:
    """A previously received, non-duplicate draft."""
    draft_id: str
    fingerprint: str
    created_at: datetime


def find_original(
    draft: DraftQuote, fp: str, originals: Iterable[Original], window: timedelta
) -> Original | None:
    """The most recent original with the same fingerprint received within `window` before this draft."""
    matches = [o for o in originals
               if o.fingerprint == fp and o.draft_id != draft.draft_id
               and timedelta(0) <= draft.created_at - o.created_at <= window]
    return max(matches, key=lambda o: o.created_at) if matches else None
