# AI大脑统一升级方案：从"盲目修复"到"智能自愈"

**方案版本**: V2.0 (综合Claude P0 + ChatGPT架构方案)
**制定日期**: 2025-11-26
**目标**: 建立统一的"健康感知 → 诊断 → 自愈 → 决策"控制平面

---

## 🎯 核心理念

> **不再是"补丁式修复"，而是建立统一的控制平面**

### 当前问题（架构层面）

1. ✅ **管线是通的**（bars → features → labels → train）
2. ❌ **但健康状态不统一**（各模块各说各话）
3. ❌ **Preflight只在训练阶段**（问题延迟爆炸）
4. ❌ **配置与Schema感知是手动的**（171次HPO才发现）
5. ❌ **诊断和修复没有标准协议**（Brain无法消费）

### 解决方案（两个方案的结合）

**Claude P0方案优点**:
- ✅ 立即可用（已实现并验证）
- ✅ 具体工具已落地（diagnose_oof_failure.py等）
- ✅ 有证据验证（AUC 0.47 → 0.70）

**ChatGPT方案优点**:
- ✅ 架构更系统化（统一控制平面）
- ✅ 协议标准化（health_snapshot / issue / diagnosis）
- ✅ 长期可演进（不需要反复重构）

**结合策略**:
- **Phase 0-1**: 使用Claude P0已实现的工具（立即见效）
- **Phase 2-4**: 按ChatGPT方案逐步重构成统一控制平面

---

## 📋 完整升级路线图

### Phase 0: 立即部署 (已完成✅)

**时间**: 1天
**状态**: ✅ 完成

#### 已落地组件

1. ✅ `tools/diagnose_oof_failure.py` (380行)
   - 检查数据利用率
   - 检查label列存在性
   - 检查label质量
   - 输出根因和修复建议

2. ✅ `tools/check_ml_config_health.py` (320行)
   - 启动时检查配置一致性
   - 验证label列存在性

3. ✅ 集成到 `control_event_consumer.py`
   - OOF失败时自动诊断

4. ✅ 集成到 `orchestrator.py`
   - 启动时配置健康检查

#### 验证结果

```
label_h5:   AUC=0.47, IC=-0.08 ❌
label_h120: AUC=0.70, IC=0.37  ✅ (+48% AUC, +5x IC)
```

---

### Phase 1: 统一健康快照 (3-4天)

**目标**: 建立统一的`SystemHealthSnapshot`协议

#### 1.1 定义标准数据结构

**新增文件**: `libs/health/snapshot.py`

