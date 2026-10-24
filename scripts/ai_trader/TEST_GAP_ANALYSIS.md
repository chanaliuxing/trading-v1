# 🔍 AI Trader 测试覆盖率缺口分析

**生成日期**: 2025-10-29
**当前覆盖率**: 26% (6/23 核心模块)
**目标覆盖率**: 80% (18/23 核心模块)

---

## 📊 总体概览

### 已完成测试 ✅ (6 模块)

| 模块 | 测试数量 | 覆盖率 | 状态 |
|------|---------|--------|------|
| **Gates** | 24 | 100% | ✅ 完成 |
| **Orchestrator** | 25 | ~80% | ✅ 核心逻辑完成 |
| **Controllers** | 12 | ~70% | ✅ 主要流程完成 |
| **AB Deployer** | 6 | 100% | ✅ 完成 |
| **Integration** | 8 | ~60% | ✅ 主工作流完成 |
| **Fixes** | 3 | 100% | ✅ 边界条件完成 |
| **总计** | **78** | - | **100% 通过率** |

### 未测试模块 ⏳ (17 模块)

| 优先级 | 模块数量 | 估算测试数 | 预计工作量 |
|--------|----------|-----------|----------|
| 🔴 High | 4 | 35-45 | 4-6 小时 |
| 🟡 Medium | 7 | 40-60 | 5-8 小时 |
| 🟢 Low | 6 | 20-30 | 2-4 小时 |

---

## 🔴 High Priority - 关键路径 (必须测试)

### 1. Brain Module (brain.py)
**为什么重要**: 核心决策引擎，所有 LLM 驱动的决策从这里发出

**文件**: `scripts/ai_trader/brain/brain.py` (141 lines)

**核心类**:
- `ControlBrain`: LLM 决策控制器

**关键方法**:
- `propose_plan()` - 生成交易计划
- `decide()` - 做出控制决策 (morning_start, hpo_deploy)
- `_ask()` - LLM API 调用（带超时和错误处理）

**建议测试用例** (10-12 个):
```python
class TestControlBrain:
    """Test ControlBrain LLM decision making"""

    # propose_plan 测试
    def test_propose_plan_morning_success()
    def test_propose_plan_intraday_success()
    def test_propose_plan_with_memory_context()
    def test_propose_plan_llm_timeout()
    def test_propose_plan_invalid_json_response()

    # decide 测试
    def test_decide_start_morning_proceed()
    def test_decide_start_morning_block()
    def test_decide_hpo_deploy_apply()
    def test_decide_hpo_deploy_reject()

    # 错误处理
    def test_ask_timeout_error()
    def test_ask_api_error()
    def test_missing_api_key()
```

**Mock 策略**:
```python
# Mock LLM provider
with patch('importlib.import_module') as mock_import:
    mock_provider = MagicMock()

    async def mock_analyze():
        return MagicMock(raw_response='{"proceed": true}')

    mock_provider.analyze_market_with_prompt = mock_analyze
    mock_import.return_value = MagicMock(
        DeepSeekNvidiaProvider=MagicMock(return_value=mock_provider)
    )
```

**工作量**: 2 小时

---

### 2. Memory - Episode Store (memory/episode_store.py)
**为什么重要**: 持久化所有学习记录到 ClickHouse/PostgreSQL

**文件**: `scripts/ai_trader/memory/episode_store.py` (308 lines)

**核心函数**:
- `build_episode()` - 从 artifacts 构建 episode
- `store_episode()` - 持久化到 ClickHouse + PostgreSQL
- `write_clickhouse()` - CH 写入
- `write_postgres()` - PG 写入
- `_ch_sector()`, `_ch_regime()` - 查询 CH 元数据

**建议测试用例** (8-10 个):
```python
class TestEpisodeStore:
    """Test episode building and storage"""

    # build_episode 测试
    def test_build_episode_with_full_data()
    def test_build_episode_missing_oos()
    def test_build_episode_missing_reflection()
    def test_build_episode_params_hash()

    # storage 测试
    def test_store_episode_clickhouse_success()
    def test_store_episode_postgres_success()
    def test_store_episode_both_db_failure()
    def test_store_episode_per_symbol_expansion()

    # helpers
    def test_ch_sector_query()
    def test_ch_regime_calculation()
```

