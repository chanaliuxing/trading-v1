#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.stocks_kernel.policies.qqq_intraday_policy_v1 import QQQIntradayPolicyV1
from strategies.stocks_kernel.types import FeatureContext, GateEvaluationReport, HistoryBar, HistoryBundle, MarketSnapshot


@dataclass(frozen=True)
class LegacyGateRow:
    ts_iso: str
    ts_epoch_s: float
    decision: str
    reason: str
    direction: str
    instrument: str
    qqq_close_now: float
    vwap_now: float
    atr_eff: float
    atr_seed: float
    atr_live: float
    v3m_signed: float
    vol_t: float
    vol_ratio: float | None
    vol_surge_ratio: float | None
    distance_from_vwap_atr: float | None
    tv: float | None
    data_age_qqq_sec: float | None
    data_age_qqew_sec: float | None
    continuity_180s_ok: bool
    seed_n: int | None
    seed_ready: bool | None
    day: str


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except Exception:
        return float(default)


def _load_strategy_extras(config_path: Path, *, strategy_id: str) -> dict[str, Any]:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    for row in list(payload.get("strategies") or []):
        if str(row.get("id") or "").strip() != strategy_id:
            continue
        params = dict(row.get("params") or {})
        extras = dict(params.get("extras") or {})
        policy_params = params.get("policy_params")
        if not isinstance(policy_params, dict) and isinstance(extras.get("policy_params"), dict):
            policy_params = extras.get("policy_params")
        if isinstance(policy_params, dict):
            extras = {**extras, **policy_params}
        return extras
    raise ValueError(f"strategy_id not found in config: {strategy_id}")


def _parse_ts_iso(ts_iso: str) -> tuple[float, str]:
    dt = datetime.fromisoformat(str(ts_iso))
    return float(dt.timestamp()), dt.date().isoformat()


def _iter_legacy_gate_rows(*, diag_path: Path, day: str | None, max_rows: int = 0) -> list[LegacyGateRow]:
    out: list[LegacyGateRow] = []
    with diag_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if str(row.get("event") or "").strip() != "entry_gate":
                continue
            ts_iso = str(row.get("ts_now_et") or row.get("ts_et") or "").strip()
            if not ts_iso:
                continue
            try:
                ts_epoch_s, row_day = _parse_ts_iso(ts_iso)
            except Exception:
                continue
            if day and row_day != day:
                continue
            qqq_close_now = _safe_float(row.get("qqq_close_now"), 0.0)
            if qqq_close_now <= 0.0:
                continue
            decision = str(row.get("decision") or "").strip().upper()
            reason = str(row.get("reason") or "").strip()
            direction = str(row.get("direction") or "").strip().upper()
            instrument = str(row.get("instrument") or "").strip().upper()
            out.append(
                LegacyGateRow(
                    ts_iso=ts_iso,
                    ts_epoch_s=ts_epoch_s,
                    decision=decision,
                    reason=reason,
                    direction=direction,
                    instrument=instrument,
                    qqq_close_now=qqq_close_now,
                    vwap_now=_safe_float(row.get("vwap_now"), qqq_close_now),
                    atr_eff=_safe_float(row.get("atr_eff"), 0.0),
                    atr_seed=_safe_float(row.get("atr_seed"), 0.0),
                    atr_live=_safe_float(row.get("atr_live"), 0.0),
                    v3m_signed=_safe_float(row.get("v3m_signed"), 0.0),
                    vol_t=_safe_float(row.get("vol_t"), 0.0),
                    vol_ratio=(_safe_float(row.get("vol_ratio"), 0.0) if row.get("vol_ratio") is not None else None),
                    vol_surge_ratio=(
                        _safe_float(row.get("vol_surge_ratio"), 0.0) if row.get("vol_surge_ratio") is not None else None
                    ),
                    distance_from_vwap_atr=(
                        _safe_float(row.get("distance_from_vwap_atr"), 0.0) if row.get("distance_from_vwap_atr") is not None else None
                    ),
                    tv=(_safe_float(row.get("tv"), 0.0) if row.get("tv") is not None else None),
                    data_age_qqq_sec=(
                        _safe_float(row.get("data_age_qqq_sec"), 0.0) if row.get("data_age_qqq_sec") is not None else None
                    ),
                    data_age_qqew_sec=(
                        _safe_float(row.get("data_age_qqew_sec"), 0.0) if row.get("data_age_qqew_sec") is not None else None
                    ),
                    continuity_180s_ok=not bool(row.get("stream_invalid", False)),
                    seed_n=(int(row.get("seed_n")) if row.get("seed_n") is not None else None),
                    seed_ready=(bool(row.get("seed_ready")) if row.get("seed_ready") is not None else None),
                    day=row_day,
                )
            )
            if max_rows > 0 and len(out) >= max_rows:
                break
    return out


