from __future__ import annotations

import importlib
import time
from dataclasses import dataclass
from typing import Any

from core.cache import GatekeeperCache
from core.context import GatekeeperContext, GatekeeperState
from core.resolver import GatekeeperResolver, GlobalInputs
from core.time_utils import now_iso
from core.types import ResolvedInput, ResolvedInputs, SignalCard, StepResult
from providers.ibkr import IBKRProvider


@dataclass(slots=True)
class RunResult:
    card: SignalCard
    steps: list[StepResult]
    resolved_inputs: ResolvedInputs


def _get_nested(config: dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = config
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set_nested(state: GatekeeperState, key: str, value: Any) -> None:
    if key == "market_mode":
        state.market_mode = value
        return
    if key == "bias":
        state.bias = value
        return
    if key == "structure":
        state.structure = value
        return
    if key == "params":
        if isinstance(value, dict):
            state.params = value
        return
    if key.startswith("flags."):
        state.flags[key.split(".", 1)[1]] = value
        return
    if key == "flags":
        if isinstance(value, dict):
            state.flags.update(value)
        return
    if key == "constraints.forbidden_structures":
        if isinstance(value, (set, list, tuple)):
            state.constraints.forbidden_structures |= set(value)
        return
    if key == "constraints.notes":
        if isinstance(value, list):
            state.constraints.notes.extend([str(v) for v in value])
        return


def _signal_status(steps: list[StepResult], structure: str | None) -> str:
    for step in steps:
        if step.stop and step.severity == "ERROR":
            return "NO_SIGNAL"
        if step.status == "FAIL" and step.severity == "ERROR":
            return "NO_SIGNAL"
    if not structure or structure == "FLAT":
        return "NO_TRADE"
    return "ALLOW"


def _build_reason_chain(steps: list[StepResult]) -> list[str]:
    reasons: list[str] = []
    stop_headline: str | None = None

    for step in steps:
        if step.status == "SKIP":
            continue
        if step.explain:
            reasons.append(str(step.explain[0]))
        if step.stop and step.explain:
            stop_headline = str(step.explain[0])

    if stop_headline:
        reasons = [r for r in reasons if r != stop_headline]
        reasons.insert(0, stop_headline)
    return reasons


def _inputs_snapshot(resolved_inputs: ResolvedInputs) -> dict[str, Any]:
    snapshot_keys = [
        "vvix",
        "vix",
        "event_confirmed",
        "event_level",
        "bofa_9_3",
        "spread_width_usd",
        "anchor_price",
        "anchor_vwap",
        "ticker_price",
        "ticker_vwap",
        "ivr",
        "skew_value",
        "atm_spread_pct",
        "css",
        "earnings_lt_5d",
    ]
    snap: dict[str, Any] = {}
    for key in snapshot_keys:
        ri = resolved_inputs.get(key)
        if ri is None:
            continue
        if key.endswith("_bars_1m"):
            continue
        snap[key] = {"value": ri.value, "source": ri.source}
    return snap


class GatekeeperEngine:
    def __init__(
        self,
        *,
        config: dict[str, Any],
        provider: IBKRProvider,
        cache: GatekeeperCache,
        manual_overrides: dict[str, ResolvedInput],
    ) -> None:
        self.config = config
        self.provider = provider
        self.cache = cache
        self.manual_overrides = manual_overrides
        self.resolver = GatekeeperResolver(
            config=config, provider=provider, cache=cache, manual_overrides=manual_overrides
        )

    def get_watchlist(self) -> list[str]:
        wl = self.config.get("watchlist", [])
        if isinstance(wl, list):
            out: list[str] = []
            seen: set[str] = set()
            for raw in wl:
                sym = str(raw).upper().strip()
                if not sym or sym in seen:
                    continue
                seen.add(sym)
                out.append(sym)
            return out
        return []

    def default_profile_for_symbol(self, symbol: str) -> str:
        profiles = self.config.get("profiles", {})
        default_profile = str(profiles.get("default_profile", "gatekeeper_v1"))
        mapping = profiles.get("symbol_profile_map", {})
        if isinstance(mapping, dict):
            mapped = mapping.get(symbol.upper())
            if mapped:
                return str(mapped)
        return default_profile

    def get_pipeline(self, profile: str) -> list[str]:
        profiles = self.config.get("profiles", {})
        p = profiles.get(profile, {})
        pipeline = p.get("pipeline") if isinstance(p, dict) else None
        if isinstance(pipeline, list):
            return [str(x) for x in pipeline]
        raise KeyError(f"Profile pipeline not found: {profile}")

    def refresh_data(self, *, anchor_symbol: str, tickers: list[str]) -> dict[str, Any]:
        status: dict[str, Any] = {
            "ts": now_iso(),
            "anchor_symbol": anchor_symbol,
            "tickers": tickers,
            "errors": [],
            "details": {},
            "trace": [],
        }
        start_ts = time.perf_counter()
        status["trace"].append({"ts": now_iso(), "event": "refresh_start"})
        if not self.provider.is_connected():
            ok, msg = self.resolver.connect_ibkr()
            if not ok:
                status["errors"].append(msg)
                status["trace"].append({"ts": now_iso(), "event": "connect_failed", "detail": msg})
                return status
            status["trace"].append({"ts": now_iso(), "event": "connect_ok"})

        try:
            status["trace"].append({"ts": now_iso(), "event": "anchor_fetch_start"})
            anchor_resolved = self.resolver.resolve_anchor(anchor_symbol, fetch_auto=True)
            status["details"]["anchor"] = {
                "anchor_price": anchor_resolved.get("anchor_price").source if anchor_resolved.get("anchor_price") else None,
                "anchor_bars_1m": anchor_resolved.get("anchor_bars_1m").source
                if anchor_resolved.get("anchor_bars_1m")
                else None,
                "anchor_vwap": anchor_resolved.get("anchor_vwap").source if anchor_resolved.get("anchor_vwap") else None,
                "vix": anchor_resolved.get("vix").source if anchor_resolved.get("vix") else None,
                "vvix": anchor_resolved.get("vvix").source if anchor_resolved.get("vvix") else None,
            }
            status["trace"].append({"ts": now_iso(), "event": "anchor_fetch_end"})
        except Exception as exc:  # noqa: BLE001
            status["errors"].append(f"Anchor refresh failed: {exc}")
            status["trace"].append({"ts": now_iso(), "event": "anchor_fetch_error", "detail": str(exc)})

        status["details"]["tickers"] = {}
        for symbol in tickers:
            try:
                status["trace"].append({"ts": now_iso(), "event": "ticker_fetch_start", "symbol": symbol})
                ticker_resolved = self.resolver.resolve_ticker(symbol, fetch_auto=True, include_options=False)
                status["details"]["tickers"][symbol] = {
                    "ticker_price": ticker_resolved.get("ticker_price").source
                    if ticker_resolved.get("ticker_price")
                    else None,
                    "ticker_bars_1m": ticker_resolved.get("ticker_bars_1m").source
                    if ticker_resolved.get("ticker_bars_1m")
                    else None,
                    "ticker_vwap": ticker_resolved.get("ticker_vwap").source
                    if ticker_resolved.get("ticker_vwap")
                    else None,
                }
                status["trace"].append({"ts": now_iso(), "event": "ticker_fetch_end", "symbol": symbol})
            except Exception as exc:  # noqa: BLE001
                status["errors"].append(f"{symbol} refresh failed: {exc}")
                status["trace"].append(
                    {"ts": now_iso(), "event": "ticker_fetch_error", "symbol": symbol, "detail": str(exc)}
                )

        status["elapsed_s"] = round(time.perf_counter() - start_ts, 3)
        status["trace"].append({"ts": now_iso(), "event": "refresh_end", "elapsed_s": status["elapsed_s"]})
        return status

    def run_symbol(
        self,
        *,
        symbol: str,
        profile: str,
        global_inputs: GlobalInputs,
        fetch_auto: bool,
    ) -> RunResult:
        anchor_symbol = global_inputs.anchor_symbol

        if fetch_auto and not self.provider.is_connected():
            self.resolver.connect_ibkr()

        global_resolved = self.resolver.resolve_global(global_inputs)
        anchor_resolved = self.resolver.resolve_anchor(anchor_symbol, fetch_auto=fetch_auto)
        ticker_resolved = self.resolver.resolve_ticker(symbol, fetch_auto=fetch_auto, include_options=True)

        resolved_inputs: ResolvedInputs = {}
        resolved_inputs.update(global_resolved)
        resolved_inputs.update(anchor_resolved)
        resolved_inputs.update(ticker_resolved)
        resolved_inputs["profile"] = ResolvedInput(value=profile, source="MANUAL", ts=now_iso(), is_manual_lock=False)

        state = GatekeeperState()
        context = GatekeeperContext(
            resolved_inputs=resolved_inputs,
            config=self.config,
            cache=self.cache,
            state=state,
            symbol=symbol,
            anchor_symbol=anchor_symbol,
            profile=profile,
        )

        steps: list[StepResult] = []
        stop = False
        stop_by: str | None = None
        pipeline = self.get_pipeline(profile)

        for module_path in pipeline:
            if stop:
                steps.append(
                    StepResult(
                        step_id=module_path.split(".")[-1],
                        step_title=module_path,
                        status="SKIP",
                        stop=False,
                        severity="INFO",
                        explain=[f"Skipped due to STOP: {stop_by}"],
                        debug={"rule_path": module_path},
                    )
                )
                continue

            try:
                mod = importlib.import_module(module_path)
            except Exception as exc:  # noqa: BLE001
                step = StepResult(
                    step_id=module_path.split(".")[-1],
                    step_title=module_path,
                    status="FAIL",
                    stop=True,
                    severity="ERROR",
                    explain=[f"Rule import failed: {module_path} ({exc})"],
                    debug={"rule_path": module_path},
                )
                steps.append(step)
                stop = True
                stop_by = step.step_id
                continue
            meta = getattr(mod, "RULE_META", {}) or {}
            step_id = str(meta.get("step_id") or module_path.split(".")[-1])
            step_title = str(meta.get("step_title") or step_id)
            requires = meta.get("requires", [])

            run_fn = getattr(mod, "run", None)
            if not callable(run_fn):
                step = StepResult(
                    step_id=step_id,
                    step_title=step_title,
                    status="FAIL",
                    stop=True,
                    severity="ERROR",
                    explain=[f"Rule missing run(context): {module_path}"],
                    debug={"rule_path": module_path},
                )
                steps.append(step)
                stop = True
                stop_by = step_id
                continue

            try:
                step: StepResult = run_fn(context)
            except Exception as exc:  # noqa: BLE001
                step = StepResult(
                    step_id=step_id,
                    step_title=step_title,
                    status="FAIL",
                    stop=True,
                    severity="ERROR",
                    explain=[f"Rule crashed: {step_id} ({exc})"],
                    debug={"rule_path": module_path},
                )
                steps.append(step)
                stop = True
                stop_by = step_id
                continue
            if not step.step_id:
                step.step_id = step_id
            if not step.step_title:
                step.step_title = step_title
            if not step.debug:
                step.debug = {}
            step.debug.setdefault("rule_path", module_path)
            step.debug.setdefault("config_keys", meta.get("config_keys", []))
            step.debug.setdefault("rule_meta", meta)
            if not step.inputs_used and isinstance(requires, list):
                step.inputs_used = [str(k) for k in requires]

            for k, v in (step.outputs_set or {}).items():
                _set_nested(state, str(k), v)

            steps.append(step)

            if step.stop:
                stop = True
                stop_by = step.step_id

        structure = state.structure
        status = _signal_status(steps, structure)

        card = SignalCard(
            symbol=symbol,
            profile=profile,
            status=status,  # type: ignore[arg-type]
            market_mode=state.market_mode,
            bias=state.bias,
            structure=structure,
            params=state.params,
            flags=dict(state.flags),
            reason_chain=_build_reason_chain(steps),
            inputs_snapshot=_inputs_snapshot(resolved_inputs),
            steps=[s.step_id for s in steps],
        )

        if state.constraints.notes:
            card.flags["constraints"] = True
            card.reason_chain.append(f"Blocked by Gate: {'; '.join(state.constraints.notes)}")

        card.status = _signal_status(steps, card.structure)  # type: ignore[assignment]
        return RunResult(card=card, steps=steps, resolved_inputs=resolved_inputs)
