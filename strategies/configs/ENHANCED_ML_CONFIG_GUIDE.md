# EnhancedModelSignalStrategy 配置指南

## 概述

EnhancedModelSignalStrategy 支持 **基于文件的股票级配置系统**,实现:
- ✅ **Per-Symbol 配置**: 每个股票独立的 JSON 配置文件
- ✅ **热加载**: 运行时自动检测配置文件变更 (60秒检查间隔)
- ✅ **配置分离**: 避免 `config.yml` 膨胀,易于版本控制
- ✅ **向后兼容**: 未配置的股票使用 `config.yml` 的默认配置

---

## 目录结构

```
strategies/configs/
├── enhanced_ml_AAPL.json          # AAPL 专属配置
├── enhanced_ml_TSLA.json          # TSLA 专属配置
├── enhanced_ml_NVDA.json          # NVDA 专属配置
└── enhanced_ml_*.json             # 其他股票...
```

**文件命名规则**: `enhanced_ml_{SYMBOL}.json` (symbol 大写)

---

## 快速开始

### 1. 在 config.yml 中添加股票

```yaml
strategies:
- id: enhanced_ml_001
  enabled: true
  module: strategies.enhanced_model_signal_strategy
  class: EnhancedModelSignalStrategy
  capital_limit_usd: 100000
  params:
    ttl_ms: 60000
    extras:
      symbols:
      - AAPL
      - TSLA
      - NVDA
      # ... 添加更多股票只需在此列表添加
```

### 2. 创建股票配置文件

复制模板到 `strategies/configs/enhanced_ml_{SYMBOL}.json`:

```json
{
  "symbol": "AAPL",
  "description": "Enhanced ML strategy config for AAPL",
  "version": "1.0.0",
  "last_updated": "2025-10-30",

  "sizing": {
    "mode": "confidence_scaled",
    "base_qty": 100,
    "max_qty": 1000,
    "lot": 100,
    "multiplier": 2.0
  },

  "market_filter": {
    "conf_floor": 0.65,
    "max_spread_bps": 5.0,
    "min_volume": 500000,
    "check_after_hours": true
  },

  "risk": {
    "take_profit_pct": 0.015,
    "stop_loss_pct": 0.008,
    "time_stop_min": 45
  }
}
```

### 3. 启动策略

```bash
make up  # 启动系统 (infra 目录下的 compose.yml)
# 策略会自动加载各股票的配置文件
```

### 4. 热加载测试

修改 `enhanced_ml_AAPL.json`:
```json
{
  "market_filter": {
    "conf_floor": 0.7  // 从 0.65 提高到 0.7
  }
}
```

**60秒后**策略会自动重新加载新配置(无需重启)。

---

## 配置文件结构详解

### 完整配置示例

参考现有文件:
- `strategies/configs/enhanced_ml_AAPL.json` (保守型)
- `strategies/configs/enhanced_ml_TSLA.json` (激进型)
- `strategies/configs/enhanced_ml_NVDA.json` (平衡型)

### 主要配置块

#### 1. `ensemble` - 多模型组合

```json
{
  "ensemble": {
    "mode": "weighted_avg",  // 或 "vote", "max_conf"
    "models": [
      {"name": "ml", "weight": 0.7},
      {"name": "rl", "weight": 0.3}
    ]
  }
}
```

**模式说明**:
- `weighted_avg`: 加权平均 (推荐)
- `vote`: 多数投票
- `max_conf`: 选择置信度最高的模型

#### 2. `sizing` - 仓位计算

```json
{
  "sizing": {
    "mode": "confidence_scaled",  // 见下方模式列表
    "base_qty": 100,
    "max_qty": 1000,
    "lot": 100,
    "multiplier": 2.0
  }
}
```

**模式说明**:
| 模式 | 适用场景 | 必需参数 |
|------|---------|---------|
| `fixed` | 固定仓位 | `base_qty`, `max_qty`, `lot` |
| `confidence_scaled` | 按信号置信度缩放 | + `multiplier` |
| `vol_target` | 基于ATR的风险目标 | `risk_per_trade`, `atr_multiple` |
| `kelly` | Kelly准则 | `win_rate`, `avg_win`, `avg_loss` |

#### 3. `market_filter` - 市场过滤

```json
{
  "market_filter": {
    "conf_floor": 0.65,         // 最低信号置信度
    "max_spread_bps": 5.0,      // 最大点差(基点)
    "min_volume": 500000,       // 最小成交量
    "max_vix": 30,              // VIX上限 (0=禁用)
    "check_after_hours": true   // 检查盘后交易
  }
}
```

#### 4. `risk` - 风险管理

```json
{
  "risk": {
    "take_profit_pct": 0.015,   // 止盈 1.5%
    "stop_loss_pct": 0.008,     // 止损 0.8%
    "time_stop_min": 45,        // 时间止损 45分钟
    "partial_exit_pct": 0.5     // 部分止盈比例
  }
}
```

