# AI大脑三模型管理 - 详细执行清单
# AI Brain Three-Model Management - Detailed Execution Checklist

**版本**: 1.0
**创建日期**: 2025-11-01
**状态**: Ready for Execution
**关联文档**: `AI_BRAIN_THREE_MODEL_MANAGEMENT.md`

---

## 执行原则 (Execution Principles)

### ✅ 必须遵守 (MUST Follow)

1. **每步验证**: 每个命令执行后必须检查输出和结果
2. **记录日志**: 所有输出保存到dated日志文件
3. **失败即停**: 遇到错误立即停止,排查后再继续
4. **备份优先**: 任何修改配置前先备份
5. **增量执行**: 不跳步,按顺序逐步完成
6. **Be Honest**: 如实记录结果,包括失败和警告

### ❌ 严格禁止 (NEVER Do)

1. **跳过验证步骤**
2. **批量执行未测试的命令**
3. **在生产环境直接测试**
4. **忽略警告和错误**
5. **修改核心代码而不测试**

---

## Pre-Flight 检查清单

### 环境检查 (Environment Check)

```bash
# 1. 检查 Docker 服务状态
cd .
make verify

# 预期输出: All services healthy (ClickHouse, Postgres, Redis, Kafka)
# ✅ 通过标准: 所有服务返回 "OK" 或 "healthy"
# ❌ 失败处理: 如果任何服务失败,运行 make down && make up
```

**验证清单**:
- [ ] ClickHouse 响应 (端口 8123)
- [ ] PostgreSQL 连接正常
- [ ] Redis 可访问
- [ ] Kafka broker 运行中

```bash
# 2. 检查 ClickHouse 数据库和表
docker exec trading_clickhouse clickhouse-client --query "SHOW DATABASES"

# 预期输出: 应包含 trading, lake, sfs
# ✅ 通过标准: 三个数据库都存在
```

**验证清单**:
- [ ] `trading` 数据库存在
- [ ] `lake` 数据库存在
- [ ] `sfs` 数据库存在

```bash
# 3. 检查特征表是否有数据
docker exec trading_clickhouse clickhouse-client --query \
  "SELECT count() FROM trading.features_1m WHERE symbol IN ('TSLA','AAPL','NVDA') AND ts > now() - INTERVAL 7 DAY"

# 预期输出: 行数 > 10000 (每个symbol约7天 * 390分钟/天)
# ✅ 通过标准: count > 5000
# ❌ 失败处理: 运行 make recon1m && make compute-features-all
```

**验证清单**:
- [ ] 特征数据存在且最近 (< 7天)
- [ ] 至少有3个测试symbol的数据

```bash
# 4. 检查 OOF 数据是否存在
docker exec trading_clickhouse clickhouse-client --query \
  "SELECT count() FROM lake.xgb_oof_h5 WHERE symbol='TSLA' LIMIT 1"

docker exec trading_clickhouse clickhouse-client --query \
  "SELECT count() FROM lake.lstm_oof_h5 WHERE symbol='TSLA' LIMIT 1"

# 预期输出: 两个查询都返回 > 0
# ✅ 通过标准: 两个表都有数据
# ❌ 失败处理: 需要运行模型训练生成 OOF 数据 (见下文)
```

**验证清单**:
- [ ] `lake.xgb_oof_h5` 表存在且有数据
- [ ] `lake.lstm_oof_h5` 表存在且有数据

```bash
# 5. 检查 AI 配置文件
ls -lh configs/ai_trader.yaml

# 预期输出: 文件存在且可读
# ✅ 通过标准: 文件大小 > 1KB
```

**验证清单**:
- [ ] `configs/ai_trader.yaml` 存在
- [ ] 文件格式正确 (YAML语法)

```bash
# 6. 验证 Python 工具存在
ls -lh tools/verify_oof_view.py
ls -lh tools/backtest_ensemble_compare.py
ls -lh tools/nightly_hpo.py

# 预期输出: 三个文件都存在
# ✅ 通过标准: 所有文件存在且可执行
```

**验证清单**:
- [ ] `tools/verify_oof_view.py` 存在
- [ ] `tools/backtest_ensemble_compare.py` 存在
- [ ] `tools/nightly_hpo.py` 存在

---

## 第一步: Baseline 确认 (Step 1: Baseline Confirmation)

**目标**: 创建和验证 OOF 视图,确认数据完整性

### 1.1 创建 OOF 视图

```bash
# 命令
cd .
make setup-oof-view 2>&1 | tee logs/step1_setup_oof_$(date +%Y%m%d_%H%M%S).log

# 预期输出:
# Creating/refreshing ClickHouse OOF tables and view
# ✓ View 'trading.features_1m_with_oof' created successfully
# ✓ Schema verified: xgb_signal, xgb_conf, lstm_signal, lstm_conf columns present

# ✅ 成功标准:
- 命令返回码 = 0
- 输出包含 "successfully"
- 无 ERROR 或 CRITICAL 日志

# ❌ 失败处理:
# 如果报错 "Table not found":
#   → 检查 lake.xgb_oof_h5 和 lake.lstm_oof_h5 是否存在
#   → 运行模型训练生成 OOF 数据
# 如果报错 "Permission denied":
#   → 检查 ClickHouse 权限配置
```

