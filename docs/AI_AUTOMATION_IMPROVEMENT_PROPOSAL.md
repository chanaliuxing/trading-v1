# AI交易系统自动化与智能化改进方案

**日期**: 2025-11-25
**状态**: 提案
**优先级**: P0 (Critical)

---

## 一、问题根因分析：为什么会出现Label缺失？

### 1.1 直接原因 (Immediate Cause)
```
特征构建完成 → ML训练启动 → 发现labels表为空 → AUC=0.5
```

### 1.2 系统性根因 (Root Causes)

#### 🔴 **RC1: 缺失数据依赖性检查 (Data Dependency Validation)**
**问题**: ML训练启动前,没有验证`features_1m_with_labels`表是否存在且有数据

**证据**:
- `scripts/ai_trader/ml_scheduler.py` 直接调用训练,未检查labels
- `pipelines/train_with_ic_objective.py` 假设数据存在
- Brain循环启动训练时无先决条件验证

**影响**:
- 训练在无数据情况下运行,浪费资源
- AUC=0.5的"僵尸模型"被生成并部署

**修复优先级**: P0

---

#### 🟠 **RC2: Pipeline步骤未原子化 (Non-Atomic Pipeline)**
**问题**: 特征构建和标签生成是两个独立步骤,无强制执行顺序

**当前流程** (松耦合):
```
Step 1: rebuild_features_1m.py → features_1m ✅
Step 2: [手动?自动?] → features_1m_with_labels ❌ (被遗漏)
Step 3: train_with_ic_objective.py → 训练 (无数据)
```

**期望流程** (原子化):
```
Step 1: Feature Build + Label Generation (事务性)
Step 2: 验证数据完整性
Step 3: 训练 (仅在验证通过后)
```

**修复优先级**: P0

---

#### 🟡 **RC3: 缺失健康检查与告警 (No Health Checks)**
**问题**: Brain循环未集成数据质量监控

**应有的检查点**:
- [ ] **Pre-Flight Check**: 训练前验证tables存在、row count > threshold
- [ ] **In-Flight Check**: 训练中监控loss/AUC异常 (如精确=0.5)
- [ ] **Post-Flight Check**: 模型评估后验证指标合理性

**当前状态**: 所有检查均缺失

**修复优先级**: P1

---

#### 🟢 **RC4: 文档化不足 (Undocumented Dependencies)**
**问题**: Pipeline依赖关系未明确文档化

**缺失文档**:
- Label生成何时运行? (每日? 按需?)
- 谁负责触发? (Cron? Brain? 手动?)
- 失败重试策略?
- SLA定义? (labels延迟多久算异常)

**修复优先级**: P2

---

## 二、智能化改进方案

### 2.1 数据流自愈系统 (Self-Healing Data Pipeline)

#### 设计目标
**"零人工干预完成从数据缺失到模型训练的完整修复"**

#### 架构设计

