# AI交易系统实用化自动化路线图

**版本**: 2.0 (Pragmatic Edition)
**日期**: 2025-11-25
**原则**: 小步快跑，逐步演进，不推翻重来

---

## 一、现状评估

### 已落地的基础设施 ✅
```
✅ services/data/data_healer.py - Bars/Features自愈
✅ libs/features/feature_engineer_v2.py - 特征工程
✅ scripts/ai_trader/brain/* - Brain决策系统
✅ scripts/ai_trader/orchestrator.py - 任务编排
✅ tools/monitor_system.py - 系统监控
```

### 缺失的关键环节 ❌
```
❌ Label生成未集成到DataHealer
❌ 训练前无PreFlight检查
❌ Brain context缺少label_health
❌ AUC异常无自动诊断
```

### 设计约束
- **不引入新框架** (Prefect/Airflow暂缓)
- **不重写已有系统** (DataHealer/Brain保持原样)
- **保持同步风格** (不大规模async化)
- **最小化依赖** (Prometheus等Phase 2+)

---

## 二、渐进式改进路线图

### Phase 1: 紧急止血 (本周完成) ⚡

**目标**: 永远不会再出现"labels空表 + 训练照跑"

#### 1.1 扩展Monitor - 添加label_health检查

**文件**: `tools/monitor_system.py`

**新增函数**:
```python
def check_label_health(symbols: List[str], horizons: List[int] = [30]) -> dict:
    """
    检查label健康度 - 集成到现有monitor

    Returns:
        {
            "by_symbol": {
                "TSLA": {
                    "rows": 159545,
                    "null_ratio": 0.0,
                    "age_hours": 2.5,
                    "status": "healthy"
                },
                "NVDA": {
                    "rows": 0,
                    "null_ratio": 1.0,
                    "age_hours": None,
                    "status": "missing"  # ← 关键!
                }
            },
            "summary": {
                "total_rows": 1315213,
                "missing_symbols": ["NVDA"],
                "stale_symbols": [],
                "overall_status": "degraded"
            }
        }
    """
    from libs.db.ch_pool import get_client

    client = get_client()
    result = {"by_symbol": {}, "summary": {}}

    for symbol in symbols:
        # 查询该symbol的label行数和质量
        query = f"""
        SELECT
            count() as total_rows,
            countIf(label_h30 IS NULL) as null_count,
            max(ts) as latest_ts
        FROM trading.features_1m_with_labels
        WHERE symbol = '{symbol}'
        """

        data = client.query(query)
        if not data:
            result["by_symbol"][symbol] = {
                "rows": 0,
                "null_ratio": 1.0,
                "age_hours": None,
                "status": "missing"
            }
            continue

        row = data[0]
        total = row['total_rows']
        null_count = row['null_count']
        latest_ts = row['latest_ts']

        # 计算数据年龄
        if latest_ts and total > 0:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now = datetime.now(tz=ZoneInfo("America/New_York"))
            age_hours = (now - latest_ts).total_seconds() / 3600
        else:
            age_hours = None

        # 计算NULL比例
        null_ratio = null_count / total if total > 0 else 1.0

        # 判断状态
        if total == 0:
            status = "missing"
        elif age_hours and age_hours > 24:
            status = "stale"
        elif null_ratio > 0.1:
            status = "poor_quality"
        else:
            status = "healthy"

        result["by_symbol"][symbol] = {
            "rows": total,
            "null_ratio": round(null_ratio, 4),
            "age_hours": round(age_hours, 1) if age_hours else None,
            "status": status
        }

    # 生成summary
    all_rows = sum(s["rows"] for s in result["by_symbol"].values())
    missing = [sym for sym, info in result["by_symbol"].items()
               if info["status"] == "missing"]
    stale = [sym for sym, info in result["by_symbol"].items()
             if info["status"] == "stale"]

    result["summary"] = {
        "total_rows": all_rows,
        "missing_symbols": missing,
        "stale_symbols": stale,
        "overall_status": "healthy" if not missing and not stale else "degraded"
    }

    return result
```

