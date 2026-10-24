from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from services.reconciliation.strategy_recovery_policy import known_recovery_policy_ids


@dataclass(frozen=True)
class DataRequirement:
    dataset: str
    symbols: tuple[str, ...] = ()
    resolution: str = ""
    ttl_sec: float = 30.0
    rth: bool = True
    warmup_required: bool = False
    min_rows: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["symbols"] = list(self.symbols)
        return data


@dataclass(frozen=True)
class StrategyDataContract:
    strategy_id: str
    symbols: tuple[str, ...]
    datasets: tuple[str, ...] = ("ticks", "bars_5s")
    ttl_sec: float = 30.0
    enabled: bool = True
    production_blocking: bool = True
    requirements: tuple[DataRequirement, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["symbols"] = list(self.symbols)
        data["datasets"] = list(self.datasets)
        data["requirements"] = [item.to_dict() for item in self.requirements]
        return data


@dataclass(frozen=True)
class StrategyProductionManifest:
    strategy_id: str
    production_blocking: bool
    data_contract_present: bool
    entry_uses_decision_pipeline: bool
    entry_uom_only: bool
    protection_required: bool
    protection_fallback: str
    recovery_policy_id: str
    recovery_unknown_owner_action: str
    recovery_fallback: str
    eod_no_overnight: bool
    eod_platform_required: bool
    tradability_reporting: bool
    option_contract_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyProductionManifestValidation:
    ok: bool
    manifests: tuple[StrategyProductionManifest, ...]
    blockers: tuple[str, ...]
    missing_strategy_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "manifests": [manifest.to_dict() for manifest in self.manifests],
            "blockers": list(self.blockers),
            "missing_strategy_ids": list(self.missing_strategy_ids),
        }


