# experiments/manifest.py

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
import json


@dataclass
class ExperimentManifest:
    """
    记录一次完整实验 / 训练任务的"契约":
    - 用了什么数据、标签、特征、训练计划
    - 数据体检结果摘要
    - 方便审计 & 复现
    """
    job_id: str
    created_at: str

    symbol: str
    freq: str
    label_id: str

    data_source: Dict[str, Any]
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]

    sanity_summary: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def generate_job_id(
    symbol: str,
    freq: str,
    label_id: str,
    start_ts: str,
    end_ts: str,
) -> str:
    """
    统一 job_id 规范: NVDA_1m_ret_10b_fwd_2024-01-01_2024-03-01
    """
    safe_label = label_id.replace("/", "-")
    return f"{symbol}_{freq}_{safe_label}_{start_ts}_{end_ts}"


def create_manifest(
    *,
    symbol: str,
    freq: str,
    label_id: str,
    start_ts: str,
    end_ts: str,
    data_source: Dict[str, Any],
    feature_recipe: Dict[str, Any],
    training_plan: Dict[str, Any],
    sanity_summary: Optional[Dict[str, Any]] = None,
    notes: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> ExperimentManifest:
    job_id = generate_job_id(symbol, freq, label_id, start_ts, end_ts)
    return ExperimentManifest(
        job_id=job_id,
        created_at=datetime.utcnow().isoformat(),
        symbol=symbol,
        freq=freq,
        label_id=label_id,
        data_source=data_source,
        feature_recipe=feature_recipe,
        training_plan=training_plan,
        sanity_summary=sanity_summary,
        notes=notes,
        meta=meta or {},
    )


def save_manifest(
    manifest: ExperimentManifest,
    base_dir: str = "manifests",
) -> Path:
    """
    把 manifest 存成一个 JSON 文件，方便后面审计和回溯。
    """
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{manifest.job_id}.json"
    path.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2))
    return path
