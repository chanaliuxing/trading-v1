from __future__ import annotations

from typing import Any, Dict, Optional


def normalize_qty_bucket(qty: Any) -> str:
    try:
        value = abs(float(qty or 0.0))
    except Exception:
        value = 0.0
    if value <= 0.0:
        return "0"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def compute_price_band_id(
    price: Any,
    *,
    tick_size: Any = 0.01,
    configured_band_width: Any = None,
) -> str:
    try:
        px = float(price)
    except Exception:
        px = 0.0
    try:
        tick = abs(float(tick_size or 0.01))
    except Exception:
        tick = 0.01
    try:
        band = abs(float(configured_band_width)) if configured_band_width is not None else tick
    except Exception:
        band = tick
    width = max(tick, band, 0.0001)
    if px <= 0.0:
        return "band:0"
    return f"band:{int(round(px / width))}"


def build_protection_key(
    *,
    account_id: str,
    strategy_id: str,
    symbol: str,
    lot_id: str,
    protection_kind: str,
    side: str,
    qty_bucket: str,
    price_band_id: str,
) -> str:
    parts = [
        str(account_id or "").strip() or "UNKNOWN_ACCOUNT",
        str(strategy_id or "").strip(),
        str(symbol or "").strip().upper(),
        str(lot_id or "").strip(),
        str(protection_kind or "").strip().lower(),
        str(side or "").strip().upper(),
        str(qty_bucket or "").strip(),
        str(price_band_id or "").strip(),
    ]
    return "|".join(parts)


def build_protection_metadata(
    *,
    account_id: str,
    strategy_id: str,
    symbol: str,
    lot_id: str,
    protection_kind: str,
    side: str,
    qty: Any,
    price: Any,
    tick_size: Any = 0.01,
    configured_band_width: Any = None,
) -> Dict[str, str]:
    qty_bucket = normalize_qty_bucket(qty)
    price_band_id = compute_price_band_id(
        price,
        tick_size=tick_size,
        configured_band_width=configured_band_width,
    )
    return {
        "protection_kind": str(protection_kind or "").strip().lower(),
        "qty_bucket": qty_bucket,
        "price_band_id": price_band_id,
        "protection_key": build_protection_key(
            account_id=account_id,
            strategy_id=strategy_id,
            symbol=symbol,
            lot_id=lot_id,
            protection_kind=protection_kind,
            side=side,
            qty_bucket=qty_bucket,
            price_band_id=price_band_id,
        ),
    }
