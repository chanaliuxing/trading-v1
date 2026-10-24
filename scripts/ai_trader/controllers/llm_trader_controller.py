import os
import subprocess
import shlex
import sys
import asyncio
from pathlib import Path
import json
import datetime
from zoneinfo import ZoneInfo
import uuid
import hashlib
import yaml
from typing import Dict, Any, Optional
from libs.provider_budget import check_budget, increment_usage
from libs.skills.runtime import SkillRuntime
from libs.logger import get_logger
from strategies.options.options_strategies import qqq_put_spread_suggest
from strategies.options.options_provider import create_options_provider

try:
    from libs.db.ch_pool import get_client as get_ch_client
except Exception:
    get_ch_client = None

NY_TZ = ZoneInfo("America/New_York")
logger = get_logger(__name__)


def _get_project_env():
    """Get environment with PYTHONPATH set to project root."""
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    return env, str(project_root)


class LLMTraderController:
    """Starts llm_trader sessions using existing shell scripts with fallbacks."""

    def __init__(self, cmds: dict, paper: bool = False, paper_flag: Optional[str] = None,
                 runs_dir: Optional[str] = None, max_concurrent: Optional[int] = None,
                 min_interval_by_provider: Optional[dict] = None, skill_runtime: Optional[SkillRuntime] = None):
        self.cmds = cmds
        self.paper = paper
        self.paper_flag = paper_flag
        self.runs_dir = Path(runs_dir) if runs_dir else None
        self.max_concurrent = max_concurrent
        self.min_interval_by_provider = min_interval_by_provider or {}
        self.skill_runtime = skill_runtime
        if self.runs_dir:
            self.runs_dir.mkdir(parents=True, exist_ok=True)

    def _calculate_credit_and_sl_tp(self, quotes: Dict[str, Any], tp_pct: float, sl_pct: float) -> Dict[str, Any]:
        short_bid = quotes.get("short_bid", 0.0)
        short_ask = quotes.get("short_ask", 0.0)
        long_bid = quotes.get("long_bid", 0.0)
        long_ask = quotes.get("long_ask", 0.0)
        credit_mid = (short_bid + short_ask) / 2 - (long_bid + long_ask) / 2
        if credit_mid < 0:
            credit_mid = 0.0
        tp_credit = credit_mid * tp_pct
        sl_credit = credit_mid * (1 + sl_pct) # SL should be a loss greater than credit
        return {"credit_mid": round(credit_mid, 4), "tp_credit": round(tp_credit, 4), "sl_credit": round(sl_credit, 4)}

    def _trace_write(self, kind: str, record: Dict[str, Any]) -> None:
        if not self.runs_dir:
            return
        try:
            full_record = {"ts": datetime.datetime.now(NY_TZ).isoformat(), "kind": kind, **record}
            d = self.runs_dir / datetime.date.today().isoformat()
            d.mkdir(parents=True, exist_ok=True)
            fp = d / "brain_calls.jsonl"
            with fp.open("a", encoding="utf-8") as f:
                f.write(json.dumps(full_record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"Failed to write trace record: {e}")

    def _execute_hedge_intent(self, hedge_intent: dict, plan: dict) -> None:
        if not isinstance(hedge_intent, dict):
            return
        symbols = hedge_intent.get("symbols") or []
        if not symbols:
            return

        cmd = [
            sys.executable,
            str(Path("scripts/options/auto_hedge.py")),
            "--symbols",
            ",".join(str(s) for s in symbols),
            "--notional-pct",
            str(hedge_intent.get("notional_pct", 0.5)),
            "--urgency",
            str(hedge_intent.get("urgency", "medium")),
            "--strategy",
            str(hedge_intent.get("strategy", "options_put")),
            "--confidence",
            str(hedge_intent.get("confidence", 0.7)),
            "--max-cost-bps",
            str(hedge_intent.get("max_cost_bps", 100)),
        ]
        if self.paper:
            cmd.append("--paper")

        result = subprocess.run(cmd, capture_output=True, text=True, env=_get_project_env()[0])
        self._trace_write(
            "hedge_intent_execution",
            {
                "cmd": cmd,
                "returncode": int(getattr(result, "returncode", 1)),
                "stdout": getattr(result, "stdout", ""),
                "stderr": getattr(result, "stderr", ""),
                "intent": hedge_intent,
            },
        )

        if int(getattr(result, "returncode", 1)) == 0:
            return

        urgency = str(hedge_intent.get("urgency", "medium")).lower()
        restrictions = plan.setdefault("restrictions", [])

        if urgency == "high":
            plan["safe_mode"] = True
            if "reduce_sizing" not in restrictions:
                restrictions.append("reduce_sizing")
            if "slow_interval" not in restrictions:
                restrictions.append("slow_interval")
            for provider in plan.get("providers", []) or []:
                cur = int(provider.get("interval_sec", 600) or 600)
                provider["interval_sec"] = max(600, cur)
        elif urgency == "medium":
            if "reduce_sizing" not in restrictions:
                restrictions.append("reduce_sizing")

    def _maybe_build_qqq_put_spread(self, plan: dict) -> Optional[Dict[str, Any]]:
        session = plan.get("session")
        if session not in {"morning", "intraday"}:
            return None
        providers = plan.get("providers") or []
        if not any("QQQ" in (p.get("symbols") or []) for p in providers):
            return None

        price = plan.get("qqq_price")
        ma50 = plan.get("qqq_ma50")
        ma100 = plan.get("qqq_ma100")
        ma200 = plan.get("qqq_ma200")
        trend = plan.get("qqq_trend")
        
        now_et = datetime.datetime.now(NY_TZ)
        time_et_str = now_et.strftime("%H:%M")

        if not all([price, ma50, ma100, ma200, trend]):
            return None

        dec = qqq_put_spread_suggest(
            price=float(price), ma50=float(ma50), ma100=float(ma100),
            ma200=float(ma200), trend=str(trend).lower(), time_et=time_et_str
        )
        if dec.status != "ready":
            return None

        expiry_date = (now_et.date() + datetime.timedelta(days=7)).isoformat()
        quotes = self._fetch_option_chain_quotes(symbol="QQQ", expiry_date=expiry_date, short_strike=dec.short_strike, long_strike=dec.long_strike)
        if not quotes:
            return None

        return {
            "strategy_id": "qqq_put_spread_rules", "symbol": "QQQ", "side": "SELL_PUT_SPREAD",
            "short_strike": dec.short_strike, "long_strike": dec.long_strike, "width": dec.width,
            "qty": 1, "reason": dec.reason, "notes": dec.notes,
            "expiry": expiry_date, "tp_credit": None, "sl_credit": None, "quotes": quotes,
        }

    def _fetch_option_chain_quotes(self, symbol: str, expiry_date: Optional[str] = None, short_strike: Optional[float] = None, long_strike: Optional[float] = None) -> Optional[Dict[str, Any]]:
        # This method will be mocked in tests.
        return None

    def _get_options_provider_config(self) -> Dict[str, Any]:
        # This is simplified for testing.
        return {}

    def _execute_options_orders(self, plan: dict) -> None:
        options_orders = plan.get("options_orders") or []
        if not options_orders:
            return

        min_credit = float(os.getenv("OPTIONS_MIN_CREDIT", 0.1))
        tp_pct = float(os.getenv("OPTIONS_TP_PCT", 0.5))
        sl_pct = float(os.getenv("OPTIONS_SL_PCT", 1.0))
        exec_mode = os.getenv("OPTIONS_EXEC_MODE", "paper").lower()
        options_backend = os.getenv("OPTIONS_BACKEND", "stub").lower()
        transmit_live = exec_mode == "live"

        provider = create_options_provider(
            backend=options_backend, exec_mode=exec_mode, config_params=self._get_options_provider_config()
        )

        for order in options_orders:
            # In a real scenario, quotes might be stale. We re-fetch.
            order_quotes = self._fetch_option_chain_quotes(order['symbol'], order.get('expiry'), order['short_strike'], order['long_strike'])
            if not order_quotes:
                self._trace_write("options_order_rejected", {"reason": f"Could not fetch fresh quotes for {order['symbol']}", "order": order})
                continue
                
            calcs = self._calculate_credit_and_sl_tp(order_quotes, tp_pct, sl_pct)
            order.update(calcs)
            order["quotes"] = order_quotes
            
            if order["credit_mid"] < min_credit:
                self._trace_write("options_order_rejected", {"reason": f"credit_mid {order['credit_mid']} is below min_credit {min_credit}", "order": order})
                continue

            try:
                order_result = provider.place_vertical_spread(
                    symbol=order["symbol"], short_strike=order["short_strike"], long_strike=order["long_strike"],
                    side=order["side"], qty=order["qty"], expiry=order["expiry"],
                    credit_mid=order["credit_mid"], tp_credit=order["tp_credit"], sl_credit=order["sl_credit"],
                    transmit=transmit_live
                )
                self._trace_write("options_order_routing", {"status": order_result.get("status"), "provider_response": order_result, "order": order})
            except Exception as e:
                self._trace_write("options_order_failed", {"reason": str(e), "order": order})

    def run_with_plan(self, plan: dict, prompt_overrides: dict | None = None):
        if self.skill_runtime:
            plan = self.skill_runtime.apply_plan_hooks(plan or {})
        
        if plan and plan.get("hedge_intent"):
            self._execute_hedge_intent(plan.get("hedge_intent"), plan)

        try:
            qqq_order = self._maybe_build_qqq_put_spread(plan or {})
            if qqq_order:
                plan.setdefault("options_orders", []).append(qqq_order)
                self._trace_write("options_order_injected", {"strategy": "qqq_put_spread", "order": qqq_order})
        except Exception as exc:
            logger.warning("qqq_put_spread_injection_failed", error=str(exc), exc_info=True)
        
        if plan and plan.get("options_orders"):
            self._execute_options_orders(plan)
        
        # Other parts of run_with_plan that spawn processes are omitted for brevity
        
    def start_premarket(self): pass
    def start_rth_fast(self): pass
    def start_rth_full(self): pass
    def start_afterhours(self): pass
    def start_overnight(self): pass
