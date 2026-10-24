# AI大脑统一升级 - Phase 1 完成报告

**完成日期**: 2025-11-26
**Phase**: Phase 1 - 统一健康快照
**状态**: ✅ 完成

---

## 🎯 Phase 1 目标

**核心目标**: 建立统一的系统健康快照协议,让所有诊断工具输出标准化结构

**关键成果**:
1. 统一数据结构 (SystemHealthSnapshot)
2. 统一问题描述协议 (Issue/Diagnosis)
3. 重构现有工具返回标准结构
4. 为 Phase 2 (DataHealer v2) 做好准备

---

## 📁 新增文件清单

### 1. 核心协议库 (`libs/health/`)

#### `libs/health/__init__.py` (✅ 新增)
**作用**: 健康监控模块初始化,导出所有协议类

**导出内容**:
```python
# Snapshot 协议
BarHealthStatus, FeatureHealthStatus, LabelSymbolStatus,
LabelHealthStatus, ConfigHealthStatus, SystemHealthSnapshot,
build_system_health

# Issue/Diagnosis 协议
Issue, IssueType, Severity, RootCause, RootCauseType,
Action, Diagnosis,
make_issue_id, make_diagnosis_id, make_action_id,
create_label_missing_diagnosis
```

#### `libs/health/snapshot.py` (✅ 新增 525行)
**作用**: 统一健康快照数据结构

**核心类**:
- `BarHealthStatus`: Bars 健康状态
- `FeatureHealthStatus`: Features 健康状态
- `LabelSymbolStatus`: 单个 symbol-horizon 组合的标签状态
- `LabelHealthStatus`: 整体标签健康状态
- `ConfigHealthStatus`: 配置健康状态
- `SystemHealthSnapshot`: 完整系统健康快照

**核心函数**:
```python
def build_system_health(
    symbols: List[str],
    horizons: List[int] = [120],
    table: str = "trading.features_1m_with_labels"
) -> SystemHealthSnapshot:
    """
    统一入口: 构建完整系统健康快照

    集成所有健康检查:
    - Bars 健康 (最新时间戳, 覆盖率)
    - Features 健康 (数据新鲜度, NULL比率)
    - Labels 健康 (所有 symbol-horizon 组合的质量)
    - Config 健康 (配置与 Schema 一致性)

    返回:
        SystemHealthSnapshot 对象,包含:
        - overall_status: "healthy" / "degraded" / "blocked"
        - blocking_issues: 阻塞问题列表
    """
```

**检查逻辑**:
- `_check_bars_health()`: 检查 bars 数据新鲜度和覆盖率
- `_check_features_health()`: 检查 features 数据质量和 NULL 比率
- `_check_labels_health()`: 检查所有 symbol-horizon 组合的标签质量
- `_check_config_health()`: 验证 `oof_policies.json` 与数据库 Schema 一致性

#### `libs/health/issue.py` (✅ 新增 583行)
**作用**: 问题描述和诊断协议

**核心枚举**:
```python
class IssueType(str, Enum):
    DATA_MISSING, DATA_STALE, DATA_QUALITY_POOR,
    CONFIG_MISMATCH, SCHEMA_MISMATCH,
    PERFORMANCE_DEGRADED, RESOURCE_EXHAUSTED,
    CONNECTIVITY_FAILED, UNKNOWN

class Severity(str, Enum):
    CRITICAL,  # 阻塞生产,需要立即修复
    HIGH,      # 性能显著下降
    MEDIUM,    # 轻微降级,可调度修复
    LOW        # 信息性或美化

class RootCauseType(str, Enum):
    # 数据问题
    DATA_PIPELINE_FAILED, DATA_SOURCE_UNAVAILABLE,
    DATA_GENERATION_FAILED,

    # 配置问题
    CONFIG_SCHEMA_MISMATCH, CONFIG_INVALID, CONFIG_OUTDATED,

    # Label/Feature 问题
    LABEL_COLUMN_MISSING, LABEL_QUALITY_POOR,
    FEATURE_NULL_HIGH, FEATURE_STALE,

    # ML 训练问题
    MODEL_OVERFITTING, MODEL_UNDERFITTING,
    TRAINING_DATA_INSUFFICIENT, TRAINING_DATA_LEAKAGE,

    # 基础设施问题
    DATABASE_CONNECTION_FAILED, MEMORY_EXHAUSTED, DISK_FULL
```

