#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client


def _parse_ts(raw: Any) -> Optional[datetime]:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _load_strategy_rows(audit_root: Path, day: str, strategy_id: str) -> List[Dict[str, Any]]:
    base = audit_root / day
    rows: List[Dict[str, Any]] = []
    if not base.exists():
        return rows
    for path in sorted(base.glob("strategy-*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if str(rec.get("strategy_id") or "") != strategy_id:
                    continue
                ts = _parse_ts(rec.get("ts"))
                if ts is None:
                    continue
                rec["_ts"] = ts
                rows.append(rec)
        except Exception:
            continue
    rows.sort(key=lambda x: x["_ts"])
    return rows


def _find_first(rows: List[Dict[str, Any]], stage: str) -> Optional[Dict[str, Any]]:
    for r in rows:
        if str(r.get("stage") or "") == stage:
            return r
    return None


def _bars_close_at_or_before(ch: Any, symbol: str, ts: datetime) -> Optional[Dict[str, Any]]:
    q = ch.query(
        """
        SELECT ts_end, close, volume
        FROM trading.bars_1m_ibkr
        WHERE symbol = {sym:String}
          AND ts_end <= {ts:DateTime64(3, 'America/New_York')}
          AND ts_end >= {lb:DateTime64(3, 'America/New_York')}
        ORDER BY ts_end DESC
        LIMIT 1
        """,
        parameters={"sym": symbol.upper(), "ts": ts, "lb": ts - timedelta(minutes=5)},
    )
    return q[0] if q else None


def _latest_prior_option_quote(ch: Any, ts: datetime, underlying: str = "QQQ") -> Optional[Dict[str, Any]]:
    q = ch.query(
        """
        SELECT ts, created_at, underlying, expiry, strike, right, dte, bid, ask, mid
        FROM trading.options_quotes_1m
        WHERE underlying = {u:String}
          AND created_at <= {ts:DateTime64(3, 'America/New_York')}
          AND created_at >= {lb:DateTime64(3, 'America/New_York')}
          AND dte IN (0, 1)
        ORDER BY created_at DESC
        LIMIT 1
        """,
        parameters={"u": underlying.upper(), "ts": ts, "lb": ts - timedelta(minutes=10)},
    )
    return q[0] if q else None


def _iso(x: Any) -> Optional[str]:
    if isinstance(x, datetime):
        return x.isoformat()
    if x is None:
        return None
    return str(x)


def build_report(audit_root: Path, day: str, strategy_id: str, out_dir: Path) -> Dict[str, Any]:
    rows = _load_strategy_rows(audit_root, day, strategy_id)
    signals = [
        r
        for r in rows
        if str(r.get("stage") or "") == "entry_intent_requested"
        and str((r.get("payload") or {}).get("reason") or "").startswith("qqq_intraday_")
    ]
    ch = get_client()

    details: List[Dict[str, Any]] = []
    for sig in signals:
        decision_id = str(sig.get("decision_id") or "").strip()
        trace_id = str(sig.get("trace_id") or "").strip()
        trigger_id = str(sig.get("trigger_id") or "").strip()
        sig_ts = sig["_ts"]
        sig_symbol = str(sig.get("symbol") or "").upper()
        payload = sig.get("payload") or {}

        chain = [
            r
            for r in rows
            if (
                (decision_id and str(r.get("decision_id") or "") == decision_id)
                or (trace_id and str(r.get("trace_id") or "") == trace_id)
            )
        ]
        chain.sort(key=lambda x: x["_ts"])

        gate = _find_first(chain, "gate_evaluated")
        submit_ready = _find_first(chain, "entry_submit_ready")
        submitted = _find_first(chain, "entry_intent_submitted")

        gate_payload = dict((gate or {}).get("payload") or {})
        sr_payload = dict((submit_ready or {}).get("payload") or {})

        bar = _bars_close_at_or_before(ch, sig_symbol, sig_ts)
        bar_close = float(bar.get("close")) if bar and bar.get("close") is not None else None
        ref_px = payload.get("ref_px")
        ref_minus_bar_close = None
        if ref_px is not None and bar_close is not None:
            try:
                ref_minus_bar_close = float(ref_px) - float(bar_close)
            except Exception:
                ref_minus_bar_close = None

        oq = _latest_prior_option_quote(ch, sig_ts, "QQQ")
        quote_age_sec = None
        if oq and oq.get("created_at") is not None:
            qts = _parse_ts(oq.get("created_at"))
            if qts is not None:
                quote_age_sec = max(0.0, (sig_ts - qts).total_seconds())

        reason = str(payload.get("reason") or "")
        qty = int(payload.get("qty") or 0)
        probe_qty = int(sr_payload.get("probe_qty") or 0)
        add_qty = int(sr_payload.get("add_qty") or 0)
        qty_total = int(sr_payload.get("qty_total") or 0)
        qty_ok = True
        if reason.endswith("entry") and submit_ready is not None:
            qty_ok = (qty == probe_qty and qty_total == probe_qty + add_qty)
        elif reason.endswith("add") and submit_ready is not None:
            qty_ok = qty == add_qty

        is_entry_reason = reason.endswith("entry")
        checks = {
            "is_entry_signal": is_entry_reason,
            "has_gate_evaluated": gate is not None,
            "has_entry_submit_ready": submit_ready is not None,
            "has_entry_intent_submitted": submitted is not None,
            "gate_status_entry_allowed": str((gate or {}).get("status") or "") == "entry_allowed",
            "gate_instrument_matches_symbol": str(gate_payload.get("instrument") or "").upper() == sig_symbol if gate is not None else None,
            "qty_consistent_with_submit_ready": qty_ok,
        }

        detail = {
            "signal": {
                "ts": sig_ts.isoformat(),
                "symbol": sig_symbol,
                "decision_id": decision_id,
                "trace_id": trace_id,
                "trigger_id": trigger_id,
                "reason": reason,
                "qty": qty,
                "ref_px": payload.get("ref_px"),
                "ref_px_source": payload.get("ref_px_source"),
                "order_ref": payload.get("order_ref"),
            },
            "gate": {
                "ts": _iso((gate or {}).get("_ts")),
                "status": (gate or {}).get("status"),
                "reason": (gate or {}).get("reason"),
                "payload": gate_payload,
            },
            "entry_submit_ready": {
                "ts": _iso((submit_ready or {}).get("_ts")),
                "status": (submit_ready or {}).get("status"),
                "reason": (submit_ready or {}).get("reason"),
                "payload": sr_payload,
            },
            "entry_intent_submitted": {
                "ts": _iso((submitted or {}).get("_ts")),
                "status": (submitted or {}).get("status"),
            },
            "market_checks": {
                "bar_ts": _iso((bar or {}).get("ts_end")),
                "bar_close": bar_close,
                "ref_minus_bar_close": ref_minus_bar_close,
                "latest_option_quote_created_at": _iso((oq or {}).get("created_at")),
                "latest_option_quote_symbol": (
                    f"OPT:{(oq or {}).get('underlying')}:{(oq or {}).get('expiry')}:{(oq or {}).get('strike')}:{(oq or {}).get('right')}"
                    if oq
                    else None
                ),
                "latest_option_quote_mid": (oq or {}).get("mid"),
                "quote_age_sec": quote_age_sec,
            },
            "checks": checks,
            "audit_chain_stages": [
                {
                    "ts": r["_ts"].isoformat(),
                    "stage": r.get("stage"),
                    "status": r.get("status"),
                    "reason": r.get("reason"),
                }
                for r in chain
            ],
        }
        details.append(detail)

    entry_details = [d for d in details if bool(d["checks"].get("is_entry_signal"))]
    add_details = [d for d in details if not bool(d["checks"].get("is_entry_signal"))]
    summary = {
        "date": day,
        "strategy_id": strategy_id,
        "signal_count": len(details),
        "entry_signal_count": len(entry_details),
        "add_signal_count": len(add_details),
        "all_entry_signals_have_gate": all(bool(d["checks"]["has_gate_evaluated"]) for d in entry_details) if entry_details else True,
        "all_entry_signals_have_submit_ready": all(bool(d["checks"]["has_entry_submit_ready"]) for d in entry_details) if entry_details else True,
        "all_signals_submitted": all(bool(d["checks"]["has_entry_intent_submitted"]) for d in details) if details else False,
        "qty_consistency_pass_count": sum(1 for d in details if bool(d["checks"]["qty_consistent_with_submit_ready"])),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"summary": summary, "details": details}

    json_path = out_dir / "qqq_signal_logic_report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_path = out_dir / "qqq_signal_logic_report.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "signal_ts",
                "symbol",
                "reason",
                "qty",
                "decision_id",
                "gate_mode",
                "gate_v3m_signed",
                "gate_atr_eff",
                "gate_vol_surge_ratio",
                "gate_distance_from_vwap_atr",
                "gate_status",
                "submitted",
                "ref_px",
                "bar_close",
                "ref_minus_bar_close",
                "quote_age_sec",
                "qty_consistent",
            ],
        )
        w.writeheader()
        for d in details:
            gp = d["gate"]["payload"] or {}
            w.writerow(
                {
                    "signal_ts": d["signal"]["ts"],
                    "symbol": d["signal"]["symbol"],
                    "reason": d["signal"]["reason"],
                    "qty": d["signal"]["qty"],
                    "decision_id": d["signal"]["decision_id"],
                    "gate_mode": gp.get("mode"),
                    "gate_v3m_signed": gp.get("v3m_signed"),
                    "gate_atr_eff": gp.get("atr_eff"),
                    "gate_vol_surge_ratio": gp.get("vol_surge_ratio"),
                    "gate_distance_from_vwap_atr": gp.get("distance_from_vwap_atr"),
                    "gate_status": d["gate"].get("status"),
                    "submitted": d["checks"].get("has_entry_intent_submitted"),
                    "ref_px": d["signal"].get("ref_px"),
                    "bar_close": d["market_checks"].get("bar_close"),
                    "ref_minus_bar_close": d["market_checks"].get("ref_minus_bar_close"),
                    "quote_age_sec": d["market_checks"].get("quote_age_sec"),
                    "qty_consistent": d["checks"].get("qty_consistent_with_submit_ready"),
                }
            )

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Export QQQ intraday signal logic verification report")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--strategy-id", default="qqq_intraday_001")
    parser.add_argument("--audit-root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--out-dir")
    args = parser.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else ROOT / "runtime" / "reports" / f"qqq_signal_verify_{args.date}"
    report = build_report(
        audit_root=Path(args.audit_root),
        day=str(args.date),
        strategy_id=str(args.strategy_id),
        out_dir=out_dir,
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
