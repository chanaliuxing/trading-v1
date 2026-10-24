# EnhancedModelSignalStrategy 配置目录

本目录包含 EnhancedModelSignalStrategy 的 **Per-Symbol 配置系统**。

## 📁 目录结构

```
strategies/configs/
├── enhanced_ml_AAPL.json          # AAPL 配置 (保守型)
├── enhanced_ml_TSLA.json          # TSLA 配置 (激进型)
├── enhanced_ml_NVDA.json          # NVDA 配置 (平衡型)
├── enhanced_ml_*.json             # 其他股票配置...
├── ENHANCED_ML_CONFIG_GUIDE.md    # 详细配置指南 (5000+ 字)
├── ENHANCED_ML_QUICK_START.md     # 5 分钟快速上手
└── README.md                      # 本文件
```

## 🚀 快速开始

### 1. 添加新股票

```bash
# 复制模板
cp enhanced_ml_AAPL.json enhanced_ml_META.json

# 编辑配置
vim enhanced_ml_META.json

# 添加到 config.yml 的 symbols 列表
vim ../../config.yml
```

### 2. 查看现有配置

```bash
# 列出所有配置文件
ls enhanced_ml_*.json

# 查看特定配置
cat enhanced_ml_AAPL.json | jq .

# 查看所有股票的 sizing 模式
for f in enhanced_ml_*.json; do
  echo "$f: $(jq -r '.sizing.mode' $f)"
done
```

### 3. 验证配置

```bash
# 验证 JSON 语法
jq . enhanced_ml_AAPL.json

# 运行完整测试
python ../../test_enhanced_ml_config.py
```

## 📖 文档速查

| 文档 | 用途 | 阅读时间 |
|------|------|---------|
| [ENHANCED_ML_QUICK_START.md](./ENHANCED_ML_QUICK_START.md) | 5 分钟上手指南 | 5 分钟 |
| [ENHANCED_ML_CONFIG_GUIDE.md](./ENHANCED_ML_CONFIG_GUIDE.md) | 完整配置参考手册 | 20 分钟 |
| [../../ENHANCED_ML_CONFIG_IMPLEMENTATION.md](../../ENHANCED_ML_CONFIG_IMPLEMENTATION.md) | 技术实现报告 | 10 分钟 |

## 🔥 热加载特性

配置文件修改后 **60 秒**内自动生效,无需重启策略!

```bash
# 修改配置
vim enhanced_ml_AAPL.json

# 等待 60 秒,系统自动重新加载

# 查看日志确认
tail -f ../../logs/trading_system.log | grep "Config loaded from file"
```

## 📊 配置文件结构

### 最小示例

```json
{
  "symbol": "AAPL",
  "version": "1.0.0",
  "sizing": {
    "mode": "fixed",
    "base_qty": 100
  },
  "market_filter": {
    "conf_floor": 0.65
  },
  "risk": {
    "take_profit_pct": 0.02,
    "stop_loss_pct": 0.01
  }
}
```

### 完整示例

参考现有文件:
- `enhanced_ml_AAPL.json` - 保守型 (低风险,长持仓)
- `enhanced_ml_TSLA.json` - 激进型 (高风险,对冲)
- `enhanced_ml_NVDA.json` - 平衡型 (RL 仓位管理)

## 🎯 配置块说明

| 配置块 | 必需 | 说明 |
|--------|------|------|
| `symbol` | ✅ | 股票代码 (大写) |
| `version` | ✅ | 配置版本号 |
| `sizing` | ✅ | 仓位计算策略 |
| `market_filter` | ✅ | 市场准入条件 |
| `risk` | ✅ | 风险管理参数 |
| `order` | ❌ | 订单执行配置 |
| `sfs_1s` | ❌ | 微观结构确认 |
| `rl` | ❌ | 强化学习仓位管理 |
| `hedging` | ❌ | 对冲配置 |
| `guardian` | ❌ | 实时风控熔断 |

## 🔧 常用命令

### 配置管理

```bash
# 创建新配置
cp enhanced_ml_AAPL.json enhanced_ml_NEW.json

# 备份所有配置
tar -czf configs_backup_$(date +%Y%m%d).tar.gz enhanced_ml_*.json

# 恢复配置
tar -xzf configs_backup_20251030.tar.gz

# 版本控制
git add enhanced_ml_*.json
git commit -m "feat: update AAPL config to v1.1.0"
```

### 批量操作

```bash
# 查看所有 conf_floor
for f in enhanced_ml_*.json; do
  echo "$f: $(jq -r '.market_filter.conf_floor' $f)"
done

# 批量降低仓位 (示例)
for f in enhanced_ml_*.json; do
  jq '.sizing.base_qty /= 2' "$f" > tmp && mv tmp "$f"
done
```

## ⚠️ 注意事项

1. **文件命名**: 必须是 `enhanced_ml_{SYMBOL}.json` (SYMBOL 大写)
2. **JSON 格式**: 使用 `jq` 验证语法,避免运行时错误
3. **版本管理**: 每次修改更新 `version` 和 `last_updated` 字段
4. **Git 追踪**: 所有配置文件应纳入版本控制
5. **测试先行**: 重大参数变更应先在 Shadow 模式或回测中验证

## 🐛 故障排查

### 配置未加载

```bash
# 检查文件名格式
ls enhanced_ml_*.json

# 检查 JSON 语法
jq . enhanced_ml_AAPL.json

# 查看加载日志
tail -f ../../logs/trading_system.log | grep ConfigLoader
```

### 热加载不生效

```bash
# 检查文件修改时间
stat enhanced_ml_AAPL.json

# 手动触发重载 (Python REPL)
from strategies.enhanced_model_config_loader import get_loader
get_loader().invalidate_cache("AAPL")
```

## 📞 支持

- **问题报告**: 在日志中搜索 `ConfigLoader` 关键字
- **文档**: 查看 [ENHANCED_ML_CONFIG_GUIDE.md](./ENHANCED_ML_CONFIG_GUIDE.md)
- **测试**: 运行 `python ../../test_enhanced_ml_config.py`

---

**最后更新**: 2025-10-30
**维护**: Trading Strategy Team
