"""Contract selection and pricing helpers for the option replay engine."""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from services.option_replay.core.types import ContractQuote, ReplayConfig


def entry_fill_price(quote: ContractQuote, urgent: bool, cfg: ReplayConfig) -> float:
    improve = cfg.urgent_entry_improve_pct if urgent else cfg.adaptive_entry_improve_pct
    raw = quote.ask - (quote.spread * improve)
    return min(quote.ask, max(quote.mid, raw))


def exit_fill_price(quote: ContractQuote, reason: str, cfg: ReplayConfig) -> float:
    improve = cfg.stop_exit_improve_pct if "STOP" in reason else cfg.adaptive_exit_improve_pct
    raw = quote.bid + (quote.spread * improve)
    return max(quote.bid, min(quote.mid, raw))


def _rank_key(quote: ContractQuote, *, spot: float, aggressive_same_day: bool, cfg: ReplayConfig) -> Tuple[float, float, float, float]:
    preferred = 0
    if aggressive_same_day:
        preferred = 0 if quote.dte == 0 else 1
    else:
        if quote.dte == cfg.dte_preferred:
            preferred = 0
        elif quote.dte <= cfg.dte_fallback_max:
            preferred = 1
        else:
            preferred = 2
    return (
        preferred,
        abs(quote.dte - (0 if aggressive_same_day else cfg.dte_preferred)),
        abs(abs(quote.delta) - 0.50),
        abs(quote.strike - spot),
    )


def rank_contract_candidates(
    chain: Iterable[ContractQuote],
    *,
    right: str,
    spot: float,
    aggressive_same_day: bool,
    cfg: ReplayConfig,
    delta_min_override: Optional[float] = None,
    delta_max_override: Optional[float] = None,
    no_delta_fallback: bool = False,
) -> List[ContractQuote]:
    base_rows = []
    for quote in chain:
        if quote.right.upper() != right.upper():
            continue
        if quote.mid <= 0 or quote.ask <= 0 or quote.bid < 0:
            continue
        if quote.spread_pct > cfg.max_spread_pct:
            continue
        if quote.open_interest and quote.open_interest < cfg.min_open_interest:
            continue
        base_rows.append(quote)
    if not base_rows:
        return []

    if aggressive_same_day:
        preferred_rows = [q for q in base_rows if q.dte == 0]
        if not preferred_rows:
            preferred_rows = [q for q in base_rows if q.dte <= 0]
    else:
        preferred_rows = [q for q in base_rows if q.dte == cfg.dte_preferred]
        if not preferred_rows:
            preferred_rows = [q for q in base_rows if q.dte <= cfg.dte_fallback_max]
    rows = preferred_rows if preferred_rows else base_rows

    dmin = cfg.delta_min if delta_min_override is None else float(delta_min_override)
    dmax = cfg.delta_max if delta_max_override is None else float(delta_max_override)
    candidates = [q for q in rows if dmin <= abs(q.delta) <= dmax]
    if not candidates and not no_delta_fallback:
        candidates = [q for q in rows if cfg.delta_fallback_min <= abs(q.delta) <= cfg.delta_fallback_max]
    if not candidates:
        return []
    return sorted(candidates, key=lambda q: _rank_key(q, spot=spot, aggressive_same_day=aggressive_same_day, cfg=cfg))


def describe_selection_snapshot(
    chain: Iterable[ContractQuote],
    *,
    right: str,
    spot: float,
    aggressive_same_day: bool,
    cfg: ReplayConfig,
    top_n: int = 5,
    delta_min_override: Optional[float] = None,
    delta_max_override: Optional[float] = None,
    no_delta_fallback: bool = False,
) -> Dict[str, object]:
    ranked = rank_contract_candidates(
        chain,
        right=right,
        spot=spot,
        aggressive_same_day=aggressive_same_day,
        cfg=cfg,
        delta_min_override=delta_min_override,
        delta_max_override=delta_max_override,
        no_delta_fallback=no_delta_fallback,
    )
    selected = ranked[0] if ranked and _rank_key(ranked[0], spot=spot, aggressive_same_day=aggressive_same_day, cfg=cfg)[0] < 2 else None
    top_candidates = []
    for quote in ranked[:top_n]:
        top_candidates.append(
            {
                "expiry": quote.expiry,
                "strike": float(quote.strike),
                "dte": int(quote.dte),
                "delta": float(quote.delta),
                "bid": float(quote.bid),
                "ask": float(quote.ask),
                "mid": float(quote.mid),
                "spread_pct": float(quote.spread_pct),
                "open_interest": int(quote.open_interest),
            }
        )
    return {
        "selected": selected,
        "top_candidates": top_candidates,
        "rejection_reason": None if selected is not None else explain_contract_rejection(
            chain,
            right=right,
            aggressive_same_day=aggressive_same_day,
            cfg=cfg,
            delta_min_override=delta_min_override,
            delta_max_override=delta_max_override,
            no_delta_fallback=no_delta_fallback,
        ),
    }


