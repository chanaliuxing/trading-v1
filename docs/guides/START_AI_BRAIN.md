# AI 大脑启动指南 🧠

## 🎯 目标: 让 AI 大脑不间断地进行机器学习优化,训练出能赚钱的模型

---

## ⚡ 快速启动 (推荐)

### 方式 1: 全自动模式 (推荐 - 全天候运行)

**一条命令启动,全自动运行:**

```bash
ENV_MODE=paper venv/bin/python scripts/ai_trader/auto_loop.py
```

**功能:**
- ✅ 自动启动并监控 `control_event_consumer` (执行 Brain 动作)
- ✅ 自动启动并监控 `ml_scheduler` (执行 ML 训练任务)
- ✅ 周而复始地让 AI 大脑根据结果调整配置、发起新的训练
- ✅ 进程挂了会自动重启

**适合:** 放到 tmux/screen 里长期运行

---

### 方式 2: 手动控制模式 (更灵活)

**分 3 个终端运行:**

#### 终端 1: 启动控制事件消费者
```bash
ENV_MODE=paper venv/bin/python scripts/ai_trader/control_event_consumer.py
```

**作用:** 执行 Brain 的决策 (fix_config, queue_job, backfill 等)

---

#### 终端 2: 启动 ML 调度器
```bash
ENV_MODE=paper venv/bin/python scripts/ai_trader/ml_scheduler.py
```

**作用:** 执行机器学习任务队列 (HPO, OOF, alpha_loop 等)

---

#### 终端 3: 触发学习任务
```bash
# 单次完整学习 (晚上学习 + Brain 评审)
ENV_MODE=paper venv/bin/python scripts/ai_trader/cli.py learn
```

**或者,持续学习循环:**
```bash
ENV_MODE=paper venv/bin/python tools/ml_brain_loop.py \
  --symbols TSLA AAPL \
  --start-job-type oof \
  --sleep-sec 10
```

---

## 📊 查看运行状态

### 🎯 一键总览 (每天必看!)

```bash
venv/bin/python tools/brain_dashboard.py
```

**显示内容:**
- 0️⃣ 基础文件自检: 今天/昨天的日志是否存在
- 1️⃣ Daily Brain Report: 最近 ML job 的 Sharpe/IC 对比
- 2️⃣ Brain 效果验证: 最近 7 天动作成功率统计

---

### 📈 查看详细报告

```bash
# 最近 2 天的 Brain 日报
venv/bin/python tools/brain_daily_report.py --days 2 --runs-dir runs/ai_trader

# Brain 效果验证 (7 天统计)
venv/bin/python tools/validate_brain_effectiveness.py --days 7 --runs-dir runs/ai_trader

# 查看特定 job 的详情
venv/bin/python scripts/ai_trader/tools/review_ml_job.py \
  --job-id <job_id> \
  --runs-dir runs/ai_trader
```

---

### 📝 查看日志

```bash
# 主日志 (Orchestrator/Brain 决策)
tail -f ~/grafana-stack/logs/trader.log

# 或者
tail -f ./logs/trader.log

# ML 事件日志 (每个训练任务的结果)
cat runs/ai_trader/$(date +%F)/ml_events.jsonl

# Brain 动作日志 (Brain 做了什么调整)
cat runs/ai_trader/$(date +%F)/ml_actions.jsonl

# Brain 评审报告
cat runs/ai_trader/$(date +%F)/ml_review.json | jq '.review'
```

---

## 🛑 停止运行

```bash
# 一键停止所有 AI Brain 相关进程
venv/bin/python scripts/ai_trader/stop_brain.py

# 或者手动停止
ps aux | grep 'alpha_loop.py\|ml_scheduler\|control_event_consumer\|auto_loop'
kill <PID>
```

---

## 🔍 监控 Brain "干得对不对"

### 关键指标

**好的表现:**
- ✅ `ΔSharpe` 逐渐提升 (OOS Sharpe 在增加)
- ✅ `ΔIC` 保持正值 (Information Coefficient 改善)
- ✅ `fix_config` 动作逐渐收敛 (不再频繁调整)
- ✅ 交易数量合理 (每个 fold 有 5-20 笔交易)