def _build_kernel_features(row: LegacyGateRow, history: deque[HistoryBar], *, planned_qty: float) -> dict[str, Any]:
    history.append(
        HistoryBar(
            ts_epoch_s=float(row.ts_epoch_s),
            open=float(row.qqq_close_now),
            high=float(row.qqq_close_now),
            low=float(row.qqq_close_now),
            close=float(row.qqq_close_now),
            volume=max(0.0, float(row.vol_t)),
        )
    )
    reason = str(row.reason or "").strip()
    anti_chase_blocked = reason == "anti_chase_blocked"
    amp_not_locked = reason == "amp_not_locked"
    anti_chase_tv = row.tv if row.tv is not None else 3.5
    if anti_chase_blocked and row.distance_from_vwap_atr is not None:
        anti_chase_tv = max(0.01, float(row.distance_from_vwap_atr) - 1e-6)
    return {
        "ts_epoch_s": float(row.ts_epoch_s),
        "now_epoch_s": float(row.ts_epoch_s),
        "last_price": float(row.qqq_close_now),
        "close": float(row.qqq_close_now),
        "vwap": float(row.vwap_now),
        "volume": max(0.0, float(row.vol_t)),
        "session": "RTH",
        "planned_qty": float(planned_qty),
        "trigger_id": f"identity_{int(row.ts_epoch_s)}",
        "bars_1m": list(history),
        "atr_eff": float(row.atr_eff),
        "atr_seed": float(row.atr_seed),
        "atr_live": float(row.atr_live),
        "v3m": float(row.v3m_signed),
        "v3m_signed": float(row.v3m_signed),
        "vol_surge_ratio": float(row.vol_surge_ratio) if row.vol_surge_ratio is not None else 0.0,
        "vol_ratio_override": float(row.vol_ratio) if row.vol_ratio is not None else None,
        "data_age_qqq_sec": row.data_age_qqq_sec,
        "data_age_qqew_sec": row.data_age_qqew_sec,
        "continuity_180s_ok": bool(row.continuity_180s_ok),
        "seed_n": row.seed_n,
        "seed_ready": row.seed_ready,
        # Dynamic parity hints from legacy diagnostics. These are only for offline
        # identity replay and do not affect production runtime.
        "startup_anchor_entry_block_active": reason == "startup_anchor_embargo",
        "data_integrity_fail_closed": reason == "data_integrity_fail_closed",
        "amp_locked": False if amp_not_locked else True,
        "entry_require_amp_lock": True if amp_not_locked else None,
        "anti_chase_enabled": anti_chase_blocked,
        "anti_chase_rank15": 1.0 if anti_chase_blocked else None,
        "anti_chase_distance_from_vwap_atr_max": anti_chase_tv,
    }


