from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[2]


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_json_if_exists(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return _load_json(path)
    except Exception:
        return {}


def _latest_file(root: Path, pattern: str) -> Optional[Path]:
    matches = list(root.glob(pattern))
    if not matches:
        return None
    return sorted(matches, key=lambda p: p.stat().st_mtime, reverse=True)[0]


@dataclass(frozen=True)
class StrategyEvidenceStatus:
    strategy_id: str
    readiness: str
    reasons: List[str]
    evidence: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "readiness": self.readiness,
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
        }


def _collect_common_contract_map(rollout_report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    rows = list(rollout_report.get("strategies") or [])
    active_rows = [row for row in rows if str(row.get("status") or "") == "active"]
    out: Dict[str, Dict[str, Any]] = {}
    for row in active_rows:
        out[str(row.get("strategy_id") or "")] = {
            "rollout_contract_pass": bool(row.get("rollout_contract_pass")),
            "issues": list(row.get("issues") or []),
            "definition_id": str(row.get("definition_id") or ""),
            "state_shadow_required": bool(row.get("state_shadow_required")),
            "cutover_policy": str(row.get("cutover_policy") or ""),
        }
    return out


def _qqq_evidence(state_root: Path) -> Dict[str, Any]:
    cutover = _load_json_if_exists(state_root / "qqq_state_shadow_acceptance" / "qqq_state_shadow_cutover_recommendation.json")
    return {
        "cutover_recommendation": str(cutover.get("recommendation") or ""),
        "cutover_still_blocked": bool(cutover.get("cutover_still_blocked", True)),
        "post_instrumentation_day_count": int(cutover.get("post_instrumentation_day_count") or 0),
        "post_instrumentation_pass_days": int(cutover.get("post_instrumentation_pass_days") or 0),
        "post_instrumentation_fail_days": int(cutover.get("post_instrumentation_fail_days") or 0),
        "cutover_reasons": list(cutover.get("reasons") or []),
        "next_actions": list(cutover.get("next_actions") or []),
    }


def _spy_parity_evidence(state_root: Path) -> Dict[str, Any]:
    parity_paths = sorted(state_root.glob("spy_option_ma_parity*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    shadow_paths = sorted(state_root.glob("spy_option_ma_shadow_parity_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)

    parity_rows: List[Dict[str, Any]] = []
    for path in parity_paths:
        payload = _load_json_if_exists(path)
        for row in list(payload.get("days") or []):
            parity_rows.append(
                {
                    "report_path": str(path),
                    "day": str(row.get("day") or ""),
                    "matched": bool(row.get("matched")),
                    "old_count": int(row.get("old_count") or 0),
                    "new_count": int(row.get("new_count") or 0),
                }
            )

    day_with_real_match = any(
        bool(row.get("matched")) and int(row.get("old_count") or 0) > 0 and int(row.get("new_count") or 0) > 0
        for row in parity_rows
    )
    parity_real_mismatch_days = sorted(
        {
            str(row.get("day") or "")
            for row in parity_rows
            if int(row.get("old_count") or 0) > 0 and int(row.get("new_count") or 0) > 0 and not bool(row.get("matched"))
        }
    )

    shadow_rows: List[Dict[str, Any]] = []
    for path in shadow_paths:
        payload = _load_json_if_exists(path)
        shadow_rows.append(
            {
                "report_path": str(path),
                "report_date": str(payload.get("report_date") or ""),
                "matched_entries": int(payload.get("matched_entries") or 0),
                "replay_expected_entries": int(payload.get("replay_expected_entries") or 0),
                "live_shadow_entries": int(payload.get("live_shadow_entries") or 0),
                "missing_live_entries": int(payload.get("missing_live_entries") or 0),
                "extra_live_entries": int(payload.get("extra_live_entries") or 0),
                "trade_parity": float(payload.get("trade_parity") or 0.0),
            }
        )
    best_shadow = max(shadow_rows, key=lambda row: int(row.get("matched_entries") or 0), default={})
    shadow_matched_entries = int(best_shadow.get("matched_entries") or 0)

    latest_parity = parity_paths[0] if parity_paths else None
    latest_shadow = shadow_paths[0] if shadow_paths else None
    return {
        "latest_parity_report": str(latest_parity) if latest_parity else "",
        "latest_shadow_report": str(latest_shadow) if latest_shadow else "",
        "parity_reports_count": len(parity_paths),
        "shadow_reports_count": len(shadow_paths),
        "parity_real_mismatch_days": parity_real_mismatch_days,
        "day_with_real_match": day_with_real_match,
        "shadow_matched_entries": shadow_matched_entries,
        "shadow_best_report": dict(best_shadow),
        "recent_parity_samples": parity_rows[:10],
        "recent_shadow_samples": shadow_rows[:10],
    }


def build_option_active_strategy_evidence_status(
    platform_report: Dict[str, Any],
    rollout_report: Dict[str, Any],
    *,
    state_root: str | Path = ROOT / "runtime" / "state",
) -> Dict[str, Any]:
    root = Path(state_root).expanduser()
    platform_blocked = bool(platform_report.get("platform_admission_blocked", True))
    contract_map = _collect_common_contract_map(rollout_report)
    rows: List[StrategyEvidenceStatus] = []

    for strategy_id, contract in sorted(contract_map.items(), key=lambda x: x[0]):
        reasons: List[str] = []
        evidence: Dict[str, Any] = {"contract": contract}
        if platform_blocked:
            reasons.append("platform_admission_blocked")
        if not bool(contract.get("rollout_contract_pass")):
            reasons.append("rollout_contract_not_pass")
            for issue in list(contract.get("issues") or []):
                reasons.append(f"rollout_issue:{issue}")

        if strategy_id == "qqq_intraday_option_v1_001":
            qqq = _qqq_evidence(root)
            evidence["qqq_state_shadow"] = qqq
            if bool(qqq.get("cutover_still_blocked", True)):
                reasons.append("qqq_state_shadow_cutover_blocked")
        elif strategy_id == "spy_1dte_ma10_reject_0324_001":
            spy = _spy_parity_evidence(root)
            evidence["spy_parity_shadow"] = spy
            if not bool(spy.get("day_with_real_match", False)):
                reasons.append("spy_replay_parity_no_real_matched_day")
            if int(spy.get("shadow_matched_entries") or 0) <= 0:
                reasons.append("spy_live_shadow_no_matched_entries")

        rows.append(
            StrategyEvidenceStatus(
                strategy_id=strategy_id,
                readiness=("ready" if not reasons else "blocked"),
                reasons=reasons,
                evidence=evidence,
            )
        )

    blocked = [row.strategy_id for row in rows if row.readiness != "ready"]
    return {
        "generated_at": datetime.now().isoformat(),
        "platform_recommendation": str(platform_report.get("recommendation") or ""),
        "rollout_recommendation": str(rollout_report.get("recommendation") or ""),
        "active_strategy_count": len(rows),
        "ready_strategy_count": len(rows) - len(blocked),
        "blocked_strategy_count": len(blocked),
        "blocked_strategy_ids": blocked,
        "recommendation": ("ACTIVE_STRATEGIES_EVIDENCE_READY" if not blocked else "BLOCK"),
        "strategies": [row.to_dict() for row in rows],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build active option strategy real-evidence status report")
    parser.add_argument("--state-root", default=str(ROOT / "runtime" / "state"))
    parser.add_argument("--platform-report", default=str(ROOT / "runtime" / "state" / "option_platform_admission_gate.json"))
    parser.add_argument(
        "--rollout-report",
        default=str(ROOT / "runtime" / "state" / "option_strategy_rollout_evidence_report.json"),
    )
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    platform = _load_json_if_exists(Path(args.platform_report).expanduser())
    rollout = _load_json_if_exists(Path(args.rollout_report).expanduser())
    report = build_option_active_strategy_evidence_status(platform, rollout, state_root=args.state_root)
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        out = Path(args.output).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
        return
    print(payload)


if __name__ == "__main__":
    main()