**执行清单**:
- [ ] 命令执行完成无错误
- [ ] 日志文件保存到 `logs/step1_setup_oof_*.log`
- [ ] 输出确认视图创建成功

### 1.2 验证 OOF 视图结构

```bash
# 命令
docker exec trading_clickhouse clickhouse-client --query \
  "DESCRIBE trading.features_1m_with_oof FORMAT Pretty"

# 预期输出: 应包含以下关键列
# - xgb_signal (Float64)
# - xgb_conf (Float64)
# - lstm_signal (Float64)
# - lstm_conf (Float64)
# - close, volume 等标准特征列

# ✅ 成功标准:
- 至少4个OOF列存在 (xgb_signal, xgb_conf, lstm_signal, lstm_conf)
- 数据类型正确 (Float64)
```

**执行清单**:
- [ ] 视图包含 `xgb_signal` 列
- [ ] 视图包含 `xgb_conf` 列
- [ ] 视图包含 `lstm_signal` 列
- [ ] 视图包含 `lstm_conf` 列

### 1.3 验证 OOF 数据覆盖率

```bash
# 命令 (测试 3 个 symbol)
cd .
make verify-oof SYMS="TSLA,AAPL,NVDA" \
  START="2025-10-01 09:30:00" \
  END="2025-10-31 16:00:00" \
  2>&1 | tee logs/step1_verify_oof_$(date +%Y%m%d_%H%M%S).log

# 预期输出:
# Symbol: TSLA
#   Total bars: 8190 (21 trading days × 390 minutes/day)
#   XGB coverage: 8150 (99.5%)
#   LSTM coverage: 8120 (99.1%)
# Symbol: AAPL
#   ...

# ✅ 成功标准:
- 每个 symbol 覆盖率 > 95%
- 无 "OOF data missing" 警告

# ❌ 失败处理:
# 如果覆盖率 < 80%:
#   → 检查训练数据时间范围是否覆盖测试期
#   → 重新训练模型或调整测试时间范围
```

**执行清单**:
- [ ] TSLA 覆盖率 > 95%
- [ ] AAPL 覆盖率 > 95%
- [ ] NVDA 覆盖率 > 95%
- [ ] 日志保存到 `logs/step1_verify_oof_*.log`

### 1.4 快速数据抽查

```bash
# 命令: 查询最近一条数据
docker exec trading_clickhouse clickhouse-client --query \
  "SELECT symbol, ts, close, xgb_signal, xgb_conf, lstm_signal, lstm_conf
   FROM trading.features_1m_with_oof
   WHERE symbol='TSLA'
   ORDER BY ts DESC
   LIMIT 1 FORMAT Vertical"

# 预期输出:
# Row 1:
# ──────
# symbol:       TSLA
# ts:           2025-10-31 15:59:00
# close:        234.56
# xgb_signal:   0.0234    (非零,范围 -1 到 1)
# xgb_conf:     0.67      (非零,范围 0 到 1)
# lstm_signal:  0.0156    (非零,范围 -1 到 1)
# lstm_conf:    0.72      (非零,范围 0 到 1)

# ✅ 成功标准:
- 所有 OOF 字段非 NULL
- signal 值在 [-1, 1] 范围
- conf 值在 [0, 1] 范围
```

**执行清单**:
- [ ] 数据最新 (ts 在最近一周内)
- [ ] OOF 信号值合理 (非 NULL, 非 NaN)
- [ ] 置信度值合理 (0-1 范围)

---

## 第二步: 三模式对比回测 (Step 2: Three-Mode Comparison Backtest)

**目标**: 对比 XGBoost-only, LSTM-only, Fusion 三种模式的表现

### 2.1 准备回测参数

```bash
# 定义测试参数 (写入临时文件便于复用)
cat > /tmp/backtest_params.sh <<'EOF'
export TEST_SYMBOLS="TSLA,AAPL,NVDA"
export TEST_START="2025-10-01 09:30:00"
export TEST_END="2025-10-31 16:00:00"
export FEATURES_TABLE="trading.features_1m_with_oof"
EOF

source /tmp/backtest_params.sh

# ✅ 参数验证:
echo "Symbols: $TEST_SYMBOLS"
echo "Period: $TEST_START to $TEST_END"
echo "Table: $FEATURES_TABLE"
```

**执行清单**:
- [ ] 参数文件创建成功
- [ ] 测试期至少包含 10 个交易日
- [ ] 测试 symbol 至少 3 个

### 2.2 运行三模式对比回测

