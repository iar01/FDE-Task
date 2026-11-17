"""Review-API demo. Pure Python + httpx, so it runs on Windows.

Setup (fresh state):
    python data/mock_stock_api.py                       # in another terminal
    python -m app.process --reset                       # route the 10 sample drafts
    set CLOCK_OVERRIDE=2026-10-12T09:30:00-05:00        # PowerShell: $env:CLOCK_OVERRIDE="..."
    uvicorn app.api:app --port 8000                     # in another terminal
    python scripts/demo.py [--base http://127.0.0.1:8000]

AUTH IS A STUB: the X-User-Id header decides who you are.
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

GREEN, RED, DIM, BOLD, END = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


class Demo:
    def __init__(self, base: str):
        self.http = httpx.Client(base_url=base, timeout=10)

    def call(self, method: str, path: str, user: str | None, body: dict | None = None, expect: int = 200,
             show=None) -> dict:
        headers = {"X-User-Id": user} if user else {}
        r = self.http.request(method, path, headers=headers, json=body)
        data = r.json()
        ok = r.status_code == expect
        colour = GREEN if r.status_code < 300 else RED
        b = f" {json.dumps(body)}" if body else ""
        print(f"{BOLD}{user or '-':>4}{END} {method:<5} {path}{DIM}{b}{END}")
        summary = show(data) if (show and r.status_code < 300) else (
            data.get("message") or data.get("error") or "")
        mark = "" if ok else f"  {RED}<< expected {expect}{END}"
        print(f"      {colour}{r.status_code}{END} {summary}{mark}")
        if not ok:
            raise SystemExit(f"unexpected status {r.status_code} for {method} {path}")
        return data

    def get(self, draft_id: str, user: str) -> dict:
        return self.call("GET", f"/quotes/{draft_id}", user, show=state)


def state(q: dict) -> str:
    return f"status={q['status']} version={q['version']} approver={q['approver_id']} specialist={q['specialist_id']}"


def section(title: str) -> None:
    print(f"\n{BOLD}== {title} =={END}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    d = Demo(ap.parse_args().base)

    section("auth stub")
    d.call("GET", "/quotes", None, expect=401)
    d.call("GET", "/quotes", "U04", show=lambda r: f"{r['role']} sees {[q['draft_id'] for q in r['quotes']]}")

    q = d.get("DQ-24101", "S01")
    if q["status"] != "pending_specialist_review":
        print(f"\n{RED}DQ-24101 is already {q['status']}. Run `python -m app.process --reset` and restart the API.{END}")
        return 1

    section("DQ-24101: specialist only (8.00%, $435.23)")
    q = d.call("POST", "/quotes/DQ-24101/specialist-review", "S01",
               {"version": q["version"], "comment": "prices and stock checked"}, show=state)
    q = d.call("POST", "/quotes/DQ-24101/final-signoff", "S01", {"version": q["version"]}, show=state)

    section("DQ-24106: net $47,652 -> Central regional manager U02, who is out -> backup U04")
    q = d.get("DQ-24106", "S03")
    print(f"      {DIM}trace: {q['trace']}{END}")
    seen_by_u04 = q["version"]
    q = d.call("POST", "/quotes/DQ-24106/specialist-review", "S03",
               {"version": q["version"], "comment": "large order; stock split across DAL/PHX/ATL"}, show=state)

    section("denied attempts (each one is audited)")
    d.call("POST", "/quotes/DQ-24106/approve", "U02", {"version": q["version"]}, expect=403)
    d.call("POST", "/quotes/DQ-24106/approve", "U01", {"version": q["version"]}, expect=403)
    d.call("POST", "/quotes/DQ-24106/approve", "S03", {"version": q["version"]}, expect=403)
    d.call("POST", "/quotes/DQ-24106/approve", "U04", {"version": seen_by_u04}, expect=409)
    d.call("POST", "/quotes/DQ-24110/specialist-review", "S02", {"version": 2}, expect=409)

    section("DQ-24106: U04 approves as U02's backup, then the specialist signs off")
    q = d.call("POST", "/quotes/DQ-24106/approve", "U04",
               {"version": q["version"], "comment": "approved as backup for U02"}, show=state)
    q = d.call("POST", "/quotes/DQ-24106/final-signoff", "S03", {"version": q["version"]}, show=state)

    section("edit is a stub")
    d.call("PATCH", "/quotes/DQ-24106", "S03", {}, expect=501)

    section("audit trail for DQ-24106")
    entries = d.call("GET", "/audit?entity_id=DQ-24106", "S01",
                     show=lambda r: f"{len(r['entries'])} entries")["entries"]
    for e in entries:
        det = json.loads(e["details_json"])
        extra = det.get("message") or det.get("comment") or det.get("permission") or det.get("approver_id") or ""
        print(f"      #{e['seq']:<3} {e['at'][11:19]} {e['actor_id']:<7} {e['action']:<22} "
              f"{str(e['before_status']):<26} -> {str(e['after_status']):<26} {DIM}{extra}{END}")
    d.call("GET", "/audit/verify", "S01", show=lambda r: f"chain ok={r['ok']} entries={r['entries']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
