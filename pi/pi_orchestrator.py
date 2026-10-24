"""Principal Investigator orchestration pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import clickhouse_connect
import pandas as pd

from grid_guard import sanitize_and_constrain_grid
from llm.llm_client import LLMClient
from pi.feature_sql import build_select_sql, build_select_sql_safe
from quality.dataset_sanity import run_dataset_sanity
from labels.label_engine import LabelRegistry, LabelEngine
from labels.label_integration import build_labels_with_sanity
from experiments.manifest import create_manifest, save_manifest
from features.standard_features import (
    PYTHON_DERIVED_FEATURES,
    add_standard_features,
)
from features.registry import get_default_registry
from pi.sandbox_sql import filter_valid_derived
from pi.sandbox_sql import dry_run_feature_sql
import json
from features.planner import plan_from_recipe


@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]

    # NEW: 体检结果 + label_id + manifest_path
    sanity_report: Optional[Dict[str, Any]] = None
    sanity_summary: Optional[Dict[str, Any]] = None
    label_id: Optional[str] = None
    manifest_path: Optional[str] = None

    # UF 16: Dataset quality summary for AI Brain
    quality_summary: Optional[Dict[str, Any]] = None


def _fetch_catalog(client, table: str) -> List[str]:
    q = "SELECT name FROM system.columns WHERE table = {table:String}"
    df = client.query_df(q, parameters={"table": table})
    return sorted(df["name"].tolist())


class PrincipalInvestigator:
    """LLM-driven feature + training plan generator."""

    def __init__(self, ch_cfg: Dict[str, Any], llm_cfg: Optional[Dict[str, Any]] = None) -> None:
        self._ch_cfg = ch_cfg
        self._llm = LLMClient(
            model=(llm_cfg or {}).get("model"),
            base_url=(llm_cfg or {}).get("base_url"),
        )
        self._client = clickhouse_connect.get_client(
            host=ch_cfg.get("host", "localhost"),
            port=ch_cfg.get("port", 8123),
            username=ch_cfg.get("username", ch_cfg.get("user", "default")),
            password=ch_cfg.get("password", ""),
            database=ch_cfg.get("database", "default"),
        )

        # Load Label Registry (UF 16)
        from pathlib import Path
        label_registry_path = Path("config/label_registry.json")
        if label_registry_path.exists():
            self._label_registry = LabelRegistry(label_registry_path)
            self._label_engine = LabelEngine(self._label_registry)
        else:
            # Fallback to None if registry doesn't exist
            self._label_registry = None
            self._label_engine = None

        # Load Feature Registry (UF 17)
        # Note: registry is loaded in propose() method to avoid circular dependency
        self._feature_registry = None

    def propose(
        self,
        *,
        table: str,
        symbol: str,
        start_ts: str,
        end_ts: str,
        goal: str,
        horizon: str,
        guard_cfg: Optional[Dict[str, Any]] = None,
        freq: str = "1m",
        default_label_id: str = "ret_10b_fwd",
        use_safe_mode: bool = True,
    ) -> PIResult:
        # === 1) 拿表结构（原始列） ===
        catalog = _fetch_catalog(self._client, table)

        # === NEW: 加载 Feature Registry，并生成给 LLM 的特征摘要 ===
        registry = get_default_registry()
        registry_summary = registry.summarize_for_llm(freq=freq)

        # === 2) 让 LLM 生成 feature_recipe + training_plan ===
        python_feat_str = ", ".join(PYTHON_DERIVED_FEATURES)

        user_prompt = (
            f"目标: {goal}\n"
            f"表: {table}\n"
            f"符号: {symbol}\n"
            f"时间: {start_ts}~{end_ts}\n"
            f"原始特征列(来自 ClickHouse 表): {', '.join(catalog)}\n"
            f"可选的标准 Python 衍生特征: {python_feat_str}\n"
            f"预测窗口: {horizon}\n\n"
            "===== Curated Feature Registry (recommended features) =====\n"
            f"{registry_summary}\n"
            "===========================================================\n"
            "在设计 feature_recipe.base_features 时，应优先使用上面登记的特征，"
            "必要时可以额外选用原始列中的特征。\n"
        )
        system_prompt = (
            "你是量化平台的首席科学家。必须返回 JSON，包含 feature_recipe 和 training_plan。"
        )
        if use_safe_mode:
            system_prompt += (
                "\n**重要规则**:\n"
                "你现在拥有 `dry_run_feature` 工具。\n"
                "在生成任何 `propose_features` 动作之前，**必须**先调用 `dry_run_feature` 测试你的 SQL。\n"
                "只有当 `dry_run_feature` 返回成功时，你才可以提交 `propose_features`。\n"
                "如果 `dry_run_feature` 报错，请根据错误信息修改 SQL 并再次 Dry Run。\n"
                "\n禁止输出任何 SQL 代码或 target 表达式，label 将在 Python 层统一计算。"
                "\nfeature_recipe.base_features 中可以包括两类:\n"
                "1) 原始列名（在原始特征列表中出现）\n"
                "2) 标准 Python 衍生特征名（在标准 Python 特征列表中出现）\n"
                "尽量优先使用 Registry 中的推荐特征。"
            )
        
        # Load tools and define callbacks
        try:
            with open("ai_brain/tools_manifest.json", "r") as f:
                tools = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            tools = []

        tool_callbacks = {
            "dry_run_feature": dry_run_feature_sql
        }

        def _normalize_plan(raw: Any) -> Dict[str, Any]:
            if isinstance(raw, dict):
                return raw
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    return {}
            return {}

        plan: Dict[str, Any] = {}
        if use_safe_mode and tools and hasattr(self._llm, "generate_with_tools"):
            plan = _normalize_plan(
                self._llm.generate_with_tools(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    tools=tools,
                    tool_callbacks=tool_callbacks,
                )
            )
        # Fallback to plain generation if tool-call result is malformed or empty.
        if not plan:
            plan = _normalize_plan(
                self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)
            )

        recipe = plan.get("feature_recipe", {})
        if not isinstance(recipe, dict):
            recipe = {}
        tplan = plan.get("training_plan", {})
        if not isinstance(tplan, dict):
            tplan = {}

        # Since tool usage handles validation, we can assume the recipe is valid.
        # For logging, we might want to get a report of dropped features from the tool loop.
        # For now, we assume the LLM self-corrects and the final recipe is clean.
        dropped_derived = []

        # === 3) 决定 label_id（仅允许 registry 中存在的） ===
        label_id = tplan.get("label_id", default_label_id) if use_safe_mode else None
        if use_safe_mode:
            if not isinstance(label_id, str) or not label_id.strip():
                label_id = default_label_id
            if self._label_registry:
                try:
                    self._label_registry.get(label_id)
                except Exception:
                    # Guard against malformed LLM outputs or mocked objects in tests.
                    label_id = default_label_id

        # === 4) 构造 SQL ===
        if use_safe_mode and label_id:
            # 安全构造 SQL: 只包含原始列 + label 所需列 + LLM 选的、实际存在于表中的列
            sql = build_select_sql_safe(
                table=recipe.get("use_table", table),
                symbol=tplan.get("symbol", symbol),
                start_ts=start_ts,
                end_ts=end_ts,
                recipe=recipe,
                label_id=label_id,
                feature_catalog=catalog,
                python_derived_features=PYTHON_DERIVED_FEATURES,
            )

            df = self._client.query_df(
                sql,
                parameters={"symbol": symbol, "start_ts": start_ts, "end_ts": end_ts},
            )

            # === NEW: 根据 Registry + 实际列，生成特征物化计划 ===
            materialization_plan = plan_from_recipe(
                recipe=recipe,
                registry=registry,
                available_columns=df.columns.tolist(),
            )

            # 记录一下被丢掉的特征，方便以后追踪
            dropped_features = materialization_plan.dropped_features or []

            # === 5) 物化标准 Python 特征 ===
            if materialization_plan.python_features:
                df = add_standard_features(
                    df,
                    enabled=materialization_plan.python_features,
                )

            # === 6) 附加 label（使用新的 Label Engine，UF 16） ===
            if self._label_engine:
                # Use new Label Engine
                df = self._label_engine.attach_label(df, label_id=label_id)
                # Rename to "label" for backward compatibility
                if label_id in df.columns and label_id != "label":
                    df = df.rename(columns={label_id: "label"})
            else:
                # Fallback to old contract-based approach
                from contracts.labels import attach_label
                df = attach_label(df, label_id=label_id, colname="label")

        else:
            # 使用旧版 SQL（向后兼容）
            sql = build_select_sql(
                table=recipe.get("use_table", table),
                symbol=tplan.get("symbol", symbol),
                start_ts=start_ts,
                end_ts=end_ts,
                recipe=recipe,
                target=tplan.get("target"),
                feature_catalog=catalog,
            )
            df = self._client.query_df(sql)

        # === 7) Dataset Sanity 体检（现在包括标准特征 + label） ===
        sanity_report, sanity_summary = run_dataset_sanity(
            df,
            label_cols=["label"] if "label" in df.columns else None
        )

        # === 7.5) Create Dataset Quality Summary for AI Brain (UF 16) ===
        quality_summary_obj = None
        if use_safe_mode:
            from quality.dataset_sanity import run_dataset_sanity_v2
            from brain.dataset_summary import create_quality_summary

            # Run v2 sanity check to get DatasetSanityResult
            sanity_v2 = run_dataset_sanity_v2(df)

            # Create quality summary (combining dataset + label sanity)
            quality_summary_obj = create_quality_summary(
                dataset_sanity=sanity_v2,
                label_sanity=sanity_summary.get("label_sanity") if sanity_summary else None,
            )

        # === 7.6) Filter features through Registry constraints (UF 17) ===
        registry_rejected: List[str] = []
        if use_safe_mode and materialization_plan:
            # Load registry if not already loaded
            if self._feature_registry is None:
                self._feature_registry = registry  # Already loaded at top of propose()

            # Get task type and horizon
            task = tplan.get("task", "regression")
            horizon_bars = None  # Will extract from label_def if available
            if self._label_engine and label_id:
                try:
                    label_def = self._label_engine.registry.get(label_id)
                    horizon_bars = label_def.horizon_bars
                except Exception:
                    pass

            # Filter candidate features through Registry
            candidate_features = materialization_plan.final_feature_list
            allowed_features, rejected_reasons = self._feature_registry.filter_for_task_and_label(
                candidate_features=candidate_features,
                task=task,
                label_id=label_id,
                horizon_bars=horizon_bars,
            )

            # Update materialization plan with filtered features
            materialization_plan.final_feature_list = allowed_features
            registry_rejected = rejected_reasons

        # === 8) 构造 HPO 网格（用 planner 的最终特征列表） ===
        if use_safe_mode:
            # 使用 planner 计算出的最终特征列表
            feature_subset = materialization_plan.final_feature_list

            # 如果 LLM 提的某些特征根本不存在，也不会导致训练时崩溃，只是被 silently drop
            if not feature_subset:
                # fallback: 至少给一个默认的基线特征组合
                default_candidates = [
                    "ret_1b",
                    "ret_5b",
                    "rvol_20",
                    "ma_20_close",
                    "ma_50_close",
                ]
                available_features = [col for col in df.columns if col != "label"]
                feature_subset = [f for f in default_candidates if f in available_features]
        else:
            # Old mode: trust LLM proposal
            feature_subset = [
                recipe.get("base_features", [])
                + [d.get("alias") for d in recipe.get("derived", []) or []]
            ]
            dropped_features = []
            registry_rejected = []  # UF 17

        grid = {
            "model_type": [],
            "feature_subset": [feature_subset] if isinstance(feature_subset, list) and not isinstance(feature_subset[0] if feature_subset else None, list) else feature_subset,
            "task": [tplan.get("task", "reg")],
            "decision": [
                tplan.get(
                    "decision",
                    {"mode": "quantile", "q_long": 0.7, "q_short": 0.3},
                )
            ],
            "model_params": [],
        }
        for cand in tplan.get("model_candidates", []) or []:
            grid["model_type"].append(cand.get("model_type", "xgb"))
            grid["model_params"].append(cand.get("model_params", {}))

        guard = (guard_cfg or {}) | {
            "allow_params": [
                "model_type",
                "feature_subset",
                "task",
                "decision",
                "model_params",
            ]
        }
        grid_sane, _ = sanitize_and_constrain_grid(grid, guard)

        # === 9) 生成并保存 Experiment Manifest（记录这次实验所有关键信息） ===
        manifest = None
        manifest_path = None
        if use_safe_mode and label_id:
            manifest = create_manifest(
                symbol=symbol,
                freq=freq,
                label_id=label_id,
                start_ts=start_ts,
                end_ts=end_ts,
                data_source={
                    "table": table,
                    "use_table": recipe.get("use_table", table),
                    "clickhouse": self._ch_cfg,
                },
                feature_recipe=recipe,
                training_plan=tplan,
                sanity_summary=sanity_summary,
                meta={
                    "sql": sql,
                    "feature_catalog": catalog,
                    "python_derived_features": materialization_plan.python_features,
                    "feature_subset_effective": feature_subset if isinstance(feature_subset, list) else [],
                    "registry_version": registry.version,
                    "sandbox_dropped_derived": dropped_derived,
                    "planner_dropped_features": dropped_features,
                    "registry_rejected_features": registry_rejected,  # UF 17
                },
            )
            manifest_path = str(save_manifest(manifest))

        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
            sanity_report=sanity_report,
            sanity_summary=sanity_summary,
            label_id=label_id,
            manifest_path=manifest_path,
            quality_summary=quality_summary_obj.to_dict() if quality_summary_obj else None,
        )