```bash
# 命令
cd .
source /tmp/backtest_params.sh

make compare-ensemble \
  SYMS="$TEST_SYMBOLS" \
  START="$TEST_START" \
  END="$TEST_END" \
  2>&1 | tee logs/step2_compare_ensemble_$(date +%Y%m%d_%H%M%S).log

# 预期执行时间: 5-15 分钟 (取决于数据量)

# 预期输出结构:
# ===== Backtesting XGB-only mode =====
# Processing TSLA... 8190 bars
# Processing AAPL... 8150 bars
# Processing NVDA... 8200 bars
# XGB-only Results:
#   Total Return: +12.3%
#   Sharpe Ratio: 1.45
#   Win Rate: 58.2%
#   Max Drawdown: -8.7%
#
# ===== Backtesting LSTM-only mode =====
# ...
#
# ===== Backtesting Fusion mode =====
# ...
#
# ===== Comparison Summary =====
# Metric         | XGB-only | LSTM-only | Fusion
# Total Return   | +12.3%   | +9.8%     | +14.5%
# Sharpe Ratio   | 1.45     | 1.32      | 1.62
# Win Rate       | 58.2%    | 55.1%     | 60.3%
# Max Drawdown   | -8.7%    | -9.2%     | -7.1%

# ✅ 成功标准:
- 三个模式都完成回测
- 每个模式至少有 10+ 交易
- Fusion 模式表现 >= max(XGB, LSTM) × 0.95

# ❌ 失败处理:
# 如果任何模式报错:
#   → 检查日志中的具体错误信息
#   → 验证 OOF 数据完整性 (返回 Step 1.3)
# 如果 Fusion 表现显著差于单模式:
#   → 可能是权重初始化问题,继续 Step 3 HPO 优化
```

**执行清单**:
- [ ] XGB-only 模式回测完成
- [ ] LSTM-only 模式回测完成
- [ ] Fusion 模式回测完成
- [ ] 比较表格生成
- [ ] 日志保存到 `logs/step2_compare_ensemble_*.log`

### 2.3 分析回测结果

```bash
# 命令: 提取关键指标到摘要文件
cd .

# 从日志中提取比较表格
grep -A 20 "Comparison Summary" \
  logs/step2_compare_ensemble_*.log | tail -10 \
  > logs/step2_summary.txt

cat logs/step2_summary.txt

# 手动记录到执行日志:
# - 哪个模式表现最好?
# - Fusion 是否优于单模式?
# - 是否有异常结果 (如 Sharpe < 0)?
```

**执行清单**:
- [ ] 摘要文件创建: `logs/step2_summary.txt`
- [ ] 记录最佳模式: ________________
- [ ] Fusion 优于单模式: Yes / No
- [ ] 发现异常: Yes / No (如有,描述: ________________)

### 2.4 决策点: 是否继续 HPO

**决策规则**:

- ✅ **继续 HPO** (以下任一条件满足):
  - Fusion 表现比最佳单模式差 > 5%
  - Fusion Sharpe < 1.0
  - Win Rate < 52%
  - 希望进一步优化权重

- ⏸️ **暂停,人工审查** (以下任一条件满足):
  - 任何模式 Sharpe < 0
  - 任何模式 Total Return < -10%
  - XGB 和 LSTM 结果高度相关 (可能数据泄露)

**决策记录**:
- [ ] 决策: 继续 HPO / 暂停审查
- [ ] 理由: ________________________________________________

---

## 第三步: Nightly HPO 优化 (Step 3: Nightly HPO)

**目标**: 使用 Optuna 优化 Fusion 模式的 w_xgb 和 w_lstm 权重

### 3.1 生成 HPO 建议 (Dry-Run)

```bash
# 命令: 不实际运行回测,仅生成建议
cd .

make nightly-hpo 2>&1 | tee logs/step3_hpo_suggestions_$(date +%Y%m%d_%H%M%S).log

# 预期输出:
# [nightly_hpo] Starting HPO suggestion generation (mock mode)
# Symbols: TSLA, AAPL, NVDA
# Search space: w_xgb [0.3, 0.7], w_lstm [0.3, 0.7]
# Trials: 20 (mock)
#
# Best trial (mock): w_xgb=0.55, w_lstm=0.45 → Sharpe=1.75 (projected)
#
# Suggestion written to: runs/ai_trader/2025-11-01/hpo_suggestions.json

# ✅ 成功标准:
- 命令返回码 = 0
- 生成 hpo_suggestions.json 文件
- 建议的权重和为 1.0 (w_xgb + w_lstm = 1.0)
```

**执行清单**:
- [ ] HPO 建议生成完成
- [ ] 文件存在: `runs/ai_trader/YYYY-MM-DD/hpo_suggestions.json`
- [ ] 日志保存到 `logs/step3_hpo_suggestions_*.log`

### 3.2 运行真实 HPO 回测 (可选)