**集成到现有generate_health_report**:
```python
def generate_health_report() -> dict:
    """现有函数 - 添加label_health"""
    report = {
        "timestamp": datetime.now().isoformat(),
        "clickhouse": check_clickhouse_health(),
        "redis": check_redis_health(),
        "kafka": check_kafka_health(),
        # === 新增 ===
        "label_health": check_label_health(
            symbols=["TSLA", "NVDA", "SPY", "AAPL", "MSFT",
                    "AMD", "META", "AMZN", "GOOGL", "QQQ",
                    "TQQQ", "SQQQ", "VOO", "ORCL", "INTC"]
        )
    }
    return report
```

**预期输出**:
```json
{
  "label_health": {
    "by_symbol": {
      "TSLA": {"rows": 159545, "null_ratio": 0.0, "age_hours": 2.5, "status": "healthy"},
      "NVDA": {"rows": 0, "null_ratio": 1.0, "age_hours": null, "status": "missing"}
    },
    "summary": {
      "total_rows": 1315213,
      "missing_symbols": ["NVDA"],
      "stale_symbols": [],
      "overall_status": "degraded"
    }
  }
}
```

---

#### 1.2 Brain Context集成 - 暴露label_health

**文件**: `scripts/ai_trader/brain/context_builder.py` (或直接在main loop)

**修改**:
```python
# 在Brain主循环构建context时
from tools.monitor_system import generate_health_report

health = generate_health_report()

context = {
    "market": {...},
    "portfolio": {...},
    "data_quality_snapshot": {
        "clickhouse": health["clickhouse"],
        "redis": health["redis"],
        # === 新增: Brain能看到label健康度 ===
        "label_health": health["label_health"]
    },
    ...
}
```

**效果**: Brain现在能在context里直接看到哪些symbol的labels缺失/过期

---

#### 1.3 PreFlight Check - 训练前强制验证

**文件**: `scripts/ai_trader/brain/preflight.py` (新建)