def select_contract(
    chain: Iterable[ContractQuote],
    *,
    right: str,
    spot: float,
    aggressive_same_day: bool,
    cfg: ReplayConfig,
    delta_min_override: Optional[float] = None,
    delta_max_override: Optional[float] = None,
    no_delta_fallback: bool = False,
) -> Optional[ContractQuote]:
    ranked = rank_contract_candidates(
        chain,
        right=right,
        spot=spot,
        aggressive_same_day=aggressive_same_day,
        cfg=cfg,
        delta_min_override=delta_min_override,
        delta_max_override=delta_max_override,
        no_delta_fallback=no_delta_fallback,
    )
    if not ranked:
        return None
    top = ranked[0]
    if _rank_key(top, spot=spot, aggressive_same_day=aggressive_same_day, cfg=cfg)[0] >= 2:
        return None
    return top


def explain_contract_rejection(
    chain: Iterable[ContractQuote],
    *,
    right: str,
    aggressive_same_day: bool,
    cfg: ReplayConfig,
    delta_min_override: Optional[float] = None,
    delta_max_override: Optional[float] = None,
    no_delta_fallback: bool = False,
) -> str:
    rows = [q for q in chain if q.right.upper() == right.upper()]
    if not rows:
        return "no_chain_snapshot"
    positive = [q for q in rows if q.mid > 0 and q.ask > 0 and q.bid >= 0]
    if not positive:
        return "invalid_quote"
    dte_rows = [
        q for q in positive
        if (q.dte == 0 if aggressive_same_day else q.dte == cfg.dte_preferred or q.dte <= cfg.dte_fallback_max)
    ]
    if not dte_rows:
        return "dte_mismatch"
    dmin = cfg.delta_min if delta_min_override is None else float(delta_min_override)
    dmax = cfg.delta_max if delta_max_override is None else float(delta_max_override)
    delta_rows = [q for q in dte_rows if dmin <= abs(q.delta) <= dmax]
    if not delta_rows and not no_delta_fallback:
        delta_rows = [q for q in dte_rows if cfg.delta_fallback_min <= abs(q.delta) <= cfg.delta_fallback_max]
    if not delta_rows:
        return "delta_out_of_range"
    spread_rows = [q for q in delta_rows if q.spread_pct <= cfg.max_spread_pct]
    if not spread_rows:
        return "spread_too_wide"
    oi_rows = [q for q in spread_rows if (not q.open_interest or q.open_interest >= cfg.min_open_interest)]
    if not oi_rows:
        return "oi_too_low"
    return "unknown_contract_filter"


def infer_iv_context(
    chain_now: Iterable[ContractQuote],
    chain_prev: Iterable[ContractQuote],
    spot: float,
) -> Tuple[Optional[float], Optional[float]]:
    now_list = list(chain_now)
    prev_list = list(chain_prev)
    if not now_list:
        return None, None
    atm_now = min(now_list, key=lambda q: abs(q.strike - spot))
    iv_now = atm_now.iv if atm_now.iv > 0 else None
    if not prev_list or iv_now is None:
        return iv_now, None
    atm_prev = min(prev_list, key=lambda q: abs(q.strike - spot))
    iv_prev = atm_prev.iv if atm_prev.iv > 0 else None
    if iv_prev is None:
        return iv_now, None
    return iv_now, iv_now - iv_prev


def compute_contracts(
    *,
    cash: float,
    grade: str,
    entry_quote: ContractQuote,
    r_per_share: float,
    cfg: ReplayConfig,
    iv_high: bool,
) -> int:
    if int(cfg.fixed_contracts_per_trade) > 0:
        return int(cfg.fixed_contracts_per_trade)
    risk_budget = cash * (cfg.risk_per_trade_pct_a if grade == "A" else cfg.risk_per_trade_pct_b)
    loss_floor = entry_quote.mid * cfg.max_loss_pct_floor * cfg.contract_multiplier
    delta_loss = abs(entry_quote.delta) * r_per_share * cfg.contract_multiplier
    model_loss = max(loss_floor, delta_loss)
    model_loss = min(model_loss, entry_quote.mid * cfg.max_loss_pct_cap * cfg.contract_multiplier)
    if model_loss <= 0:
        return 0
    qty = max(0, int(risk_budget // (model_loss + cfg.commission_per_contract * 2.0)))
    if iv_high and qty > 1:
        qty = max(1, qty // 2)
    return qty


__all__ = [
    "compute_contracts",
    "describe_selection_snapshot",
    "entry_fill_price",
    "exit_fill_price",
    "explain_contract_rejection",
    "infer_iv_context",
    "rank_contract_candidates",
    "select_contract",
]