**警告**: 真实 HPO 可能需要 30-60 分钟,建议首次执行时先用 mock 模式

```bash
# 命令: 运行真实 Optuna 搜索 (20 trials)
cd .

make nightly-hpo REAL=1 2>&1 | tee logs/step3_hpo_real_$(date +%Y%m%d_%H%M%S).log

# 预期输出:
# [nightly_hpo] Starting real HPO with Optuna
# [I 2025-11-01 12:00:00,000] Trial 0: w_xgb=0.50, w_lstm=0.50 → Sharpe=1.58
# [I 2025-11-01 12:03:15,000] Trial 1: w_xgb=0.60, w_lstm=0.40 → Sharpe=1.62
# ...
# [I 2025-11-01 12:45:30,000] Trial 19: w_xgb=0.55, w_lstm=0.45 → Sharpe=1.68
#
# Best trial: #15 → w_xgb=0.55, w_lstm=0.45, Sharpe=1.75
#
# Suggestion written to: runs/ai_trader/2025-11-01/hpo_suggestions.json

# ✅ 成功标准:
- 所有 trials 完成 (20/20)
- Best Sharpe > Baseline (Step 2.2 结果)
- 权重在合理范围 [0.3, 0.7]
```

**执行清单**:
- [ ] HPO 完成所有 trials
- [ ] Best Sharpe > Baseline: ________ > ________
- [ ] 最佳权重: w_xgb=________, w_lstm=________
- [ ] 日志保存到 `logs/step3_hpo_real_*.log`

### 3.3 检查 HPO 建议文件

```bash
# 命令: 查看生成的建议
cd .
cat runs/ai_trader/$(date +%Y-%m-%d)/hpo_suggestions.json | jq .

# 预期输出 (示例):
# {
#   "timestamp": "2025-11-01T12:45:30",
#   "best_trial": {
#     "w_xgb": 0.55,
#     "w_lstm": 0.45,
#     "sharpe": 1.75,
#     "total_return": 0.145
#   },
#   "all_symbols": ["TSLA", "AAPL", "NVDA"],
#   "per_symbol_suggestions": {
#     "TSLA": {"w_xgb": 0.60, "w_lstm": 0.40},
#     "AAPL": {"w_xgb": 0.50, "w_lstm": 0.50},
#     "NVDA": {"w_xgb": 0.55, "w_lstm": 0.45}
#   }
# }

# ✅ 验证清单:
- JSON 格式正确
- w_xgb + w_lstm ≈ 1.0 (允许 ±0.01 误差)
- sharpe > 0
```

**执行清单**:
- [ ] JSON 格式正确
- [ ] 包含 `best_trial` 字段
- [ ] 包含 `per_symbol_suggestions` 字段
- [ ] 权重和为 1.0

### 3.4 备份当前配置 (应用前必做)

```bash
# 命令: 备份所有 symbol 的当前配置
cd .

# 创建备份目录
mkdir -p backup/configs/$(date +%Y%m%d_%H%M%S)

# 备份所有 JSON 配置
cp -r runs/ai_trader/*/strategies_config/*.json \
  backup/configs/$(date +%Y%m%d_%H%M%S)/ 2>/dev/null || true

# 验证备份
ls -lh backup/configs/$(date +%Y%m%d_%H%M%S)/

# ✅ 成功标准:
- 备份目录创建成功
- 至少有 3 个 JSON 文件被备份
```

**执行清单**:
- [ ] 备份目录: `backup/configs/YYYYMMDD_HHMMSS/`
- [ ] 备份文件数: ________ (预期 >= 3)
- [ ] 记录备份路径: ________________________________________________

---

## 第四步: AI 大脑自动应用 (Step 4: AI Brain Automatic Application)

**目标**: AI 大脑自动读取 HPO 建议并生成配置更新 (需人工审批)

### 4.1 验证 AI 大脑状态

```bash
# 命令: 检查 AI 大脑进程
ps aux | grep "auto_loop.py" | grep -v grep

# 预期输出: 应有一个 auto_loop.py 进程运行
# w   12345  ... python scripts/ai_trader/auto_loop.py

# ✅ 成功标准: 进程存在且状态正常
# ❌ 失败处理: 启动 AI 大脑
#   cd .
#   PYTHONPATH=. python scripts/ai_trader/auto_loop.py \
#     2>&1 | tee /tmp/ai_brain_$(date +%Y%m%d_%H%M%S).log &
```

**执行清单**:
- [ ] AI 大脑进程运行中
- [ ] 进程 PID: ________
- [ ] 日志文件路径: ________________________________________________

### 4.2 触发 HPO 应用 (Dry-Run)

