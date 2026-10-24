# Phase 4B 实施 Checklist

> 快速参考版本，详细实现见 `phase_4b_implementation_plan.md`

---

## Step 1: HPO → ExperimentRecord (Week 1)

### 🎯 目标
HPO 完成后自动生成并写入实验记录

### 📝 任务清单

- [ ] **1.1 创建 `experiments/writer.py`**
  - 函数: `make_run_id()` - 生成唯一 run_id
  - 函数: `register_hpo_result()` - 保存模型 + 写 manifest + 写 JSONL
  - 参考: 实施计划 3.3.1 节

- [ ] **1.2 修改 HPO 入口**
  - 文件: `tools/nightly_hpo.py` (或你实际用的)
  - 修改点: HPO 完成处调用 `register_hpo_result()`
  - 参考: 实施计划 3.3.2 节

- [ ] **1.3 编写验证脚本**
  - 文件: `tools/test_hpo_writer.py`
  - 验证: 模型保存 + manifest.json + JSONL 写入
  - 参考: 实施计划 3.4.1 节

- [ ] **1.4 真实 HPO 测试**
  ```bash
  # 跑一次真实 HPO
  python tools/nightly_hpo.py --symbol NVDA --horizon 10m

  # 检查输出
  ls -lR models/NVDA/
  tail -1 experiments/experiment_index.jsonl | jq .
  ```

### ✅ 完成标准
- [ ] models/ 目录下有 `{symbol}/{run_id}/model.pkl`
- [ ] models/ 目录下有 `{symbol}/{run_id}/manifest.json`
- [ ] `experiments/experiment_index.jsonl` 最后一行是新记录
- [ ] 日志显示 `Registered experiment, run_id=...`

---

## Step 2: 模型运行时 LoadedStrategy (Week 2)

### 🎯 目标
根据 StrategyManifest 加载模型并预测

### 📝 任务清单

- [ ] **2.1 扩展 `strategies/runtime.py`**
  - 类: `LoadedStrategy` - 统一预测接口
  - 函数: `load_strategy()` - 根据 manifest 加载
  - 函数: `load_strategy_by_run_id()` - 根据 run_id 查找并加载
  - 参考: 实施计划 4.2.1 节

- [ ] **2.2 编写验证脚本**
  - 文件: `tools/test_strategy_runtime.py`
  - 验证: 加载模型 + 预测正常 + 特征对齐
  - 参考: 实施计划 4.3.1 节

- [ ] **2.3 性能测试**
  ```bash
  # 测试预测延迟
  python tools/test_strategy_runtime.py --benchmark
  ```

### ✅ 完成标准
- [ ] `load_strategy_by_run_id("xxx")` 成功返回 LoadedStrategy
- [ ] `.predict(df)` 返回正确 shape 的 ndarray
- [ ] 预测延迟 P99 < 100ms
- [ ] 支持 XGBoost, LightGBM, sklearn (至少 2 种)

---

## Step 3: Live Orchestrator 接入 (Week 3)

### 🎯 目标
开盘前 AI 大脑选策略，盘中执行

### 📝 任务清单

- [ ] **3.1 创建 `scripts/ai_trader/strategy_manager.py`**
  - 类: `StrategyManager` - 管理 active strategies
  - 方法: `apply_brain_decision()` - 根据大脑决策加载策略
  - 方法: `predict()` - 聚合多策略预测
  - 参考: 实施计划 5.3.1 节

- [ ] **3.2 修改 `scripts/ai_trader/brain/brain.py`**
  - 新增: `select_strategy_for_symbol()` - 调用 StrategySelector
  - 集成: PromptLibrary, FeatureRegistry
  - 参考: 实施计划 5.3.2 节

- [ ] **3.3 修改 orchestrator 入口**
  - 文件: `scripts/ai_trader/control_event_consumer.py`
  - 新增: `on_market_open()` - 开盘前选策略
  - 修改: `on_bar()` - 使用 strategy_manager.predict()
  - 参考: 实施计划 5.3.3 节

- [ ] **3.4 添加策略选择 prompt**
  - 文件: `prompts/prompt_library.json`
  - 新增: `"v3_strategy_selector": "..."`
  - 参考: 实施计划 八、附录

