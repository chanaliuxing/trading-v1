#!/usr/bin/env python3
"""
Compute per-decision reasoning consistency for llm_trader using real market data.

Data sources:
- ClickHouse llm_trader.decisions (ts, symbol, provider, reasoning, ...)
- ClickHouse trading.bars_1m (ts_end, symbol, close, volume)

Checks implemented:
- above_vwap ≈ close >= rolling MA20(close)
- rvol_gt_2: volume / rolling MA20(volume) > 2.0
- break_premarket_high: close >= max close before 09:30

Outputs:
- runs/ai_trader/YYYY-MM-DD/reflection.json (per-decision details + aggregates)
"""

import argparse
import json
import subprocess
import shlex
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, List

import pandas as pd

from scripts.ai_trader.evaluators.reflection import extract_claims, check_claims


def ch_query(sql: str) -> str:
    cmd = f"docker exec trading_clickhouse clickhouse-client --query \"{sql}\" --format CSVWithNames"
    return subprocess.check_output(cmd, shell=True, text=True)


def load_decisions(target_date: str) -> pd.DataFrame:
    sql = (
        "SELECT d.ts_ny, d.symbol, d.session_id, d.decision_json, "
        "s.bot_id, s.model "
        "FROM llm.llm_decisions d "
        "LEFT JOIN llm.llm_sessions s ON d.session_id = s.session_id "
        "WHERE d.trading_day = toDate('{d}') ORDER BY d.ts_ny".format(d=target_date)
    )
    csv = ch_query(sql)
    from io import StringIO
    return pd.read_csv(StringIO(csv))


def load_bars_1m(symbols: List[str], target_date: str) -> pd.DataFrame:
    syms = ",".join("'" + s + "'" for s in symbols)
    sql = (
        "SELECT ts_end, symbol, close, volume FROM trading.bars_1m "
        f"WHERE toDate(ts_end)=toDate('{target_date}') AND symbol IN ({syms}) ORDER BY symbol, ts_end"
    )
    csv = ch_query(sql)
    from io import StringIO
    return pd.read_csv(StringIO(csv))


def compute_features(df_bars: pd.DataFrame) -> pd.DataFrame:
    df = df_bars.copy()
    df['ts_end'] = pd.to_datetime(df['ts_end'])
    # Ensure numeric dtypes to avoid object-dtype groupby cumsum errors
    for col in ('close', 'volume'):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    df['close'] = df['close'].astype('float64')
    df['volume'] = df['volume'].fillna(0).astype('float64')
    df.sort_values(['symbol', 'ts_end'], inplace=True)
    # Rolling baseline for rvol20 (past 20 bars)
    df['ma20_vol'] = df.groupby('symbol')['volume'].transform(lambda s: s.rolling(20, min_periods=5).mean())
    df['rvol20'] = df['volume'] / df['ma20_vol'].replace(0, pd.NA)
    # Intraday anchored VWAP approximation from 1m bars: cum(price*vol)/cum(vol)
    df['vp'] = df['close'] * df['volume']
    df['cum_vp'] = df.groupby('symbol')['vp'].transform('cumsum')
    df['cum_vol'] = df.groupby('symbol')['volume'].transform('cumsum').replace(0, pd.NA)
    df['vwap'] = df['cum_vp'] / df['cum_vol']
    # Premarket high: max close before 09:30 NY (use hour<9 or hour==9 & minute<30)
    hh = df['ts_end'].dt.hour
    mm = df['ts_end'].dt.minute
    pre_mask = (hh < 9) | ((hh == 9) & (mm < 30))
    # For each symbol, take cummax of close only within premarket; ffill to carry into RTH
    df['premkt_high'] = df['close'].where(pre_mask)
    df['premkt_high'] = df.groupby('symbol')['premkt_high'].transform(lambda s: s.cummax())
    df['premkt_high'] = df.groupby('symbol')['premkt_high'].transform('ffill')
    return df


