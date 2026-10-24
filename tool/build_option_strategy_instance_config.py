from __future__ import annotations

import argparse
import contextlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _json_arg(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    return dict(json.loads(raw))


def build_admission_report(strategy_config: dict[str, Any]) -> dict[str, Any]:
    from strategies.options_universal.builder import validate_option_strategy_config

    generated_at = datetime.now(tz=ZoneInfo("America/New_York"))
    blockers = validate_option_strategy_config(strategy_config)
    return {
        "schema_version": 1,
        "artifact_type": "option_strategy_onboarding_admission",
        "generated_at": generated_at.isoformat(),
        "go_no_go": "go" if not blockers else "no_go",
        "ok": not blockers,
        "blockers": blockers,
        "strategy_config": strategy_config,
    }


def build_plugin_config(
    *,
    plugin_cls: str,
    strategy_id: str,
    enabled: bool = False,
    capital_limit_usd: float = 1000.0,
    cadence_s: int = 1,
    ttl_ms: int = 2000,
    plugin_params: dict[str, Any] | None = None,
    extra_extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from strategies.options_universal.builder import build_option_strategy_instance_config, load_option_plugin

    plugin = load_option_plugin(plugin_cls, plugin_params=plugin_params)
    return build_option_strategy_instance_config(
        plugin=plugin,
        plugin_cls=plugin_cls,
        strategy_id=strategy_id,
        enabled=enabled,
        capital_limit_usd=capital_limit_usd,
        cadence_s=cadence_s,
        ttl_ms=ttl_ms,
        plugin_params=plugin_params,
        extra_extras=extra_extras,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and validate a universal option strategy config block.")
    parser.add_argument("--plugin-cls", required=True, help="Import path MODULE:CLASS for an OptionStrategyPlugin.")
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--enabled", action="store_true")
    parser.add_argument("--capital-limit-usd", type=float, default=1000.0)
    parser.add_argument("--cadence-s", type=int, default=1)
    parser.add_argument("--ttl-ms", type=int, default=2000)
    parser.add_argument("--plugin-params", default="")
    parser.add_argument("--extra-extras", default="")
    parser.add_argument("--validate-production", action="store_true")
    args = parser.parse_args()

    with contextlib.redirect_stdout(sys.stderr):
        payload = build_plugin_config(
            plugin_cls=args.plugin_cls,
            strategy_id=args.strategy_id,
            enabled=bool(args.enabled),
            capital_limit_usd=float(args.capital_limit_usd),
            cadence_s=int(args.cadence_s),
            ttl_ms=int(args.ttl_ms),
            plugin_params=_json_arg(args.plugin_params),
            extra_extras=_json_arg(args.extra_extras),
        )
        report = build_admission_report(payload) if args.validate_production else None
    if report is not None:
        print(yaml.safe_dump(report, sort_keys=False, allow_unicode=False))
        if not bool(report.get("ok")):
            raise SystemExit(2)
    else:
        print(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False))


if __name__ == "__main__":
    main()