- [ ] **3.5 Dry-run 测试**
  - 文件: `tools/test_orchestrator_dry_run.py`
  - 验证: 策略加载 + 预测 + 不下单
  - 参考: 实施计划 5.4.1 节

- [ ] **3.6 Paper trading 测试**
  ```bash
  # config.yml 设置 mode: paper
  python scripts/ai_trader/control_event_consumer.py
  ```

- [ ] **3.7 小仓位实盘测试**
  - 1-2 个 symbol
  - max_position_size: 100
  - 运行 1 周，每日 review 日志

### ✅ 完成标准
- [ ] 开盘前大脑成功选出 run_id
- [ ] StrategyManager 成功加载策略
- [ ] 盘中每个 bar 都有预测输出
- [ ] Paper trading PnL 合理 (无异常大额亏损)
- [ ] 日志完整，包含 run_id, prediction, decision

---

## 风险检查清单

### 每次修改后必须检查

- [ ] **模型加载失败自动降级**
  ```python
  try:
      strategy = load_strategy_by_run_id(run_id)
  except Exception:
      # 降级为 HOLD，不应炸掉整个系统
  ```

- [ ] **预测超时保护**
  ```python
  prediction = strategy.predict(df, timeout=200)  # ms
  ```

- [ ] **特征缺失检测**
  ```python
  missing = [f for f in required if f not in df.columns]
  if missing:
      raise ValueError(...)
  ```

- [ ] **日志结构化**
  ```python
  logger.info(
      "Strategy prediction",
      symbol=symbol,
      run_id=run_id,
      prediction=pred,
      latency_ms=elapsed,
  )
  ```

- [ ] **Metrics 暴露**
  ```python
  STRATEGIES_LOADED.labels(symbol=symbol).inc()
  PREDICTION_LATENCY.labels(symbol=symbol).observe(elapsed)
  ```

---

## 分工建议

### Option 1: 全部交给 Claude

```bash
# Step 1
给 Claude: "请实现 Phase 4B Step 1，参考 phase_4b_implementation_plan.md 第三节"

# Step 2
给 Claude: "请实现 Phase 4B Step 2，参考 phase_4b_implementation_plan.md 第四节"

# Step 3
给 Claude: "请实现 Phase 4B Step 3，参考 phase_4b_implementation_plan.md 第五节"
```

### Option 2: 你写核心，Claude 写测试

```bash
# 你自己写
- experiments/writer.py
- strategies/runtime.py (LoadedStrategy)
- strategy_manager.py

# Claude 写
- tools/test_*.py (所有测试脚本)
- 修改现有 HPO 入口 (你指定哪个文件)
- 修改 orchestrator (你指定修改点)
```

### Option 3: 混合模式 (推荐)

**你负责设计 + Review**:
- 决定 run_id 格式
- 决定目录结构 (models/ 怎么放)
- 决定 orchestrator 修改点
- Review 所有代码

**Claude 负责实现**:
- 写所有新文件
- 写所有测试
- 修改现有文件 (你给定位置)

---

## 时间估算

| 阶段 | 时间 | 前置条件 |
|------|------|----------|
| Step 1 | 1-2 天 | 无 |
| Step 2 | 1 天 | Step 1 完成 |
| Step 3.1-3.5 | 2-3 天 | Step 2 完成 |
| Step 3.6 Paper | 1-2 天 | Step 3.1-3.5 完成 |
| Step 3.7 实盘 | 1 周 | Paper trading 稳定 |
| **总计** | **2-3 周** | - |

---

## 下一步行动

### 🚀 立即开始

1. **Review 实施计划**
   ```bash
   cat docs/ml/phase_4b_implementation_plan.md
   ```

2. **决定分工模式** (见上面 Option 1/2/3)

3. **开始 Step 1**
   - 创建 `experiments/writer.py`
   - 或直接给 Claude: "请实现 Step 1"

### 📞 需要澄清的问题

- [ ] HPO 入口是哪个文件？`tools/nightly_hpo.py` 还是其他？
- [ ] Orchestrator 入口是哪个文件？`control_event_consumer.py`？
- [ ] 现有的 HPO 结果结构是什么？(best_model, best_params, metrics 的格式)
- [ ] 希望 run_id 格式是什么？(建议: `{symbol}_{date}_{horizon}_{model}_{hash6}`)

---

**更新日期**: 2025-03-18
**版本**: v1.0