**Mock 策略**:
```python
# Mock ClickHouseDB
with patch('scripts.ai_trader.memory.episode_store.ClickHouseDB') as mock_ch:
    mock_client = MagicMock()
    mock_client.query.return_value = [{"sector": "Technology"}]
    mock_ch.return_value = mock_client
```

**工作量**: 1.5 小时

---

### 3. Memory - Aggregator (memory/aggregator.py)
**为什么重要**: 为 Brain 提供历史记忆上下文（top providers, symbols, sectors）

**文件**: `scripts/ai_trader/memory/aggregator.py` (137 lines)

**核心函数**:
- `memory_summary()` - 汇总记忆快照
- `top_providers()` - 最优 provider 排名
- `top_by_sector()` - 按行业排名
- `top_by_regime()` - 按市场状态排名
- `top_symbols()` - 最优 symbol 排名

**建议测试用例** (8-10 个):
```python
class TestMemoryAggregator:
    """Test memory aggregation queries"""

    def test_top_providers_with_data()
    def test_top_providers_no_data()
    def test_top_providers_min_trades_filter()

    def test_top_by_sector_grouping()
    def test_top_by_sector_top_k_limit()

    def test_top_by_regime_volatility()
    def test_top_by_regime_trend()

    def test_top_symbols_ranking()

    def test_memory_summary_full()
    def test_memory_summary_no_clickhouse()
```

**Mock 策略**:
```python
# Mock ClickHouse query results
mock_ch.query.return_value = [
    {"provider": "deepseek_nvidia", "avg_sharpe": 1.35, "trades": 50},
    {"provider": "openai", "avg_sharpe": 1.20, "trades": 45}
]
```

**工作量**: 1.5 小时

---

### 4. Orchestrator - Evening Learning (orchestrator.py)
**为什么重要**: 已有 morning_sequence 测试，但 evening_learning 未覆盖

**缺失测试**:
- `evening_learning()` - 完整工作流
- `intraday_sequence()` - 日内交易序列
- `afterhours_sequence()` - 盘后序列
- `overnight_sequence()` - 隔夜序列

**建议测试用例** (8-10 个):
```python
class TestOrchestratorEvening:
    """Test evening learning workflow"""

    def test_evening_learning_full_flow()
    def test_evening_learning_hpo_gate_pass()
    def test_evening_learning_hpo_gate_block()
    def test_evening_learning_no_best_params()
    def test_evening_learning_brain_approval()
    def test_evening_learning_brain_rejection()

class TestOrchestratorOtherSequences:
    """Test other time-based sequences"""

    def test_intraday_fast_mode()
    def test_intraday_full_mode()
    def test_afterhours_sequence()
    def test_overnight_sequence()
```

**工作量**: 1.5 小时

---

## 🟡 Medium Priority - 核心功能 (建议测试)

### 5. Evaluators - Reflection (evaluators/reflection.py)
**为什么重要**: LLM 推理一致性检查

**文件**: `scripts/ai_trader/evaluators/reflection.py` (40 lines)

**核心函数**:
- `extract_claims()` - 从 LLM 文本提取声明
- `check_claims()` - 与实际特征对比验证

**建议测试用例** (6 个):
```python
class TestReflection:
    def test_extract_claims_above_vwap()
    def test_extract_claims_rvol_gt_2()
    def test_extract_claims_break_premarket_high()

    def test_check_claims_all_pass()
    def test_check_claims_all_fail()
    def test_check_claims_partial()
```

**工作量**: 0.5 小时

---

### 6. Evaluators - Scorecard (evaluators/scorecard.py)
**为什么重要**: 性能评估和指标计算

**预期功能**: 计算 Sharpe, MDD, Win Rate 等指标

**建议测试用例** (6-8 个):
```python
class TestScorecard:
    def test_compute_sharpe_ratio()
    def test_compute_max_drawdown()
    def test_compute_win_rate()
    def test_compute_metrics_empty_trades()
    def test_compute_metrics_single_trade()
    def test_compute_metrics_all_wins()
    def test_compute_metrics_all_losses()
```

**工作量**: 1 小时

---

### 7. Reflection - Analyzer (reflection/analyzer.py)
**为什么重要**: 分析交易后表现

**建议测试用例** (6-8 个):
```python
class TestReflectionAnalyzer:
    def test_analyze_day_trades()
    def test_compute_trade_metrics()
    def test_group_by_provider()
    def test_group_by_symbol()
    def test_detect_patterns()
```

**工作量**: 1 小时

---