```python
"""
训练前飞行检查 - 确保labels就绪

设计原则:
- 同步函数,不引入async
- 集成现有DataHealer
- 简单明了,无过度抽象
"""

from typing import List, Tuple, Dict
from libs.db.ch_pool import get_client
import structlog

logger = structlog.get_logger()


def preflight_check_labels(
    symbols: List[str],
    min_rows_per_symbol: int = 1000,
    max_null_ratio: float = 0.1
) -> Tuple[bool, Dict]:
    """
    训练前检查labels是否就绪

    Args:
        symbols: 待训练的symbols
        min_rows_per_symbol: 每个symbol最少需要的样本数
        max_null_ratio: label NULL比例上限

    Returns:
        (ready: bool, details: dict)

        ready=True 表示可以开始训练
        ready=False 表示有阻塞问题,details里有具体原因
    """
    client = get_client()

    details = {
        "status": "passed",
        "blocked_by": [],
        "warnings": [],
        "symbol_stats": {}
    }

    # 检查每个symbol
    for symbol in symbols:
        query = f"""
        SELECT
            count() as total_rows,
            countIf(label_h30 IS NULL) as null_count
        FROM trading.features_1m_with_labels
        WHERE symbol = '{symbol}'
        """

        result = client.query(query)
        if not result:
            details["blocked_by"].append(
                f"{symbol}: labels表查询失败"
            )
            details["symbol_stats"][symbol] = {"error": "query_failed"}
            continue

        row = result[0]
        total = row['total_rows']
        null_count = row['null_count']
        null_ratio = null_count / total if total > 0 else 1.0

        details["symbol_stats"][symbol] = {
            "rows": total,
            "null_count": null_count,
            "null_ratio": round(null_ratio, 4)
        }

        # 检查1: 表为空
        if total == 0:
            details["blocked_by"].append(
                f"{symbol}: labels表为空 (0行)"
            )
            continue

        # 检查2: 样本数不足
        if total < min_rows_per_symbol:
            details["warnings"].append(
                f"{symbol}: 仅{total}行labels (建议≥{min_rows_per_symbol})"
            )

        # 检查3: NULL比例过高
        if null_ratio > max_null_ratio:
            details["blocked_by"].append(
                f"{symbol}: {null_ratio:.1%} labels为NULL (上限{max_null_ratio:.1%})"
            )

    # 判断整体状态
    if details["blocked_by"]:
        details["status"] = "blocked"
        ready = False
        logger.error(
            "preflight_check_failed",
            blocked_by=details["blocked_by"],
            symbols=symbols
        )
    else:
        ready = True
        if details["warnings"]:
            logger.warning(
                "preflight_check_passed_with_warnings",
                warnings=details["warnings"]
            )
        else:
            logger.info("preflight_check_passed", symbols=symbols)

    return ready, details


def preflight_check_features(symbols: List[str], max_age_hours: int = 24) -> Tuple[bool, Dict]:
    """
    检查features数据新鲜度

    Returns:
        (ready: bool, details: dict)
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    client = get_client()
    details = {"status": "passed", "blocked_by": [], "warnings": []}

    # 查询最新的features时间戳
    query = """
    SELECT max(ts) as latest_ts
    FROM trading.features_1m
    """

    result = client.query(query)
    if not result or not result[0]['latest_ts']:
        details["blocked_by"].append("features_1m表为空或无时间戳")
        details["status"] = "blocked"
        return False, details

    latest_ts = result[0]['latest_ts']
    now = datetime.now(tz=ZoneInfo("America/New_York"))
    age_hours = (now - latest_ts).total_seconds() / 3600

    details["latest_ts"] = latest_ts.isoformat()
    details["age_hours"] = round(age_hours, 1)

    if age_hours > max_age_hours:
        details["warnings"].append(
            f"Features数据过期 ({age_hours:.1f}小时,上限{max_age_hours}小时)"
        )

    return True, details


def preflight_check_all(symbols: List[str]) -> Tuple[bool, Dict]:
    """
    综合检查 - labels + features

    Returns:
        (ready: bool, full_report: dict)
    """
    report = {
        "timestamp": datetime.now().isoformat(),
        "symbols": symbols,
        "checks": {},
        "overall_ready": False
    }

    # 检查labels
    labels_ready, labels_details = preflight_check_labels(symbols)
    report["checks"]["labels"] = labels_details

    # 检查features
    features_ready, features_details = preflight_check_features(symbols)
    report["checks"]["features"] = features_details

    # 整体判断
    report["overall_ready"] = labels_ready and features_ready

    return report["overall_ready"], report
```

---

#### 1.4 集成到ML Scheduler - 训练前调用PreFlight

**文件**: `scripts/ai_trader/ml_scheduler.py` (或 `brain/policy/oof_policy.py`)

**修改OOF训练入口**:
```python
from brain.preflight import preflight_check_all
from services.data.data_healer import DataHealer  # 现有的

def run_oof_training(symbols: List[str], context: dict):
    """
    OOF训练 - 集成PreFlight检查
    """
    logger.info("oof_training_start", symbols=symbols)

    # === 新增: PreFlight Check ===
    ready, preflight_report = preflight_check_all(symbols)

    # 记录到events
    write_ml_event({
        "type": "preflight_check",
        "ready": ready,
        "report": preflight_report,
        "timestamp": datetime.now().isoformat()
    })

    if not ready:
        logger.error(
            "oof_training_aborted",
            reason="preflight_check_failed",
            blocked_by=preflight_report["checks"]["labels"]["blocked_by"]
        )

        # === 尝试自动修复 (Phase 1.5) ===
        if "labels表为空" in str(preflight_report["checks"]["labels"]["blocked_by"]):
            logger.warning("attempting_auto_heal", action="generate_labels")

            # 调用现有DataHealer (扩展后的版本)
            healer = DataHealer(runs_dir="runs/ai_trader")
            heal_result = healer.ensure_labels_ready(
                symbols=symbols,
                horizons=[30],
                lookback_days=90
            )

            if heal_result["status"] == "fixed":
                logger.info("auto_heal_success", result=heal_result)

                # 重新检查
                ready, preflight_report = preflight_check_all(symbols)
                if not ready:
                    logger.error("auto_heal_failed_to_fix", report=preflight_report)
                    return {"status": "aborted", "reason": "preflight_failed_after_heal"}
            else:
                logger.error("auto_heal_failed", result=heal_result)
                return {"status": "aborted", "reason": "preflight_failed_and_heal_failed"}
        else:
            # 其他类型的阻塞,暂不自动修复
            return {"status": "aborted", "reason": "preflight_failed"}

    logger.info("preflight_check_passed", warnings=preflight_report["checks"]["labels"].get("warnings", []))

    # === 原有训练逻辑 ===
    # ... 你现在的OOF训练代码 ...
```