```python
# services/data/intelligent_data_healer.py

class IntelligentDataHealer:
    """
    智能数据自愈器 - Brain的自主修复能力

    功能:
    1. 诊断: 自动检测数据完整性问题
    2. 决策: 根据问题类型选择修复策略
    3. 执行: 自动修复并验证
    4. 学习: 记录修复历史,优化策略
    """

    async def diagnose(self, context: MLContext) -> DiagnosisReport:
        """
        诊断数据完整性

        Returns:
            DiagnosisReport:
                - missing_tables: 缺失的表
                - empty_tables: 空表
                - stale_data: 过期数据 (>1天)
                - data_quality_issues: 质量问题 (NULL比例>5%)
        """
        report = DiagnosisReport()

        # 1. 检查必需表存在性
        required_tables = [
            'trading.bars_1m_ibkr',
            'trading.features_1m',
            'trading.features_1m_active',
            'trading.features_1m_with_labels'  # ← 关键!
        ]

        for table in required_tables:
            if not await self._table_exists(table):
                report.missing_tables.append(table)
            elif await self._table_is_empty(table):
                report.empty_tables.append(table)
            elif await self._data_is_stale(table, max_age_hours=24):
                report.stale_data.append(table)

        # 2. 检查数据质量
        if 'trading.features_1m_with_labels' not in report.empty_tables:
            quality = await self._check_label_quality()
            if quality.null_ratio > 0.05:
                report.data_quality_issues.append(
                    f"label_h30 null ratio: {quality.null_ratio:.1%}"
                )

        return report

    async def heal(self, report: DiagnosisReport) -> HealingResult:
        """
        自动修复数据问题

        修复策略决策树:

        IF features_1m_with_labels 缺失 OR 为空:
            → 运行 generate_labels.py (分批)

        IF features_1m 过期:
            → 运行 rebuild_features_1m.py
            → 然后重新生成labels

        IF bars_1m_ibkr 缺失:
            → 运行 DataHealer.backfill_bars()
            → 级联修复features + labels
        """
        result = HealingResult()

        # 策略1: 修复labels缺失
        if 'trading.features_1m_with_labels' in report.empty_tables:
            logger.warning(
                "detect_missing_labels",
                action="auto_generate",
                symbols=self.config.symbols
            )

            success = await self._generate_labels_batched(
                symbols=self.config.symbols,
                batch_size=5  # 避免内存溢出
            )

            result.actions_taken.append({
                "action": "generate_labels",
                "success": success,
                "rows_generated": await self._count_rows('features_1m_with_labels')
            })

        # 策略2: 修复特征过期
        if report.stale_data:
            # ... 实现特征重建逻辑
            pass

        return result

    async def _generate_labels_batched(
        self,
        symbols: List[str],
        batch_size: int = 5
    ) -> bool:
        """
        分批生成labels,避免内存溢出

        核心改进:
        1. 自动检测ClickHouse内存限制
        2. 动态调整batch_size
        3. 失败自动重试 (指数退避)
        4. 进度跟踪与告警
        """
        batches = [
            symbols[i:i+batch_size]
            for i in range(0, len(symbols), batch_size)
        ]

        temp_tables = []

        for i, batch in enumerate(batches):
            temp_table = f"features_1m_with_labels_batch{i}"

            try:
                # 调用优化后的label生成器
                await self._run_subprocess(
                    cmd=[
                        "venv/bin/python",
                        "pipelines/generate_labels.py",
                        "--symbols", *batch,
                        "--horizons", "30",
                        "--target-table", temp_table
                    ],
                    timeout=300,  # 5分钟超时
                    retry=3
                )

                temp_tables.append(temp_table)

            except subprocess.TimeoutExpired:
                # 自动降低batch_size重试
                logger.warning(
                    "label_generation_timeout",
                    batch=batch,
                    action="retry_with_smaller_batch"
                )
                # 递归调用,batch_size减半
                await self._generate_labels_batched(batch, batch_size=max(1, batch_size//2))

        # 合并所有batch
        await self._merge_label_tables(temp_tables, "features_1m_with_labels")

        return True
```

---

### 2.2 ML Brain Pre-Flight Checklist (训练前检查清单)

#### 实现位置
`scripts/ai_trader/brain/preflight_validator.py`

```python
class PreFlightValidator:
    """
    训练前飞行检查 - 确保所有先决条件满足

    灵感来源: 飞机起飞前的安全检查清单
    """

    async def validate_training_ready(
        self,
        symbols: List[str],
        min_label_rows: int = 1000
    ) -> ValidationResult:
        """
        训练就绪性验证

        Returns:
            ValidationResult:
                - ready: bool
                - blocked_by: List[str] (阻塞原因)
                - warnings: List[str]
        """
        result = ValidationResult(ready=True, blocked_by=[], warnings=[])

        # ✓ Check 1: Labels表存在且有足够数据
        label_count = await self._count_table_rows(
            'trading.features_1m_with_labels',
            where=f"symbol IN {tuple(symbols)}"
        )

        if label_count == 0:
            result.ready = False
            result.blocked_by.append(
                f"CRITICAL: features_1m_with_labels is EMPTY. "
                f"Run generate_labels.py first!"
            )
        elif label_count < min_label_rows:
            result.warnings.append(
                f"LOW_DATA: Only {label_count} labeled rows. "
                f"Recommended: >{min_label_rows}"
            )

        # ✓ Check 2: Features数据新鲜度
        latest_ts = await self._get_latest_timestamp('trading.features_1m')
        age_hours = (datetime.now(tz=ZoneInfo("America/New_York")) - latest_ts).total_seconds() / 3600

        if age_hours > 24:
            result.warnings.append(
                f"STALE_FEATURES: Latest feature is {age_hours:.1f}h old"
            )

        # ✓ Check 3: Label质量检查
        null_ratio = await self._check_null_ratio(
            'trading.features_1m_with_labels',
            column='label_h30'
        )

        if null_ratio > 0.1:
            result.ready = False
            result.blocked_by.append(
                f"BAD_LABELS: {null_ratio:.1%} of labels are NULL"
            )

        # ✓ Check 4: ClickHouse健康度
        ch_status = await self._check_clickhouse_health()
        if not ch_status.healthy:
            result.ready = False
            result.blocked_by.append(
                f"DB_UNHEALTHY: {ch_status.error}"
            )

        return result

    async def auto_fix_if_possible(self, result: ValidationResult):
        """
        尝试自动修复阻塞问题
        """
        if "features_1m_with_labels is EMPTY" in str(result.blocked_by):
            logger.info("auto_fix_attempt", issue="missing_labels")

            healer = IntelligentDataHealer()
            await healer.heal(DiagnosisReport(
                empty_tables=['trading.features_1m_with_labels']
            ))
```

