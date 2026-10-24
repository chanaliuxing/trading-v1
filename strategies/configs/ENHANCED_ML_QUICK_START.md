# EnhancedModelSignalStrategy 快速开始

## 5 分钟上手指南

### 1. 添加新股票 (3 步)

#### Step 1: 创建配置文件

```bash
cd .
cp strategies/configs/enhanced_ml_AAPL.json strategies/configs/enhanced_ml_META.json
```

#### Step 2: 修改配置

编辑 `strategies/configs/enhanced_ml_META.json`:

```json
{
  "symbol": "META",
  "description": "Enhanced ML strategy config for META",
  "version": "1.0.0",
  "last_updated": "2025-10-30",

  "sizing": {
    "mode": "confidence_scaled",
    "base_qty": 50,
    "max_qty": 500,
    "lot": 50
  },

  "market_filter": {
    "conf_floor": 0.68,
    "max_spread_bps": 6.0,
    "min_volume": 600000
  },

  "risk": {
    "take_profit_pct": 0.018,
    "stop_loss_pct": 0.009,
    "time_stop_min": 40
  }
}
```

#### Step 3: 更新 config.yml

```yaml
strategies:
- id: enhanced_ml_001
  enabled: true
  params:
    extras:
      symbols:
      - AAPL
      - TSLA
      - NVDA
      - META  # 👈 添加这一行
```

#### Step 4: 重启或等待热加载

```bash
# 方式 1: 重启策略 (立即生效)
make down && make up

# 方式 2: 等待热加载 (60秒后自动生效)
# 无需操作,系统会自动检测配置文件
```

---

## 常见场景

### 场景 1: 调整单个股票的风险参数

**需求**: TSLA 最近波动大,想降低仓位和提高止损

**操作**: 编辑 `enhanced_ml_TSLA.json`

```json
{
  "sizing": {
    "base_qty": 50,    // 从 100 降低到 50
    "max_qty": 300     // 从 500 降低到 300
  },
  "risk": {
    "stop_loss_pct": 0.015  // 从 0.012 提高到 0.015
  }
}
```

**生效时间**: 约 60 秒后 (无需重启)

---

### 场景 2: 禁用某个股票的对冲

**需求**: AAPL 对冲效果不好,想暂时关闭

**操作**: 编辑 `enhanced_ml_AAPL.json`

```json
{
  "hedging": {
    "enabled": false  // 👈 改为 false
  }
}
```

---

### 场景 3: 测试新的仓位策略

**需求**: 想在 NVDA 上测试波动率目标仓位法

**操作**: 编辑 `enhanced_ml_NVDA.json`

```json
{
  "sizing": {
    "mode": "vol_target",       // 👈 从 confidence_scaled 改为 vol_target
    "risk_per_trade": 250.0,
    "atr_multiple": 2.0,
    "max_qty": 800,
    "lot": 20
  }
}
```

**建议**: 先在 Shadow 模式测试 1-2 天

---

### 场景 4: 紧急熔断 (快速降低风险)

**需求**: 市场突然大跌,想立即降低所有仓位

**操作 1: 全局降低** (修改 `config.yml`)

```yaml
extras:
  sizing:
    base_qty: 50      # 从 100 降低到 50
    max_qty: 500      # 从 1000 降低到 500
```

**操作 2: 单股票降低** (修改各股票的 JSON)

```bash
# 批量降低所有股票仓位 (Linux/Mac)
for file in strategies/configs/enhanced_ml_*.json; do
  jq '.sizing.base_qty /= 2 | .sizing.max_qty /= 2' "$file" > "$file.tmp" && mv "$file.tmp" "$file"
done
```

---

## 配置模板参考

### 保守型 (低风险,长持仓)

```json
{
  "sizing": {"mode": "fixed", "base_qty": 100, "max_qty": 500},
  "market_filter": {"conf_floor": 0.7, "min_volume": 1000000},
  "risk": {
    "take_profit_pct": 0.015,
    "stop_loss_pct": 0.008,
    "time_stop_min": 60
  },
  "sfs_1s": {"enabled": false},
  "guardian": {"dd_intraday_pct": 0.015}
}
```

### 激进型 (高风险,短持仓)