**效果**:
- ✅ 训练永远不会在labels缺失时启动
- ✅ 自动尝试修复 (调用DataHealer)
- ✅ 所有检查记录到ml_events.jsonl

---

### Phase 1.5: 扩展DataHealer - 集成Label生成 (本周完成)

**目标**: 复用现有DataHealer架构,添加label模块

#### 文件: `services/data/data_healer.py`

**新增方法**:
```python
class DataHealer:
    """现有类 - 添加label自愈能力"""

    def ensure_labels_ready(
        self,
        symbols: List[str],
        horizons: List[int] = [30],
        lookback_days: int = 90
    ) -> dict:
        """
        确保labels就绪 - 类似ensure_data_ready的设计

        Args:
            symbols: 需要labels的symbols
            horizons: label时间窗口 (分钟)
            lookback_days: 回填多少天的历史

        Returns:
            {
                "status": "fixed" | "failed" | "already_ok",
                "missing_symbols": [...],
                "generated_rows": 123456,
                "errors": [...]
            }
        """
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        logger.info(
            "ensure_labels_ready_start",
            symbols=symbols,
            horizons=horizons,
            lookback_days=lookback_days
        )

        result = {
            "status": "already_ok",
            "missing_symbols": [],
            "generated_rows": 0,
            "errors": []
        }

        # 1. 检查哪些symbol缺少labels
        client = get_client()
        for symbol in symbols:
            query = f"""
            SELECT count() as cnt
            FROM trading.features_1m_with_labels
            WHERE symbol = '{symbol}'
            """
            data = client.query(query)
            if not data or data[0]['cnt'] == 0:
                result["missing_symbols"].append(symbol)

        if not result["missing_symbols"]:
            logger.info("labels_already_exist", symbols=symbols)
            return result

        # 2. 分批生成labels (避免内存溢出)
        result["status"] = "fixing"
        batch_size = 5  # 每批5个symbol

        missing = result["missing_symbols"]
        batches = [missing[i:i+batch_size] for i in range(0, len(missing), batch_size)]

        temp_tables = []

        for batch_idx, batch in enumerate(batches):
            temp_table = f"features_1m_with_labels_temp_batch{batch_idx}"

            try:
                # 调用你现有的generate_labels.py脚本
                cmd = [
                    "venv/bin/python",
                    "pipelines/generate_labels.py",
                    "--symbols", *batch,
                    "--horizons", *[str(h) for h in horizons],
                    "--target-table", temp_table
                ]

                logger.info("generating_labels_batch", batch=batch, cmd=" ".join(cmd))

                subprocess.run(
                    cmd,
                    check=True,
                    timeout=300,  # 5分钟超时
                    capture_output=True,
                    text=True
                )

                temp_tables.append(temp_table)
                logger.info("batch_success", batch=batch, table=temp_table)

            except subprocess.TimeoutExpired:
                # 超时 - 尝试缩小batch
                logger.warning("batch_timeout", batch=batch, action="retry_with_smaller_batch")

                if batch_size > 1:
                    # 递归调用,batch_size减半
                    for symbol in batch:
                        sub_result = self.ensure_labels_ready(
                            symbols=[symbol],
                            horizons=horizons,
                            lookback_days=lookback_days
                        )
                        if sub_result["status"] == "failed":
                            result["errors"].append(f"{symbol}: {sub_result['errors']}")
                else:
                    result["errors"].append(f"{batch}: 单symbol也超时")

            except subprocess.CalledProcessError as e:
                logger.error("batch_failed", batch=batch, error=str(e), stderr=e.stderr)
                result["errors"].append(f"{batch}: {e.stderr}")

        # 3. 合并所有batch到最终表
        if temp_tables and not result["errors"]:
            try:
                self._merge_label_batches(temp_tables, "features_1m_with_labels")

                # 统计生成的行数
                query = "SELECT count() FROM trading.features_1m_with_labels"
                data = client.query(query)
                result["generated_rows"] = data[0]['count()'] if data else 0

                result["status"] = "fixed"
                logger.info("labels_generated_success", rows=result["generated_rows"])

            except Exception as e:
                logger.error("merge_failed", error=str(e))
                result["errors"].append(f"合并失败: {e}")
                result["status"] = "failed"
        else:
            result["status"] = "failed"

        return result

    def _merge_label_batches(self, temp_tables: List[str], final_table: str):
        """合并临时label表到最终表"""
        from libs.db.ch_pool import get_client

        client = get_client()

        # 检查最终表是否存在
        table_exists = client.query(f"EXISTS TABLE trading.{final_table}")

        if not table_exists or table_exists[0]['result'] == 0:
            # 从第一个batch创建表
            client.command(f"""
                CREATE TABLE trading.{final_table}
                ENGINE = MergeTree()
                ORDER BY (symbol, ts)
                AS SELECT * FROM trading.{temp_tables[0]}
            """)
            logger.info("created_final_table", table=final_table, from_batch=temp_tables[0])
            start_idx = 1
        else:
            start_idx = 0

        # 插入剩余batches
        for temp_table in temp_tables[start_idx:]:
            client.command(f"""
                INSERT INTO trading.{final_table}
                SELECT * FROM trading.{temp_table}
            """)
            logger.info("inserted_batch", from_table=temp_table, to_table=final_table)

        # 清理临时表
        for temp_table in temp_tables:
            client.command(f"DROP TABLE IF EXISTS trading.{temp_table}")
            logger.info("dropped_temp_table", table=temp_table)
```