**需要关注:**
- ⚠️ `ΔSharpe` 持续下降
- ⚠️ 大量 `data_corruption` / `ic_nan` 错误
- ⚠️ `fix_config` 频繁改动 (每天多次)
- ⚠️ 交易数量极少 (< 5 笔) 或极多 (> 100 笔)

### 审计文件位置

所有 Brain 动作都有审计记录,**不是黑箱**:

```
runs/ai_trader/<date>/
├── ml_events.jsonl          # 每个 ML 任务的结果
├── ml_actions.jsonl         # Brain 的所有动作
├── ml_review.json           # Brain 的评审报告
├── hpo_retry_audit.json     # HPO 重试记录
├── hpo_stop_loss_report.json # HPO 止损报告
├── features_build/          # 特征构建结果
└── feature_eval/            # 特征评估报告
```

**配置变更备份:**
```
backups/policies/            # 所有 Brain 修改过的配置
backups/alpha_config/        # alpha_config 历史版本
```

**查看 Brain 动作效果:**
```bash
venv/bin/python tools/summarize_ml_actions.py \
  --days 7 \
  --runs-dir runs/ai_trader \
  --output runs/ai_trader/$(date +%F)/ml_branches_summary.json
```

---

## 🎓 理解自动化流程

### Brain 的决策循环

```
1. ML Job 完成 (HPO/OOF)
   ↓
2. Brain 评审结果 (review_ml)
   ├─ 检查 Sharpe/IC/trades
   ├─ 诊断问题 (data_corruption/ic_nan/low_trades)
   └─ 决定下一步动作
   ↓
3. 执行动作
   ├─ fix_config: 调整配置参数
   ├─ queue_job: 发起新的训练任务
   ├─ backfill: 补充历史数据
   ├─ extend_window: 扩大训练窗口
   └─ reduce_window: 缩小训练窗口
   ↓
4. ML Scheduler 执行任务
   ↓
5. 返回步骤 1 (循环)
```

### Brain 可以做什么

**自动调整配置:**
- `ml_policy.json` - ML 模型参数
- `oof_policies.json` - OOF 策略配置
- `gating_policies.json` - 模型 gating 阈值
- `alpha_config_enhanced.json` - HPO 配置

**自动发起任务:**
- `alpha_loop` - 完整的 HPO 循环
- `hpo_cli` - 短期 HPO 任务
- `oof` - Out-of-Fold 预测
- `feature_build` - 构建新特征
- `feature_eval` - 评估特征质量

**自动数据修复:**
- `backfill` - 补充缺失数据
- `extend_window` - 扩大训练窗口
- `reduce_window` - 缩小训练窗口

---

## 🚀 推荐工作流

### 第一次启动 (测试模式)

```bash
# 1. 启动必要的后台进程 (方式 2)
# 终端 1
ENV_MODE=paper venv/bin/python scripts/ai_trader/control_event_consumer.py

# 终端 2
ENV_MODE=paper venv/bin/python scripts/ai_trader/ml_scheduler.py

# 2. 触发一次学习
# 终端 3
ENV_MODE=paper venv/bin/python scripts/ai_trader/cli.py learn

# 3. 查看结果
venv/bin/python tools/brain_dashboard.py
```

### 长期运行 (生产模式)

```bash
# 1. 在 tmux/screen 中启动 auto_loop
tmux new -s ai_brain
ENV_MODE=paper venv/bin/python scripts/ai_trader/auto_loop.py

# 2. 分离 tmux (Ctrl+B, D)

# 3. 每天查看一次状态
venv/bin/python tools/brain_dashboard.py
```

---

## 🔧 关键配置文件

### 主配置
- `alpha_config_enhanced.json` - 主 HPO 配置 (长周期训练)
- `config/alpha_config_enhanced_debug.json` - Debug 配置 (短周期测试)

### 策略配置
- `strategies/configs/enhanced_ml_TSLA.json` - TSLA 实盘配置
- `ml_policy.json` - ML 训练策略
- `config/oof_policies.json` - OOF 策略

### 特征配置
- `feature_programs.json` - DSL 特征定义
- `config/features_catalog.json` - 特征目录

