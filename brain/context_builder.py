# brain/context_builder.py
"""
Brain Context Builder

Builds context for AI Brain LLM consumption:
- build_brain_prompts: Full prompt construction for trading decisions
- sanity_for_brain: Compress sanity reports for LLM (UF 21)
- build_brain_context: Construct experiment context (UF 21)
"""

from __future__ import annotations

from typing import Any, Dict, Optional, List

from brain.prompt_library import PromptLibrary
from features.registry import FeatureRegistry


def _format_dict_block(title: str, data: Optional[Dict[str, Any]]) -> str:
    if not data:
        return f"[{title}]\n- n/a\n"
    lines = [f"[{title}]"]
    for k, v in data.items():
        lines.append(f"- {k}: {v}")
    return "\n".join(lines) + "\n"


def _format_list_block(title: str, data: Optional[list]) -> str:
    if not data:
        return f"[{title}]\n- n/a\n"
    lines = [f"[{title}]"]
    for item in data:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def build_brain_prompts(
    *,
    prompt_lib: PromptLibrary,
    prompt_id: str,
    symbol: str,
    now_ts: str,
    freq: str,
    horizon: str,
    market_snapshot: Dict[str, Any],
    risk_gates: Dict[str, Any],
    dataset_sanity: Optional[Dict[str, Any]] = None,
    experiment_summary: Optional[Dict[str, Any]] = None,
    registry: Optional[FeatureRegistry] = None,
    allowed_run_ids: Optional[list] = None,
) -> Dict[str, str]:
    """
    构造给执行大脑的 system_prompt / user_prompt（两段字符串）。

    - prompt_id: 比如 "v3_fast_market_following_tactical_20251117"
    - market_snapshot: 一般由上游整理好，比如:
        {
          "last_price": 612.5,
          "change_pct": -0.8,
          "rvol_20": 2.3,
          "session": "RTH",
          ...
        }
    - risk_gates: 今天启用的所有硬门槛 & 软参数
    - dataset_sanity: 来自训练数据集的主要质量结论（status / warnings / etc）
    - experiment_summary: 来自 manifest / 回测，包含模型性能指标等
    """
    base_prompt = prompt_lib.get(prompt_id)

    # ===== system_prompt =====
    system_prompt = (
        base_prompt
        + "\n\n"
        "You will receive structured context in the user message, with sections:\n"
        "[Market State], [Risk Gates], [Dataset Quality], [Experiment Summary], [Feature Registry].\n"
        "Use this information to make a risk-aware intraday decision.\n"
        "Always obey risk gates. Always output JSON only, following the agreed schema."
    )

    # ===== user_prompt: 拼接各个部分 =====
    parts = []

    # 0) 基本描述
    header = (
        f"Symbol: {symbol}\n"
        f"Now (exchange or NY time): {now_ts}\n"
        f"Primary bar frequency: {freq}\n"
        f"Decision horizon: {horizon}\n\n"
    )
    parts.append(header)

    # 1) 市场状态
    parts.append(_format_dict_block("Market State", market_snapshot))

    # 2) 风险门槛
    parts.append(_format_dict_block("Risk Gates", risk_gates))

    # 3) 数据集质量
    parts.append(_format_dict_block("Dataset Quality", dataset_sanity))

    # 4) 历史实验 / 模型表现
    parts.append(_format_dict_block("Experiment Summary", experiment_summary))

    # 5) Feature Registry 概览
    if registry is not None:
        registry_text = registry.summarize_for_llm(freq=freq)
        parts.append("[Feature Registry]\n" + registry_text + "\n")
    else:
        parts.append("[Feature Registry]\n- n/a (no registry loaded)\n")

    # 6) Strategy Universe (if provided)
    if allowed_run_ids:
        parts.append("\n[Strategy Universe]")
        parts.append("You MUST choose from the following strategy run_ids:")
        for rid in allowed_run_ids:
            parts.append(f"- {rid}")
        parts.append("")

        # Output JSON Contract
        parts.append("[Output JSON Contract]")
        parts.append("You MUST reply with a single JSON object only, no extra text.")
        parts.append("JSON schema:")
        parts.append("{")
        parts.append('  "decision": "HOLD" | "ENTER" | "EXIT" | "ADJUST",')
        parts.append('  "chosen_run_id": string or null,  // MUST be one of the allowed run_ids when decision != "HOLD"')
        parts.append('  "side": "LONG" | "SHORT" | null,  // required for ENTER/ADJUST, can be null for HOLD/EXIT')
        parts.append('  "size_frac": float between 0.0 and 1.0,')
        parts.append('  "notes": string or null  // short rationale')
        parts.append("}")
        parts.append("Do not invent new fields. Do not include any explanation outside JSON.")
        parts.append("")

    user_prompt = "\n".join(parts).strip()

    return {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
    }


# ========== UF 21: Sanity Report Compression for Brain ==========


def sanity_for_brain(sanity_report: Dict[str, Any]) -> Dict[str, Any]:
    """
    从 ExperimentRecord.sanity_report 中抽取最关键的信息给 LLM。

    Only includes essential information to avoid context bloat.

    Args:
        sanity_report: Full sanity report dict (from sanity_report_to_dict)

    Returns:
        Compressed sanity dict for AI Brain context with keys:
        - level: Overall health level
        - label_health: Label quality
        - feature_health: Feature quality
        - risk_flags: List of risk codes
        - summary: One-line summary
        - label_notes: Top 5 label issues
        - feature_notes: Top 5 feature issues
    """
    level = sanity_report.get("level", "unknown")
    label_health = sanity_report.get("label_health", "unknown")
    feature_health = sanity_report.get("feature_health", "unknown")
    risk_flags = sanity_report.get("risk_flags", [])
    summary = sanity_report.get("summary", "")

    # 只选前几条 note，防止太长
    label_notes = (sanity_report.get("label_notes") or [])[:5]
    feature_notes = (sanity_report.get("feature_notes") or [])[:5]

    return {
        "level": level,
        "label_health": label_health,
        "feature_health": feature_health,
        "risk_flags": risk_flags,
        "summary": summary,
        "label_notes": label_notes,
        "feature_notes": feature_notes,
    }


def build_brain_context(
    run_id: str,
    symbol: str,
    metrics: Dict[str, float],
    sanity_report: Dict[str, Any] | None = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    构建完整 Brain 上下文（示例）。

    Args:
        run_id: Experiment run ID
        symbol: Trading symbol
        metrics: Performance metrics
        sanity_report: Optional sanity report dict
        **kwargs: Additional context fields

    Returns:
        Brain context dict suitable for LLM prompt
    """
    ctx: Dict[str, Any] = {
        "run_id": run_id,
        "symbol": symbol,
        "metrics": metrics,
    }

    # Add sanity info
    if sanity_report:
        ctx["sanity"] = sanity_for_brain(sanity_report)
    else:
        ctx["sanity"] = {"level": "unknown", "risk_flags": []}

    # Add any additional context
    ctx.update(kwargs)

    return ctx
