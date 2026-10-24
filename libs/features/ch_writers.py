# ch_writers.py
# SPDX-License-Identifier: Apache-2.0
#
# ClickHouse写入工具 - 与现有ClickHouseClient完全兼容
from __future__ import annotations
from typing import Dict, Any, List, Optional
import json
from datetime import datetime

# 导入现有的ClickHouse客户端
try:
    from libs.checkpoint import ClickHouseClient
    _HAS_CH_CLIENT = True
except ImportError:
    _HAS_CH_CLIENT = False
    ClickHouseClient = None


def insert_oos_metrics(ch: ClickHouseClient, rows: List[Dict[str, Any]]) -> None:
    """
    写入OOS评估指标到trading.ml_oos_metrics表

    Args:
        ch: ClickHouseClient实例
        rows: 指标数据列表，每个元素包含:
            - run_id: str
            - study: str
            - trial_id: int
            - fold_id: int
            - symbol: str
            - window_start: str (datetime)
            - window_end: str (datetime)
            - oos_avg_sharpe: float
            - oos_avg_ic: float
            - ic_coverage: float
            - pf: float
            - max_dd: float
            - trades: int
            - feature_program: str (optional)
            - num_features: int (optional)
            - notes: str (optional)
    """
    if not rows:
        return

    if not _HAS_CH_CLIENT:
        raise ImportError("ClickHouseClient not available")

    # 转换为JSONEachRow格式
    values = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    sql = "INSERT INTO trading.ml_oos_metrics FORMAT JSONEachRow\n" + values

    ch.query(sql)


def insert_features_snapshot(ch: ClickHouseClient, payloads: List[Dict[str, Any]]) -> None:
    """
    写入特征快照到trading.features_1m_active表

    Args:
        ch: ClickHouseClient实例
        payloads: 特征数据列表，每个元素包含:
            - ts: str (datetime)
            - symbol: str
            - open, high, low, close, volume: float
            - label_1m: float (optional)
            - features: dict (所有生成的特征)
    """
    if not payloads:
        return

    if not _HAS_CH_CLIENT:
        raise ImportError("ClickHouseClient not available")

    # 转换features dict为JSON字符串
    for p in payloads:
        if "features" in p and isinstance(p["features"], dict):
            p["features"] = json.dumps(p["features"], ensure_ascii=False)

    values = "\n".join(json.dumps(p, ensure_ascii=False) for p in payloads)
    sql = "INSERT INTO trading.features_1m_active FORMAT JSONEachRow\n" + values

    ch.query(sql)


def update_feature_metadata(ch: ClickHouseClient, updates: List[Dict[str, Any]]) -> None:
    """
    更新特征元数据到trading.feature_metadata表

    Args:
        ch: ClickHouseClient实例
        updates: 元数据列表，每个元素包含:
            - feature_name: str
            - program_id: str
            - avg_ic: float (optional)
            - avg_coverage: float (optional)
            - avg_variance: float (optional)
            - avg_importance: float (optional)
            - status: str (optional: active/deprecated/archived)
            - usage_count: int (optional)
            - ic_history: list[float] (optional)
            - notes: str (optional)
    """
    if not updates:
        return

    if not _HAS_CH_CLIENT:
        raise ImportError("ClickHouseClient not available")

    # ReplacingMergeTree会自动处理重复数据
    for u in updates:
        if "ic_history" in u and isinstance(u["ic_history"], list):
            u["ic_history"] = u["ic_history"]  # ClickHouse支持Array类型

    values = "\n".join(json.dumps(u, ensure_ascii=False) for u in updates)
    sql = "INSERT INTO trading.feature_metadata FORMAT JSONEachRow\n" + values

    ch.query(sql)


def query_feature_metadata(ch: ClickHouseClient,
                           program_id: Optional[str] = None,
                           status: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    查询特征元数据

    Args:
        ch: ClickHouseClient实例
        program_id: 程序ID过滤 (optional)
        status: 状态过滤 (optional: active/deprecated/archived)

    Returns:
        特征元数据列表
    """
    if not _HAS_CH_CLIENT:
        raise ImportError("ClickHouseClient not available")

    sql = "SELECT * FROM trading.feature_metadata WHERE 1=1"

    if program_id:
        sql += f" AND program_id = '{program_id}'"

    if status:
        sql += f" AND status = '{status}'"

    sql += " ORDER BY avg_importance DESC FORMAT JSONEachRow"

    result = ch.query(sql)

    # Parse JSONEachRow response
    rows = []
    for line in result.strip().split('\n'):
        if line:
            rows.append(json.loads(line))

    return rows


def query_oos_performance(ch: ClickHouseClient,
                         study: Optional[str] = None,
                         top_k: int = 10) -> List[Dict[str, Any]]:
    """
    查询OOS性能汇总

    Args:
        ch: ClickHouseClient实例
        study: 研究名称过滤 (optional)
        top_k: 返回top K结果

    Returns:
        OOS性能汇总列表
    """
    if not _HAS_CH_CLIENT:
        raise ImportError("ClickHouseClient not available")

    sql = "SELECT * FROM trading.v_oos_performance WHERE 1=1"

    if study:
        sql += f" AND study = '{study}'"

    sql += f" ORDER BY avg_sharpe DESC LIMIT {top_k} FORMAT JSONEachRow"

    result = ch.query(sql)

    rows = []
    for line in result.strip().split('\n'):
        if line:
            rows.append(json.loads(line))

    return rows


# ========== 便捷批量写入函数 ==========

def batch_insert_oos(ch: ClickHouseClient,
                     study: str,
                     run_id: str,
                     trial_id: int,
                     oos_results: List[Dict[str, Any]],
                     feature_program: str = "",
                     num_features: int = 0) -> None:
    """
    批量写入OOS结果（便捷函数）

    Args:
        ch: ClickHouseClient实例
        study: 研究名称
        run_id: 运行ID
        trial_id: Trial ID
        oos_results: OOS结果列表，每个元素包含:
            - fold_id, symbol, window_start, window_end
            - oos_avg_sharpe, oos_avg_ic, ic_coverage, pf, max_dd, trades
        feature_program: 特征程序ID
        num_features: 特征数量
    """
    rows = []
    for r in oos_results:
        row = {
            "study": study,
            "run_id": run_id,
            "trial_id": trial_id,
            "fold_id": r["fold_id"],
            "symbol": r.get("symbol", ""),
            "window_start": r["window_start"],
            "window_end": r["window_end"],
            "oos_avg_sharpe": r["oos_avg_sharpe"],
            "oos_avg_ic": r.get("oos_avg_ic", 0.0),
            "ic_coverage": r.get("ic_coverage", 0.0),
            "pf": r.get("pf", 0.0),
            "max_dd": r.get("max_dd", 0.0),
            "trades": r.get("trades", 0),
            "feature_program": feature_program,
            "num_features": num_features,
            "notes": r.get("notes", "")
        }
        rows.append(row)

    insert_oos_metrics(ch, rows)


def deprecate_features(ch: ClickHouseClient,
                      feature_names: List[str],
                      reason: str) -> None:
    """
    标记特征为deprecated

    Args:
        ch: ClickHouseClient实例
        feature_names: 要标记的特征名列表
        reason: 弃用原因
    """
    updates = []
    for fname in feature_names:
        updates.append({
            "feature_name": fname,
            "program_id": "",  # 需要先查询获取
            "status": "deprecated",
            "deprecation_reason": reason
        })

    update_feature_metadata(ch, updates)