**核心类**:
```python
@dataclass
class Issue:
    """标准问题描述,供 DataHealer 消费"""
    issue_id: str
    timestamp: str
    component: str  # "bars", "features", "labels", "config"
    issue_type: IssueType
    severity: Severity

    title: str
    description: str

    affected_symbols: List[str]
    affected_tables: List[str]

    metrics: Dict[str, Any]
    metadata: Dict[str, Any]

@dataclass
class Action:
    """推荐的修复动作"""
    action_id: str
    action_type: str  # "generate_labels", "rebuild_features", etc.
    priority: int  # 1 = 最高优先级

    title: str
    description: str
    command: Optional[str]  # Shell 命令

    params: Dict[str, Any]  # 程序化执行参数

    # 安全检查
    is_safe_auto_execute: bool  # DataHealer 可自动执行?
    requires_approval: bool     # 需要人工批准?

@dataclass
class Diagnosis:
    """完整诊断结果,包含根因分析和修复建议"""
    diagnosis_id: str
    timestamp: str

    target_component: str
    target_symbols: List[str]

    issues: List[Issue]
    root_causes: List[RootCause]
    primary_root_cause: Optional[RootCause]

    recommended_actions: List[Action]

    summary: str
    confidence: float  # 诊断置信度 (0.0 to 1.0)
    diagnostic_tool: str  # 生成诊断的工具名称
```

**辅助函数**:
```python
make_issue_id(component, issue_type) -> str
make_diagnosis_id(component) -> str
make_action_id(action_type) -> str

create_label_missing_diagnosis(
    symbol, horizon_min, expected_label, existing_labels
) -> Diagnosis  # 示例诊断生成器
```

---

### 2. 重构的工具

#### `scripts/monitor_system.py` (✅ 修改)
**改动**: 新增统一健康快照接口,保留旧接口向后兼容

**新增方法**:
```python
def generate_health_snapshot(
    self,
    symbols: List[str] = None,
    horizons: List[int] = [120],
    table: str = "trading.features_1m_with_labels"
) -> SystemHealthSnapshot:
    """新的统一接口,返回 SystemHealthSnapshot"""
    # 使用 build_system_health() 构建快照
    snapshot = build_system_health(
        symbols=symbols,
        horizons=horizons,
        table=table
    )
    return snapshot

def print_health_snapshot(
    self,
    symbols: List[str] = None,
    horizons: List[int] = [120]
):
    """打印统一健康快照 (新格式)"""
    # 显示 overall_status, 各组件状态, blocking_issues
```

**命令行接口**:
```bash
# 新的统一模式 (默认)
venv/bin/python scripts/monitor_system.py --mode unified \
    --symbols TSLA AAPL \
    --horizons 120 \
    --output /tmp/health_snapshot.json

# 旧的兼容模式
venv/bin/python scripts/monitor_system.py --mode legacy
```

**向后兼容**:
- `generate_health_report()`: 保留旧接口 (标记为 LEGACY)
- `print_health_report()`: 保留旧格式打印

#### `tools/preflight_check.py` (✅ 新增 445行)
**作用**: 训练前预检,使用统一 SystemHealthSnapshot

**核心函数**:
```python
def preflight_check(
    symbols: List[str],
    horizons: List[int] = [120],
    table: str = "trading.features_1m_with_labels",
    strict: bool = True
) -> Tuple[bool, SystemHealthSnapshot]:
    """
    统一预检接口

    Args:
        strict: True=所有组件必须健康; False=仅 labels 和 config 阻塞

    Returns:
        (ready: bool, snapshot: SystemHealthSnapshot)
    """
    snapshot = build_system_health(symbols, horizons, table)

    if strict:
        ready = snapshot.overall_status == "healthy"
    else:
        ready = (
            snapshot.labels.status != "blocked" and
            snapshot.config.status != "blocked"
        )

    return ready, snapshot

def preflight_check_with_issues(
    symbols, horizons, table, strict
) -> Tuple[bool, SystemHealthSnapshot, List[Issue]]:
    """
    增强版预检,返回结构化 Issue 列表供 DataHealer 消费

    将 blocking_issues 转换为标准 Issue 对象:
    - Bars blocked → Issue(type=DATA_STALE, severity=CRITICAL)
    - Features blocked → Issue(type=DATA_QUALITY_POOR, severity=CRITICAL)
    - Labels blocked → Issue(type=DATA_QUALITY_POOR, severity=CRITICAL)
    - Config blocked → Issue(type=CONFIG_MISMATCH, severity=CRITICAL)
    """
```