**使用方式**:
```python
# 在PreFlight失败后自动调用
healer = DataHealer(runs_dir="runs/ai_trader")
result = healer.ensure_labels_ready(
    symbols=["TSLA", "NVDA", "SPY"],
    horizons=[30],
    lookback_days=90
)

if result["status"] == "fixed":
    print(f"✅ 自动修复成功,生成{result['generated_rows']}行labels")
else:
    print(f"❌ 修复失败: {result['errors']}")
```

---

### Phase 2: Brain智能决策 (2周内) 🧠

**目标**: Brain能主动发现label问题并请求修复

#### 2.1 Brain观察到label_health

Brain现在context里有:
```python
context["data_quality_snapshot"]["label_health"] = {
    "summary": {
        "missing_symbols": ["NVDA", "AMD"],
        "stale_symbols": ["MSFT"],
        "overall_status": "degraded"
    }
}
```

#### 2.2 添加新的Action: `queue_label_recompute`

**文件**: `scripts/ai_trader/brain/actions.py` (或你定义actions的地方)

```python
# Brain可以输出的新action
{
    "action": "queue_job",
    "params": {
        "job_type": "label_recompute",
        "params": {
            "symbols": ["NVDA", "AMD"],
            "horizons": [30],
            "lookback_days": 90,
            "priority": "high"
        }
    },
    "reasoning": "检测到NVDA和AMD的labels缺失,请求后台重新生成"
}
```

#### 2.3 Orchestrator处理label_recompute job

**文件**: `scripts/ai_trader/orchestrator.py`