```bash
# 命令: 应用 HPO 建议 (不实际修改配置,仅生成预览)
cd .

make hpo-apply 2>&1 | tee logs/step4_hpo_apply_preview_$(date +%Y%m%d_%H%M%S).log

# 预期输出:
# [apply_hpo] Reading HPO suggestions from runs/ai_trader/2025-11-01/hpo_suggestions.json
# [apply_hpo] Current configs:
#   TSLA: w_xgb=0.5, w_lstm=0.5
#   AAPL: w_xgb=0.5, w_lstm=0.5
#   NVDA: w_xgb=0.5, w_lstm=0.5
# [apply_hpo] Suggested changes:
#   TSLA: w_xgb=0.5→0.60, w_lstm=0.5→0.40 (Δ Sharpe: +0.12)
#   AAPL: w_xgb=0.5→0.50, w_lstm=0.5→0.50 (No change)
#   NVDA: w_xgb=0.5→0.55, w_lstm=0.5→0.45 (Δ Sharpe: +0.08)
# [apply_hpo] PREVIEW MODE: No configs modified
# [apply_hpo] Run with --force to apply changes

# ✅ 成功标准:
- 读取到 HPO 建议
- 对比显示差异
- 未实际修改文件 (PREVIEW MODE)
```

**执行清单**:
- [ ] HPO 建议读取成功
- [ ] 差异对比清晰
- [ ] 预览模式确认 (未修改文件)
- [ ] 日志保存到 `logs/step4_hpo_apply_preview_*.log`

### 4.3 人工审批决策点

**审批清单**:

1. **权重变化合理性**:
   - [ ] 权重变化幅度 < 0.2 (单次不超过 20%)
   - [ ] 变化方向符合预期 (如 XGBoost 近期表现好 → w_xgb 增加)

2. **预期收益**:
   - [ ] Δ Sharpe > 0.05 (至少提升 0.05)
   - [ ] 无负面影响预测

3. **风险评估**:
   - [ ] 不是极端权重 (如 w_xgb > 0.9)
   - [ ] 有足够历史数据支持 (测试期 >= 10 天)

4. **审批决策**:
   - [ ] ✅ 批准应用
   - [ ] ❌ 拒绝应用 (理由: ________________________________________________)
   - [ ] ⏸️ 需更多测试 (补充测试计划: ________________________________________________)

**如果批准,继续 4.4; 如果拒绝或暂停,跳到 4.6**

### 4.4 应用 HPO 建议 (生产环境)

```bash
# 命令: 实际应用 HPO 建议到配置文件
cd .

make hpo-apply FORCE=1 2>&1 | tee logs/step4_hpo_apply_real_$(date +%Y%m%d_%H%M%S).log

# 预期输出:
# [apply_hpo] Reading HPO suggestions...
# [apply_hpo] FORCE MODE: Applying changes...
# [apply_hpo] Updated TSLA config: runs/ai_trader/2025-11-01/strategies_config/TSLA.json
# [apply_hpo] Updated NVDA config: runs/ai_trader/2025-11-01/strategies_config/NVDA.json
# [apply_hpo] Skipped AAPL (no changes)
# [apply_hpo] ✓ 2/3 configs updated
# [apply_hpo] Changes will take effect on next strategy reload

# ✅ 成功标准:
- 配置文件实际修改
- 修改数量 = 预览中显示的数量
- 无错误或警告
```

**执行清单**:
- [ ] 配置文件修改成功
- [ ] 修改文件数: ________ (预期: ________)
- [ ] 日志保存到 `logs/step4_hpo_apply_real_*.log`

### 4.5 验证配置更新

```bash
# 命令: 验证 JSON 配置文件已更新
cd .

# 检查 TSLA 配置
cat runs/ai_trader/$(date +%Y-%m-%d)/strategies_config/TSLA.json | jq '.ensemble_weights'

# 预期输出:
# {
#   "w_xgb": 0.60,
#   "w_lstm": 0.40
# }

# 验证其他 symbol
for sym in AAPL NVDA; do
  echo "=== $sym ==="
  cat runs/ai_trader/$(date +%Y-%m-%d)/strategies_config/${sym}.json | jq '.ensemble_weights'
done

# ✅ 成功标准:
- 权重与 HPO 建议一致
- JSON 格式正确
- 文件时间戳为最近 (< 5 分钟)
```

**执行清单**:
- [ ] TSLA 权重已更新: w_xgb=________, w_lstm=________
- [ ] AAPL 权重已更新: w_xgb=________, w_lstm=________
- [ ] NVDA 权重已更新: w_xgb=________, w_lstm=________
- [ ] 文件时间戳正确

### 4.6 热重载配置 (无需重启 AI 大脑)