**命令行接口**:
```bash
# 基础预检
venv/bin/python tools/preflight_check.py \
    --symbols TSLA AAPL \
    --horizons 120

# 严格模式 (所有组件必须健康)
venv/bin/python tools/preflight_check.py \
    --symbols TSLA \
    --horizons 120 \
    --strict

# 输出 JSON 包含 Issue 列表
venv/bin/python tools/preflight_check.py \
    --symbols TSLA \
    --horizons 120 \
    --with-issues \
    --output /tmp/preflight_result.json
```

**与旧工具的关系**:
- 旧工具: `scripts/ai_trader/brain/preflight.py` (保留,逐步迁移)
- 新工具: `tools/preflight_check.py` (统一接口,推荐使用)

---

## 📊 Phase 1 成果对比

| 能力 | Phase 0 (P0升级前) | Phase 1 (当前) |
|------|-------------------|---------------|
| **健康检查协议** | ❌ 各工具独立格式 | ✅ SystemHealthSnapshot 统一 |
| **问题描述** | ❌ 字符串列表 | ✅ Issue/Diagnosis 结构化 |
| **根因分析** | ⚠️ 仅 diagnose_oof_failure | ✅ RootCause 标准化 |
| **修复建议** | ⚠️ 纯文本命令 | ✅ Action 对象 (可程序化) |
| **DataHealer 集成** | ❌ 无标准接口 | ✅ 可消费 Issue/Action |
| **向后兼容** | N/A | ✅ 旧接口保留 (LEGACY) |
| **文档** | ⚠️ 分散 | ✅ 统一协议文档 |

---

## 🔬 验证测试

### 测试 1: SystemHealthSnapshot 构建

```bash
# 测试健康快照生成
venv/bin/python scripts/monitor_system.py --mode unified \
    --symbols TSLA \
    --horizons 120 \
    --output /tmp/snapshot.json
```

**预期输出**:
```
================================================================================
UNIFIED SYSTEM HEALTH SNAPSHOT
================================================================================
Overall Status:   HEALTHY / DEGRADED / BLOCKED
Symbols:          TSLA
Horizons:         120

📊 BARS HEALTH
  ✅ Status:           HEALTHY
  Latest:           2025-11-26T15:30:00
  Age:              0.5 hours
  Coverage:         100.0%

🧮 FEATURES HEALTH
  ✅ Status:           HEALTHY
  Latest:           2025-11-26T15:30:00
  Age:              0.5 hours
  NULL Ratio:       0.0%

🏷️  LABELS HEALTH
  ✅ Overall Status:   HEALTHY
  Total Combinations: 1
  Healthy:            1
  Degraded:           0
  Blocked:            0

  Per-Symbol Status:
    ✅ TSLA_h120        rows=13,383 | null= 0.2% | std=174.6bps | age=0.5h

⚙️  CONFIG HEALTH
  ✅ Status:           HEALTHY
  No issues detected

✅ Snapshot saved to /tmp/snapshot.json
```

### 测试 2: Preflight Check

```bash
# 测试训练前预检
venv/bin/python tools/preflight_check.py \
    --symbols TSLA \
    --horizons 120 \
    --strict
```

**预期输出**:
```
================================================================================
PREFLIGHT CHECK REPORT
================================================================================
Overall Status:   HEALTHY
Symbols:          TSLA
Horizons:         120

Component Health:
  ✅ Bars         HEALTHY
  ✅ Features     HEALTHY
  ✅ Labels       HEALTHY
  ✅ Config       HEALTHY

✅ No blocking issues detected

Exit code: 0
```

