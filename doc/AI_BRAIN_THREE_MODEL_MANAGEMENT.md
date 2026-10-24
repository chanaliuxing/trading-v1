# AI大脑三模型管理完整执行文档

**文档版本**: 1.0
**创建日期**: 2025-11-01
**适用范围**: AI Trader系统 XGBoost + LSTM + ML信号三模型融合管理

---

## 📋 执行目标

让AI大脑全面管理三种ML形态:
1. **XGBoost-only**: 纯XGBoost信号 (xgb_signal/xgb_conf)
2. **LSTM-only**: 纯LSTM信号 (lstm_signal/lstm_conf)
3. **Fusion**: 融合模式,加权组合两种信号

**核心能力**:
- 自动对比三种模式的收益/回撤/胜率
- 夜间自动调参(含融合权重)
- 产出可审计的建议,应用到per-symbol配置
- 在生产中选择当前最优模式,持续迭代闭环

---

## ✅ 现状确认 (已完成部分)

### 1. 双模型信号通道 (方案A)
- **文件**: `adapters/enhanced_model_adapter.py`
- **功能**: 适配器输出两路键
  - `xgb_signal/xgb_conf` (别名自 ml_signal/ml_conf)
  - `lstm_signal/lstm_conf` (来自 lstm_oof_h5/lstm_oof_conf_h5)

### 2. RL状态读取增强
- **文件**: `strategies/enhanced_model_signal_strategy.py`
- **功能**: 多键回退机制,避免命名差异
  - `ret_1m/ret_5m/spy_ret_1m/spread_bps`

### 3. ClickHouse视图
- **视图**: `trading.features_1m_with_oof`
- **验证工具**: `tools/verify_oof_view.py`
- **命令**: `make setup-oof-view`, `make verify-oof`

### 4. 对比回测工具
- **文件**: `tools/backtest_ensemble_compare.py`
- **功能**: 一键对比 XGB-only / LSTM-only / Fusion
- **命令**: `make compare-ensemble`
- **引擎**: `backtest_ml_engine.py` (自动映射 OOF→lstm_, ml→xgb_)

### 5. HPO调权
- **文件**: `tools/nightly_hpo.py`, `tools/apply_hpo_suggestions.py`
- **功能**: 搜索 ensemble_weight_xgb/lstm,自动归一化

---

## 🎯 分步执行清单

### 步骤 1: 基线确认 (数据/视图)

#### 1.1 创建/刷新ClickHouse视图

```bash
# 位置: .
make setup-oof-view
```

**预期输出**: 视图 `trading.features_1m_with_oof` 创建成功

**失败处理**:
- 检查ClickHouse是否运行: `docker ps | grep clickhouse`
- 检查配置: `scripts/ai_trader/config/ai_trader.yaml` 中 `clickhouse.client/dsn`

#### 1.2 验证数据覆盖率

```bash
# 验证TSLA和AAPL在指定时间范围的OOF数据覆盖
make verify-oof SYMS='TSLA,AAPL' START='2025-10-01 09:30:00' END='2025-11-01 16:00:00'
```

**预期输出**:
```
✓ TSLA: lstm_oof_h5 覆盖率 100%
✓ AAPL: lstm_oof_h5 覆盖率 100%
```

**覆盖率要求**: ≥95% 才能保证回测质量

**失败处理**:
- 覆盖率<95%: 检查 `trading.features_1m` 表是否有 `lstm_oof_h5/lstm_oof_conf_h5` 列
- 无数据: 检查时间范围是否在交易时段内

---

### 步骤 2: 三模式对比回测

#### 2.1 运行对比回测

```bash
# 对比XGB-only, LSTM-only, Fusion三种模式
make compare-ensemble SYMS='TSLA,AAPL' START='2025-10-01 09:30:00' END='2025-11-01 16:00:00'
```

**输出位置**: `runs/ai_trader/ensemble_compare/compare.json`

#### 2.2 解读对比结果