```python
"""
统一的系统健康快照协议

所有Monitor / Preflight / Diagnostics输出相同结构，便于Brain消费
"""

from typing import Dict, List, Any, Literal
from dataclasses import dataclass, asdict
from datetime import datetime


@dataclass
class BarHealthStatus:
    """Bars健康状态"""
    status: Literal["healthy", "degraded", "blocked"]
    latest_ts: str
    age_hours: float
    missing_symbols: List[str]
    coverage_pct: float


@dataclass
class FeatureHealthStatus:
    """Features健康状态"""
    status: Literal["healthy", "degraded", "blocked"]
    latest_ts: str
    age_hours: float
    null_ratio: float
    rebuild_needed: bool


@dataclass
class LabelSymbolStatus:
    """单个Symbol的Label状态"""
    symbol: str
    horizon_min: int
    rows: int
    null_ratio: float
    zero_ratio: float
    age_hours: float
    std_bps: float
    status: Literal["healthy", "degraded", "blocked"]
    issues: List[str]


@dataclass
class LabelHealthStatus:
    """Labels总体健康状态"""
    status: Literal["healthy", "degraded", "blocked"]
    by_symbol: Dict[str, LabelSymbolStatus]
    summary: Dict[str, Any]


@dataclass
class ConfigHealthStatus:
    """配置健康状态"""
    status: Literal["healthy", "degraded", "blocked"]
    problems: List[Dict[str, Any]]


@dataclass
class SystemHealthSnapshot:
    """系统总体健康快照"""
    timestamp: str
    symbols: List[str]
    horizons: List[int]

    bars: BarHealthStatus
    features: FeatureHealthStatus
    labels: LabelHealthStatus
    config: ConfigHealthStatus

    overall_status: Literal["healthy", "degraded", "blocked"]
    blocking_issues: List[str]

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（供JSON序列化）"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SystemHealthSnapshot":
        """从字典构建"""
        # ... 实现反序列化逻辑
        pass


def build_system_health(
    symbols: List[str],
    horizons: List[int],
    table: str = "trading.features_1m_with_labels"
) -> SystemHealthSnapshot:
    """
    构建系统健康快照（统一入口）

    整合所有健康检查：
    - monitor_system.py 的 bars/features检查
    - check_label_health 的 labels检查
    - check_ml_config_health 的配置检查

    Returns:
        SystemHealthSnapshot对象
    """

    from tools.monitor_system import SystemMonitor
    from tools.check_ml_config_health import check_ml_config_health
    import clickhouse_connect

    cli = clickhouse_connect.get_client(...)
    monitor = SystemMonitor()

    # 1. Bars健康检查
    bars_status = _check_bars_health(cli, symbols)

    # 2. Features健康检查
    features_status = _check_features_health(cli, symbols)

    # 3. Labels健康检查
    labels_status = _check_labels_health(cli, symbols, horizons, table)

    # 4. Config健康检查
    config_status = _check_config_health()

    # 汇总总体状态
    overall_status = "healthy"
    blocking_issues = []

    if any(s.status == "blocked" for s in [bars_status, features_status, labels_status, config_status]):
        overall_status = "blocked"

    if bars_status.status == "blocked":
        blocking_issues.append(f"bars: {bars_status.missing_symbols}")

    if labels_status.status == "blocked":
        blocking_issues.extend([
            f"{sym_status.symbol}: {', '.join(sym_status.issues)}"
            for sym_status in labels_status.by_symbol.values()
            if sym_status.status == "blocked"
        ])

    return SystemHealthSnapshot(
        timestamp=datetime.now().isoformat(),
        symbols=symbols,
        horizons=horizons,
        bars=bars_status,
        features=features_status,
        labels=labels_status,
        config=config_status,
        overall_status=overall_status,
        blocking_issues=blocking_issues
    )
```

#### 1.2 重构现有Monitor工具

**修改**: `tools/monitor_system.py`

```python
# 原来的check_label_health改为返回LabelHealthStatus对象
def check_label_health(symbols, horizons, table) -> LabelHealthStatus:
    """
    检查Labels健康度，返回标准化结构

    Returns:
        LabelHealthStatus对象（可直接填入SystemHealthSnapshot）
    """
    from libs.health.snapshot import LabelHealthStatus, LabelSymbolStatus

    by_symbol = {}

    for symbol in symbols:
        for horizon in horizons:
            label_col = f"label_h{horizon}"

            # ... 执行现有检查逻辑 ...

            status = LabelSymbolStatus(
                symbol=symbol,
                horizon_min=horizon,
                rows=row_count,
                null_ratio=null_ratio,
                zero_ratio=zero_ratio,
                age_hours=age_hours,
                std_bps=std_bps,
                status="healthy" if is_healthy else "blocked",
                issues=issues
            )

            by_symbol[f"{symbol}_h{horizon}"] = status

    # 计算summary
    overall_status = "blocked" if any(s.status == "blocked" for s in by_symbol.values()) else "healthy"

    return LabelHealthStatus(
        status=overall_status,
        by_symbol=by_symbol,
        summary={"total_symbols": len(symbols), "total_horizons": len(horizons)}
    )
```

#### 1.3 统一Preflight接口

**新增/修改**: `tools/preflight_check.py`