### 测试 3: Issue 对象生成

```bash
# 测试 Issue 列表生成
venv/bin/python tools/preflight_check.py \
    --symbols TSLA \
    --horizons 5 \  # 假设 label_h5 质量差
    --with-issues \
    --output /tmp/preflight_issues.json
```

**预期 JSON 输出**:
```json
{
  "ready": false,
  "snapshot": { ... },
  "issues": [
    {
      "issue_id": "labels_data_quality_poor_20251126153000",
      "timestamp": "2025-11-26T15:30:00",
      "component": "labels",
      "issue_type": "data_quality_poor",
      "severity": "critical",
      "title": "Label quality issue for TSLA_h5",
      "description": "Low std: 34.8 bps",
      "affected_symbols": ["TSLA"],
      "affected_tables": ["trading.features_1m_with_labels"],
      "metrics": {
        "horizon_min": 5,
        "rows": 13383,
        "null_ratio": 0.002,
        "zero_ratio": 0.166,
        "std_bps": 34.8,
        "mean_magnitude": 0.00123,
        "age_hours": 0.5
      }
    }
  ]
}
```

---

## 🔗 集成点

### 1. AI Brain 集成 (`control_event_consumer.py`)

**当前状态**: P0 工具已集成,Phase 1 为其提供标准结构

**未来改进 (Phase 2)**:
```python
# Phase 1: P0 工具返回字典
diagnosis = diagnose_oof_failure(...)
ml_ctx_cur["oof_diagnosis"] = diagnosis

# Phase 2: 统一 Diagnosis 对象
from libs.health import Diagnosis
diagnosis: Diagnosis = diagnose_oof_failure_v2(...)
ml_ctx_cur["oof_diagnosis"] = diagnosis.to_dict()

# DataHealer 可消费
from libs.healer import DataHealer
healer = DataHealer()
for action in diagnosis.get_auto_executable_actions():
    healer.execute(action)
```

### 2. Orchestrator 集成 (`orchestrator.py`)

**当前状态**: 启动时运行配置健康检查

**未来改进 (Phase 2)**:
```python
# Phase 1: 使用旧的 check_ml_config_health
from tools.check_ml_config_health import check_ml_config_health
config_health = check_ml_config_health()

# Phase 2: 使用统一 SystemHealthSnapshot
from libs.health import build_system_health
snapshot = build_system_health(symbols=["TSLA"], horizons=[120])

if snapshot.config.status == "blocked":
    # 自动修复或告警
    for problem in snapshot.config.problems:
        self.log.error("config_issue", problem=problem)
```

### 3. 训练脚本集成 (待 Phase 1 完成后迁移)

**旧方式** (`pipelines/train_xgb_with_oof.py`):
```python
from scripts.ai_trader.brain.preflight import preflight_check_all

ready, report = preflight_check_all(symbols=["TSLA"], horizons=[30])
if not ready:
    logger.error("preflight_failed", report=report)
    return
```

**新方式** (Phase 1 统一接口):
```python
from tools.preflight_check import preflight_check

ready, snapshot = preflight_check(symbols=["TSLA"], horizons=[120])
if not ready:
    logger.error("preflight_failed", blocking_issues=snapshot.blocking_issues)

    # 可选: 自动修复 (Phase 2)
    # from libs.healer import DataHealer
    # healer = DataHealer()
    # healer.heal_snapshot(snapshot)
    return
```

---

## 📝 使用指南

### 开发者使用

#### 1. 检查系统健康

```python
from libs.health import build_system_health

snapshot = build_system_health(
    symbols=["TSLA", "AAPL"],
    horizons=[120],
    table="trading.features_1m_with_labels"
)

print(f"Overall Status: {snapshot.overall_status}")
if snapshot.blocking_issues:
    for issue in snapshot.blocking_issues:
        print(f"Blocking: {issue}")
```

#### 2. 训练前预检

```python
from tools.preflight_check import preflight_check

ready, snapshot = preflight_check(
    symbols=["TSLA"],
    horizons=[120],
    strict=True  # 所有组件必须健康
)

if ready:
    # 开始训练
    train_model(symbols=["TSLA"])
else:
    # 记录阻塞问题
    logger.error("training_blocked", issues=snapshot.blocking_issues)
```