```python
def handle_job(job: dict):
    """现有函数 - 添加label_recompute处理"""

    if job["job_type"] == "label_recompute":
        params = job["params"]

        logger.info("executing_label_recompute_job", params=params)

        # 调用DataHealer
        healer = DataHealer(runs_dir="runs/ai_trader")
        result = healer.ensure_labels_ready(
            symbols=params["symbols"],
            horizons=params.get("horizons", [30]),
            lookback_days=params.get("lookback_days", 90)
        )

        # 记录结果
        write_ml_event({
            "type": "label_recompute_completed",
            "job_id": job["id"],
            "result": result,
            "timestamp": datetime.now().isoformat()
        })

        return result

    # ... 处理其他job types ...
```

**效果**: Brain → Orchestrator → DataHealer,保持你的"Brain不直接操作系统"原则

---

### Phase 3: 诊断与监控 (1个月内) 📊

#### 3.1 AUC异常诊断器

**文件**: `scripts/ai_trader/brain/diagnostics.py` (新建)

```python
"""
AUC异常诊断 - 当AUC=0.5时自动定位根因

集成到OOF评估后
"""

def diagnose_auc_anomaly(auc: float, symbols: List[str]) -> dict:
    """
    诊断AUC异常 - 简单决策树

    Returns:
        {
            "is_anomaly": bool,
            "root_cause": str | None,
            "recommended_action": dict | None
        }
    """
    # 1. 判断是否异常
    if not (0.49 < auc < 0.51):
        return {"is_anomaly": False}

    logger.warning("auc_anomaly_detected", auc=auc, symbols=symbols)

    diagnosis = {
        "is_anomaly": True,
        "root_cause": None,
        "recommended_action": None
    }

    client = get_client()

    # 2. 检查labels是否缺失
    query = f"""
    SELECT count() as cnt
    FROM trading.features_1m_with_labels
    WHERE symbol IN {tuple(symbols) if len(symbols) > 1 else f"('{symbols[0]}')"}
    """
    data = client.query(query)
    label_count = data[0]['cnt'] if data else 0

    if label_count == 0:
        diagnosis["root_cause"] = "MISSING_LABELS"
        diagnosis["recommended_action"] = {
            "action": "queue_job",
            "params": {
                "job_type": "label_recompute",
                "params": {
                    "symbols": symbols,
                    "horizons": [30],
                    "lookback_days": 90
                }
            }
        }
        return diagnosis

    # 3. 检查labels是否全NULL
    query = f"""
    SELECT
        countIf(label_h30 IS NULL) as null_count,
        count() as total
    FROM trading.features_1m_with_labels
    WHERE symbol IN {tuple(symbols) if len(symbols) > 1 else f"('{symbols[0]}')"}
    """
    data = client.query(query)
    null_ratio = data[0]['null_count'] / data[0]['total'] if data and data[0]['total'] > 0 else 1.0

    if null_ratio > 0.9:
        diagnosis["root_cause"] = "NULL_LABELS"
        diagnosis["recommended_action"] = {
            "action": "queue_job",
            "params": {
                "job_type": "label_recompute",
                "params": {
                    "symbols": symbols,
                    "horizons": [30],
                    "lookback_days": 90
                }
            }
        }
        return diagnosis

    # 4. 检查训练样本量
    if label_count < 1000:
        diagnosis["root_cause"] = "INSUFFICIENT_DATA"
        diagnosis["recommended_action"] = {
            "action": "extend_lookback",
            "params": {"lookback_days": 180}
        }
        return diagnosis

    # 5. 未知原因
    diagnosis["root_cause"] = "UNKNOWN"
    diagnosis["recommended_action"] = None

    return diagnosis
```

