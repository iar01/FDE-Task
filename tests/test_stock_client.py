from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import httpx
import pytest

from app.clock import SAMPLE_RUN_TIME, FixedClock
from app.models import DraftQuote
from app.stock_client import StockClient, StockResult
from app.stock_flags import stock_flags

FRESH = "2026-10-12T08:00:00-05:00"
STALE = "2026-10-10T08:00:00-05:00"  # 49.5 h before the sample clock


def item(pn, dal=5, phx=5, atl=5, as_of=FRESH):
    return {"part_number": pn, "warehouses": {"DAL": dal, "PHX": phx, "ATL": atl}, "as_of": as_of}


class Script:
    """A transport that replays a list of responses/exceptions and records requests."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(request)
        code, body = step
        return httpx.Response(code, json=body)


def make(script, *, max_attempts=4, rand=None):
    sleeps: list[float] = []
    calls: list[tuple[float, float]] = []

    def fake_rand(a, b):
        calls.append((a, b))
        return b  # deterministic: worst-case jitter

    client = StockClient("http://stock.test", FixedClock(SAMPLE_RUN_TIME), max_attempts=max_attempts,
                         backoff_base_seconds=0.5, max_backoff_seconds=5.0, max_age_hours=Decimal(24),
                         sleep=sleeps.append, rand=rand or fake_rand, transport=httpx.MockTransport(script))
    return client, sleeps, calls


UNAVAILABLE_503 = (503, {"error": "service_unavailable", "retry_after_seconds": 2})


def test_ok_first_try():
    s = Script((200, {"items": [item("A")], "not_found": ["B"]}))
    client, sleeps, _ = make(s)
    res = client.fetch(["A", "B", "A"])
    assert res["A"].status == "ok" and res["A"].total_on_hand == 15 and res["A"].attempts == 1
    assert res["B"].status == "not_found"
    assert sleeps == [] and s.requests[0].url.params["part_numbers"] == "A,B"


def test_503_retries_using_retry_after_from_body():
    s = Script(UNAVAILABLE_503, UNAVAILABLE_503, (200, {"items": [item("A")], "not_found": []}))
    client, sleeps, calls = make(s)
    res = client.fetch(["A"])
    assert res["A"].status == "ok" and res["A"].attempts == 3
    assert sleeps == [2.0, 2.0] and calls == []


def test_retry_after_is_capped():
    s = Script((503, {"retry_after_seconds": 60}), (200, {"items": [item("A")], "not_found": []}))
    client, sleeps, _ = make(s)
    client.fetch(["A"])
    assert sleeps == [5.0]


def test_timeouts_and_connection_errors_use_full_jitter_backoff():
    s = Script(httpx.ReadTimeout("slow"), httpx.ConnectError("down"), (500, {"error": "boom"}),
               (200, {"items": [item("A")], "not_found": []}))
    client, sleeps, calls = make(s)
    res = client.fetch(["A"])
    assert res["A"].attempts == 4
    assert calls == [(0, 0.5), (0, 1.0), (0, 2.0)]
    assert sleeps == [0.5, 1.0, 2.0]


def test_exhausted_retries_mark_every_part_unavailable_and_never_raise():
    s = Script(UNAVAILABLE_503, UNAVAILABLE_503, UNAVAILABLE_503, UNAVAILABLE_503)
    client, sleeps, _ = make(s)
    res = client.fetch(["A", "B"])
    assert {r.status for r in res.values()} == {"unavailable"}
    assert all(r.attempts == 4 for r in res.values())
    assert len(s.requests) == 4 and sleeps == [2.0, 2.0, 2.0]  # no sleep after the last attempt


def test_4xx_is_not_retried():
    s = Script((400, {"error": "part_numbers query parameter required (max 50)"}))
    client, sleeps, _ = make(s)
    res = client.fetch(["A"])
    assert res["A"].status == "unavailable" and res["A"].attempts == 1
    assert len(s.requests) == 1 and sleeps == []


def test_chunks_of_50():
    def echo(request):
        pns = request.url.params["part_numbers"].split(",")
        return httpx.Response(200, json={"items": [item(p) for p in pns], "not_found": []})

    s = Script(echo, echo, echo)
    client, _, _ = make(s)
    parts = [f"P{i:03}" for i in range(120)]
    res = client.fetch(parts)
    assert [len(r.url.params["part_numbers"].split(",")) for r in s.requests] == [50, 50, 20]
    assert len(res) == 120 and all(r.status == "ok" for r in res.values())


def test_one_failed_chunk_does_not_poison_others():
    def echo(request):
        pns = request.url.params["part_numbers"].split(",")
        return httpx.Response(200, json={"items": [item(p) for p in pns], "not_found": []})

    s = Script(echo, UNAVAILABLE_503)
    client, _, _ = make(s, max_attempts=1)
    res = client.fetch([f"P{i:03}" for i in range(60)])
    assert sum(r.status == "ok" for r in res.values()) == 50
    assert sum(r.status == "unavailable" for r in res.values()) == 10


def test_stale_reading():
    s = Script((200, {"items": [item("A", as_of=STALE)], "not_found": []}))
    client, _, _ = make(s)
    r = client.fetch(["A"])["A"]
    assert r.status == "stale" and r.age_hours == Decimal("49.5")


def test_exactly_24h_is_not_stale():
    s = Script((200, {"items": [item("A", as_of="2026-10-11T09:30:00-05:00")], "not_found": []}))
    client, _, _ = make(s)
    assert client.fetch(["A"])["A"].status == "ok"


def test_malformed_item_and_missing_part_stay_unavailable():
    s = Script((200, {"items": [{"part_number": "A", "warehouses": None, "as_of": FRESH}], "not_found": []}))
    client, _, _ = make(s)
    res = client.fetch(["A", "B"])
    assert res["A"].status == "unavailable" and res["B"].status == "unavailable"


def test_invalid_json_body_is_unavailable():
    s = Script(lambda req: httpx.Response(200, text="<html>"))
    client, _, _ = make(s)
    assert client.fetch(["A"])["A"].status == "unavailable"


# ---- flags --------------------------------------------------------------------------------------

def _draft(lines):
    return DraftQuote.model_validate({
        "draft_id": "DQ-T", "rfq_id": "R", "customer_id": "C1", "created_at": FRESH, "prepared_by": "S01",
        "proposed_discount_pct": 0,
        "lines": [{"line": i, "part_number": pn, "description": "x", "quantity": q, "unit": "EA",
                   "extraction_confidence": "0.97"} for i, (pn, q) in enumerate(lines, start=1)]})


def _r(pn, status, total=None, age=None, attempts=1):
    wh = None if total is None else {"DAL": total, "PHX": 0, "ATL": 0}
    as_of = datetime.fromisoformat(STALE if status == "stale" else FRESH) if total is not None else None
    return StockResult(pn, status, total, wh, as_of, attempts, age)


def test_flags_cover_every_case_and_never_block():
    d = _draft([("OK", 1), ("NF", 1), ("ZERO", 1), ("OLD", 1), ("PART", 10), ("OLDZERO", 1)])
    stock = {"OK": _r("OK", "ok", 5), "NF": _r("NF", "not_found"), "ZERO": _r("ZERO", "ok", 0),
             "OLD": _r("OLD", "stale", 5, Decimal("49.5")), "PART": _r("PART", "ok", 3),
             "OLDZERO": _r("OLDZERO", "stale", 0, Decimal("30"))}
    got = [(f.line, f.code) for f in stock_flags(d, stock)]
    assert got == [(2, "stock_not_found"), (3, "out_of_stock"), (4, "stock_stale"), (5, "partial_stock"),
                   (6, "out_of_stock"), (6, "stock_stale")]
    reasons = {(f.line, f.code): f.reason for f in stock_flags(d, stock)}
    assert reasons[(5, "partial_stock")] == "partial stock: have 3, need 10"
    assert reasons[(4, "stock_stale")] == "stock reading 49.5 h old (as_of 2026-10-10T08:00:00-05:00)"


def test_unavailable_gives_one_quote_level_flag():
    d = _draft([("A", 1), ("B", 1)])
    stock = {"A": _r("A", "unavailable", attempts=4), "B": _r("B", "unavailable", attempts=4)}
    flags = stock_flags(d, stock)
    assert [(f.line, f.reason) for f in flags] == [(None, "stock unavailable after 4 attempts")]
