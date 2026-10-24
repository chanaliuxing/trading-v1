"""Utilities for reproducibility metadata."""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import Iterable, Optional


def git_commit_sha() -> str:
    """Return current git commit SHA or "unknown" if unavailable."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return "unknown"


def config_sha256(cfg: dict) -> str:
    """Deterministic hash for configuration dictionaries."""
    payload = json.dumps(cfg or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def dataframe_snapshot(df, cols: Optional[Iterable[str]] = None) -> str:
    """Create a lightweight snapshot hash for data provenance."""
    if df is None:
        return "unknown"

    try:
        import pandas as pd  # Local import to avoid hard dependency when not needed

        if not isinstance(df, pd.DataFrame) or df.empty:
            return "unknown"

        cols = list(cols) if cols is not None else ["ts", "symbol"]
        h = hashlib.sha256()

        # Record column ordering to detect schema drifts
        h.update("|".join(sorted(df.columns)).encode("utf-8"))

        for col in cols:
            if col not in df.columns:
                continue
            series = df[col].astype(str)
            if series.empty:
                continue
            head = series.iloc[0]
            tail = series.iloc[-1]
            h.update(f"{col}:{head}->{tail}".encode("utf-8"))

        h.update(str(len(df)).encode("utf-8"))
        return h.hexdigest()
    except Exception:
        return "unknown"