### 8. Reflection - Report Builder (reflection/report_builder.py)
**为什么重要**: 生成每日 reflection 报告

**建议测试用例** (5 个):
```python
class TestReportBuilder:
    def test_build_daily_report()
    def test_aggregate_consistency()
    def test_format_output_json()
    def test_handle_no_trades()
```

**工作量**: 0.75 小时

---

### 9. Reflection - LLM Reflector (reflection/llm_reflector.py)
**为什么重要**: LLM 驱动的反思分析

**建议测试用例** (5-7 个):
```python
class TestLLMReflector:
    def test_reflect_on_trades()
    def test_identify_mistakes()
    def test_suggest_improvements()
    def test_timeout_handling()
    def test_invalid_response()
```

**工作量**: 1 小时

---

### 10. Evolvers - Decision Engine (evolvers/decision_engine.py)
**为什么重要**: RL 决策引擎

**建议测试用例** (6-8 个):
```python
class TestDecisionEngine:
    def test_make_decision_with_state()
    def test_update_policy()
    def test_explore_exploit_balance()
```

**工作量**: 1 小时

---

### 11. Evolvers - Prompt Evolver (evolvers/prompt_evolver.py)
**为什么重要**: 动态优化 LLM prompt

**建议测试用例** (5 个):
```python
class TestPromptEvolver:
    def test_evolve_prompt_based_on_feedback()
    def test_mutate_prompt()
    def test_crossover_prompts()
```

**工作量**: 1 小时

---

## 🟢 Low Priority - 辅助功能 (可选测试)

### 12. Context Analyzer (context/context_analyzer.py)
**工作量**: 0.75 小时
**测试数**: 4-5 个

### 13. MLflow Utils (mlflow_utils.py)
**工作量**: 0.5 小时
**测试数**: 3-4 个

### 14. RL Eval (rl/eval_rl.py)
**工作量**: 1 小时
**测试数**: 5-6 个

### 15. Reports - Reporter (reports/reporter.py)
**工作量**: 0.75 小时
**测试数**: 4-5 个

### 16. Consistency Job (reflection/consistency_job.py)
**工作量**: 0.75 小时
**测试数**: 4-5 个

### 17. CLI (cli.py)
**为什么低优先级**: 主要是命令行包装，核心逻辑已在 Orchestrator 测试

**建议测试用例** (3-4 个):
```python
class TestCLI:
    def test_morning_command()
    def test_intraday_command()
    def test_learn_command()
    def test_learn_with_workflow()
```

**工作量**: 0.5 小时

---

## 📈 优先级决策矩阵

| 模块 | 业务影响 | 复杂度 | 缺陷风险 | 优先级 | 工作量 |
|------|---------|--------|---------|--------|--------|
| Brain | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | 🔴 High | 2h |
| Episode Store | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐ | 🔴 High | 1.5h |
| Memory Aggregator | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐ | 🔴 High | 1.5h |
| Evening Learning | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | 🔴 High | 1.5h |
| Reflection | ⭐⭐⭐⭐ | ⭐⭐ | ⭐⭐⭐ | 🟡 Medium | 0.5h |
| Scorecard | ⭐⭐⭐⭐ | ⭐⭐ | ⭐⭐⭐ | 🟡 Medium | 1h |
| Analyzer | ⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐ | 🟡 Medium | 1h |
| Report Builder | ⭐⭐⭐ | ⭐⭐ | ⭐⭐ | 🟡 Medium | 0.75h |
| LLM Reflector | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ | 🟡 Medium | 1h |
| Decision Engine | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ | 🟡 Medium | 1h |
| Prompt Evolver | ⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐ | 🟡 Medium | 1h |
| Context Analyzer | ⭐⭐ | ⭐⭐ | ⭐⭐ | 🟢 Low | 0.75h |
| MLflow Utils | ⭐⭐ | ⭐⭐ | ⭐ | 🟢 Low | 0.5h |
| RL Eval | ⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐ | 🟢 Low | 1h |
| Reporter | ⭐⭐ | ⭐⭐ | ⭐ | 🟢 Low | 0.75h |
| Consistency Job | ⭐⭐ | ⭐⭐ | ⭐ | 🟢 Low | 0.75h |
| CLI | ⭐ | ⭐ | ⭐ | 🟢 Low | 0.5h |

---

## 🎯 建议实施路径

### Phase 1: 核心路径 (6-8 小时)
**目标**: 覆盖关键决策和持久化逻辑