#### 5. `order` - 订单执行

```json
{
  "order": {
    "order_type": "LMT",        // LMT 或 MKT
    "limit_offset_bps": 3.0,    // 限价单偏移(基点)
    "lot": 100,                 // 手数
    "exec": {
      "mode": "TWAP",           // TWAP/POV/IOC
      "horizon_s": 60           // TWAP时间窗口(秒)
    }
  }
}
```

#### 6. `sfs_1s` - 微观结构确认

```json
{
  "sfs_1s": {
    "enabled": true,
    "lookback": 20,             // 回看20秒
    "confirm_up": 6,            // 多头需要6次上涨tick
    "confirm_down": 6           // 空头需要6次下跌tick
  }
}
```

#### 7. `rl` - 强化学习仓位管理

```json
{
  "rl": {
    "enabled": true,
    "model_path": "models/rl/sac_position_manager_latest.zip",
    "action_mode": "scale",     // scale 或 target_pct
    "max_scale_amp": 0.5,       // 最大缩放幅度
    "safety": {
      "max_position_pct": 0.25,
      "max_step_pct": 0.3
    }
  }
}
```

#### 8. `hedging` - 对冲管理

```json
{
  "hedging": {
    "enabled": true,
    "confidence_threshold": 0.75,  // 低于此置信度时对冲
    "hedge_instrument": "SPY",     // 对冲工具
    "beta": 1.0                    // 对冲比例
  }
}
```

#### 9. `guardian` - 实时风控熔断

```json
{
  "guardian": {
    "enabled": true,
    "dd_intraday_pct": 0.02,         // 日内最大回撤 2%
    "max_trades_per_min": 10,        // 每分钟最大交易数
    "max_gross_exposure_pct": 0.8,   // 最大总仓位 80%
    "on_trip": "reduce",             // 触发动作: reduce/halt/flat
    "reduce_scale": 0.5,             // 减仓比例
    "cooldown_sec": 1800             // 冷却时间(秒)
  }
}
```

---

## 配置优先级

```
Symbol-specific JSON > config.yml base config > Strategy defaults
```

**示例**: AAPL 的 `conf_floor`
1. 如果 `enhanced_ml_AAPL.json` 中定义了 `market_filter.conf_floor = 0.7`
2. `config.yml` 中定义了 `market_filter.conf_floor = 0.6`
3. **实际使用**: `0.7` (文件配置优先)

---

## 热加载机制

### 工作原理

1. **初始化**: 策略启动时加载所有股票的配置文件
2. **缓存**: 配置文件内容缓存在内存中,带 mtime 时间戳
3. **定期检查**: 每隔 60 秒(可配置)检查文件 mtime
4. **自动重载**: 如果文件被修改,自动重新加载新配置
5. **无缝切换**: 下一次 `on_features` 触发时使用新配置

### 配置热加载参数

在 `config.yml` 中调整:

```yaml
extras:
  config_dir: strategies/configs              # 配置目录
  config_prefix: enhanced_ml                  # 文件前缀
  config_reload_interval_sec: 60              # 检查间隔(秒,默认60s)
```

**推荐设置**:
- **默认 (非高频策略)**: 60 秒 - 稳定可靠
- **测试/调试**: 可临时设置为 10-30 秒,生产环境务必恢复 60 秒

### 手动强制重载 (调试用)

```python
from strategies.enhanced_model_config_loader import get_loader, reset_loader

# 方式 1: 清除特定符号的缓存
loader = get_loader()
loader.invalidate_cache("AAPL")  # 清除 AAPL 缓存
loader.invalidate_cache()         # 清除所有缓存

# 方式 2: 完全重置单例 (罕用)
reset_loader()  # 销毁现有实例
loader = get_loader(config_dir="strategies/configs")  # 创建新实例
```

**⚠️ 单例模式注意事项**:
- `get_loader()` 使用单例模式,全局唯一实例
- **首次调用**的参数(config_dir, prefix, check_interval_sec)会被使用
- **后续调用**会返回相同实例,忽略新参数
- 如果参数不匹配,会记录 WARNING 日志
- 需要不同配置时使用 `reset_loader()` (仅测试时使用)

---

## 不同股票的配置策略

### 保守型 (AAPL, MSFT)

```json
{
  "sizing": {"mode": "fixed", "base_qty": 100},
  "market_filter": {"conf_floor": 0.7},
  "risk": {
    "take_profit_pct": 0.015,
    "stop_loss_pct": 0.008,
    "time_stop_min": 60
  }
}
```

### 激进型 (TSLA, NVDA)

```json
{
  "sizing": {"mode": "vol_target", "risk_per_trade": 300},
  "market_filter": {"conf_floor": 0.65},
  "risk": {
    "take_profit_pct": 0.025,
    "stop_loss_pct": 0.012,
    "time_stop_min": 30
  },
  "hedging": {"enabled": true, "beta": 1.5}
}
```

