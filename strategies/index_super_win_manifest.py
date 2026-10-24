from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ReplicaSymbolManifest:
    key: str
    signal_symbol: str
    confirm_symbol: Optional[str] = None
    long_exec_symbol: Optional[str] = None
    short_exec_symbol: Optional[str] = None
    volatility_symbol: Optional[str] = None
    enabled: bool = True

    def to_binding_dict(self) -> Dict[str, Any]:
        signal_symbol = str(self.signal_symbol or "").strip().upper()
        long_exec = str(self.long_exec_symbol or signal_symbol).strip().upper() or signal_symbol
        short_exec = str(self.short_exec_symbol or long_exec).strip().upper() or long_exec
        return {
            "key": self.key,
            "signal_symbol": signal_symbol,
            "exec_symbol": long_exec,
            "long_exec_symbol": long_exec,
            "short_exec_symbol": short_exec,
            "confirm_symbol": str(self.confirm_symbol or signal_symbol).strip().upper() or signal_symbol,
            "volatility_symbol": str(self.volatility_symbol or signal_symbol).strip().upper() or signal_symbol,
            "enabled": bool(self.enabled),
        }


@dataclass(frozen=True)
class ReplicaManifest:
    symbols: List[ReplicaSymbolManifest]

    @classmethod
    def from_params(cls, extras: Dict[str, Any]) -> "ReplicaManifest":
        manifests_raw = extras.get("replica_manifests")
        symbols: List[ReplicaSymbolManifest] = []
        if isinstance(manifests_raw, list) and manifests_raw:
            for idx, item in enumerate(manifests_raw):
                if not isinstance(item, dict):
                    continue
                signal_symbol = str(item.get("signal_symbol") or item.get("symbol") or "").strip().upper()
                if not signal_symbol:
                    continue
                long_exec = str(item.get("long_exec_symbol") or item.get("exec_symbol") or signal_symbol).strip().upper()
                short_exec = str(item.get("short_exec_symbol") or long_exec).strip().upper()
                symbols.append(
                    ReplicaSymbolManifest(
                        key=str(item.get("key") or signal_symbol or f"manifest_{idx}").strip(),
                        signal_symbol=signal_symbol,
                        confirm_symbol=str(item.get("confirm_symbol") or signal_symbol).strip().upper() or signal_symbol,
                        long_exec_symbol=long_exec or signal_symbol,
                        short_exec_symbol=short_exec or long_exec or signal_symbol,
                        volatility_symbol=str(item.get("volatility_symbol") or signal_symbol).strip().upper() or signal_symbol,
                        enabled=bool(item.get("enabled", True)),
                    )
                )
            return cls(symbols=symbols)

        plain_symbols = extras.get("symbols") or extras.get("signal_symbols") or []
        if isinstance(plain_symbols, list):
            for item in plain_symbols:
                symbol = str(item or "").strip().upper()
                if not symbol:
                    continue
                symbols.append(
                    ReplicaSymbolManifest(
                        key=symbol,
                        signal_symbol=symbol,
                        confirm_symbol=symbol,
                        long_exec_symbol=symbol,
                        short_exec_symbol=symbol,
                        volatility_symbol=symbol,
                        enabled=True,
                    )
                )
        return cls(symbols=symbols)

    def to_binding_dicts(self) -> List[Dict[str, Any]]:
        return [item.to_binding_dict() for item in self.symbols if item.enabled]

    def as_dict(self) -> Dict[str, Any]:
        return {"symbols": [asdict(item) for item in self.symbols]}