def nearest_bar(df_feat: pd.DataFrame, sym: str, ts: pd.Timestamp) -> Dict[str, Any]:
    d = df_feat[df_feat['symbol'] == sym]
    if d.empty:
        # Return default values if no data
        return {
            'close': 0.0,
            'vwap': 0.0,
            'rvol20': 0.0,
            'premkt_high': 0.0,
            'last': 0.0,
        }
    # Prefer last bar at or before ts to avoid lookahead; fallback to first after
    before = d[d['ts_end'] <= ts].tail(1)
    after = d[d['ts_end'] > ts].head(1)
    if len(before):
        row = before.iloc[0]
    elif len(after):
        row = after.iloc[0]
    else:
        row = d.iloc[-1]
    return {
        'close': float(row['close']),
        'vwap': float(row['vwap']) if pd.notna(row['vwap']) else float(row['close']),
        'rvol20': float(row['rvol20']) if pd.notna(row['rvol20']) else 0.0,
        'premkt_high': float(row['premkt_high']) if pd.notna(row['premkt_high']) else float(row['close']),
        'last': float(row['close']),
    }


def run(target_date: str, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    decisions = load_decisions(target_date)
    if decisions.empty:
        (out_dir / 'reflection.json').write_text(json.dumps({'message': 'no decisions'}, indent=2))
        return

    symbols = sorted(decisions['symbol'].dropna().unique().tolist())
    bars = load_bars_1m(symbols, target_date)
    feats = compute_features(bars)

    details = []
    for _, r in decisions.iterrows():
        ts = pd.to_datetime(r['ts_ny'])
        sym = r['symbol']

        # Parse decision_json
        try:
            dec = json.loads(r['decision_json'])
        except Exception:
            dec = {}

        claims = extract_claims(str(dec.get('rationale') or ''))
        feat = nearest_bar(feats, sym, ts)
        res = check_claims(claims, feat)
        # Provider: use bot_id from llm_sessions JOIN; fallback to model, then session_id
        provider = str(r.get('bot_id') or r.get('model') or r.get('session_id') or 'unknown')
        details.append({
            'ts': str(ts),
            'symbol': sym,
            'provider': provider,
            'decision': dec.get('action', 'UNKNOWN'),
            'confidence': float(dec.get('confidence') or 0),
            'consistency': res['market_alignment'],  # backward alias
            'market_alignment': res['market_alignment'],
            'claim_consistency': res['claim_consistency'],
            'claims': res['claims'],
        })

    df = pd.DataFrame(details)
    agg = {
        # Backward: overall_consistency == market_alignment
        'overall_consistency': float(df['consistency'].mean()) if not df.empty else 0.0,
        'overall_market_alignment': float(df['market_alignment'].mean()) if (not df.empty and 'market_alignment' in df) else float(df['consistency'].mean()) if not df.empty else 0.0,
        'overall_claim_consistency': float(df['claim_consistency'].mean()) if (not df.empty and 'claim_consistency' in df) else 0.0,
        'by_provider': df.groupby('provider')['consistency'].mean().to_dict() if not df.empty else {},
        'by_provider_market_alignment': df.groupby('provider')['market_alignment'].mean().to_dict() if (not df.empty and 'market_alignment' in df) else {},
        'by_provider_claim_consistency': df.groupby('provider')['claim_consistency'].mean().to_dict() if (not df.empty and 'claim_consistency' in df) else {},
        'by_symbol': df.groupby('symbol')['consistency'].mean().to_dict() if not df.empty else {},
        'by_symbol_market_alignment': df.groupby('symbol')['market_alignment'].mean().to_dict() if (not df.empty and 'market_alignment' in df) else {},
        'by_symbol_claim_consistency': df.groupby('symbol')['claim_consistency'].mean().to_dict() if (not df.empty and 'claim_consistency' in df) else {},
        'n': int(len(df)),
    }

    out = {'date': target_date, 'details': details, 'aggregate': agg}
    (out_dir / 'reflection.json').write_text(json.dumps(out, indent=2), encoding='utf-8')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=date.today().isoformat())
    ap.add_argument('--runs-dir', default='./runs/ai_trader')
    args = ap.parse_args()
    out_dir = Path(args.runs_dir) / args.date
    run(args.date, out_dir)


if __name__ == '__main__':
    main()
