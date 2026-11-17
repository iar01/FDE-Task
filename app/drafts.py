"""Adapter: read draft quotes from the extraction team's JSON. Numbers are parsed as Decimal, never float."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.models import DraftQuote


def parse_drafts(text: str) -> tuple[DraftQuote, ...]:
    payload = json.loads(text, parse_float=Decimal)
    return tuple(DraftQuote.model_validate(d) for d in payload["drafts"])


def load_drafts(path: Path) -> tuple[DraftQuote, ...]:
    return parse_drafts(Path(path).read_text(encoding="utf-8"))
