"""Write guard: this service never writes, creates or deletes anything under data/.

data/ holds the read-only reporting snapshot (an SAP export) and the sample data pack.
Our own writes go only to app.db and outputs/; every write path is checked here first.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

# Anchored to the project root, so the guard holds whatever the current working directory is.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROTECTED_DIRS: tuple[Path, ...] = (PROJECT_ROOT / "data",)


class ProtectedPathError(ValueError):
    """Refusing to write to a protected (read-only) location."""


def ensure_writable(path: Path, protected: Iterable[Path] = PROTECTED_DIRS, *, what: str = "write") -> Path:
    """Return the resolved path, or raise if it is inside a protected directory (symlinks followed)."""
    resolved = Path(path).resolve()
    for d in protected:
        root = Path(d).resolve()
        if resolved == root or resolved.is_relative_to(root):
            raise ProtectedPathError(f"refusing to {what} under {d}/ (read-only data): {path}")
    return resolved