---

### 2.3 集成到Brain循环

#### 修改位置
`scripts/ai_trader/brain/policy/oof_policy.py`

```python
class OOFPolicy:
    """OOF训练策略 - 集成Pre-Flight Check"""

    async def execute(self, ctx: BrainContext) -> PolicyDecision:
        # === 新增: Pre-Flight Validation ===
        validator = PreFlightValidator()
        validation = await validator.validate_training_ready(
            symbols=ctx.symbols,
            min_label_rows=5000  # 至少5K训练样本
        )

        if not validation.ready:
            logger.error(
                "training_blocked",
                reasons=validation.blocked_by,
                action="auto_heal"
            )

            # 尝试自动修复
            await validator.auto_fix_if_possible(validation)

            # 重新验证
            validation = await validator.validate_training_ready(ctx.symbols)

            if not validation.ready:
                # 修复失败,发送告警并跳过本次训练
                await self._send_alert(
                    severity="CRITICAL",
                    message=f"Training aborted: {validation.blocked_by}",
                    action_required="Manual intervention needed"
                )

                return PolicyDecision(
                    action="skip",
                    reason="pre_flight_check_failed",
                    next_check_in_sec=1800  # 30分钟后重试
                )

        # 所有检查通过,继续训练
        logger.info("pre_flight_check_passed", warnings=validation.warnings)

        # ... 原有训练逻辑 ...
```

---

## 三、监控与告警增强

### 3.1 数据质量监控指标

#### Prometheus Metrics
```python
# libs/monitoring/metrics.py

# 表行数监控
data_table_rows = Gauge(
    'trading_data_table_rows',
    'Row count in critical tables',
    ['table_name', 'symbol']
)

# 数据新鲜度监控 (距最新数据的小时数)
data_staleness_hours = Gauge(
    'trading_data_staleness_hours',
    'Hours since latest data point',
    ['table_name']
)

# Label质量监控
label_null_ratio = Gauge(
    'trading_label_null_ratio',
    'Ratio of NULL values in labels',
    ['horizon']
)

# Pre-Flight检查失败次数
preflight_check_failures = Counter(
    'trading_preflight_check_failures_total',
    'Total pre-flight check failures',
    ['check_type']
)
```

#### Grafana告警规则
```yaml
# infra/grafana/alerts/data_quality.yml

- alert: MissingLabels
  expr: data_table_rows{table_name="features_1m_with_labels"} == 0
  for: 5m
  labels:
    severity: critical
  annotations:
    summary: "训练标签表为空"
    description: "{{ $labels.table_name }}无数据,ML训练将失败"
    action: "运行 generate_labels.py"

- alert: StaleFeatures
  expr: data_staleness_hours{table_name="features_1m"} > 24
  for: 10m
  labels:
    severity: warning
  annotations:
    summary: "特征数据过期"
    description: "最新特征距今{{ $value }}小时"
    action: "运行 rebuild_features_1m.py"

- alert: HighLabelNullRatio
  expr: label_null_ratio{horizon="30"} > 0.05
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "标签数据质量差"
    description: "{{ $value | humanizePercentage }} 的labels为NULL"
```