```json
{
  "xgb_only": {
    "total_return": 0.0523,
    "sharpe": 1.82,
    "max_drawdown": -0.0234,
    "win_rate": 0.58,
    "trades": 45
  },
  "lstm_only": {
    "total_return": 0.0612,
    "sharpe": 2.01,
    "max_drawdown": -0.0198,
    "win_rate": 0.61,
    "trades": 38
  },
  "fusion": {
    "total_return": 0.0687,
    "sharpe": 2.15,
    "max_drawdown": -0.0189,
    "win_rate": 0.63,
    "trades": 42,
    "weights": {"xgb": 0.4, "lstm": 0.6}
  }
}
```

**关键指标**:
- `total_return`: 总收益率
- `sharpe`: 夏普比率 (>1.5 为优秀)
- `max_drawdown`: 最大回撤 (<5% 为良好)
- `win_rate`: 胜率 (>55% 为良好)
- `trades`: 交易次数 (太少可能参数过严)

#### 2.3 常见问题排查

**问题**: 某模式显示 0 笔交易

**排查步骤**:
1. **XGB-only 无交易**:
   - 检查 `features` 表是否有 `ml_signal/ml_conf` 列
   - 如无,可先仅用 LSTM-only/Fusion,或补充 ml_signal 列

2. **LSTM-only 无交易**:
   - 检查 `features` 表是否有 `lstm_oof_h5/lstm_oof_conf_h5` 列
   - 运行步骤1.2验证覆盖率

3. **所有模式无交易**:
   - 放宽置信度阈值: 工具已设置 `market_filter.conf_floor=0.0`
   - 检查仓位设置: 默认 `lot=1`, `base_qty=100`
   - 扩大时间范围或增加样本标的

---

### 步骤 3: 夜间HPO (自动调权与策略参数)

#### 3.1 运行Walk-Forward HPO

```bash
# 30天数据,24次采样,10天IS/5天OOS,真实回测
python tools/nightly_hpo.py \
  --symbols TSLA,AAPL \
  --days 30 \
  --samples 24 \
  --real-backtest \
  --wf \
  --is-days 10 \
  --oos-days 5
```

**参数说明**:
- `--symbols`: 标的列表
- `--days`: 总历史天数
- `--samples`: Optuna采样次数
- `--real-backtest`: 使用真实回测引擎 (vs 模拟)
- `--wf`: 启用Walk-Forward验证
- `--is-days/oos-days`: In-Sample/Out-of-Sample天数

**搜索空间** (含融合权重):
```python
{
  "ensemble_weight_xgb": (0.0, 1.0),
  "ensemble_weight_lstm": (0.0, 1.0),  # 自动归一化
  "market_filter.conf_floor": (0.5, 0.9),
  "sizing.base_qty": (50, 200),
  "risk.take_profit_pct": (0.01, 0.05),
  "risk.stop_loss_pct": (0.005, 0.03)
}
```

**输出位置**: `runs/ai_trader/YYYY-MM-DD/hpo_suggestions/*.json`

#### 3.2 审查HPO建议

```bash
# 查看生成的建议
ls -l runs/ai_trader/$(date +%Y-%m-%d)/hpo_suggestions/

# 查看具体建议内容
cat runs/ai_trader/$(date +%Y-%m-%d)/hpo_suggestions/TSLA_hpo.json
```

**建议格式**:
```json
{
  "symbol": "TSLA",
  "is_score": 2.15,
  "oos_score": 1.98,
  "params": {
    "ensemble": {
      "mode": "fusion",
      "models": [
        {"type": "xgb", "weight": 0.42},
        {"type": "lstm", "weight": 0.58}
      ]
    },
    "market_filter": {
      "conf_floor": 0.72
    },
    "sizing": {
      "base_qty": 120
    }
  },
  "approved": false
}
```

**审查要点**:
- IS/OOS分数是否合理 (OOS不应远低于IS,防止过拟合)
- 权重分配是否合理 (避免极端如100%单模型)
- 参数是否在常识范围内

#### 3.3 应用HPO建议

```bash
# 需要人工审批 (默认)
make hpo-apply

# 强制应用 (跳过审批,谨慎使用!)
make hpo-apply FORCE=1
```

**应用流程**:
1. 读取 `hpo_suggestions/*.json`
2. 检查 `approved` 标志 (除非FORCE=1)
3. 合并到 `per-symbol JSON` (如 `configs/per_symbol/TSLA.json`)
4. 备份旧配置到 `configs/per_symbol/backups/`
5. 策略热加载,即时生效

