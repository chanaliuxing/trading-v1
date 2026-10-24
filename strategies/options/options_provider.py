
from typing import Any, Dict, Optional
import os
import asyncio
from services.market.options_data import IBKROptionsDataProvider

class OptionsProvider:
    """Base class for options trading providers."""

    def __init__(self, exec_mode: str, config_params: Dict[str, Any]):
        self.exec_mode = exec_mode
        self.config_params = config_params

    def place_vertical_spread(
        self,
        symbol: str,
        short_strike: float,
        long_strike: float,
        side: str, # "BUY" or "SELL"
        qty: int,
        expiry: str,
        credit_mid: float,
        tp_credit: float,
        sl_credit: float,
        transmit: bool = False
    ) -> Dict[str, Any]:
        raise NotImplementedError

class IbkrOptionsProvider(OptionsProvider):
    """Interactive Brokers options provider."""
    def __init__(self, exec_mode: str, config_params: Dict[str, Any]):
        super().__init__(exec_mode, config_params)
        host = config_params.get("host", "127.0.0.1")
        port = int(config_params.get("port") or (4001 if exec_mode == "live" else 4002))
        self.account = config_params.get("account")
        self.provider = IBKROptionsDataProvider(host=host, port=port)

    def place_vertical_spread(
        self,
        symbol: str,
        short_strike: float,
        long_strike: float,
        side: str,
        qty: int,
        expiry: str,
        credit_mid: float,
        tp_credit: float,
        sl_credit: float,
        transmit: bool = False
    ) -> Dict[str, Any]:
        # Only supports SELL credit spreads for now
        if side.upper() not in {"SELL", "SELL_PUT_SPREAD"}:
            return {"status": "unsupported_side", "transmit": False}
        try:
            loop = None
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Use new loop for blocking call
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            res = loop.run_until_complete(
                self.provider.place_vertical_spread(
                    underlying=symbol,
                    expiry=expiry,
                    short_strike=short_strike,
                    long_strike=long_strike,
                    qty=qty,
                    right="P",
                    limit_credit=credit_mid,
                    account=self.account,
                    transmit=transmit,
                )
            )
            # Annotate tp/sl for audit; not placed automatically
            res["tp_credit"] = tp_credit
            res["sl_credit"] = sl_credit
            return res
        except Exception as e:
            return {"status": "error", "error": str(e), "transmit": False}

class StubOptionsProvider(OptionsProvider):
    """Stub options provider for testing without external dependencies."""
    def place_vertical_spread(
        self,
        symbol: str,
        short_strike: float,
        long_strike: float,
        side: str,
        qty: int,
        expiry: str,
        credit_mid: float,
        tp_credit: float,
        sl_credit: float,
        transmit: bool = False
    ) -> Dict[str, Any]:
        print(f"STUB: Placing {side} vertical spread for {symbol} {expiry} {short_strike}/{long_strike} @ {credit_mid} (transmit={transmit})")
        return {"status": "stub", "order_id": f"stub_{os.urandom(4).hex()}", "transmit": transmit}


def create_options_provider(
    backend: str,
    exec_mode: str,
    config_params: Dict[str, Any]
) -> OptionsProvider:
    """Factory function to create an options provider instance."""
    if backend == "ibkr":
        return IbkrOptionsProvider(exec_mode, config_params)
    elif backend == "stub":
        return StubOptionsProvider(exec_mode, config_params)
    else:
        raise ValueError(f"Unknown options backend: {backend}")