---

## ⚠️ 重要提示

### 环境变量

```bash
# 必须设置 ENV_MODE
ENV_MODE=paper  # 或 dev/test/production
```

### 防止配置混乱

**研究模式 vs 实盘模式:**
- 研究模式 (HPO/debug): 使用宽松的过滤器 (`min_volume=1000`)
- 实盘模式 (实际交易): 使用保守的过滤器 (`min_volume=100000`)

**配置分离:**
- HPO 配置使用 `config_prefix: "enhanced_ml_debug"` 避免读取实盘配置
- 实盘配置不受 HPO 影响

### VSCode 崩溃怎么办?

**不用担心!** 所有日志都写入文件,VSCode 崩溃不影响:
- ✅ `trader.log` 持续写入
- ✅ `ml_events.jsonl` 持续更新
- ✅ `ml_actions.jsonl` 持续记录

**事后排查:**
```bash
# 查看崩溃前的日志
tail -n 500 ~/grafana-stack/logs/trader.log

# 查看最后的 ML 事件
cat runs/ai_trader/$(date +%F)/ml_events.jsonl | tail -10
```

---

## 📚 深入学习

### 关键文档
- `docs/ml/test_ml.md` - 详细的运行指南和问题诊断
- `CLAUDE.md` - 开发规范和架构说明
- `doc/features/upgradeFeatures_finalcheck.md` - 系统升级验证

### 工具脚本
- `tools/brain_dashboard.py` - 一键总览
- `tools/brain_daily_report.py` - 每日报告
- `tools/validate_brain_effectiveness.py` - Brain 效果验证
- `tools/summarize_ml_actions.py` - 动作汇总分析

---

## 🎯 成功标准

### 短期目标 (1-2 周)
- [ ] Brain 能稳定运行不崩溃
- [ ] 每天产生 1-2 个 ML job
- [ ] OOS Sharpe 在 0.5-1.0 范围
- [ ] 每个 fold 有 5-20 笔交易

### 中期目标 (1 个月)
- [ ] OOS Sharpe > 1.0
- [ ] IC > 0.02 (持续正相关)
- [ ] Win Rate > 52%
- [ ] Brain fix_config 动作收敛

### 长期目标 (3 个月)
- [ ] OOS Sharpe > 1.5
- [ ] 实盘验证 (Paper Trading)
- [ ] 多 symbol 扩展 (AAPL, NVDA, etc.)
- [ ] 特征自动发现稳定

---

## 💡 常见问题

### Q: Brain 一直在调整配置,是不是出问题了?
A: 前 1-2 周是正常的探索期。如果 2 周后还在频繁调整 (每天 >5 次 fix_config),需要检查:
- 数据质量 (data_corruption)
- 特征稳定性 (ic_nan)
- 训练窗口设置

### Q: 为什么交易数量很少?
A: 检查:
- `min_volume` 设置 (研究模式应该 ≤ 10000)
- `conf_floor` 设置 (研究模式应该 = 0)
- Decision scheme (threshold 太高会导致很少信号)

### Q: Sharpe 一直是负数,怎么办?
A: 说明策略还没找到 edge,需要:
1. 检查标签分布 (是否太窄/噪声太大)
2. 检查特征 IC (是否有预测力)
3. 尝试不同的 label_profile
4. 扩大训练窗口

### Q: 如何知道 Brain 是在帮忙还是在瞎搞?
A: 使用 `validate_brain_effectiveness.py`:
```bash
venv/bin/python tools/validate_brain_effectiveness.py --days 7
```
看 "有 Brain 干预" vs "无 Brain 干预" 的 IC/Sharpe 对比。

---

## 🔗 相关链接

- [Model Gating 实现](./scripts/ai_trader/control_event_consumer.py:60-194)
- [实时推理服务](./inference/manager.py)
- [StrategyEngine 集成](./services/strategy/engine.py:582-593)
- [测试验证报告](./doc/analysis/final_implementation_report_20251120.md)

---

**最后更新**: 2025-11-20
**维护者**: AI Trading Team

🎉 祝你的 AI 大脑训练出能赚钱的模型!