```bash
# 命令: 触发 AI 大脑重新加载配置
# 方法 1: 通过信号量 (推荐)
kill -USR1 $(pgrep -f "auto_loop.py")

# 方法 2: 通过文件标记 (如果支持)
touch /tmp/ai_trader_reload.flag

# 等待 30 秒让 AI 大脑重新加载
sleep 30

# 验证: 检查日志确认重载
tail -50 /tmp/ai_brain_*.log | grep -i "reload\|config"

# 预期输出:
# [orchestrator] Received USR1 signal, reloading configs...
# [orchestrator] Loaded config for TSLA: w_xgb=0.60, w_lstm=0.40
# [orchestrator] Loaded config for AAPL: w_xgb=0.50, w_lstm=0.50
# [orchestrator] Loaded config for NVDA: w_xgb=0.55, w_lstm=0.45
# [orchestrator] Config reload complete

# ✅ 成功标准:
- 日志显示 "reload" 消息
- 无错误或崩溃
- AI 大脑进程仍在运行
```

**执行清单**:
- [ ] 重载信号发送成功
- [ ] 日志确认重载完成
- [ ] AI 大脑进程状态正常
- [ ] 无错误日志

---

## 第五步: 生产保障与回滚 (Step 5: Production Safeguards & Rollback)

**目标**: 监控配置变更效果,必要时快速回滚

### 5.1 监控配置生效

```bash
# 命令: 实时监控 AI 大脑决策 (观察 5 分钟)
cd .

tail -f /tmp/ai_brain_*.log | grep -E "decision|signal|ensemble"

# 预期输出 (示例):
# [TSLA] XGB signal: 0.023, LSTM signal: 0.018
# [TSLA] Ensemble (w_xgb=0.60, w_lstm=0.40): final_signal=0.0210
# [TSLA] Decision: BUY 50 shares (conf=0.68)
# ...

# ✅ 验证要点:
- ensemble_weights 与配置一致
- final_signal 计算正确 (= w_xgb * xgb_signal + w_lstm * lstm_signal)
- 决策正常生成
```

**执行清单**:
- [ ] 监控 5 分钟无异常
- [ ] Ensemble 权重正确应用
- [ ] 至少观察到 3 个决策
- [ ] 记录观察到的决策示例: ________________________________________________

### 5.2 检查交易日志 (Paper Trading)

```bash
# 命令: 查询最近的 shadow orders (Paper Trading)
cd .

docker exec trading_clickhouse clickhouse-client --query \
  "SELECT ts, symbol, side, qty, ensemble_w_xgb, ensemble_w_lstm
   FROM trading.shadow_orders
   WHERE ts > now() - INTERVAL 1 HOUR
   ORDER BY ts DESC
   LIMIT 10 FORMAT Vertical"

# 预期输出:
# Row 1:
# ──────
# ts:              2025-11-01 14:35:12
# symbol:          TSLA
# side:            BUY
# qty:             50
# ensemble_w_xgb:  0.60    ← 确认新权重
# ensemble_w_lstm: 0.40    ← 确认新权重
# ...

# ✅ 成功标准:
- shadow_orders 表有新记录
- ensemble_w_xgb 和 ensemble_w_lstm 字段与配置一致
```

**执行清单**:
- [ ] Shadow orders 生成正常
- [ ] Ensemble 权重记录正确
- [ ] 至少有 3 条新订单 (过去 1 小时)

### 5.3 性能对比 (配置变更前后)

```bash
# 命令: 对比配置变更前后的表现 (需要至少 1 小时数据)
cd .

# 提取变更前 1 小时的 Sharpe (从备份日志)
# 提取变更后 1 小时的 Sharpe (从当前日志)

# 手动计算或运行分析脚本 (如果有)
python tools/compare_performance.py \
  --before backup/configs/YYYYMMDD_HHMMSS/ \
  --after runs/ai_trader/$(date +%Y-%m-%d)/strategies_config/ \
  --window 1h

# 预期输出:
# Before (1h): Sharpe=1.58, Win Rate=57.5%, Avg PnL=$125
# After (1h):  Sharpe=1.65, Win Rate=59.2%, Avg PnL=$142
# Improvement: Sharpe +0.07, Win Rate +1.7%, Avg PnL +$17

# ✅ 成功标准:
- After Sharpe >= Before Sharpe × 0.95 (允许 5% 波动)
- 无异常大幅下降
```

**执行清单**:
- [ ] 变更前性能: Sharpe=________, Win Rate=________
- [ ] 变更后性能: Sharpe=________, Win Rate=________
- [ ] 性能提升: ✅ Yes / ❌ No / ⏸️ 需更多时间
- [ ] 决策: 继续观察 / 立即回滚

### 5.4 回滚准备 (如需回滚)

**回滚触发条件** (任一满足):
- [ ] Sharpe 下降 > 20%
- [ ] Win Rate 下降 > 10%
- [ ] 出现系统错误或崩溃
- [ ] 人工判断需要回滚

**回滚命令**:

