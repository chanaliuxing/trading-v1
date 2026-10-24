#!/usr/bin/env python3
"""
Premarket readiness check for concurrent QQQ option live strategies.

Checks:
1. Both target strategies exist and are enabled
2. Shared cap group settings are present and identical
3. Telegram allowlist includes both strategies
4. QQQ options_quotes_1m freshness / coverage for dte 0-2
5. 1DTE runtime state file is either absent or belongs to today
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, time
from pathlib import Path
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client

ET = ZoneInfo("America/New_York")

TARGET_STRATEGY_IDS = [
    "qqq_0dte_opt_live_001",
    "qqq_1dte_ma10_reject_0324_001",
]
SHARED_FIELDS = [
    "shared_cap_group_id",
    "shared_cap_group_limit_usd",
    "shared_cap_group_strategy_ids",
]


def _load_config(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _strategy_map(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    strategies = cfg.get("strategies") or []
    return {str(item.get("id") or ""): (item or {}) for item in strategies}


def _check_strategy_enablement(cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    infos: List[str] = []
    strategies = _strategy_map(cfg)
    for strategy_id in TARGET_STRATEGY_IDS:
        item = strategies.get(strategy_id)
        if not item:
            errors.append(f"missing strategy: {strategy_id}")
            continue
        enabled = bool(item.get("enabled", False))
        infos.append(f"strategy {strategy_id} enabled={enabled}")
        if not enabled:
            errors.append(f"strategy disabled: {strategy_id}")
    return errors, infos


def _shared_cap_signature(extras: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(extras or {})
    ids = normalized.get("shared_cap_group_strategy_ids") or []
    normalized["shared_cap_group_strategy_ids"] = sorted(str(x) for x in ids)
    return {field: normalized.get(field) for field in SHARED_FIELDS}


def _check_shared_cap_group(cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    infos: List[str] = []
    strategies = _strategy_map(cfg)
    baseline: Dict[str, Any] | None = None
    for strategy_id in TARGET_STRATEGY_IDS:
        item = strategies.get(strategy_id) or {}
        extras = ((item.get("params") or {}).get("extras") or {})
        sig = _shared_cap_signature(extras)
        infos.append(f"shared-cap {strategy_id}: {sig}")
        if any(sig.get(field) in (None, "", []) for field in SHARED_FIELDS):
            errors.append(f"shared cap incomplete: {strategy_id}")
            continue
        if baseline is None:
            baseline = sig
            continue
        if sig != baseline:
            errors.append(f"shared cap mismatch: {strategy_id}")
    return errors, infos


def _check_telegram_allowlist(cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    allowlist = (
        ((cfg.get("notifications") or {}).get("telegram") or {}).get("strategy_allowlist")
        or []
    )
    infos = [f"telegram allowlist={allowlist}"]
    for strategy_id in TARGET_STRATEGY_IDS:
        if strategy_id not in allowlist:
            errors.append(f"telegram allowlist missing: {strategy_id}")
    return errors, infos


def _check_runtime_state(strategy_id: str, now: datetime) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    infos: List[str] = []
    state_path = ROOT / "runtime" / "state" / f"qqq_1dte_ma10_reject_live_{strategy_id}.json"
    if not state_path.exists():
        infos.append(f"runtime state absent: {state_path}")
        return errors, infos
    try:
        import json

        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"runtime state unreadable: {exc}"], infos

    day = str(raw.get("day") or "")
    reason = str(raw.get("reason") or "")
    infos.append(f"runtime state present: day={day} reason={reason} path={state_path}")
    if day and day != now.strftime("%Y-%m-%d"):
        errors.append(f"stale runtime state day={day} path={state_path}")
    return errors, infos


def _check_quotes_health(underlying: str, lookback_min: int, max_age_sec: int) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    infos: List[str] = []
    ch = get_client()
    now = datetime.now(ET).replace(second=0, microsecond=0)
    rows = ch.query(
        """
        SELECT
          max(created_at) AS last_created_at,
          max(ts) AS last_ts,
          countDistinct(toStartOfMinute(ts)) AS covered_minutes,
          countDistinctIf(toStartOfMinute(ts), bid > 0 AND ask > 0) AS nbbo_minutes,
          count() AS rows_n
        FROM trading.options_quotes_1m
        WHERE underlying = {u:String}
          AND dte >= 0
          AND dte <= 2
          AND ts >= {ts_start:DateTime64(3, 'America/New_York')}
          AND ts < {ts_end:DateTime64(3, 'America/New_York')}
        """,
        parameters={
            "u": underlying.upper(),
            "ts_start": now - timedelta(minutes=lookback_min),
            "ts_end": now + timedelta(minutes=1),
        },
    )
    row = rows[0] if rows else {}
    src = row.get("last_created_at") or row.get("last_ts")
    age_sec = None
    if isinstance(src, datetime):
        age_sec = max(0.0, (datetime.now(ET) - src.astimezone(ET)).total_seconds())
    covered = int(row.get("covered_minutes", 0) or 0)
    nbbo = int(row.get("nbbo_minutes", 0) or 0)
    rows_n = int(row.get("rows_n", 0) or 0)
    infos.append(
        f"quotes underlying={underlying.upper()} age_sec={None if age_sec is None else round(age_sec, 2)} "
        f"covered_minutes={covered} nbbo_minutes={nbbo} rows={rows_n}"
    )
    if now.time() < time(9, 30) and rows_n == 0:
        infos.append("pre-open window with no live option quotes yet; quote freshness check skipped")
        return errors, infos
    if age_sec is None:
        errors.append("quotes missing: no recent options_quotes_1m rows")
        return errors, infos
    if age_sec > float(max_age_sec):
        errors.append(f"quotes stale: age_sec={round(age_sec, 2)} > {max_age_sec}")
    if covered < max(1, lookback_min - 1):
        errors.append(f"quotes coverage low: covered_minutes={covered} lookback_min={lookback_min}")
    if nbbo <= 0:
        errors.append("quotes nbbo missing in lookback window")
    return errors, infos


def main() -> int:
    ap = argparse.ArgumentParser(description="Check dual-live readiness for QQQ option strategies.")
    ap.add_argument("--config", default="config.yml")
    ap.add_argument("--underlying", default="QQQ")
    ap.add_argument("--lookback-min", type=int, default=10)
    ap.add_argument("--max-age-sec", type=int, default=120)
    ap.add_argument("--skip-clickhouse", action="store_true")
    args = ap.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"FAIL config not found: {config_path}")
        return 1

    cfg = _load_config(config_path)
    now = datetime.now(ET)

    checks: List[Tuple[str, List[str], List[str]]] = []
    checks.append(("strategy_enablement", *_check_strategy_enablement(cfg)))
    checks.append(("shared_cap_group", *_check_shared_cap_group(cfg)))
    checks.append(("telegram_allowlist", *_check_telegram_allowlist(cfg)))
    checks.append(("runtime_state", *_check_runtime_state("qqq_1dte_ma10_reject_0324_001", now)))
    if not args.skip_clickhouse:
        try:
            checks.append(("quotes_health", *_check_quotes_health(args.underlying, args.lookback_min, args.max_age_sec)))
        except Exception as exc:
            checks.append(("quotes_health", [f"clickhouse query failed: {exc}"], []))

    total_errors = 0
    print("=" * 72)
    print("QQQ option dual-live readiness")
    print("=" * 72)
    for name, errors, infos in checks:
        print(f"[{name}]")
        for info in infos:
            print(f"  INFO  {info}")
        for err in errors:
            print(f"  ERROR {err}")
        if not infos and not errors:
            print("  INFO  no output")
        total_errors += len(errors)

    print("-" * 72)
    if total_errors:
        print(f"FAIL total_errors={total_errors}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