```python
"""
统一的Preflight检查（所有训练前必过的门卫）

不再只在OOF入口使用，而是所有训练脚本统一调用
"""

from typing import Tuple, Dict, Any, List
from libs.health.snapshot import SystemHealthSnapshot, build_system_health


def preflight_check_all(
    symbols: List[str],
    horizons: List[int] = [120],
    min_rows: int = 1000,
    max_null_ratio: float = 0.1
) -> Tuple[bool, SystemHealthSnapshot]:
    """
    训练前综合Preflight检查

    Args:
        symbols: 要训练的股票列表
        horizons: 要使用的label horizons
        min_rows: 最小行数要求
        max_null_ratio: 最大NULL比例

    Returns:
        (ready, health_snapshot)
        - ready: True if 可以训练
        - health_snapshot: 完整健康快照
    """

    # 构建统一健康快照
    health = build_system_health(symbols, horizons)

    ready = True
    reasons = []

    # 检查总体状态
    if health.overall_status == "blocked":
        ready = False
        reasons.extend(health.blocking_issues)

    # 检查Labels具体要求
    for label_status in health.labels.by_symbol.values():
        if label_status.rows < min_rows:
            ready = False
            reasons.append(f"{label_status.symbol}_h{label_status.horizon_min}: rows {label_status.rows} < {min_rows}")

        if label_status.null_ratio > max_null_ratio:
            ready = False
            reasons.append(f"{label_status.symbol}_h{label_status.horizon_min}: null_ratio {label_status.null_ratio:.2%} > {max_null_ratio:.2%}")

    if not ready:
        print("❌ Preflight FAILED:")
        for reason in reasons:
            print(f"  - {reason}")
    else:
        print("✅ Preflight PASSED")

    return ready, health


# 向后兼容的包装函数
def preflight_check(symbols: List[str]) -> Dict[str, Any]:
    """旧版接口，返回dict（向后兼容）"""
    ready, health = preflight_check_all(symbols)

    return {
        "ready": ready,
        "health_snapshot": health.to_dict(),
        "blocked_by": health.blocking_issues if not ready else [],
    }
```

#### 1.4 所有训练脚本统一使用Preflight

**修改**: `pipelines/train_xgb_with_oof.py` 等所有训练脚本

```python
# 在训练开始前统一检查
def main():
    # ... 参数解析 ...

    # 🆕 统一Preflight
    from tools.preflight_check import preflight_check_all

    ready, health = preflight_check_all(
        symbols=[args.symbol],
        horizons=[args.horizon_min]
    )

    if not ready:
        # 写入ml_event
        with open("runs/ai_trader/<date>/ml_events.jsonl", "a") as f:
            f.write(json.dumps({
                "event": "training_preflight_failed",
                "timestamp": datetime.now().isoformat(),
                "health_snapshot": health.to_dict(),
                "blocking_issues": health.blocking_issues
            }) + "\n")

        logger.error("preflight_failed", blocking_issues=health.blocking_issues)
        sys.exit(1)

    # Preflight通过，开始训练
    logger.info("preflight_passed", health_status=health.overall_status)
    # ... 训练逻辑 ...
```

**交付成果**:
- ✅ `libs/health/snapshot.py` (300行) - 统一数据结构
- ✅ 修改 `tools/monitor_system.py` - 返回标准结构
- ✅ 修改 `tools/preflight_check.py` - 统一接口
- ✅ 修改所有训练脚本 - 使用统一Preflight

---

### Phase 2: DataHealer V2 统一自愈 (3-5天)

**目标**: 统一所有修复动作到`DataHealer.heal(issue)`

#### 2.1 定义Issue协议

**新增文件**: `libs/health/issue.py`

```python
"""
统一的Issue协议

所有诊断结果都生成标准化的Issue，可以直接传给DataHealer修复
"""

from typing import Dict, List, Any, Literal
from dataclasses import dataclass, asdict


@dataclass
class Issue:
    """问题描述（可被DataHealer消费）"""

    type: Literal[
        "missing_bars",
        "stale_features",
        "missing_labels",
        "sparse_labels",
        "config_schema_mismatch",
        "insufficient_oof_samples"
    ]

    severity: Literal["critical", "high", "medium", "low"]

    symbols: List[str]
    horizons: List[int] = None
    lookback_days: int = 90

    # 具体问题详情
    details: Dict[str, Any] = None

    # 推荐修复参数
    fix_params: Dict[str, Any] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
```

