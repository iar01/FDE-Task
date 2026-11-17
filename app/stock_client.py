"""Adapter for Ridgeline's read-only stock API (data/stock_api.md).

Availability problems never raise to the caller: they come back as StockResult(status="unavailable").
Sleep and random are injected so tests are instant and deterministic.
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Iterable, Literal, Mapping

import httpx

from app.clock import Clock

log = logging.getLogger("app.stock_client")

BULK_LIMIT = 50  # stock_api.md: "GET /v1/stock?part_numbers=... (max 50)"

StockStatus = Literal["ok", "stale", "not_found", "unavailable"]


@dataclass(frozen=True)
class StockResult:
    part_number: str
    status: StockStatus
    total_on_hand: int | None
    by_warehouse: Mapping[str, int] | None
    as_of: datetime | None
    attempts: int
    age_hours: Decimal | None = None


class StockClient:
    def __init__(
        self,
        base_url: str,
        clock: Clock,
        *,
        timeout_seconds: float = 2.0,
        max_attempts: int = 4,
        backoff_base_seconds: float = 0.5,
        max_backoff_seconds: float = 5.0,
        max_age_hours: Decimal = Decimal("24"),
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[float, float], float] = random.uniform,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._http = httpx.Client(base_url=base_url, timeout=timeout_seconds, transport=transport)
        self._clock = clock
        self._max_attempts = max_attempts
        self._base = backoff_base_seconds
        self._cap = max_backoff_seconds
        self._max_age = max_age_hours
        self._sleep = sleep
        self._rand = rand

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "StockClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- public ---------------------------------------------------------------------------------

    def fetch(self, part_numbers: Iterable[str]) -> dict[str, StockResult]:
        parts = list(dict.fromkeys(p for p in part_numbers if p))  # dedupe, keep order
        results: dict[str, StockResult] = {}
        for i in range(0, len(parts), BULK_LIMIT):
            chunk = parts[i:i + BULK_LIMIT]
            payload, attempts = self._get_with_retries(chunk)
            results.update(self._parse(chunk, payload, attempts))
        return results

    # ---- retry loop -----------------------------------------------------------------------------

    def _get_with_retries(self, parts: list[str]) -> tuple[dict | None, int]:
        """Retry timeouts, connection errors and 5xx; never 4xx. Returns (payload or None, attempts)."""
        for attempt in range(1, self._max_attempts + 1):
            retry_after = None
            try:
                resp = self._http.get("/v1/stock", params={"part_numbers": ",".join(parts)})
                outcome = resp.status_code
                if resp.status_code < 400:
                    payload = resp.json()
                    self._log(parts, attempt, outcome, None)
                    return payload, attempt
                if resp.status_code < 500:
                    self._log(parts, attempt, outcome, None, final="not retried (4xx)")
                    return None, attempt
                retry_after = _retry_after(resp)
            except httpx.TransportError as exc:  # includes TimeoutException and ConnectError
                outcome = type(exc).__name__
            except ValueError:  # 2xx with a body that isn't JSON: don't guess
                self._log(parts, attempt, "invalid_json", None, final="not retried")
                return None, attempt
            if attempt == self._max_attempts:
                self._log(parts, attempt, outcome, None, final="retries exhausted")
                return None, attempt
            delay = min(retry_after if retry_after is not None
                        else self._rand(0, self._base * 2 ** (attempt - 1)), self._cap)
            self._log(parts, attempt, outcome, delay)
            self._sleep(delay)
        raise AssertionError("unreachable")

    def _log(self, parts, attempt, outcome, delay, final: str | None = None) -> None:
        event = {"event": "stock_attempt", "parts": len(parts), "first_part": parts[0],
                 "attempt": attempt, "max_attempts": self._max_attempts, "outcome": outcome,
                 "delay_s": None if delay is None else round(delay, 3)}
        if final:
            event["final"] = final
        ok = isinstance(outcome, int) and outcome < 400
        log.log(logging.INFO if ok else logging.WARNING, json.dumps(event))

    # ---- parsing --------------------------------------------------------------------------------

    def _parse(self, chunk: list[str], payload: dict | None, attempts: int) -> dict[str, StockResult]:
        unavailable = {p: StockResult(p, "unavailable", None, None, None, attempts) for p in chunk}
        if not isinstance(payload, dict):
            return unavailable
        out = dict(unavailable)  # anything the API didn't mention stays unavailable
        for p in payload.get("not_found") or ():
            if p in out:
                out[p] = StockResult(p, "not_found", None, None, None, attempts)
        now = self._clock.now()
        for item in payload.get("items") or ():
            try:
                pn = item["part_number"]
                by_wh = {str(k): int(v) for k, v in item["warehouses"].items()}
                as_of = datetime.fromisoformat(item["as_of"])
                if as_of.tzinfo is None or any(v < 0 for v in by_wh.values()):
                    raise ValueError("bad as_of or negative stock")
            except (KeyError, TypeError, ValueError, AttributeError):
                log.warning(json.dumps({"event": "stock_item_invalid", "item": str(item)[:200]}))
                continue  # stays unavailable
            if pn not in out:
                continue
            age = Decimal(str((now - as_of).total_seconds())) / Decimal(3600)
            status: StockStatus = "stale" if age > self._max_age else "ok"
            out[pn] = StockResult(pn, status, sum(by_wh.values()), by_wh, as_of, attempts, age)
        return out


def _retry_after(resp: httpx.Response) -> float | None:
    """stock_api.md: 503 body {"error": "service_unavailable", "retry_after_seconds": 2}."""
    try:
        value = resp.json().get("retry_after_seconds")
    except (ValueError, AttributeError):
        return None
    return float(value) if isinstance(value, (int, float)) and value >= 0 else None