**集成到OOF评估后**:
```python
# 在pipelines/train_with_ic_objective.py或ml_scheduler.py

oof_metrics = evaluate_oof(model, X_val, y_val)
auc = oof_metrics["auc"]

# === 新增: AUC异常诊断 ===
from brain.diagnostics import diagnose_auc_anomaly

diagnosis = diagnose_auc_anomaly(auc, symbols)

if diagnosis["is_anomaly"]:
    logger.error(
        "auc_anomaly_diagnosed",
        auc=auc,
        root_cause=diagnosis["root_cause"],
        recommended_action=diagnosis["recommended_action"]
    )

    # 自动执行推荐动作 (可选)
    if diagnosis["recommended_action"]:
        # 写入job队列,让Orchestrator处理
        enqueue_job(diagnosis["recommended_action"])
```

---

#### 3.2 简单监控指标 (可选,不强制Prometheus)

**文件**: `tools/export_metrics.py` (新建,定时运行)

```python
"""
导出关键指标到JSON - 供Grafana或自定义dashboard使用

Cron: 每5分钟运行一次
"""

def export_metrics_to_json():
    """导出指标到JSON文件"""
    from tools.monitor_system import generate_health_report

    report = generate_health_report()

    # 提取关键指标
    metrics = {
        "timestamp": datetime.now().isoformat(),
        "label_table_rows": report["label_health"]["summary"]["total_rows"],
        "missing_symbols_count": len(report["label_health"]["summary"]["missing_symbols"]),
        "stale_symbols_count": len(report["label_health"]["summary"]["stale_symbols"]),
        "features_age_hours": report.get("features_age_hours", None),  # 需要monitor添加
        "clickhouse_healthy": report["clickhouse"]["status"] == "healthy"
    }

    # 写入文件
    output_path = Path("runs/metrics/latest.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w') as f:
        json.dump(metrics, f, indent=2)

    print(f"✅ Metrics exported to {output_path}")
    return metrics

if __name__ == "__main__":
    export_metrics_to_json()
```

**Cron配置**:
```bash
# 每5分钟导出一次指标
*/5 * * * * cd /app && venv/bin/python tools/export_metrics.py
```

**简单的告警脚本** (可选):
```python
# tools/check_alerts.py

def check_alerts():
    """检查指标,超阈值则发送告警"""
    with open("runs/metrics/latest.json") as f:
        metrics = json.load(f)

    alerts = []

    # Alert 1: Labels表为空
    if metrics["label_table_rows"] == 0:
        alerts.append({
            "severity": "critical",
            "message": "Labels表完全为空,ML训练将失败",
            "action": "运行 venv/bin/python pipelines/generate_labels.py"
        })

    # Alert 2: 有缺失的symbols
    if metrics["missing_symbols_count"] > 0:
        alerts.append({
            "severity": "warning",
            "message": f"{metrics['missing_symbols_count']}个symbols缺少labels",
            "action": "检查 monitor_system.py 输出"
        })

    # 发送告警 (Slack/Email/Discord)
    if alerts:
        send_alerts_to_slack(alerts)  # 你可以实现

    return alerts
```

---

## 三、对比原方案的裁剪

### 保留的核心思想 ✅
1. **PreFlightValidator** - 训练前强制检查
2. **IntelligentDataHealer** 的label分批生成逻辑
3. **AUC异常诊断** - 0.5自动定位根因
4. **label_health监控** - 集成到现有monitor

### 裁剪/延后的部分 ⏸️
1. ❌ Prefect/Airflow DAG - 暂用cron + 现有job queue
2. ❌ FailureLearningSystem (RL自学习) - Phase 4+
3. ❌ PredictiveMaintenance - Phase 4+
4. ❌ 大规模async改造 - 保持同步风格
5. ⏸️ Prometheus/Grafana - 先用JSON metrics,有需要再上

### 复用现有系统 🔄
1. ✅ DataHealer - 直接扩展`ensure_labels_ready`
2. ✅ monitor_system.py - 添加`check_label_health`
3. ✅ Brain/Orchestrator - 添加`label_recompute` action
4. ✅ ml_events.jsonl - 记录所有preflight结果

---

## 四、实施清单

