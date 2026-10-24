from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Protocol

from strategies.index_super_win_manifest import ReplicaManifest


@dataclass(frozen=True)
class InstrumentBinding:
    key: str
    signal_symbol: str
    exec_symbol: str
    long_exec_symbol: Optional[str] = None
    short_exec_symbol: Optional[str] = None
    confirm_symbol: Optional[str] = None
    volatility_symbol: Optional[str] = None
    enabled: bool = True

    def exec_symbol_for_side(self, side: Optional[str]) -> str:
        side_u = str(side or "").strip().upper()
        if side_u == "SHORT" and self.short_exec_symbol:
            return str(self.short_exec_symbol).strip().upper()
        if side_u == "LONG" and self.long_exec_symbol:
            return str(self.long_exec_symbol).strip().upper()
        return str(self.exec_symbol or self.signal_symbol).strip().upper()


class InstrumentMap:
    def __init__(self, bindings: list[InstrumentBinding]) -> None:
        self._bindings = list(bindings)
        self._by_signal: dict[str, InstrumentBinding] = {}
        for binding in self._bindings:
            if not binding.enabled:
                continue
            # Keep first binding to preserve explicit-preferred ordering.
            self._by_signal.setdefault(binding.signal_symbol, binding)

    @classmethod
    def from_params(cls, extras: Dict[str, object]) -> "InstrumentMap":
        bindings_raw = extras.get("instrument_bindings")
        bindings: list[InstrumentBinding] = []
        seen_signals: set[str] = set()
        manifest = ReplicaManifest.from_params(extras)
        manifest_bindings = manifest.to_binding_dicts()
        if manifest_bindings:
            if isinstance(bindings_raw, list):
                bindings_raw = list(bindings_raw) + manifest_bindings
            else:
                bindings_raw = manifest_bindings
        if isinstance(bindings_raw, list) and bindings_raw:
            for idx, item in enumerate(bindings_raw):
                if not isinstance(item, dict):
                    continue
                signal_symbol = str(item.get("signal_symbol") or item.get("symbol") or "").strip().upper()
                exec_symbol = str(item.get("exec_symbol") or signal_symbol).strip().upper()
                long_exec_symbol = str(item.get("long_exec_symbol") or exec_symbol).strip().upper()
                short_exec_symbol = str(item.get("short_exec_symbol") or long_exec_symbol).strip().upper()
                if not signal_symbol or not exec_symbol:
                    continue
                key = str(item.get("key") or signal_symbol or f"binding_{idx}").strip()
                enabled = bool(item.get("enabled", True))
                bindings.append(
                    InstrumentBinding(
                        key=key,
                        signal_symbol=signal_symbol,
                        exec_symbol=exec_symbol,
                        long_exec_symbol=long_exec_symbol or exec_symbol,
                        short_exec_symbol=short_exec_symbol or long_exec_symbol or exec_symbol,
                        confirm_symbol=str(item.get("confirm_symbol") or signal_symbol).strip().upper() or signal_symbol,
                        volatility_symbol=str(item.get("volatility_symbol") or signal_symbol).strip().upper() or signal_symbol,
                        enabled=enabled,
                    )
                )
                seen_signals.add(signal_symbol)
        symbols = extras.get("symbols") or extras.get("signal_symbols") or []
        if isinstance(symbols, list):
            for item in symbols:
                symbol = str(item or "").strip().upper()
                if not symbol or symbol in seen_signals:
                    continue
                bindings.append(
                    InstrumentBinding(
                        key=symbol,
                        signal_symbol=symbol,
                        exec_symbol=symbol,
                        long_exec_symbol=symbol,
                        short_exec_symbol=symbol,
                        confirm_symbol=symbol,
                        volatility_symbol=symbol,
                        enabled=True,
                    )
                )
        return cls(bindings)

    def signal_symbols(self) -> list[str]:
        out: list[str] = []
        for binding in self._bindings:
            if not binding.enabled:
                continue
            symbol = binding.signal_symbol
            if symbol and symbol not in out:
                out.append(symbol)
        return out

    def exec_symbols(self) -> list[str]:
        out: list[str] = []
        for binding in self._bindings:
            if not binding.enabled:
                continue
            for symbol in (
                binding.exec_symbol,
                binding.long_exec_symbol,
                binding.short_exec_symbol,
            ):
                symbol_u = str(symbol or "").strip().upper()
                if symbol_u and symbol_u not in out:
                    out.append(symbol_u)
        return out

    def binding_for_signal(self, symbol: str) -> Optional[InstrumentBinding]:
        return self._by_signal.get(str(symbol or "").strip().upper())

    def bindings(self) -> list[InstrumentBinding]:
        return list(self._bindings)


@dataclass(frozen=True)
class SignalDecision:
    signal_symbol: str
    exec_symbol: str
    signal_active: bool
    target_qty: float
    side: Optional[str]
    reason: str
    signal_price: Optional[float] = None
    exec_price: Optional[float] = None
    metadata: Optional[dict[str, object]] = None


class SignalModel(Protocol):
    def extract(self, *, binding: InstrumentBinding, features: Dict[str, object]) -> SignalDecision:
        ...


class FeatureDrivenSignalModel:
    def __init__(self, *, default_notional_usd: float = 0.0) -> None:
        self.default_notional_usd = max(0.0, float(default_notional_usd or 0.0))

    def extract(self, *, binding: InstrumentBinding, features: Dict[str, object]) -> SignalDecision:
        signal_active = bool(features.get("is_entry_signal")) or bool(features.get("signal_active"))
        raw_target_qty = features.get("intent_target_qty", features.get("target_qty", 0.0))
        target_qty = self._safe_float(raw_target_qty) or 0.0
        side_raw = str(features.get("intent_side", features.get("side", "")) or "").strip().upper()
        side = side_raw if side_raw in {"LONG", "SHORT"} else None
        exec_symbol = binding.exec_symbol_for_side(side)
        signal_price = self._safe_float(features.get("signal_price", features.get("last_px", features.get("close"))))
        reason = str(features.get("signal_reason", features.get("reason", "feature_signal")) or "feature_signal").strip()
        exec_price = self._safe_float(
            features.get("exec_price", features.get("exec_last_px", features.get("exec_close")))
        )
        if exec_price is None and exec_symbol == binding.signal_symbol:
            exec_price = signal_price
        if target_qty <= 0.0 and signal_active and self.default_notional_usd > 0:
            if exec_price and exec_price > 0:
                target_qty = float(max(0, int(math.floor(self.default_notional_usd / exec_price))))
            elif exec_symbol != binding.signal_symbol:
                reason = f"{reason}|missing_exec_price_for_sizing"
        if not signal_active or side is None:
            target_qty = 0.0
        return SignalDecision(
            signal_symbol=binding.signal_symbol,
            exec_symbol=exec_symbol,
            signal_active=signal_active,
            target_qty=max(0.0, float(target_qty)),
            side=side,
            reason=reason,
            signal_price=signal_price,
            exec_price=exec_price,
            metadata={
                "binding_key": binding.key,
                "feature_keys": sorted(list(features.keys()))[:20],
            },
        )

    @staticmethod
    def _safe_float(value: object) -> Optional[float]:
        try:
            out = float(value)  # type: ignore[arg-type]
        except Exception:
            return None
        if not math.isfinite(out):
            return None
        return out
