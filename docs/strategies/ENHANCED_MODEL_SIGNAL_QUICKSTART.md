# EnhancedModelSignalStrategy 快速参考

> 1页纸速查表 - 打印贴在桌面

---

## 🚀 5分钟启动

```yaml
# config.yml
strategies:
  - id: "my_enhanced_001"
    enabled: true
    module: "strategies.enhanced_model_signal_strategy"
    class: "EnhancedModelSignalStrategy"
    capital_limit_usd: 50000
    params:
      ttl_ms: 60000
      extras:
        symbols: ["TSLA"]

        ensemble:
          mode: "weighted_avg"
          models:
            - name: "ml"
              weight: 1.0

        sizing:
          mode: "confidence_scaled"
          base_qty: 100
          max_qty: 1000
          multiplier: 2.0

        market_filter:
          conf_floor: 0.6
          max_spread_bps: 5

        risk:
          take_profit_pct: 0.02
          stop_loss_pct: 0.01
          time_stop_min: 60
```

```bash
vim config.yml  # 添加配置
make restart    # 重启系统
tail -f logs/trading_system.log | grep my_enhanced_001  # 监控
```

---

## 📊 核心参数速查

### Ensemble Mode (多模型组合)

| 模式 | 说明 | 场景 |
|------|------|------|
| `weighted_avg` | 加权平均 | XGBoost + LSTM |
| `vote` | 投票 | 3+ 分类模型 |
| `max_conf` | 最高置信度 | 模型能力差异大 |

### Sizing Mode (仓位计算)

| 模式 | 说明 | 关键参数 |
|------|------|----------|
| `fixed` | 固定仓位 | `base_qty` |
| `confidence_scaled` | 置信度缩放 | `multiplier` (1-3) |
| `vol_target` | 波动率目标 | `risk_per_trade` ($) |
| `kelly` | Kelly准则 | `win_rate`, `avg_win/loss` |

### Market Filter (市场过滤)

| 参数 | 说明 | 推荐值 |
|------|------|--------|
| `conf_floor` | 最低置信度 | 0.5-0.7 |
| `max_spread_bps` | 最大点差 | 3-5 bp |
| `min_volume` | 最小成交量 | 100k-1M |
| `max_vix` | 最大VIX | 20-30 (0=禁用) |

### Risk Management (风控)

| 参数 | 说明 | 推荐值 |
|------|------|--------|
| `take_profit_pct` | 止盈 | 0.01-0.03 (1-3%) |
| `stop_loss_pct` | 止损 | 0.005-0.015 (0.5-1.5%) |
| `time_stop_min` | 时间止损 | 30-240 min |
| `partial_exit_pct` | 止盈平仓% | 0.5-1.0 |

---

## 🎯 常用场景配置

### 场景1: 单模型简单策略
```yaml
ensemble:
  models: [{"name": "ml", "weight": 1.0}]
sizing:
  mode: "fixed"
  base_qty: 100
```

### 场景2: XGBoost + LSTM 组合
```yaml
ensemble:
  mode: "weighted_avg"
  models:
    - {name: "xgb", weight: 0.6}
    - {name: "lstm", weight: 0.4}
sizing:
  mode: "confidence_scaled"
  multiplier: 2.0
```

### 场景3: 波动率控制
```yaml
sizing:
  mode: "vol_target"
  risk_per_trade: 200
  atr_multiple: 2.0
risk:
  stop_loss_pct: 0.015  # ≈ 2 ATR
```

### 场景4: Kelly优化
```yaml
sizing:
  mode: "kelly"
  win_rate: 0.55
  avg_win: 1.5
  avg_loss: 1.0
  kelly_fraction: 0.5
```

---

## 🛠️ 常用命令

```bash
# 查看决策
clickhouse-client --query "
SELECT ts, symbol, ml_signal, ml_conf, target_qty
FROM trading.decisions
WHERE strategy_id = 'my_enhanced_001'
ORDER BY ts DESC LIMIT 10 FORMAT Pretty"

# 查看跳过原因
tail -100 logs/trading_system.log | \
grep my_enhanced_001 | \
grep order_skipped | \
jq .reason_detail

# 查看持仓
clickhouse-client --query "
SELECT * FROM trading.positions
WHERE strategy_id = 'my_enhanced_001'
FORMAT Vertical"

# Shadow Mode 验证
# 1. 添加: shadow_mode: true
# 2. make restart
# 3. 查询: SELECT * FROM trading.shadow_decisions
```

---

## ⚠️ 故障排查

| 问题 | 原因 | 解决 |
|------|------|------|
| 不交易 | 置信度不足 | ↓ `conf_floor` |
| 不交易 | 点差过大 | ↑ `max_spread_bps` |
| 不交易 | SFS确认失败 | ↓ `sfs_confirm_up/down` |
| 仓位过大 | multiplier太高 | ↓ `multiplier` |
| 仓位过小 | base_qty太小 | ↑ `base_qty` |
| 止损未触发 | 未启用 | 设置 `stop_loss_pct > 0` |
| 组合失败 | 缺少特征字段 | 检查 `xgb_signal`, `lstm_conf` |

---

## 📈 性能指标

| 指标 | 目标 | 监控命令 |
|------|------|----------|
| P95延迟 | <200ms | 查看日志 `infer_ms` |
| 胜率 | >50% | ClickHouse `trades` 表 |
| Sharpe | >1.0 | 回测结果 |
| 回撤 | <10% | 权益曲线 |

---

## 📚 文件位置

- 策略文件: `strategies/enhanced_model_signal_strategy.py`
- 配置示例: `config/examples/enhanced_model_signal_examples.yml`
- 完整文档: `docs/strategies/ENHANCED_MODEL_SIGNAL_STRATEGY.md`

---

**最后更新**: 2025-10-28
