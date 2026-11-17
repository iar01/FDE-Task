"""Pre-submission check (CHALLENGE.md "Deliverables" + "How to submit"). Read-only unless --zip.

    python scripts/check_submission.py          # check only
    python scripts/check_submission.py --zip    # also build ridgeline-quote-approvals.zip and verify it

Exit code 1 if any check FAILs. WARN items (README TODOs, uncommitted changes) don't fail the check.
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.drafts import load_drafts  # noqa: E402
from app.outputs import OutputValidationError, validate_routing_results  # noqa: E402

HEADINGS = ["# Ridgeline Quote Approvals", "## Repository", "## How to Run", "## Approach", "## Reliability",
            "## Production"]
DELIVERABLES = ["README.md", ".chat-history/log.md", "outputs/routing_results.json", ".env.example"]
NEVER_SUBMIT = [".env", ".env.*", "*.db", "*.db-*", "*.sqlite*"]
ALLOWED = {".env.example"}
ZIP_NAME = "ridgeline-quote-approvals.zip"

results: list[tuple[str, str, str]] = []


def record(status: str, check: str, detail: str = "") -> None:
    results.append((status, check, detail))


def forbidden(path: str) -> bool:
    name = Path(path).name
    return name not in ALLOWED and any(fnmatch.fnmatch(name, pat) for pat in NEVER_SUBMIT)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def check() -> list[str]:
    tracked = [p for p in git("ls-files").splitlines() if p]

    for path in DELIVERABLES:
        f = ROOT / path
        if not f.exists() or f.stat().st_size == 0:
            record("FAIL", f"deliverable {path}", "missing or empty")
        elif path not in tracked:
            record("FAIL", f"deliverable {path}", "exists but is not tracked by git")
        else:
            record("PASS", f"deliverable {path}")

    lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    in_code, heads = False, []
    for line in lines:
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and re.match(r"^#{1,2} ", line):
            heads.append(line.strip())
    record("PASS" if heads == HEADINGS else "FAIL", "README level-1/2 headings exact and in order",
           "" if heads == HEADINGS else f"found {heads}")
    todos = [f"README.md:{i}: {l.strip()}" for i, l in enumerate(lines, 1) if "TODO" in l]
    for t in todos:
        record("WARN", "README still has a TODO", t)

    try:
        ids = [d.draft_id for d in load_drafts(ROOT / "data" / "draft_quotes.json")]
        validate_routing_results((ROOT / "outputs" / "routing_results.json").read_text(encoding="utf-8"), ids)
        text = (ROOT / "outputs" / "routing_results.json").read_text(encoding="utf-8")
        if "stock unavailable after" in text:
            record("WARN", "routing_results.json", "contains 'stock unavailable' flags: was the mock running?")
        record("PASS", "outputs/routing_results.json matches Deliverable 1 schema, keys and draft order")
    except (OutputValidationError, OSError, ValueError) as exc:
        record("FAIL", "outputs/routing_results.json", str(exc).splitlines()[0])

    env_lines = [l for l in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.lstrip().startswith("#")]
    with_values = [l for l in env_lines if not re.fullmatch(r"[A-Z0-9_]+=\s*", l)]
    record("PASS" if not with_values else "FAIL", ".env.example has variable names only",
           "" if not with_values else f"lines with values: {with_values}")

    bad = [p for p in tracked if forbidden(p)]
    record("PASS" if not bad else "FAIL", "no .env / database files tracked by git", ", ".join(bad))

    if not (ROOT / ".chat-history" / "log.md").read_text(encoding="utf-8").strip():
        record("FAIL", ".chat-history/log.md", "empty")

    dirty = [l for l in git("status", "--porcelain").splitlines() if l.strip()]
    if dirty:
        record("WARN", "uncommitted changes (commit and push before zipping)", f"{len(dirty)} paths, e.g. {dirty[0]}")
    return tracked


def build_zip(tracked: list[str]) -> None:
    files = sorted({*tracked, *(str(p.relative_to(ROOT)).replace("\\", "/")
                                for p in (ROOT / ".chat-history").rglob("*") if p.is_file())})
    files = [f for f in files if not forbidden(f) and f != ZIP_NAME and (ROOT / f).is_file()]
    out = ROOT / ZIP_NAME
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(ROOT / f, f"ridgeline-quote-approvals/{f}")
    names = [n.split("/", 1)[1] for n in zipfile.ZipFile(out).namelist()]
    missing = [d for d in DELIVERABLES if d not in names]
    leaked = [n for n in names if forbidden(n)]
    ok = not missing and not leaked
    record("PASS" if ok else "FAIL", f"{ZIP_NAME}: {len(names)} files, has all deliverables, no .env",
           f"missing={missing} leaked={leaked}" if not ok else str(out))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--zip", action="store_true", help=f"build and verify {ZIP_NAME} in the repo root")
    args = ap.parse_args()
    tracked = check()
    if args.zip:
        build_zip(tracked)
    for status, name, detail in results:
        print(f"{status:<4}  {name}" + (f"  ({detail})" if detail else ""))
    return 1 if any(s == "FAIL" for s, _, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