---

### 3.2 异常检测 (Anomaly Detection)

#### AUC异常自动诊断
```python
# scripts/ai_trader/brain/diagnostics/auc_analyzer.py

class AUCDiagnostics:
    """
    AUC异常自动诊断器

    目标: 当AUC=0.5时,自动定位根因并提出修复建议
    """

    async def diagnose_random_predictions(self, auc: float) -> Diagnosis:
        """
        诊断随机预测问题 (AUC ≈ 0.5)
        """
        if not (0.49 < auc < 0.51):
            return Diagnosis(healthy=True)

        logger.warning("detect_random_predictions", auc=auc)

        # 诊断树
        diagnosis = Diagnosis(healthy=False, root_cause=None, fixes=[])

        # 1. 检查labels是否存在
        label_count = await self._count_labels()
        if label_count == 0:
            diagnosis.root_cause = "MISSING_LABELS"
            diagnosis.fixes.append({
                "action": "generate_labels",
                "command": "venv/bin/python pipelines/generate_labels.py --symbols ...",
                "auto_fixable": True
            })
            return diagnosis

        # 2. 检查labels是否全NULL
        null_ratio = await self._check_label_nulls()
        if null_ratio > 0.9:
            diagnosis.root_cause = "NULL_LABELS"
            diagnosis.fixes.append({
                "action": "rebuild_labels",
                "reason": f"{null_ratio:.1%} labels are NULL",
                "auto_fixable": True
            })
            return diagnosis

        # 3. 检查特征是否全0或全constant
        const_features = await self._detect_constant_features()
        if len(const_features) > 50:
            diagnosis.root_cause = "CONSTANT_FEATURES"
            diagnosis.fixes.append({
                "action": "rebuild_features",
                "features": const_features[:10],
                "auto_fixable": True
            })
            return diagnosis

        # 4. 检查训练数据量
        train_size = await self._get_training_size()
        if train_size < 1000:
            diagnosis.root_cause = "INSUFFICIENT_DATA"
            diagnosis.fixes.append({
                "action": "collect_more_data",
                "current": train_size,
                "required": 10000,
                "auto_fixable": False
            })

        return diagnosis
```

---

## 四、Pipeline自动化改进

### 4.1 DAG化Pipeline (使用Airflow/Prefect)

#### 当前问题
- 步骤之间无强制依赖
- 手动触发,易遗漏
- 失败无自动重试

#### 改进方案: Prefect Flow

```python
# pipelines/flows/daily_ml_pipeline.py

from prefect import flow, task
from prefect.tasks import task_input_hash
from datetime import timedelta

@task(
    retries=3,
    retry_delay_seconds=60,
    cache_key_fn=task_input_hash,
    cache_expiration=timedelta(hours=1)
)
async def rebuild_features(symbols: List[str], date: str):
    """Step 1: 重建特征"""
    subprocess.run([
        "venv/bin/python",
        "pipelines/rebuild_features_1m.py",
        "--symbols", *symbols,
        "--start", date,
        "--end", date
    ], check=True)

@task(retries=3)
async def generate_labels(symbols: List[str]):
    """Step 2: 生成labels (依赖Step 1)"""
    # 使用智能分批策略
    healer = IntelligentDataHealer()
    await healer._generate_labels_batched(symbols, batch_size=5)

@task
async def validate_data_quality(symbols: List[str]):
    """Step 3: 数据质量验证"""
    validator = PreFlightValidator()
    result = await validator.validate_training_ready(symbols)

    if not result.ready:
        raise ValueError(f"Data quality check failed: {result.blocked_by}")

    return result

@task(retries=2)
async def train_model(symbols: List[str]):
    """Step 4: 训练模型"""
    subprocess.run([
        "venv/bin/python",
        "scripts/ai_trader/ml_scheduler.py",
        "--symbols", *symbols,
        "--job-type", "oof",
        "--max-cycles", "1"
    ], check=True)

@flow(
    name="daily-ml-pipeline",
    retries=1,
    retry_delay_seconds=300
)
async def daily_ml_pipeline(
    symbols: List[str],
    date: str
):
    """
    每日ML Pipeline - 原子化执行

    DAG:
        rebuild_features → generate_labels → validate_data_quality → train_model

    特点:
    - 失败自动重试
    - 步骤缓存 (避免重复计算)
    - 依赖强制执行
    - Slack/Email告警
    """

    # Step 1: 重建特征
    await rebuild_features(symbols, date)

    # Step 2: 生成labels (等待Step 1完成)
    await generate_labels(symbols)

    # Step 3: 验证数据质量
    validation = await validate_data_quality(symbols)

    # Step 4: 训练模型 (仅在验证通过后)
    await train_model(symbols)

    return {
        "status": "success",
        "validation": validation,
        "timestamp": datetime.now()
    }

# 调度配置
if __name__ == "__main__":
    from prefect.deployments import Deployment
    from prefect.server.schemas.schedules import CronSchedule

    deployment = Deployment.build_from_flow(
        flow=daily_ml_pipeline,
        name="daily-ml-pipeline-prod",
        parameters={
            "symbols": ["TSLA", "NVDA", "SPY", "AAPL", "MSFT",
                       "AMD", "META", "AMZN", "GOOGL", "QQQ",
                       "TQQQ", "SQQQ", "VOO", "ORCL", "INTC"],
            "date": "{{ ds }}"  # 动态日期
        },
        schedule=CronSchedule(cron="0 17 * * 1-5"),  # 每个交易日17:00运行
        work_queue_name="ml-pipeline"
    )

    deployment.apply()
```