def _symbol_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable):
        return []
    result: list[str] = []
    for item in values:
        symbol = str(item or "").strip().upper()
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def _bool_value(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _float_value(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _int_value(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _dataset_name(dataset: str, resolution: str = "") -> str:
    dataset_s = str(dataset or "").strip()
    resolution_s = str(resolution or "").strip()
    if dataset_s == "market_bars" and resolution_s:
        return f"bars_{resolution_s}"
    return dataset_s


def _build_requirements(contract_cfg: dict[str, Any], *, symbols: list[str], ttl_sec: float) -> tuple[DataRequirement, ...]:
    raw = contract_cfg.get("requirements")
    requirements: list[DataRequirement] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                dataset_s = _dataset_name(item)
                if dataset_s:
                    requirements.append(DataRequirement(dataset=dataset_s, symbols=tuple(symbols), ttl_sec=ttl_sec))
                continue
            if not isinstance(item, dict):
                continue
            resolution = str(item.get("resolution") or "").strip()
            dataset_s = _dataset_name(str(item.get("dataset") or ""), resolution)
            req_symbols = _symbol_list(item.get("symbols")) or list(symbols)
            if not dataset_s or not req_symbols:
                continue
            requirements.append(
                DataRequirement(
                    dataset=dataset_s,
                    symbols=tuple(req_symbols),
                    resolution=resolution,
                    ttl_sec=max(1.0, _float_value(item.get("ttl_sec"), default=ttl_sec)),
                    rth=_bool_value(item.get("rth"), default=True),
                    warmup_required=_bool_value(item.get("warmup_required"), default=False),
                    min_rows=max(0, _int_value(item.get("min_rows"), default=0)),
                )
            )
    if requirements:
        return tuple(requirements)
    datasets = _symbol_list(contract_cfg.get("datasets")) or ["ticks", "bars_5s"]
    return tuple(DataRequirement(dataset=dataset, symbols=tuple(symbols), ttl_sec=ttl_sec) for dataset in datasets)


def build_strategy_data_contracts(config: dict[str, Any]) -> list[StrategyDataContract]:
    """Build enabled strategy data contracts from config.yml.

    This is intentionally conservative: a strategy can narrow/override later with a
    first-class contract, but the startup recovery gate must at least cover the
    symbols that enabled strategies declare in params.extras.symbols.
    """
    contracts: list[StrategyDataContract] = []
    strategies = config.get("strategies") if isinstance(config, dict) else []
    if not isinstance(strategies, list):
        return contracts
    for spec in strategies:
        if not isinstance(spec, dict) or not bool(spec.get("enabled")):
            continue
        strategy_id = str(spec.get("id") or spec.get("strategy_id") or "").strip()
        params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
        extras = params.get("extras") if isinstance(params.get("extras"), dict) else {}
        contract_cfg = extras.get("data_contract") if isinstance(extras.get("data_contract"), dict) else {}
        symbols = (
            _symbol_list(contract_cfg.get("symbols"))
            or _symbol_list(extras.get("symbols"))
            or _symbol_list(params.get("symbols"))
            or _symbol_list(spec.get("symbols"))
        )
        for key in ("underlying", "signal_symbol", "long_exec_symbol", "short_exec_symbol", "confirm_symbol", "symbol"):
            for symbol in _symbol_list(extras.get(key)):
                if symbol not in symbols:
                    symbols.append(symbol)
        if not strategy_id or not symbols:
            continue
        ttl_sec = _float_value(contract_cfg.get("ttl_sec"), default=30.0)
        requirements = _build_requirements(contract_cfg, symbols=symbols, ttl_sec=max(1.0, ttl_sec))
        datasets: list[str] = []
        for requirement in requirements:
            if requirement.dataset and requirement.dataset not in datasets:
                datasets.append(requirement.dataset)
        production_blocking = _bool_value(
            contract_cfg.get("production_blocking"),
            default=True,
        )
        contracts.append(
            StrategyDataContract(
                strategy_id=strategy_id,
                symbols=tuple(symbols),
                datasets=tuple(datasets),
                ttl_sec=max(1.0, ttl_sec),
                enabled=True,
                production_blocking=production_blocking,
                requirements=requirements,
            )
        )
    return contracts


def required_symbols_from_contracts(contracts: Iterable[StrategyDataContract]) -> list[str]:
    result: list[str] = []
    for contract in contracts:
        for symbol in contract.symbols:
            if symbol not in result:
                result.append(symbol)
    return result


def _manifest_cfg(spec: dict[str, Any]) -> dict[str, Any]:
    params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
    extras = params.get("extras") if isinstance(params.get("extras"), dict) else {}
    cfg = extras.get("production_manifest")
    return cfg if isinstance(cfg, dict) else {}


def _section(cfg: dict[str, Any], key: str) -> dict[str, Any]:
    value = cfg.get(key)
    return value if isinstance(value, dict) else {}


def build_strategy_production_manifests(config: dict[str, Any]) -> list[StrategyProductionManifest]:
    manifests: list[StrategyProductionManifest] = []
    strategies = config.get("strategies") if isinstance(config, dict) else []
    if not isinstance(strategies, list):
        return manifests
    for spec in strategies:
        if not isinstance(spec, dict) or not bool(spec.get("enabled")):
            continue
        strategy_id = str(spec.get("id") or spec.get("strategy_id") or "").strip()
        if not strategy_id:
            continue
        params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
        extras = params.get("extras") if isinstance(params.get("extras"), dict) else {}
        data_contract_present = isinstance(extras.get("data_contract"), dict)
        manifest_cfg = _manifest_cfg(spec)
        if not manifest_cfg:
            continue
        entry = _section(manifest_cfg, "entry_contract")
        protection = _section(manifest_cfg, "protection_contract")
        recovery = _section(manifest_cfg, "recovery_contract")
        eod = _section(manifest_cfg, "eod_contract")
        tradability = _section(manifest_cfg, "tradability_reporting")
        option = _section(manifest_cfg, "option_contract")
        manifests.append(
            StrategyProductionManifest(
                strategy_id=strategy_id,
                production_blocking=_bool_value(manifest_cfg.get("production_blocking"), default=True),
                data_contract_present=data_contract_present,
                entry_uses_decision_pipeline=_bool_value(entry.get("uses_decision_pipeline"), default=False),
                entry_uom_only=_bool_value(entry.get("uom_only"), default=False),
                protection_required=_bool_value(protection.get("required"), default=False),
                protection_fallback=str(protection.get("fallback") or "").strip(),
                recovery_policy_id=str(recovery.get("policy_id") or "").strip(),
                recovery_unknown_owner_action=str(recovery.get("unknown_owner_action") or "").strip(),
                recovery_fallback=str(recovery.get("fallback") or "").strip(),
                eod_no_overnight=_bool_value(eod.get("no_overnight"), default=False),
                eod_platform_required=_bool_value(eod.get("platform_eod_required"), default=False),
                tradability_reporting=_bool_value(
                    tradability.get("reports_strategy_tradability_gate"),
                    default=False,
                ),
                option_contract_required=_bool_value(option.get("required"), default=False),
            )
        )
    return manifests


def validate_strategy_production_manifests(
    config: dict[str, Any],
    *,
    mandatory_strategy_ids: Iterable[str] = (),
) -> StrategyProductionManifestValidation:
    mandatory = {str(item).strip() for item in mandatory_strategy_ids or [] if str(item).strip()}
    manifests = tuple(build_strategy_production_manifests(config))
    by_id = {manifest.strategy_id: manifest for manifest in manifests}
    blockers: list[str] = []
    missing = sorted(strategy_id for strategy_id in mandatory if strategy_id not in by_id)
    for strategy_id in missing:
        blockers.append(f"{strategy_id}:production_manifest_missing")
    for strategy_id in sorted(mandatory & set(by_id)):
        manifest = by_id[strategy_id]
        if not manifest.production_blocking:
            blockers.append(f"{strategy_id}:production_manifest_not_blocking")
        if not manifest.data_contract_present:
            blockers.append(f"{strategy_id}:data_contract_missing")
        if not manifest.entry_uses_decision_pipeline:
            blockers.append(f"{strategy_id}:entry_contract_not_decision_pipeline")
        if not manifest.entry_uom_only:
            blockers.append(f"{strategy_id}:entry_contract_not_uom_only")
        if manifest.protection_required and manifest.protection_fallback not in {"flatten", "quarantine"}:
            blockers.append(f"{strategy_id}:protection_fallback_invalid")
        known_policy_ids = known_recovery_policy_ids()
        if not manifest.recovery_policy_id:
            blockers.append(f"{strategy_id}:recovery_policy_missing")
        elif manifest.recovery_policy_id not in known_policy_ids:
            blockers.append(f"{strategy_id}:recovery_policy_unknown")
        if manifest.recovery_unknown_owner_action != "quarantine":
            blockers.append(f"{strategy_id}:unknown_owner_action_not_quarantine")
        if manifest.recovery_fallback not in {"flatten", "quarantine"}:
            blockers.append(f"{strategy_id}:recovery_fallback_invalid")
        if not manifest.eod_no_overnight:
            blockers.append(f"{strategy_id}:eod_no_overnight_missing")
        if not manifest.eod_platform_required:
            blockers.append(f"{strategy_id}:platform_eod_not_required")
        if not manifest.tradability_reporting:
            blockers.append(f"{strategy_id}:tradability_reporting_missing")
    return StrategyProductionManifestValidation(
        ok=not blockers,
        manifests=manifests,
        blockers=tuple(blockers),
        missing_strategy_ids=tuple(missing),
    )