#### 2.2 重构DataHealer

**修改**: `services/data/data_healer.py`

```python
class DataHealer:
    """
    统一数据自愈入口

    所有修复动作（bars/features/labels/config）都通过heal()统一接口
    """

    def heal(self, issue: Issue) -> Dict[str, Any]:
        """
        统一修复入口

        Args:
            issue: 标准化Issue对象

        Returns:
            {
                "status": "fixed" | "failed" | "skipped",
                "actions": [...],  # 执行的子操作
                "errors": [...],
                "duration_sec": 12.3
            }
        """

        logger.info("heal_issue_started", issue_type=issue.type, severity=issue.severity)

        start_time = time.time()
        actions = []
        errors = []

        try:
            if issue.type == "missing_bars":
                result = self._heal_missing_bars(issue)
                actions.append(result)

            elif issue.type == "stale_features":
                result = self._heal_stale_features(issue)
                actions.append(result)

            elif issue.type in ["missing_labels", "sparse_labels"]:
                result = self._heal_missing_labels(issue)
                actions.append(result)

            elif issue.type == "config_schema_mismatch":
                result = self._heal_config_mismatch(issue)
                actions.append(result)

            else:
                return {
                    "status": "skipped",
                    "reason": f"Unknown issue type: {issue.type}"
                }

            status = "fixed" if all(a.get("success") for a in actions) else "failed"

        except Exception as e:
            logger.error("heal_failed", error=str(e), exc_info=True)
            errors.append(str(e))
            status = "failed"

        duration = time.time() - start_time

        return {
            "status": status,
            "actions": actions,
            "errors": errors,
            "duration_sec": duration
        }

    def _heal_missing_labels(self, issue: Issue) -> Dict[str, Any]:
        """修复缺失的Labels"""

        logger.info("healing_missing_labels",
                    symbols=issue.symbols,
                    horizons=issue.horizons,
                    lookback_days=issue.lookback_days)

        # 调用现有的generate_labels逻辑
        from tools.generate_improved_labels import generate_labels_simple

        cli = clickhouse_connect.get_client(...)

        results = []

        for symbol in issue.symbols:
            start = (datetime.now() - timedelta(days=issue.lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
            end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            result = generate_labels_simple(
                cli, symbol, start, end, issue.horizons
            )

            results.append({
                "symbol": symbol,
                "horizons": issue.horizons,
                "rows_generated": result,
                "success": all(v > 0 for v in result.values())
            })

        return {
            "action": "generate_labels",
            "results": results,
            "success": all(r["success"] for r in results)
        }

    def _heal_config_mismatch(self, issue: Issue) -> Dict[str, Any]:
        """修复配置不匹配（例如：horizon不存在）"""

        logger.info("healing_config_mismatch", details=issue.details)

        # 策略1: 生成缺失的label列（如果horizon <= 30）
        # 策略2: 修改配置使用已有label（如果horizon > 30）

        if issue.fix_params and issue.fix_params.get("action") == "generate_missing_label":
            return self._heal_missing_labels(issue)

        elif issue.fix_params and issue.fix_params.get("action") == "switch_to_existing_label":
            # 修改oof_policies.json
            new_horizon = issue.fix_params["new_horizon_min"]

            config_file = "config/oof_policies.json"
            with open(config_file) as f:
                config = json.load(f)

            config["defaults"]["horizon_min"] = new_horizon

            with open(config_file, "w") as f:
                json.dump(config, f, indent=2)

            return {
                "action": "update_config",
                "file": config_file,
                "new_horizon_min": new_horizon,
                "success": True
            }

        return {"action": "config_mismatch_heal", "success": False, "reason": "No fix strategy"}
```

**交付成果**:
- ✅ `libs/health/issue.py` (80行) - Issue协议
- ✅ 重构 `services/data/data_healer.py` - 统一heal()接口

---

### Phase 3: 诊断标准化 (3-5天)

**目标**: 所有诊断输出统一root_cause + Issue

#### 3.1 定义Root Cause枚举

**修改**: `libs/health/issue.py` (追加)