---

### 4.2 Cron任务配置

#### 自动化调度
```bash
# /etc/cron.d/trading-ml-pipeline

# 每个交易日17:00重建特征
0 17 * * 1-5 trading cd /app && venv/bin/python pipelines/rebuild_features_1m.py --symbols ALL --date today

# 17:30生成labels
30 17 * * 1-5 trading cd /app && bash temp/generate_labels_batched.sh

# 18:00运行OOF训练
0 18 * * 1-5 trading cd /app && venv/bin/python scripts/ai_trader/ml_scheduler.py --job-type oof --max-cycles 1

# 每小时检查数据健康度
0 * * * * trading cd /app && venv/bin/python tools/data_health_monitor.py
```

---

## 五、长期智能化愿景

### 5.1 自学习修复系统 (Self-Learning Healing)

#### 设计思路
```python
# services/ai/failure_learning_system.py

class FailureLearningSystem:
    """
    失败学习系统 - 从历史故障中学习最优修复策略

    核心思想:
    1. 记录每次故障 + 修复动作 + 结果
    2. 使用强化学习优化修复策略
    3. 积累经验,提升自愈成功率
    """

    async def record_failure(
        self,
        failure_type: str,
        context: dict,
        fix_action: str,
        success: bool,
        time_to_fix: float
    ):
        """记录故障修复历史"""
        await self.db.insert(
            'failure_history',
            {
                'timestamp': datetime.now(),
                'failure_type': failure_type,
                'context': json.dumps(context),
                'fix_action': fix_action,
                'success': success,
                'time_to_fix_sec': time_to_fix
            }
        )

    async def recommend_fix(self, failure_type: str, context: dict) -> str:
        """
        基于历史数据推荐最优修复策略

        使用简单的Bandit算法:
        - 计算每个修复动作的成功率和平均修复时间
        - 选择成功率最高且修复时间短的动作
        """
        history = await self.db.query(
            '''
            SELECT
                fix_action,
                AVG(CASE WHEN success THEN 1.0 ELSE 0.0 END) as success_rate,
                AVG(time_to_fix_sec) as avg_fix_time,
                COUNT(*) as attempts
            FROM failure_history
            WHERE failure_type = ?
            GROUP BY fix_action
            HAVING attempts >= 3
            ORDER BY success_rate DESC, avg_fix_time ASC
            LIMIT 1
            ''',
            (failure_type,)
        )

        if history:
            return history[0]['fix_action']

        # Fallback: 使用默认策略
        return self.default_strategies.get(failure_type)
```

---

### 5.2 预测性维护 (Predictive Maintenance)

#### 目标
在问题发生前预测并修复