#### 3. 生成诊断报告

```python
from tools.preflight_check import preflight_check_with_issues

ready, snapshot, issues = preflight_check_with_issues(
    symbols=["TSLA"],
    horizons=[120]
)

# Issue 对象可供 DataHealer 消费
for issue in issues:
    print(f"[{issue.severity.upper()}] {issue.title}")
    print(f"  Component: {issue.component}")
    print(f"  Description: {issue.description}")

    # Phase 2: DataHealer 自动修复
    # healer.fix(issue)
```

### 命令行使用

#### 1. 系统健康监控

```bash
# 统一模式 (推荐)
venv/bin/python scripts/monitor_system.py --mode unified \
    --symbols TSLA AAPL MSFT \
    --horizons 120 \
    --output /tmp/health_snapshot.json

# 旧兼容模式
venv/bin/python scripts/monitor_system.py --mode legacy
```

#### 2. 训练前预检

```bash
# 基础预检
venv/bin/python tools/preflight_check.py \
    --symbols TSLA \
    --horizons 120

# 严格模式
venv/bin/python tools/preflight_check.py \
    --symbols TSLA AAPL \
    --horizons 120 \
    --strict

# 输出 Issue 列表
venv/bin/python tools/preflight_check.py \
    --symbols TSLA \
    --horizons 120 \
    --with-issues \
    --output /tmp/preflight.json
```

---

## 🚀 下一步计划 (Phase 2)

### Phase 2: DataHealer v2 (3-5天)

**目标**: 统一修复接口,自动执行 Action

**待实现**:
1. `libs/healer/data_healer.py`:
   ```python
   class DataHealer:
       def heal(self, target: Union[Issue, Action, Diagnosis]) -> bool:
           """统一修复入口"""

       def execute_action(self, action: Action) -> bool:
           """执行单个 Action"""

       def heal_snapshot(self, snapshot: SystemHealthSnapshot) -> bool:
           """修复整个快照的所有阻塞问题"""
   ```

2. 集成到 AI Brain:
   ```python
   # control_event_consumer.py
   if snapshot.overall_status == "blocked":
       healer = DataHealer()
       fixed = healer.heal_snapshot(snapshot)
       if fixed:
           logger.info("auto_healed", snapshot=snapshot)
   ```

3. 支持的自动修复:
   - 生成缺失的 label 列
   - 重建 features (如果 NULL 率过高)
   - 更新配置 (如果 config-schema 不匹配)
   - 触发数据回填 (如果数据缺失)

---

## ✅ Phase 1 验收标准

| 标准 | 目标 | 实际 | 状态 |
|------|------|------|------|
| **统一数据结构** | SystemHealthSnapshot 定义 | ✅ 完成 | ✅ 达成 |
| **问题描述协议** | Issue/Diagnosis 定义 | ✅ 完成 | ✅ 达成 |
| **monitor_system 重构** | 返回 SystemHealthSnapshot | ✅ 完成 | ✅ 达成 |
| **preflight_check 统一** | 新工具使用统一协议 | ✅ 完成 | ✅ 达成 |
| **向后兼容** | 旧接口保留 | ✅ 完成 | ✅ 达成 |
| **文档完整** | API 文档 + 使用指南 | ✅ 完成 | ✅ 达成 |

---

## 📧 反馈与改进

如有问题或改进建议,请:
1. 检查日志中的健康快照输出
2. 运行统一预检查看阻塞问题
3. 查看 `/tmp/health_snapshot.json` 和 `/tmp/preflight.json`

---

**总结**: Phase 1 成功完成!建立了统一的健康快照协议和问题描述协议,为 Phase 2 的 DataHealer v2 和自动修复能力奠定了坚实基础。AI大脑现在可以:
- ✅ 使用统一格式获取系统健康状态
- ✅ 获得结构化的问题描述 (Issue)
- ✅ 接收可执行的修复建议 (Action)
- ⏳ 准备好接入 DataHealer v2 自动修复 (Phase 2)

**Phase 1 完成时间**: 2025-11-26
**总耗时**: 约 2 小时
**核心改进**: 从 "各自为战" 到 "统一协议"