**验证应用**:
```bash
# 查看TSLA当前配置
cat configs/per_symbol/TSLA.json | jq '.ensemble'

# 查看备份
ls -lt configs/per_symbol/backups/ | head -5
```

---

### 步骤 4: AI大脑选择最优与持续迭代

#### 4.1 每日自动化流程 (推荐)

**Cron配置** (`crontab -e`):
```cron
# 每日盘后16:30运行HPO
30 16 * * 1-5 cd . && make daily-hpo >> logs/cron_hpo.log 2>&1

# 每日早上7:00自动应用(paper环境)
0 7 * * 1-5 cd . && make hpo-apply FORCE=1 >> logs/cron_apply.log 2>&1
```

**或使用launchd** (macOS):
```xml
<!-- ~/Library/LaunchAgents/com.trading.daily_hpo.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.trading.daily_hpo</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>cd . && make daily-hpo</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>16</integer>
        <key>Minute</key>
        <integer>30</integer>
    </dict>
</dict>
</plist>
```

加载: `launchctl load ~/Library/LaunchAgents/com.trading.daily_hpo.plist`

#### 4.2 生产策略选择方案

**方案A: HPO直接收敛** (简版,推荐)
1. `nightly_hpo` 自动收敛出最优权重
2. 应用后即为次日生产配置
3. 通过WF/OOS验证防止过拟合

**方案B: 每日对比选择** (强化版)
1. 每日盘后运行 `make compare-ensemble`
2. 生成 `ensemble_choice.json` (最佳模式与权重)
3. 写回 `per-symbol JSON` (走审批)
4. 次日使用选定配置

**方案B实现** (需要额外脚本):
```bash
# 1. 对比三种模式
make compare-ensemble SYMS='TSLA,AAPL,NVDA' START='...' END='...'

# 2. 自动选择最优 (需实现 tools/choose_ensemble.py)
python tools/choose_ensemble.py \
  --compare-file runs/ai_trader/ensemble_compare/compare.json \
  --output configs/ensemble_choice.json

# 3. 应用到per-symbol配置
make apply-ensemble-choice
```

#### 4.3 监控与观测

**Grafana面板**:
- 融合权重变化 vs 日收益
- IS/OOS分数趋势
- 三模式胜率对比

**Loki日志查询**:
```logql
{job="ai_trader"} |= "ensemble_applied" | json
```

**Audit事件**:
```bash
# 查看最近的ensemble调整事件
tail -100 runs/ai_trader/$(date +%Y-%m-%d)/audits/ensemble_*.jsonl
```

---

### 步骤 5: 生产保障与回退

#### 5.1 风控机制

**REJECT/EXIT映射** (已修正):
- REJECT → 不阻断 SELL
- EXIT → 清仓信号正常传递

**Kill-Switch**:
```bash
# 紧急停止所有交易
make kill-switch

# 恢复
make resume-trading
```

**对冲通道**:
```bash
# 查看当前对冲状态
cat runs/ai_trader/$(date +%Y-%m-%d)/overlay_hedge.json
```

#### 5.2 配置回退

```bash
# 回退TSLA到上一个备份
make config-rollback SYM=TSLA

# 回退到指定备份文件
make config-rollback SYM=TSLA FILE=configs/per_symbol/backups/TSLA_2025-11-01_14-30-00.json
```

**自动备份策略**:
- 每次应用前自动备份
- 保留最近30个版本
- 文件名格式: `{SYMBOL}_{TIMESTAMP}.json`

---

## 🔍 关键命令速查表

| 命令 | 用途 | 示例 |
|------|------|------|
| `make setup-oof-view` | 创建ClickHouse视图 | - |
| `make verify-oof` | 验证OOF覆盖率 | `SYMS='TSLA' START='...' END='...'` |
| `make compare-ensemble` | 三模式对比回测 | `SYMS='TSLA,AAPL' START='...' END='...'` |
| `python tools/nightly_hpo.py` | 运行HPO调参 | `--symbols TSLA --days 30 --wf` |
| `make hpo-apply` | 应用HPO建议 | `FORCE=1` (跳过审批) |
| `make config-rollback` | 回退配置 | `SYM=TSLA [FILE=...]` |
| `make daily-hpo` | 完整每日流程 | - |