```python
class RootCause:
    """标准化根因枚举"""

    MISSING_LABEL_COLUMN = "missing_label_column"       # 配置引用label不存在
    SPARSE_LABEL_DATA = "sparse_label_data"             # label存在但有效行太少
    NULL_LABELS = "null_labels"                         # null比例极高
    LOW_LABEL_STD = "low_label_std"                     # 标准差太低（噪声太大）
    STALE_FEATURES = "stale_features"                   # features过期
    MISSING_BARS = "missing_bars"                       # bars缺失
    BAD_CONFIG = "bad_config"                           # policy与schema不匹配
    INSUFFICIENT_OOF_SAMPLES = "insufficient_oof_samples"
    LOW_DATA_UTILIZATION = "low_data_utilization"       # 训练数据利用率过低
    UNKNOWN = "unknown"


@dataclass
class Diagnosis:
    """统一诊断结果"""

    stage: Literal["bars", "features", "labels", "config", "training"]
    root_cause: str  # RootCause中的一个
    severity: Literal["critical", "high", "medium", "low"]

    evidence: Dict[str, Any]  # 证据（现有diagnose_oof_failure的输出）

    recommended_issue: Issue = None  # 可直接丢给DataHealer.heal()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
```

#### 3.2 重构诊断工具

**修改**: `tools/diagnose_oof_failure.py`

```python
def diagnose_oof_failure(...) -> Diagnosis:
    """
    诊断OOF失败（返回标准Diagnosis对象）
    """

    from libs.health.issue import Diagnosis, Issue, RootCause

    # ... 执行现有诊断逻辑 ...

    # 确定root_cause
    if label_col not in existing_labels:
        root_cause = RootCause.MISSING_LABEL_COLUMN
        severity = "critical"

        recommended_issue = Issue(
            type="config_schema_mismatch",
            severity="critical",
            symbols=[symbol],
            horizons=[horizon_min],
            details={"missing_label": label_col, "existing": existing_labels},
            fix_params={"action": "generate_missing_label"}
        )

    elif utilization_rate < 0.3:
        root_cause = RootCause.LOW_DATA_UTILIZATION
        severity = "critical"

        recommended_issue = Issue(
            type="sparse_labels",
            severity="critical",
            symbols=[symbol],
            horizons=[horizon_min],
            lookback_days=90,
            details={"utilization_rate": utilization_rate, "expected_n": expected_n, "actual_n": actual_n}
        )

    elif label_stats["std_bps"] < 50:
        root_cause = RootCause.LOW_LABEL_STD
        severity = "high"

        recommended_issue = Issue(
            type="sparse_labels",
            severity="high",
            symbols=[symbol],
            horizons=[30, 60, 120],  # 建议测试更长horizon
            details={"std_bps": label_stats["std_bps"], "zero_rate": label_stats["zero_rate"]}
        )

    else:
        root_cause = RootCause.UNKNOWN
        severity = "medium"
        recommended_issue = None

    return Diagnosis(
        stage="training",
        root_cause=root_cause,
        severity=severity,
        evidence={
            "symptoms": {"auc": auc, "ic": ic, "n_train": n_train},
            "diagnosis": diagnosis_list,  # 保留原有详细诊断
            "label_stats": label_stats
        },
        recommended_issue=recommended_issue
    )
```

**交付成果**:
- ✅ 扩展 `libs/health/issue.py` - 增加RootCause和Diagnosis
- ✅ 重构 `tools/diagnose_oof_failure.py` - 返回标准Diagnosis

---

### Phase 4: Orchestrator & Brain集成 (3-5天)

**目标**: 打通"诊断 → 修复 → 决策"全链路

#### 4.1 Orchestrator统一处理Issue

**修改**: `scripts/ai_trader/control_event_consumer.py`