### 平衡型 (AMZN, GOOGL)

```json
{
  "sizing": {"mode": "confidence_scaled", "multiplier": 2.0},
  "market_filter": {"conf_floor": 0.68},
  "risk": {
    "take_profit_pct": 0.02,
    "stop_loss_pct": 0.01,
    "time_stop_min": 45
  }
}
```

---

## 故障排查

### 问题 1: 配置文件未加载

**症状**: 日志显示 `Config file not found`

**解决**:
1. 检查文件名: 必须是 `enhanced_ml_{SYMBOL}.json` (SYMBOL 大写)
2. 检查路径: 默认是 `strategies/configs/`
3. 检查 JSON 语法: 使用 `jq` 验证

```bash
jq . strategies/configs/enhanced_ml_AAPL.json  # 验证 JSON 格式
```

### 问题 2: 配置未热加载

**症状**: 修改配置文件后,策略仍使用旧配置

**解决**:
1. 检查 `config_reload_interval_sec` 是否过大
2. 查看日志: 搜索 `Config loaded from file`
3. 手动触发重载:
   ```python
   loader.invalidate_cache("AAPL")
   ```

### 问题 3: 配置冲突

**症状**: 策略行为异常,参数似乎混合了多个源

**解决**:
1. 理解优先级: JSON > config.yml > defaults
2. 使用 `deep_merge`: 嵌套字典会递归合并
3. 调试日志:
   ```python
   self.log.info("Merged config", symbol="AAPL", config=sym_cfg)
   ```

---

## 最佳实践

### 1. 版本控制

```bash
# 提交配置文件到 Git
git add strategies/configs/enhanced_ml_*.json
git commit -m "feat: add AAPL conservative config v1.0.0"
```

### 2. 配置备份

```bash
# 定期备份配置
tar -czf configs_backup_$(date +%Y%m%d).tar.gz strategies/configs/enhanced_ml_*.json
```

### 3. 配置模板化

创建 `_template.json` 作为新股票的起点:

```bash
cp strategies/configs/enhanced_ml_AAPL.json strategies/configs/enhanced_ml_META.json
# 修改 symbol 和参数
```

### 4. 参数调优流程

1. **Shadow 模式测试**: `shadow_mode: true` 在 `config.yml`
2. **回测验证**: 使用历史数据验证新参数
3. **Paper Trading**: 至少运行 1-2 周
4. **逐步上线**: 小仓位 → 正常仓位

### 5. 监控配置变更

在 MLflow 或 Grafana 中记录配置版本:

```python
self.log.info("Config version", symbol="AAPL", version=sym_cfg.get("version"))
```

---

## 常见配置组合

### 组合 1: 高频短线 (日内 T+0)

```json
{
  "sizing": {"mode": "fixed", "base_qty": 100, "max_qty": 500},
  "risk": {
    "take_profit_pct": 0.005,
    "stop_loss_pct": 0.003,
    "time_stop_min": 5
  },
  "sfs_1s": {"enabled": true, "confirm_up": 8},
  "guardian": {"max_trades_per_min": 20}
}
```

### 组合 2: 趋势跟踪 (持仓数小时)

```json
{
  "sizing": {"mode": "confidence_scaled", "multiplier": 3.0},
  "risk": {
    "take_profit_pct": 0.03,
    "stop_loss_pct": 0.015,
    "time_stop_min": 120
  },
  "sfs_1s": {"enabled": false}
}
```

### 组合 3: 波动率套利

```json
{
  "sizing": {"mode": "vol_target", "risk_per_trade": 500},
  "market_filter": {"max_vix": 25},
  "hedging": {"enabled": true, "beta": 1.2}
}
```

---

## 高级功能

### Per-Sector 配置 (遗留功能,不推荐)

如需更复杂的层级配置(sector → symbol),建议:
1. 使用 Python 脚本批量生成配置文件
2. 或在 JSON 中添加自定义字段,策略层解析

### 动态参数调整 (未来功能)

计划支持:
- 基于回撤自动降低风险参数
- 基于胜率自动调整 Kelly 分数
- 基于 VIX 动态调整仓位上限

---

## 相关文档

- [CLAUDE.md](../../CLAUDE.md) - 项目架构与修改规范
- [enhanced_model_signal_strategy.py](../enhanced_model_signal_strategy.py) - 策略源码
- [enhanced_model_config_loader.py](../enhanced_model_config_loader.py) - 配置加载器源码

---

## 更新日志

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0.0 | 2025-10-30 | 初始版本: Per-symbol 配置系统 + 热加载 |

---

**问题反馈**: 如遇到问题,请在日志中搜索 `ConfigLoader` 关键字,或联系策略开发团队。