```json
{
  "sizing": {"mode": "vol_target", "risk_per_trade": 300},
  "market_filter": {"conf_floor": 0.6, "min_volume": 500000},
  "risk": {
    "take_profit_pct": 0.025,
    "stop_loss_pct": 0.012,
    "time_stop_min": 30
  },
  "sfs_1s": {"enabled": true, "confirm_up": 8},
  "hedging": {"enabled": true, "beta": 1.5},
  "guardian": {"dd_intraday_pct": 0.025}
}
```

### 平衡型 (适合大多数股票)

```json
{
  "sizing": {"mode": "confidence_scaled", "base_qty": 100, "multiplier": 2.0},
  "market_filter": {"conf_floor": 0.65, "min_volume": 600000},
  "risk": {
    "take_profit_pct": 0.02,
    "stop_loss_pct": 0.01,
    "time_stop_min": 45
  },
  "sfs_1s": {"enabled": true},
  "guardian": {"dd_intraday_pct": 0.02}
}
```

---

## 验证配置正确性

### 1. JSON 语法检查

```bash
jq . strategies/configs/enhanced_ml_AAPL.json
# 如果有错误会显示行号
```

### 2. 查看加载日志

```bash
tail -f logs/trading_system.log | grep ConfigLoader
# 应该看到: "Config loaded from file" 或 "Config file not found"
```

### 3. 检查当前生效配置

在策略代码中添加日志:

```python
self.log.info("Active config", symbol="AAPL", config=sym_cfg)
```

---

## 故障排查 3 步法

### 问题 1: 配置未加载

```bash
# 检查文件是否存在
ls -lh strategies/configs/enhanced_ml_*.json

# 检查文件权限
chmod 644 strategies/configs/enhanced_ml_*.json

# 验证 JSON 格式
jq . strategies/configs/enhanced_ml_AAPL.json
```

### 问题 2: 修改未生效

```bash
# 检查文件修改时间
ls -lt strategies/configs/enhanced_ml_AAPL.json

# 强制重新加载 (Python REPL)
from strategies.enhanced_model_config_loader import get_loader
get_loader().invalidate_cache("AAPL")
```

### 问题 3: 参数冲突

**规则**: JSON 文件 > config.yml > 策略默认值

**调试**: 在 `initialize()` 中打印合并后的配置

---

## 高频问题 FAQ

**Q1: 修改配置后多久生效?**
A: 默认 60 秒。可以在 `config.yml` 中调整 `config_reload_interval_sec`。

**Q2: 可以删除配置文件吗?**
A: 可以。删除后会使用 `config.yml` 的默认配置。

**Q3: 支持实时生效吗 (< 1 秒)?**
A: 不支持。为避免频繁 I/O,最快 1 秒检查一次 (不推荐)。

**Q4: 配置文件需要重启吗?**
A: 不需要。热加载会在后台自动检测文件变更。

**Q5: 如何回滚配置?**
A: 使用 Git:
```bash
git checkout HEAD -- strategies/configs/enhanced_ml_AAPL.json
```

---

## 命令速查表

```bash
# 创建新配置
cp strategies/configs/enhanced_ml_AAPL.json strategies/configs/enhanced_ml_META.json

# 验证 JSON
jq . strategies/configs/enhanced_ml_META.json

# 查看所有配置文件
ls strategies/configs/enhanced_ml_*.json

# 批量修改 (示例: 降低仓位)
for f in strategies/configs/enhanced_ml_*.json; do
  jq '.sizing.base_qty = 50' "$f" > tmp && mv tmp "$f"
done

# 查看加载日志
tail -f logs/trading_system.log | grep -E "(ConfigLoader|Symbol config loaded)"

# 备份配置
tar -czf configs_backup_$(date +%Y%m%d_%H%M%S).tar.gz strategies/configs/enhanced_ml_*.json

# 恢复配置
tar -xzf configs_backup_20251030_120000.tar.gz
```

---

## 下一步

- 📖 阅读完整文档: [ENHANCED_ML_CONFIG_GUIDE.md](./ENHANCED_ML_CONFIG_GUIDE.md)
- 🧪 在 Shadow 模式测试: `shadow_mode: true` in `config.yml`
- 📊 监控策略表现: Grafana 或 `runs/ai_trader/` 日志
- 🔧 参数调优: 使用回测工具验证新参数

---

**提示**: 所有配置文件应纳入 Git 版本控制,便于回滚和审计。