1. **Brain** (2h) - LLM 决策引擎
2. **Episode Store** (1.5h) - 持久化
3. **Memory Aggregator** (1.5h) - 记忆检索
4. **Evening Learning** (1.5h) - 学习工作流

**成果**: 覆盖率提升至 ~50%

---

### Phase 2: 评估与反思 (5-7 小时)
**目标**: 覆盖性能评估和学习反馈

5. **Reflection** (0.5h) - 一致性检查
6. **Scorecard** (1h) - 性能指标
7. **Analyzer** (1h) - 交易分析
8. **Report Builder** (0.75h) - 报告生成
9. **LLM Reflector** (1h) - LLM 反思
10. **Decision Engine** (1h) - RL 决策
11. **Prompt Evolver** (1h) - Prompt 优化

**成果**: 覆盖率提升至 ~75%

---

### Phase 3: 辅助功能 (3-5 小时)
**目标**: 完善周边工具

12-17. 其他低优先级模块

**成果**: 覆盖率达到 ~85%

---

## 📊 总结统计

### 当前状态
- ✅ **已测试**: 6 模块, 78 测试用例, 100% 通过
- ⏳ **未测试**: 17 模块
- 📊 **覆盖率**: 26% (6/23)

### 完成 Phase 1 后
- ✅ **已测试**: 10 模块, ~120 测试用例
- ⏳ **未测试**: 13 模块
- 📊 **覆盖率**: ~50%

### 完成 Phase 2 后
- ✅ **已测试**: 17 模块, ~180 测试用例
- ⏳ **未测试**: 6 模块
- 📊 **覆盖率**: ~75%

### 完成 Phase 3 后
- ✅ **已测试**: 23 模块, ~220 测试用例
- ⏳ **未测试**: 0 模块
- 📊 **覆盖率**: ~85%

---

## 🔧 测试编写指南

### Mock 模式汇总

**1. LLM Provider Mock**:
```python
with patch('importlib.import_module') as mock_import:
    mock_provider = MagicMock()
    async def mock_analyze():
        return MagicMock(raw_response='{"key": "value"}')
    mock_provider.analyze_market_with_prompt = mock_analyze
    mock_module = MagicMock(DeepSeekNvidiaProvider=MagicMock(return_value=mock_provider))
    mock_import.return_value = mock_module
```

**2. ClickHouse Mock**:
```python
with patch('scripts.ai_trader.memory.episode_store.ClickHouseDB') as mock_ch:
    mock_client = MagicMock()
    mock_client.query.return_value = [{"sector": "Technology"}]
    mock_ch.return_value = mock_client
```

**3. PostgreSQL Mock**:
```python
async def test_postgres():
    with patch('scripts.ai_trader.memory.episode_store.PostgresDB') as mock_pg:
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.execute = AsyncMock()
        mock_pg.return_value = mock_client
```

**4. File System Mock** (使用现有 fixture):
```python
def test_with_temp_dir(temp_dir: Path):
    # temp_dir 自动清理
    file = temp_dir / "test.json"
    file.write_text('{}')
```

---

## 🚀 快速开始

### 创建新测试文件

```bash
# Brain 测试
touch scripts/ai_trader/tests/unit/test_brain.py

# Memory 测试
touch scripts/ai_trader/tests/unit/test_memory.py

# Evaluators 测试
touch scripts/ai_trader/tests/unit/test_evaluators.py
```

### 测试文件模板

```python
"""Unit tests for [Module Name]"""
import pytest
from unittest.mock import Mock, patch, MagicMock, AsyncMock

from scripts.ai_trader.[module_path] import [ClassName]


class Test[ClassName]:
    """Test [ClassName] class"""

    def test_[method_name]_success(self, mock_config, temp_dir):
        """Test [description]"""
        # Arrange
        obj = [ClassName](...)

        # Act
        result = obj.method()

        # Assert
        assert result["status"] == "success"
```

---

## 📝 下一步行动

1. **立即开始**: 实施 Phase 1 (Brain + Memory + Evening Learning)
2. **边测边修**: 发现 bug 立即修复，更新测试
3. **文档同步**: 更新 TEST_SUMMARY.md 统计覆盖率
4. **CI 集成**: 接入 GitHub Actions 自动运行

---

**生成时间**: 2025-10-29
**维护者**: Trading Team
**相关文档**: TEST_FIX_REPORT.md, TEST_SUMMARY.md, TESTING_COMPLETE.md