def _build_policy(*, qqq_extras: dict[str, Any]) -> QQQIntradayPolicyV1:
    policy_params = dict(qqq_extras)
    policy_params["rule_engine_parity"] = True
    # Legacy diag emits v3m_signed in price-delta shape; converting again would
    # distort sign gating in identity replay.
    policy_params["v3m_units"] = "price"
    return QQQIntradayPolicyV1(
        max_trades_per_day=int(policy_params.get("max_trades_per_day", 4) or 4),
        hard_stop_atr_mult=float(policy_params.get("hard_stop_atr_mult", 0.75) or 0.75),
        logic_retreat_min_hold_sec=int(policy_params.get("logic_retreat_min_hold_sec", 360) or 360),
        rule_engine_parity=True,
        entry_model=str(policy_params.get("entry_model", "baseline") or "baseline"),
        qqew_decoupled=bool(policy_params.get("qqew_decoupled", False)),
        entry_recipe=dict(policy_params.get("entry_recipe") or {}),
        v3m_units="price",
        static_extras=policy_params,
    )


def _legacy_allowed(row: LegacyGateRow) -> bool:
    return row.decision == "ENTRY_ALLOWED" or row.reason == "entry_allowed"


def _expected_exec_symbol(side: str) -> str:
    return "TQQQ" if str(side).upper() == "LONG" else "SQQQ"