```python
async def _handle_ml_completed(evt: Dict[str, Any]) -> None:
    """处理ML任务完成事件（已有函数，增强诊断和修复逻辑）"""

    # ... 现有逻辑 ...

    # 🆕 当OOF失败时运行诊断
    if job_type == "oof" and (auc < 0.55 or abs(ic) < 0.1):
        from tools.diagnose_oof_failure import diagnose_oof_failure
        from services.data.data_healer import DataHealer

        diagnosis = diagnose_oof_failure(...)  # 返回Diagnosis对象

        # 记录诊断结果到ml_events
        with open(f"runs/ai_trader/{date}/ml_events.jsonl", "a") as f:
            f.write(json.dumps({
                "event": "oof_diagnosis_completed",
                "timestamp": datetime.now().isoformat(),
                "job_id": job_id,
                "diagnosis": diagnosis.to_dict()
            }) + "\n")

        # 🆕 如果有推荐修复，尝试自动修复（可选，可通过配置开关）
        if diagnosis.recommended_issue and AUTO_HEAL_ENABLED:
            logger.info("auto_heal_triggered", root_cause=diagnosis.root_cause)

            healer = DataHealer(str(runs_dir))
            heal_result = healer.heal(diagnosis.recommended_issue)

            # 记录修复结果
            with open(f"runs/ai_trader/{date}/ml_events.jsonl", "a") as f:
                f.write(json.dumps({
                    "event": "auto_heal_completed",
                    "timestamp": datetime.now().isoformat(),
                    "job_id": job_id,
                    "root_cause": diagnosis.root_cause,
                    "heal_result": heal_result
                }) + "\n")

            # 如果修复成功，可以考虑重新触发训练
            if heal_result["status"] == "fixed":
                logger.info("auto_heal_successful_retry_training", job_id=job_id)
                # TODO: 重新触发OOF训练

        # 将诊断结果附加到ml_ctx，传递给Brain
        ml_ctx_cur["oof_diagnosis"] = diagnosis.to_dict()
        ml_ctx_cur["heal_result"] = heal_result if diagnosis.recommended_issue else None
```

#### 4.2 Brain智能决策

**修改**: `scripts/ai_trader/brain/brain.py` 的 `ML_RESULT_REVIEW_PROMPT`

```python
ML_RESULT_REVIEW_PROMPT = (
    "You are an ML result reviewer for a trading system. Return ONLY strict JSON.\n"
    # ... 现有prompt ...

    # 🆕 增加健康快照和诊断结果的说明
    "INPUT will include:\n"
    "- health_snapshot: Current system health (bars/features/labels/config status)\n"
    "- oof_diagnosis: Root cause analysis when OOF fails (root_cause + evidence + recommended_issue)\n"
    "- heal_result: Automatic healing attempt result (if any)\n"
    "\n"
    "DECISION RULES:\n"
    "1. If health_snapshot.overall_status == 'blocked':\n"
    "   → DO NOT suggest training/HPO\n"
    "   → Suggest queue_job with job_type='auto_heal' and issue from diagnosis\n"
    "\n"
    "2. If oof_diagnosis.root_cause in ['missing_label_column', 'config_schema_mismatch']:\n"
    "   → Suggest config update or label generation\n"
    "   → DO NOT keep running experiments with broken config\n"
    "\n"
    "3. If oof_diagnosis.root_cause == 'low_label_std':\n"
    "   → Suggest testing longer horizons (30m → 60m → 120m)\n"
    "   → Provide experiment plan with label_grid\n"
    "\n"
    "4. If heal_result.status == 'fixed':\n"
    "   → Acknowledge the fix\n"
    "   → Suggest retry training with fixed data\n"
    "\n"
    "5. If heal_result.status == 'failed':\n"
    "   → Escalate to human (pause_ml + alert)\n"
)
```

**交付成果**:
- ✅ 修改 `control_event_consumer.py` - 诊断→修复→记录全流程
- ✅ 修改 `brain.py` - Brain理解诊断结果和修复状态

---

### Phase 5: 文档与冻结 (1-2天)

#### 5.1 完整架构文档

**新增文件**: `docs/ai_brain_v2_control_plane.md`

记录：
- SystemHealthSnapshot协议
- Issue协议
- Diagnosis协议
- DataHealer.heal()接口
- Preflight统一规范
- Brain决策逻辑

