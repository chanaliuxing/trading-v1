# experiments/registry.py

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional

import json

from experiments.types import ExperimentRecord


DEFAULT_INDEX_PATH = Path("experiments/experiment_index.jsonl")


def append_experiment_record(
    record: ExperimentRecord,
    index_path: Path = DEFAULT_INDEX_PATH,
) -> None:
    """
    以 JSONL 形式将实验记录 append 到文件末尾。
    - 不需要一次性加载全部历史（避免随着历史增长变慢）。
    """
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")


def load_experiment_records(
    index_path: Path = DEFAULT_INDEX_PATH,
    *,
    symbol: Optional[str] = None,
    freq: Optional[str] = None,
    horizon: Optional[str] = None,
    label_id: Optional[str] = None,
) -> List[ExperimentRecord]:
    """
    从 JSONL index 里按条件加载所有匹配的记录。
    """
    if not index_path.exists():
        return []

    records: List[ExperimentRecord] = []
    with index_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec = ExperimentRecord.from_dict(raw)

            if symbol and rec.symbol != symbol:
                continue
            if freq and rec.freq != freq:
                continue
            if horizon and rec.horizon != horizon:
                continue
            if label_id and rec.label_id != label_id:
                continue

            records.append(rec)
    return records


def _safe_metric(rec: ExperimentRecord, metric: str, default: float = float("nan")) -> float:
    v = getattr(rec.metrics, metric, None)
    if v is None:
        return default
    return float(v)


def top_n_by_metric(
    records: Iterable[ExperimentRecord],
    metric: str,
    n: int = 3,
    *,
    reverse: bool = True,
) -> List[ExperimentRecord]:
    """
    根据某个 metric 排序，返回 top-n。
    默认 reverse=True 表示越大越好（如 sharpe / ic / pnl）。
    """
    rec_list = list(records)
    rec_list.sort(key=lambda r: _safe_metric(r, metric, default=float("-inf")), reverse=reverse)
    return rec_list[:n]