### Week 1 (本周) - Phase 1 完成
- [ ] `tools/monitor_system.py` 添加 `check_label_health()`
- [ ] Brain context集成 `label_health`
- [ ] 创建 `scripts/ai_trader/brain/preflight.py`
- [ ] `ml_scheduler.py` 集成PreFlight检查
- [ ] `services/data/data_healer.py` 添加 `ensure_labels_ready()`
- [ ] 测试: 模拟labels缺失 → 训练被阻塞 → 自动修复

### Week 2 - Phase 2 完成
- [ ] Brain添加 `queue_label_recompute` action
- [ ] Orchestrator处理label_recompute job
- [ ] 端到端测试: Brain发现问题 → 请求修复 → Orchestrator执行

### Week 3-4 - Phase 3 完成
- [ ] 创建 `brain/diagnostics.py` (AUC诊断)
- [ ] 集成到OOF评估后
- [ ] 创建 `tools/export_metrics.py`
- [ ] 配置cron定时导出
- [ ] (可选) 创建简单告警脚本

---

## 五、验证方式

### 测试场景1: Labels缺失
```bash
# 1. 删除labels表
clickhouse-client --query "TRUNCATE TABLE trading.features_1m_with_labels"

# 2. 触发OOF训练
venv/bin/python scripts/ai_trader/ml_scheduler.py --job-type oof --symbols TSLA

# 3. 预期行为:
# - PreFlight检查失败,记录到ml_events.jsonl
# - 自动调用DataHealer.ensure_labels_ready()
# - 分批生成labels
# - 重新检查通过
# - 训练正常进行
```

### 测试场景2: AUC=0.5异常
```bash
# 1. 手动构造空labels场景 (同上)

# 2. 强制跳过PreFlight,直接训练 (测试诊断器)

# 3. 训练后AUC=0.5

# 4. 预期行为:
# - diagnose_auc_anomaly() 判断为MISSING_LABELS
# - 输出recommended_action: label_recompute
# - (可选) 自动enqueue job
```

### 测试场景3: Brain主动请求修复
```bash
# 1. 删除某几个symbol的labels

# 2. Brain循环运行

# 3. 预期行为:
# - context里看到label_health.missing_symbols = ["NVDA", "AMD"]
# - Brain输出action: queue_label_recompute
# - Orchestrator执行DataHealer
# - 下次循环label_health恢复正常
```

---

## 六、成功指标

### 短期 (1个月)
- [ ] 训练前PreFlight检查覆盖率 = 100%
- [ ] Labels缺失自动修复成功率 ≥ 80%
- [ ] 手动干预次数 < 5次/月

### 中期 (3个月)
- [ ] AUC=0.5异常发生率 = 0次
- [ ] Label数据新鲜度 < 6小时
- [ ] Brain自主请求修复 ≥ 3次/月

### 长期 (6个月)
- [ ] 完全自主运行,无人工干预
- [ ] 数据质量监控dashboard上线
- [ ] 考虑引入DAG/Prefect (如果job复杂度增加)

---

## 七、总结

### 核心改进
1. **PreFlight Check** - 彻底杜绝"空数据训练"
2. **DataHealer扩展** - 复用现有架构,添加label自愈
3. **Brain集成** - 暴露label_health,支持主动修复
4. **AUC诊断** - 0.5异常自动定位根因

### 设计原则
- ✅ 渐进式,不推翻重来
- ✅ 复用现有系统 (DataHealer/Brain/Orchestrator)
- ✅ 保持简单 (同步,无新框架)
- ✅ 可测试,可验证

### 与原方案对比
| 维度 | 原方案 | 实用版 |
|------|--------|--------|
| 复杂度 | 高 (新类+async+Prefect) | 低 (扩展现有) |
| 实施周期 | 2-3个月 | 2-4周 |
| 风险 | 中-高 (大改) | 低 (增量) |
| 可维护性 | 需要学习新框架 | 延续现有风格 |

---

**版本**: 2.0 (Pragmatic)
**维护者**: Claude Code
**最后更新**: 2025-11-25