#### 5.2 接下来3-6个月的约定

**架构冻结**:
- ✅ DataHealer / Preflight / Brain / Orchestrator 骨架不再大改
- ✅ 只做增量：新feature / 新label / 新策略 / bugfix

**禁止的操作**:
- ❌ 不再引入新的health check工具（统一用build_system_health）
- ❌ 不再引入新的修复脚本（统一用DataHealer.heal）
- ❌ 不再引入新的诊断逻辑（统一用Diagnosis协议）

---

## 📊 升级前后对比

| 维度 | 升级前 | Phase 0-1完成后 | Phase 2-4完成后 |
|------|--------|----------------|----------------|
| **健康感知** | ❌ 分散 | ⚠️ 部分统一 | ✅ 完全统一（SystemHealthSnapshot） |
| **Preflight** | ❌ 仅训练阶段 | ⚠️ 训练阶段增强 | ✅ 全管线统一门卫 |
| **诊断能力** | ❌ 无 | ✅ diagnose_oof_failure | ✅ 标准Diagnosis协议 |
| **修复协议** | ❌ 无 | ⚠️ 各自独立 | ✅ 统一Issue→DataHealer.heal |
| **Brain智能** | ❌ 盲猜backfill | ⚠️ 看到诊断 | ✅ 理解诊断+修复状态 |
| **自愈能力** | ❌ 无 | ⚠️ 手动触发 | ✅ 自动诊断+修复 |

---

## 🔬 验收标准

### Phase 0-1验收

- [x] 诊断工具能识别label_h5问题
- [x] 配置健康检查能检测schema不匹配
- [x] 证明label_h120比label_h5好48%

### Phase 1验收

- [ ] `build_system_health()`能返回完整SystemHealthSnapshot
- [ ] 所有训练脚本使用统一Preflight
- [ ] Preflight失败时训练被阻止且写入ml_events

### Phase 2验收

- [ ] `DataHealer.heal()`能处理所有Issue类型
- [ ] 修复missing_labels成功率100%
- [ ] 修复config_mismatch成功率100%

### Phase 3验收

- [ ] 诊断工具返回标准Diagnosis对象
- [ ] RootCause覆盖所有已知问题类型
- [ ] Diagnosis包含可执行的recommended_issue

### Phase 4验收

- [ ] Orchestrator能自动执行诊断→修复
- [ ] Brain能理解诊断结果并做决策
- [ ] 完整流程：OOF失败 → 诊断 → 修复 → 重试 → 成功

---

## 💡 关键设计原则

1. **渐进式演进**
   - Phase 0立即可用（已完成）
   - Phase 1-4逐步重构
   - 每个Phase都可独立交付

2. **向后兼容**
   - 保留旧接口（preflight_check返回dict）
   - 新代码优先使用新协议
   - 逐步迁移，不强制一次性切换

3. **标准化优先**
   - 统一数据结构（dataclass）
   - 统一协议（Issue/Diagnosis/Snapshot）
   - 统一接口（build_system_health / DataHealer.heal）

4. **可测试性**
   - 每个Phase有明确验收标准
   - 所有协议都有to_dict/from_dict
   - 便于单元测试和集成测试

5. **最小侵入**
   - 不改现有核心逻辑（FeatureEngineering等）
   - 只增加控制层和协议层
   - Brain prompt增量修改，不推翻重写

---

## 🚀 立即行动

**当前状态**: Phase 0完成 ✅

**下一步**:
```bash
# 开始Phase 1: 统一健康快照
1. 创建 libs/health/snapshot.py
2. 重构 tools/monitor_system.py
3. 统一 tools/preflight_check.py
4. 修改所有训练脚本使用新Preflight

预计时间: 3-4天
```

**长期目标**:
- 4周内完成Phase 1-4
- 建立统一控制平面
- AI大脑从"盲目修复"进化到"智能自愈"

---

**文档维护**:
- 每个Phase完成后更新本文档
- 记录实际耗时和遇到的问题
- 持续优化协议和接口

**最后更新**: 2025-11-26
**当前Phase**: Phase 0完成，准备Phase 1