#### 实现方法
```python
# services/ai/predictive_maintenance.py

class PredictiveDataMaintenance:
    """
    预测性数据维护

    监控指标趋势,在数据问题发生前主动修复
    """

    async def predict_label_expiry(self) -> datetime:
        """
        预测labels何时会过期

        基于历史pattern:
        - 观察过去30天的label生成频率
        - 预测下次需要生成的时间
        - 提前24小时触发自动生成
        """
        history = await self.get_label_generation_history(days=30)

        # 简单线性预测
        avg_interval = np.mean(np.diff(history['timestamps']))
        last_gen = history['timestamps'][-1]

        predicted_expiry = last_gen + avg_interval

        # 提前24小时告警
        alert_time = predicted_expiry - timedelta(hours=24)

        if datetime.now() > alert_time:
            logger.warning(
                "predictive_alert",
                event="labels_will_expire_soon",
                predicted_at=predicted_expiry,
                action="auto_generate_scheduled"
            )

            # 自动调度生成任务
            await self.schedule_label_generation(at=alert_time)

        return predicted_expiry
```

---

## 六、实施路线图

### Phase 1: 紧急修复 (本周完成) ✅
- [x] 手动生成labels (1.3M rows)
- [x] 验证AUC脱离0.5
- [x] 创建分批生成脚本

### Phase 2: 核心自动化 (2周内)
- [ ] 实现`IntelligentDataHealer` (RC1修复)
- [ ] 实现`PreFlightValidator` (RC2修复)
- [ ] 集成到Brain循环
- [ ] 添加Prometheus监控指标

### Phase 3: 告警与监控 (1个月内)
- [ ] 配置Grafana告警规则
- [ ] 实现`AUCDiagnostics`自动诊断
- [ ] Slack/Email告警集成
- [ ] 数据质量Dashboard

### Phase 4: Pipeline工程化 (2个月内)
- [ ] 部署Prefect/Airflow
- [ ] 迁移所有Pipeline到DAG
- [ ] 配置自动调度
- [ ] 失败自动重试

### Phase 5: 智能化演进 (长期)
- [ ] `FailureLearningSystem` - 从故障中学习
- [ ] `PredictiveDataMaintenance` - 预测性维护
- [ ] 自适应batch_size调优
- [ ] 多维度异常检测

---

## 七、成功指标 (KPI)

### 数据可靠性
- **MTBF** (Mean Time Between Failures): >30天
- **MTTR** (Mean Time To Recovery): <1小时
- **自愈成功率**: >95%

### 训练质量
- **Pre-Flight检查失败率**: <5%
- **AUC=0.5异常发生率**: 0次/月
- **训练数据新鲜度**: <6小时

### 自动化程度
- **手动干预次数**: <2次/月
- **Pipeline自动化率**: >90%
- **告警响应时间**: <15分钟

---

## 八、风险与缓解

### 风险1: 自动修复错误
**缓解**:
- 所有自动操作记录审计日志
- 关键操作需要dry-run模式
- 修复失败自动回滚

### 风险2: 过度自动化导致不可控
**缓解**:
- 保留手动干预接口
- 实施kill switch (紧急停止)
- 定期人工审查

### 风险3: 监控指标过载
**缓解**:
- 采用分级告警 (Critical/Warning/Info)
- 合并相似告警
- 智能静默规则

---

## 九、总结

### 核心原则
1. **"Don't trust, verify"** - 训练前验证所有先决条件
2. **"Fail fast, heal faster"** - 快速检测,自动修复
3. **"Learn from failures"** - 从故障中学习,优化策略
4. **"Automate the boring stuff"** - Pipeline自动化,人专注于策略

### 预期收益
- 🎯 **零停机训练**: 数据问题自动修复,训练不间断
- 🚀 **减少人工干预**: 90%的故障自动处理
- 📊 **提升模型质量**: 数据质量监控 → 更好的AUC
- ⏱️ **更快迭代**: Pipeline自动化 → 从idea到production <1天

### 下一步行动
1. Review本文档,确定优先级
2. 创建Jira tickets分配任务
3. 第一个PR: 实现`PreFlightValidator`
4. 逐步实施,持续改进

---

**文档维护者**: Claude Code
**最后更新**: 2025-11-25
**版本**: 1.0
