from __future__ import annotations

from typing import Any, Dict, List, Mapping


class RealBrokerDryRunAdapter:
    def __init__(
        self,
        *,
        session_id: str,
        broker_profile: str = "real",
        credential_source: str = "missing",
        credential_valid: bool = False,
        dry_run_only: bool = True,
    ) -> None:
        self.session_id = session_id
        self.broker_profile = broker_profile
        self.credential_source = credential_source
        self.credential_valid = credential_valid
        self.dry_run_only = dry_run_only
        self._connected = False
        self._account = {
            "cash": 250_000.0,
            "buying_power": 250_000.0,
            "equity": 250_000.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "day_pnl": 0.0,
        }
        self._positions: List[Dict[str, Any]] = []
        self._orders: List[Dict[str, Any]] = []
        self._fills: List[Dict[str, Any]] = []

    def connect(self) -> Dict[str, Any]:
        if not self.credential_valid:
            self._connected = False
            return {
                "ok": False,
                "status": "controlled_fail",
                "reason": "credential_invalid",
                "broker_profile": self.broker_profile,
                "dry_run_only": self.dry_run_only,
            }
        self._connected = True
        return {
            "ok": True,
            "status": "connected",
            "broker_profile": self.broker_profile,
            "dry_run_only": self.dry_run_only,
        }

    def submit_order(self, **kwargs: Any) -> Dict[str, Any]:
        return {
            "ok": False,
            "status": "rejected",
            "reason": "real_dry_run_no_submit",
            "request": dict(kwargs),
        }

    def cancel_order(self, *, broker_order_id: str) -> Dict[str, Any]:
        return {"ok": False, "reason": "real_dry_run_no_cancel", "broker_order_id": broker_order_id}

    def get_order(self, *, broker_order_id: str) -> Dict[str, Any]:
        return {"broker_order_id": broker_order_id, "status": "unknown"}

    def list_open_orders(self) -> List[Dict[str, Any]]:
        return list(self._orders)

    def get_positions(self) -> List[Dict[str, Any]]:
        return list(self._positions)

    def get_account(self) -> Dict[str, Any]:
        return dict(self._account)

    def flatten_all(self) -> Dict[str, Any]:
        return {"ok": False, "reason": "real_dry_run_no_flatten", "flattened_orders": []}

    def preflight_order_validation(self, order: Mapping[str, Any]) -> Dict[str, Any]:
        symbol = str(order.get("symbol") or "").strip().upper()
        qty = int(order.get("qty") or 0)
        side = str(order.get("side") or "").strip().upper()
        reasons: list[str] = []
        valid = True
        if not self._connected:
            valid = False
            reasons.append("not_connected")
        if not symbol:
            valid = False
            reasons.append("missing_symbol")
        if qty <= 0:
            valid = False
            reasons.append("invalid_qty")
        if side not in {"BUY", "SELL"}:
            valid = False
            reasons.append("invalid_side")
        return {"valid": valid, "reasons": reasons or ["passed"], "order": dict(order)}

    def export_state(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "broker_profile": self.broker_profile,
            "connected": self._connected,
            "dry_run_only": self.dry_run_only,
            "credential_source": self.credential_source,
            "credential_valid": self.credential_valid,
            "orders": list(self._orders),
            "fills": list(self._fills),
            "positions": list(self._positions),
            "account": dict(self._account),
            "client_to_broker": {},
        }

