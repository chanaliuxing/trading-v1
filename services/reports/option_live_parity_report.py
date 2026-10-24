from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ET_DAY_FMT = "%Y-%m-%d"


def _safe_json_load(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _load_csv_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@dataclass(frozen=True)
class OptionLiveParityReport:
    strategy_id: str
    report_date: str
    parity_profile_id: str
    snapshots_total: int
    replay_expected_entries: int
    live_shadow_entries: int
    live_submitted_entries: int
    matched_entries: int
    missing_live_entries: int
    extra_live_entries: int
    duplicate_blocked_entries: int
    shadow_skipped_entries: int
    signal_kind_counts: Dict[str, int]
    replay_signal_kind_counts: Dict[str, int]
    matched_entry_times: List[str]
    missing_entry_times: List[str]
    extra_entry_times: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "report_date": self.report_date,
            "parity_profile_id": self.parity_profile_id,
            "snapshots_total": self.snapshots_total,
            "replay_expected_entries": self.replay_expected_entries,
            "live_shadow_entries": self.live_shadow_entries,
            "live_submitted_entries": self.live_submitted_entries,
            "matched_entries": self.matched_entries,
            "missing_live_entries": self.missing_live_entries,
            "extra_live_entries": self.extra_live_entries,
            "duplicate_blocked_entries": self.duplicate_blocked_entries,
            "shadow_skipped_entries": self.shadow_skipped_entries,
            "signal_kind_counts": dict(self.signal_kind_counts),
            "replay_signal_kind_counts": dict(self.replay_signal_kind_counts),
            "matched_entry_times": list(self.matched_entry_times),
            "missing_entry_times": list(self.missing_entry_times),
            "extra_entry_times": list(self.extra_entry_times),
            "trade_parity": (
                float(self.matched_entries) / float(self.replay_expected_entries)
                if self.replay_expected_entries > 0
                else 1.0
            ),
            "no_trade_parity_ok": self.extra_live_entries == 0,
        }


def _collect_snapshot_rows(snapshot_dir: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not snapshot_dir.exists():
        return rows
    for path in sorted(snapshot_dir.glob("*.json")):
        payload = _safe_json_load(path)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _extract_live_entry_times(snapshot_rows: Iterable[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for row in snapshot_rows:
        ts = str(row.get("bar_close_ts") or "").strip()
        if ts:
            out.append(ts)
    return sorted(set(out))


def _extract_replay_entry_times(trades_rows: Iterable[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for row in trades_rows:
        ts = str(row.get("entry_ts") or "").strip()
        if ts and ts not in seen:
            out.append(ts)
            seen.add(ts)
    return sorted(out)


def _counter_by_signal(rows: Iterable[Dict[str, Any]], key_path: str) -> Dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        if key_path == "snapshot":
            sig = str(((row.get("signal") or {}) if isinstance(row.get("signal"), dict) else {}).get("signal_kind") or "").strip()
        else:
            sig = str(row.get("signal_kind") or "").strip()
        counter[sig or "unknown"] += 1
    return dict(counter)


def generate_option_live_parity_report(
    *,
    strategy_id: str,
    report_date: str,
    snapshot_root: str | Path,
    audit_root: str | Path = "runtime/audit",
    replay_dir: str | Path | None = None,
) -> OptionLiveParityReport:
    day = str(report_date).strip()
    snapshot_dir = Path(snapshot_root).expanduser() / str(strategy_id).strip() / day
    snapshot_rows = _collect_snapshot_rows(snapshot_dir)

    audit_dir = Path(audit_root).expanduser() / day
    audit_rows: List[Dict[str, Any]] = []
    for path in sorted(audit_dir.glob("strategy-*.jsonl")):
        for row in _load_jsonl(path):
            if str(row.get("strategy_id") or "").strip() == str(strategy_id).strip():
                audit_rows.append(row)

    live_entry_times = _extract_live_entry_times(snapshot_rows)
    signal_kind_counts = _counter_by_signal(snapshot_rows, "snapshot")
    duplicate_blocked_entries = sum(1 for row in audit_rows if str(row.get("stage") or "") == "entry_snapshot_duplicate_blocked")
    shadow_skipped_entries = sum(1 for row in audit_rows if str(row.get("stage") or "") == "entry_shadow_mode_skipped")
    live_submitted_entries = sum(1 for row in audit_rows if str(row.get("stage") or "") == "entry_intent_submitted")

    replay_rows: List[Dict[str, Any]] = []
    replay_entry_times: List[str] = []
    replay_signal_kind_counts: Dict[str, int] = {}
    if replay_dir is not None:
        replay_rows = _load_csv_rows(Path(replay_dir).expanduser() / "trades.csv")
        replay_entry_times = _extract_replay_entry_times(replay_rows)
        replay_signal_kind_counts = _counter_by_signal(replay_rows, "trades")

    live_set = set(live_entry_times)
    replay_set = set(replay_entry_times)
    matched = sorted(live_set & replay_set)
    missing = sorted(replay_set - live_set)
    extra = sorted(live_set - replay_set)

    parity_profile_id = ""
    if snapshot_rows:
        parity_profile_id = str(snapshot_rows[0].get("parity_profile_id") or "").strip()

    return OptionLiveParityReport(
        strategy_id=str(strategy_id).strip(),
        report_date=day,
        parity_profile_id=parity_profile_id,
        snapshots_total=len(snapshot_rows),
        replay_expected_entries=len(replay_entry_times),
        live_shadow_entries=len(live_entry_times),
        live_submitted_entries=int(live_submitted_entries),
        matched_entries=len(matched),
        missing_live_entries=len(missing),
        extra_live_entries=len(extra),
        duplicate_blocked_entries=int(duplicate_blocked_entries),
        shadow_skipped_entries=int(shadow_skipped_entries),
        signal_kind_counts=signal_kind_counts,
        replay_signal_kind_counts=replay_signal_kind_counts,
        matched_entry_times=matched,
        missing_entry_times=missing,
        extra_entry_times=extra,
    )