```bash
# 方法 1: 使用 config-rollback 工具
cd .

# 回滚单个 symbol
make config-rollback SYM=TSLA FILE=backup/configs/YYYYMMDD_HHMMSS/TSLA.json

# 回滚所有 symbol
for sym in TSLA AAPL NVDA; do
  make config-rollback SYM=$sym FILE=backup/configs/YYYYMMDD_HHMMSS/${sym}.json
done

# 预期输出:
# [config_rollback] Restoring TSLA config from backup/configs/.../TSLA.json
# [config_rollback] ✓ Config restored
# [config_rollback] Previous config saved to: backup/configs/.../TSLA.json.before_rollback

# ✅ 成功标准:
- 配置文件恢复到备份版本
- 无错误消息
```

**执行清单** (如果回滚):
- [ ] 回滚配置文件成功
- [ ] 热重载配置 (kill -USR1)
- [ ] 验证权重恢复: w_xgb=________, w_lstm=________
- [ ] 监控 5 分钟确认正常
- [ ] 记录回滚原因: ________________________________________________

### 5.5 记录执行结果

```bash
# 命令: 创建执行报告
cd .

cat > logs/step5_execution_report_$(date +%Y%m%d_%H%M%S).md <<EOF
# AI大脑三模型管理执行报告
# Execution Report: AI Brain Three-Model Management

**执行日期**: $(date +%Y-%m-%d)
**执行人**: [Your Name]

## 执行摘要

- Step 1 (Baseline): ✅ 完成 / ❌ 失败
- Step 2 (Backtest): ✅ 完成 / ❌ 失败
- Step 3 (HPO): ✅ 完成 / ❌ 失败
- Step 4 (Apply): ✅ 完成 / ❌ 失败
- Step 5 (Monitor): ✅ 完成 / ❌ 失败 / 🔄 回滚

## 关键结果

### Baseline (Step 1)
- OOF 覆盖率: TSLA=__%, AAPL=__%, NVDA=__%

### Backtest (Step 2)
- XGB-only: Sharpe=____, Return=____%
- LSTM-only: Sharpe=____, Return=____%
- Fusion: Sharpe=____, Return=____%
- **Winner**: ________

### HPO (Step 3)
- Best weights: w_xgb=____, w_lstm=____
- Expected Sharpe improvement: +____

### Production (Step 4-5)
- Config applied: ✅ Yes / ❌ No
- Performance after 1h: Sharpe=____, Win Rate=____%
- Rollback: ✅ Yes / ❌ No

## 经验教训

- 成功因素:
  1. ________________________________________________
  2. ________________________________________________

- 改进点:
  1. ________________________________________________
  2. ________________________________________________

## 下一步计划

1. ________________________________________________
2. ________________________________________________

EOF

cat logs/step5_execution_report_*.md
```

**执行清单**:
- [ ] 执行报告创建成功
- [ ] 所有关键指标记录完整
- [ ] 经验教训总结
- [ ] 下一步计划明确

---

## 故障排查手册 (Troubleshooting Guide)

### 问题 1: OOF 数据缺失

**症状**: `make verify-oof` 报告覆盖率 < 50%

**诊断**:
```bash
# 检查 OOF 表是否存在
docker exec trading_clickhouse clickhouse-client --query \
  "SELECT count() FROM lake.xgb_oof_h5 LIMIT 1"

# 检查数据时间范围
docker exec trading_clickhouse clickhouse-client --query \
  "SELECT min(ts), max(ts) FROM lake.xgb_oof_h5"
```

**解决方案**:
1. 如果表不存在 → 需要运行模型训练生成 OOF 数据
2. 如果时间范围不覆盖测试期 → 调整测试时间范围或重新训练

### 问题 2: Backtest 报错 "Feature not found"

**症状**: `make compare-ensemble` 失败,提示某个特征列缺失

**诊断**:
```bash
# 检查视图结构
docker exec trading_clickhouse clickhouse-client --query \
  "DESCRIBE trading.features_1m_with_oof FORMAT Pretty"
```

**解决方案**:
1. 重新创建视图: `make setup-oof-view`
2. 检查 tools/backtest_ensemble_compare.py 中使用的特征列名

### 问题 3: HPO 耗时过长

**症状**: `make nightly-hpo REAL=1` 运行超过 2 小时

**诊断**:
```bash
# 检查 HPO 进程状态
ps aux | grep "nightly_hpo.py"

# 检查日志
tail -100 logs/step3_hpo_real_*.log
```

**解决方案**:
1. 减少 trials 数量: 修改 tools/nightly_hpo.py 中的 `n_trials=20` → `n_trials=10`
2. 减少测试数据量: 缩短测试时间范围 (如 1 个月 → 2 周)
3. 并行化: 修改代码使用 `n_jobs=-1`

### 问题 4: 配置不生效

**症状**: 修改配置文件后,AI 大脑仍使用旧权重

**诊断**:
```bash
# 检查进程是否收到重载信号
tail -100 /tmp/ai_brain_*.log | grep -i reload

# 检查配置文件时间戳
ls -lh runs/ai_trader/*/strategies_config/*.json
```

