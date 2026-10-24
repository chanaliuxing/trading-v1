# experiments/types.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


@dataclass
class Metrics:
    """
    一个回测/实验的核心量化指标。
    没有的字段可以用 None，保持 schema 稳定。
    """
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    ic: Optional[float] = None          # 信息系数
    hit_rate: Optional[float] = None    # 胜率
    max_drawdown: Optional[float] = None
    pnl: Optional[float] = None         # 总收益（可以是绝对或百分比）
    pnl_annualized: Optional[float] = None
    trades: Optional[int] = None
    avg_trade_pnl: Optional[float] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Metrics":
        # 安全地从任意 dict 构建，不存在的字段用默认
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__.keys()})

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExperimentRecord:
    """
    一次完整实验的"索引"记录，用于写入 JSONL。

    - 一个 run_id 对应一次训练+回测 / 或一次 HPO trial 的代表结果。
    - manifest_path 指向更详细的 JSON manifest。
    """
    run_id: str
    symbol: str
    freq: str             # e.g. "1m", "5s"
    horizon: str          # e.g. "10m"
    label_id: str
    model_type: str
    feature_subset: List[str]
    params: Dict[str, Any]
    metrics: Metrics
    run_ts: str           # ISO8601 时间戳
    data_start_ts: Optional[str] = None
    data_end_ts: Optional[str] = None
    prompt_id: Optional[str] = None     # 用了哪个执行 prompt（可选）
    manifest_path: Optional[str] = None
    tags: Optional[List[str]] = None    # 例如 ["pre-market", "trend", "fast"]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["metrics"] = self.metrics.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExperimentRecord":
        m = Metrics.from_dict(d.get("metrics", {}))
        d2 = {**d}
        d2["metrics"] = m
        # feature_subset / tags 可能被序列化成非 list，做一次兜底
        if isinstance(d2.get("feature_subset"), str):
            # 简单 split，真实环境可以按需要解析
            d2["feature_subset"] = [d2["feature_subset"]]
        if not isinstance(d2.get("feature_subset"), list):
            d2["feature_subset"] = []
        if isinstance(d2.get("tags"), str):
            d2["tags"] = [d2["tags"]]
        if d2.get("tags") is None:
            d2["tags"] = []
        return cls(**d2)