---

## ⚠️ 注意事项与改进点

### 1. XGBoost信号依赖
**问题**: `xgb-only` 模式需要 `features` 表存在 `ml_signal/ml_conf` 列

**解决方案**:
- **方案A**: 先仅用 LSTM-only/Fusion 模式
- **方案B**: 补充 `ml_signal` 列到 `features` 表
- **方案C**: 离线生成 XGBoost推理结果,写入 `xgb_signal/xgb_conf` 列

### 2. 自动决策选择器
**当前状态**: HPO产出建议,需人工审批

**改进方向**: 让AI大脑主动决策每日最优模式
- 在 `premarket_coach_flow` 或 `nightly pipeline` 加"选择器"
- 读取 `compare.json/WF/OOS` 评分
- 生成 `ensemble_choice` 并写回 `per-symbol JSON`

**需要实现**: `tools/choose_ensemble.py` 脚本

### 3. 审批流程优化
**当前**: `require_approval=true` (默认)

**生产建议**:
- Paper环境: 可设置 `FORCE=1` 自动应用
- Live环境: 保留人工审批
- 配置位置: `ai_trader.yaml` → `policies.autosettings.require_approval`

**环境变量**:
```bash
export AI_TRADER_APPROVE_AUTOS=1  # 启用自动审批
```

---

## 📊 预期输出示例

### compare.json
```json
{
  "timestamp": "2025-11-01T16:30:00",
  "symbols": ["TSLA", "AAPL"],
  "period": {
    "start": "2025-10-01 09:30:00",
    "end": "2025-11-01 16:00:00"
  },
  "results": {
    "xgb_only": {...},
    "lstm_only": {...},
    "fusion": {...}
  },
  "winner": "fusion",
  "recommendation": "使用Fusion模式,权重 XGB:40% LSTM:60%"
}
```

### hpo_suggestions/TSLA_hpo.json
```json
{
  "symbol": "TSLA",
  "timestamp": "2025-11-01T16:45:00",
  "is_score": 2.15,
  "oos_score": 1.98,
  "wf_results": [
    {"fold": 1, "is": 2.20, "oos": 2.05},
    {"fold": 2, "is": 2.18, "oos": 1.95},
    {"fold": 3, "is": 2.12, "oos": 1.92}
  ],
  "params": {
    "ensemble": {
      "mode": "fusion",
      "models": [
        {"type": "xgb", "weight": 0.42},
        {"type": "lstm", "weight": 0.58}
      ]
    },
    "market_filter": {"conf_floor": 0.72},
    "sizing": {"base_qty": 120}
  },
  "approved": false
}
```

---

## 🚀 下一步行动

### 立即执行 (第一天)
1. ✅ 运行 `make setup-oof-view`
2. ✅ 运行 `make verify-oof` 确认数据
3. ✅ 运行 `make compare-ensemble` 获取基线
4. 📝 查看 `compare.json` 决定初始策略

### 第二天
1. ✅ 运行首次 `nightly_hpo`
2. 📝 审查建议,手动审批
3. ✅ 应用配置: `make hpo-apply`
4. 🔍 观察Paper环境表现

### 第三天起
1. 🤖 配置自动化cron/launchd
2. 📊 设置Grafana监控面板
3. 🔄 进入自动迭代循环

---

## 📞 支持与反馈

**遇到问题**:
1. 查看日志: `logs/trader.log`, `logs/llm_client_YYYYMMDD.log`
2. 检查审计: `runs/ai_trader/YYYY-MM-DD/audits/`
3. 回退配置: `make config-rollback SYM=...`

**成功指标**:
- [ ] 三模式对比成功运行,输出 `compare.json`
- [ ] HPO建议生成,包含融合权重
- [ ] Per-symbol配置成功更新
- [ ] 策略热加载生效,Grafana显示新权重
- [ ] IS/OOS分数合理 (OOS ≥ IS * 0.8)

---

**文档维护**: 请在每次重大流程变更后更新此文档
**版本历史**: 见文档末尾

## 版本历史
- v1.0 (2025-11-01): 初始版本,完整执行流程