**解决方案**:
1. 重新发送重载信号: `kill -USR1 $(pgrep -f "auto_loop.py")`
2. 如果不支持热重载,重启 AI 大脑 (保存当前状态):
   ```bash
   pkill -f "auto_loop.py"
   PYTHONPATH=. python scripts/ai_trader/auto_loop.py \
     2>&1 | tee /tmp/ai_brain_$(date +%Y%m%d_%H%M%S).log &
   ```

### 问题 5: 回滚失败

**症状**: `make config-rollback` 报错 "Backup file not found"

**诊断**:
```bash
# 检查备份目录
ls -lh backup/configs/*/
```

**解决方案**:
1. 找到最近的备份目录: `ls -lt backup/configs/ | head -5`
2. 手动复制配置文件:
   ```bash
   cp backup/configs/YYYYMMDD_HHMMSS/TSLA.json \
     runs/ai_trader/$(date +%Y-%m-%d)/strategies_config/TSLA.json
   ```
3. 热重载配置

---

## 执行后检查清单 (Post-Execution Checklist)

### 文档和日志归档

- [ ] 所有日志文件保存到 `logs/` 目录
- [ ] 执行报告创建: `logs/step5_execution_report_*.md`
- [ ] 备份配置归档: `backup/configs/YYYYMMDD_HHMMSS/`

### 配置验证

- [ ] 当前配置文件与 HPO 建议一致
- [ ] 配置文件 JSON 格式正确
- [ ] Ensemble 权重和为 1.0

### 性能监控

- [ ] 设置 1 周监控计划
- [ ] 每日检查 Sharpe 和 Win Rate
- [ ] 如有异常,及时回滚

### 文档更新

- [ ] 更新 `AI_BRAIN_THREE_MODEL_MANAGEMENT.md` 状态
- [ ] 记录任何流程改进建议
- [ ] 提交执行报告到文档库

### 下一步计划

- [ ] 计划下一次 HPO 时间 (建议每周或每月)
- [ ] 评估是否扩展到更多 symbol
- [ ] 考虑添加其他模型 (如 Transformer)

---

## 附录: 快速参考命令

### 一键执行脚本 (谨慎使用)

```bash
#!/bin/bash
# 文件: run_three_model_setup.sh
# 警告: 仅用于熟悉流程后的快速执行,首次执行请逐步手动操作

set -e  # 遇到错误立即退出
cd .

echo "===== Step 1: Baseline Confirmation ====="
make setup-oof-view
make verify-oof SYMS="TSLA,AAPL,NVDA" START="2025-10-01 09:30:00" END="2025-10-31 16:00:00"

echo "===== Step 2: Three-Mode Comparison ====="
make compare-ensemble SYMS="TSLA,AAPL,NVDA" START="2025-10-01 09:30:00" END="2025-10-31 16:00:00"

echo "===== Step 3: HPO (Mock Mode) ====="
make nightly-hpo

echo "===== Step 4: Preview Changes ====="
make hpo-apply

echo "===== Manual Approval Required ====="
echo "Review logs and approve before continuing"
read -p "Press Enter to apply changes or Ctrl+C to abort..."

echo "===== Step 4: Apply Changes ====="
mkdir -p backup/configs/$(date +%Y%m%d_%H%M%S)
cp -r runs/ai_trader/*/strategies_config/*.json backup/configs/$(date +%Y%m%d_%H%M%S)/
make hpo-apply FORCE=1

echo "===== Step 5: Reload Config ====="
kill -USR1 $(pgrep -f "auto_loop.py")

echo "===== Execution Complete ====="
echo "Monitor logs for 1 hour to verify performance"
```

### 常用检查命令

```bash
# 检查 AI 大脑状态
ps aux | grep auto_loop.py | grep -v grep

# 实时监控日志
tail -f /tmp/ai_brain_*.log

# 查询最近的 shadow orders
docker exec trading_clickhouse clickhouse-client --query \
  "SELECT * FROM trading.shadow_orders ORDER BY ts DESC LIMIT 5 FORMAT Vertical"

# 查看当前配置
cat runs/ai_trader/$(date +%Y-%m-%d)/strategies_config/TSLA.json | jq .

# 快速回滚 (替换时间戳)
make config-rollback SYM=TSLA FILE=backup/configs/YYYYMMDD_HHMMSS/TSLA.json
```

---

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0 | 2025-11-01 | 初始版本,完整执行清单 |

**最后更新**: 2025-11-01
**维护者**: Trading Team
**关联文档**: `AI_BRAIN_THREE_MODEL_MANAGEMENT.md`

---

**执行提示**:
- 首次执行建议预留 4-6 小时完整时间
- 建议在非交易时段执行 (如周末或盘后)
- 准备好 ClickHouse、Python、Docker 环境
- 确保至少 50GB 可用磁盘空间
- **Be Honest**: 如实记录所有结果,包括失败和警告