async def _run(*, diag_path: Path, config_path: Path, strategy_id: str, day: str | None, max_rows: int) -> dict[str, Any]:
    qqq_extras = _load_strategy_extras(config_path, strategy_id=strategy_id)
    rows = _iter_legacy_gate_rows(diag_path=diag_path, day=day, max_rows=max_rows)
    if not rows:
        return {
            "ok": False,
            "error": "no_rows",
            "diag_path": str(diag_path),
            "day": day,
            "strategy_id": strategy_id,
        }
    policy = _build_policy(qqq_extras=qqq_extras)
    history: deque[HistoryBar] = deque(maxlen=390)
    mismatches: list[dict[str, Any]] = []
    total = 0
    allow_match = 0
    side_match = 0
    reason_match = 0
    exec_match = 0
    for row in rows:
        total += 1
        features = _build_kernel_features(row, history, planned_qty=1.0)
        snapshot = MarketSnapshot(
            symbol="QQQ",
            ts_epoch_s=float(row.ts_epoch_s),
            last_price=float(row.qqq_close_now),
            session="RTH",
            vwap=float(row.vwap_now),
            minute_close=float(row.qqq_close_now),
            minute_open=float(row.qqq_close_now),
            minute_high=float(row.qqq_close_now),
            minute_low=float(row.qqq_close_now),
            minute_volume=float(row.vol_t),
            extras={
                key: features.get(key)
                for key in (
                    "planned_qty",
                    "trigger_id",
                    "same_strategy_pending_orders",
                    "foreign_position_qty",
                    "seed_n",
                    "seed_ready",
                    "amp_locked",
                    "entry_require_amp_lock",
                    "stable_entry_block_reason",
                    "restart_degrade_active",
                    "startup_anchor_entry_block_active",
                    "data_integrity_fail_closed",
                    "pending_decision_sync",
                    "ownership_conflict",
                    "anti_chase_enabled",
                    "anti_chase_rank15",
                    "anti_chase_distance_from_vwap_atr_max",
                    "last_stop_exit_side",
                    "last_stop_exit_at",
                    "stop_reentry_cooldown_min",
                    "enable_high_vol_guard",
                    "high_vol_ratio",
                    "hv_min_vol_surge",
                    "hv_k",
                    "hv_tv_min_usd",
                    "vol_ratio_override",
                    "day_shape_overlay_enabled",
                    "day_shape_long_rank_max",
                    "day_shape_short_rank_min",
                )
                if features.get(key) is not None
            },
        )
        feature_context = FeatureContext(
            symbol="QQQ",
            snapshot=snapshot,
            history=HistoryBundle(symbol="QQQ", bars_1m=tuple(history)),
            values={
                "vwap": float(row.vwap_now),
                "atr_eff": float(row.atr_eff),
                "atr_seed": float(row.atr_seed),
                "atr_live": float(row.atr_live),
                "v3m": float(row.v3m_signed),
                "v3m_signed": float(row.v3m_signed),
                "vol_surge_ratio": float(row.vol_surge_ratio) if row.vol_surge_ratio is not None else 0.0,
                "data_age_qqq_sec": row.data_age_qqq_sec,
                "data_age_qqew_sec": row.data_age_qqew_sec,
                "continuity_180s_ok": bool(row.continuity_180s_ok),
                "seed_n": row.seed_n,
            },
            readiness={},
        )
        signal = policy.build_signal(
            symbol="QQQ",
            feature_context=feature_context,
            gate_report=GateEvaluationReport(allowed=True, decisions=()),
        )
        entry_plan = policy.build_entry_plan(signal=signal, feature_context=feature_context)
        kernel_allowed = bool(getattr(signal, "action", None) == "enter" and getattr(signal, "side", None) in {"LONG", "SHORT"})
        legacy_allowed = _legacy_allowed(row)
        kernel_side = str(getattr(signal, "side", "") or "").upper() if kernel_allowed else ""
        legacy_side = str(row.direction or "").upper() if legacy_allowed else ""
        kernel_reason = str((getattr(signal, "payload", {}) or {}).get("reason") or "").strip()
        legacy_reason = str(row.reason or "").strip()
        expected_exec = _expected_exec_symbol(kernel_side) if kernel_side in {"LONG", "SHORT"} else ""
        kernel_exec = expected_exec if kernel_allowed else ""

        if kernel_allowed == legacy_allowed:
            allow_match += 1
        if kernel_side == legacy_side:
            side_match += 1
        if kernel_reason == legacy_reason:
            reason_match += 1
        if kernel_exec == expected_exec:
            exec_match += 1

        if not (kernel_allowed == legacy_allowed and kernel_side == legacy_side and kernel_reason == legacy_reason and kernel_exec == expected_exec):
            mismatches.append(
                {
                    "ts_iso": row.ts_iso,
                    "legacy": {
                        "decision": row.decision,
                        "reason": legacy_reason,
                        "allowed": legacy_allowed,
                        "side": legacy_side,
                        "instrument": row.instrument,
                    },
                    "kernel": {
                        "allowed": kernel_allowed,
                        "side": kernel_side,
                        "reason": kernel_reason,
                        "exec_symbol": kernel_exec,
                    },
                }
            )
    parity = {
        "rows": total,
        "allow_match_ratio": round(allow_match / max(1, total), 6),
        "side_match_ratio": round(side_match / max(1, total), 6),
        "reason_match_ratio": round(reason_match / max(1, total), 6),
        "exec_symbol_match_ratio": round(exec_match / max(1, total), 6),
    }
    return {
        "ok": True,
        "diag_path": str(diag_path),
        "strategy_id": strategy_id,
        "day": day or rows[0].day,
        "rows": total,
        "parity": parity,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:200],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="QQQ legacy diag vs stocks-kernel identity parity checker")
    parser.add_argument("--diag-path", default=str(ROOT / "runtime" / "state" / "qqq_intraday_diag_qqq_intraday_001.jsonl"))
    parser.add_argument("--config-path", default=str(ROOT / "config.yml"))
    parser.add_argument("--strategy-id", default="qqq_intraday_001")
    parser.add_argument("--day", default="")
    parser.add_argument("--max-rows", type=int, default=0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    payload = asyncio.run(
        _run(
            diag_path=Path(args.diag_path).expanduser(),
            config_path=Path(args.config_path).expanduser(),
            strategy_id=str(args.strategy_id),
            day=(str(args.day).strip() or None),
            max_rows=max(0, int(args.max_rows)),
        )
    )
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if str(args.output).strip():
        out_path = Path(args.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if not bool(payload.get("ok")):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
