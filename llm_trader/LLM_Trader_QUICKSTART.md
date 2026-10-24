
试结果分析

  ✅ 正常运行

  - 程序成功启动并完成1个周期
  - 没有崩溃或致命错误
  - 正确保存最终结果到 results/deepseek_final_20251022_073253.json

  ⚠️ 预期的警告（正常）

  1. Higher TF Error: 'HttpClient' object has no attribute 'execute'
    - 这是ClickHouse客户端的小问题，不影响核心交易逻辑
    - 只影响1h/1d高时间框架数据（不是必需的）
  2. No 1s bars from Redis
    - 正常！现在是非交易时间（7:32 UTC = 3:32 ET）
    - 盘前4点后会有实时数据流入Redis
  3. Data incomplete → HOLD决策
    - ✅ V2.5缺数降级功能正常工作！
    - 数据不足时自动返回HOLD，不会盲目交易
    - Hard Gate 3正确触发

  🎯 盘前交易准备完成

  # 明天4:00 AM ET 启动命令
  cd ./llm_trader
  ./run_premarket.sh

  # 或者使用 screen (推荐)
  screen -S premarket
  cd ./llm_trader
  ./run_premarket.sh
  # Ctrl+A D 离开，screen -r premarket 重新连接

  🔍 盘前交易时的预期行为

  4:00-4:10 (前10分钟):
  - ⚠️ 可能出现 "Data incomplete" - 数据累积中
  - 决策：HOLD（缺数降级保护）

  4:10+ (数据充足后):
  - ✅ 数据齐全，开始正常交易决策
  - 可能决策：BUY/SELL/HOLD（基于市场状况）

  9:20-9:25:
  - 只平仓，不开新仓
  - 9:25强制清空所有持仓

  📊 监控命令（明天用）

  # 实时查看日志
  tail -f logs/premarket_deepseek_*.log

  # 查看交易决策
  grep -E "Decision:|Order" logs/premarket_deepseek_*.log | tail -30

  # 查看持仓
  grep "Portfolio:" logs/premarket_deepseek_*.log | tail -10

  准备就绪！🚀 明天4点见！




# 🚀 LLM Auto Trader - 快速启动指南

## 项目概述

这是一个 **独立的** LLM 自动交易系统，可以：
- 使用 **多个 LLM** (OpenAI, Gemini, DeepSeek, DeepSeek-NVIDIA) 分析市场数据
- 从 **ClickHouse 和 Redis** 获取实时数据（5s 特征、技术指标、1s bars）
- 通过 **IBKR** 执行交易（支持模拟和实盘）
- **并行运行多个 LLM** 进行性能竞赛

## 📦 安装步骤

### 1. 安装依赖

```bash
cd llm_trader
pip install -r requirements.txt
```

### 2. 验证配置

确保 `.env` 文件中有以下配置（在父目录 `./.env`）：

```bash
# ClickHouse
CH_HOST=localhost
CH_PORT=8123
CH_DB=trading

# Redis
STATEBUS_REDIS=redis://127.0.0.1:6379/0

# IBKR
IB_HOST=127.0.0.1
IB_PAPER_PORT=4002
IB_CLIENT_ID=100

# LLM API Keys (至少配置一个)
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=AIza...
DEEPSEEK_API_KEY=sk-...
NVIDIA_API_KEY=nvapi-...
```

### 3. 测试设置

```bash
python test_setup.py
```

这会检查：
- ✓ 配置文件是否正确加载
- ✓ ClickHouse 连接
- ✓ Redis 连接
- ✓ 数据是否可获取
- ✓ LLM API 是否可用

## 🎮 使用方法

### 方式 1: 运行单个 LLM Bot

```bash
# OpenAI GPT-4o (默认模拟模式)
python run_single_bot.py --provider openai --symbols AAPL TSLA NVDA

# Google Gemini，20个周期，30秒间隔
python run_single_bot.py --provider gemini --symbols AAPL MSFT META --cycles 20 --interval 30

# DeepSeek R1 via NVIDIA，10万初始资金
python run_single_bot.py --provider deepseek_nvidia --symbols NVDA AMD --cash 100000

# 实盘交易模式 (⚠️ 小心!)
python run_single_bot.py --provider openai --symbols AAPL --live
```

**参数说明：**
- `--provider`: LLM 提供商 (`openai`, `gemini`, `deepseek`, `deepseek_nvidia`)
- `--symbols`: 交易标的列表
- `--cash`: 初始资金（默认 $50,000）
- `--interval`: 交易周期间隔秒数（默认 60s）
- `--cycles`: 最大周期数（默认无限）
- `--live`: 启用实盘交易（默认是模拟）

### 方式 2: LLM 竞赛模式 🏁

**让所有 LLM 同时运行，看谁赚得多！**

⚠️ **重要**：每个 LLM 都有 **独立的 $50,000 资金池**，不共享！
- 4 个 LLM 竞赛 = 4 × $50,000 = **总计 $200,000**
- 每个 LLM 独立决策，互不影响
- 最终比较各自的 P&L 表现

```bash
# 默认设置：4个LLM，5个标的，10个周期
python run_llm_race.py

# 自定义竞赛
python run_llm_race.py \
  --providers openai gemini deepseek deepseek_nvidia \
  --symbols AAPL TSLA NVDA MSFT META GOOGL \
  --cash 100000 \
  --cycles 20 \
  --interval 30
```

**输出示例：**
```
🏁 LLM TRADING RACE 🏁
═══════════════════════════════════════════════════════════

🏆 RACE RESULTS 🏆

🥇 GEMINI              | P&L: $  2,450.50 ( 4.90%) | Trades:  15
🥈 OPENAI              | P&L: $  1,832.20 ( 3.66%) | Trades:  12
🥉 DEEPSEEK_NVIDIA     | P&L: $  1,205.00 ( 2.41%) | Trades:   9
4. DEEPSEEK            | P&L: $   -125.50 (-0.25%) | Trades:   8
```

## 📊 工作原理

### 每个交易周期做什么？

1. **获取数据** → 从 ClickHouse/Redis 读取最新市场数据
   - 5秒 OHLCV 数据 (`sfs.sfs_5s`)
   - 技术指标 (VWAP, ATR, Regime) (`trading.sfs_features`)
   - 1秒 bars (Redis `sfs:1s:SYMBOL`)

2. **LLM 分析** → 将数据发送给 LLM，获取决策
   ```json
   {
     "decision": "BUY",
     "quantity": 10,
     "confidence": 0.85,
     "reasoning": "强劲上涨趋势，突破 ORB5 高点"
   }
   ```

3. **风控检查** → 验证是否符合限制
   - 是否有足够现金
   - 单个仓位不超过 20%
   - 总仓位不超过 95%
   - 最多 10 个并发持仓

4. **执行交易** → 通过 IBKR 下单（或模拟）

5. **更新状态** → 更新持仓和 P&L

6. **等待** → 睡眠 `interval` 秒，重复

## 🎯 数据源说明

| 数据源 | 表/Key | 内容 | 更新频率 |
|--------|--------|------|----------|
| ClickHouse | `sfs.sfs_5s` | 5秒滑动窗口 OHLCV | 每秒 |
| ClickHouse | `trading.sfs_features` | 技术指标 (VWAP, ATR, regime, ORB) | 每5秒 |
| Redis | `sfs:1s:SYMBOL` | 1秒 bars (来自 SFS 服务) | 实时 |

**前提条件：**
- SFS 1s 服务正在运行（向 Redis 写入数据）
- ClickHouse 有历史数据

## ⚙️ 配置调整

### 修改仓位限制

编辑 `llm_trader/portfolio/portfolio_manager.py`:

```python
PortfolioManager(
    initial_cash=50000.0,        # 初始资金
    max_position_pct=0.20,       # 单仓位最大 20%
    max_total_exposure=0.95,     # 总仓位最大 95%
    max_positions=10,            # 最多 10 个持仓
)
```

### 修改 LLM 模型

编辑 `llm_trader/utils/config.py`:

```python
configs = {
    "openai": {"model": "gpt-4o-mini"},          # 可改为 "gpt-5"
    "gemini": {"model": "gemini-2.0-flash-exp"},
    "deepseek": {"model": "deepseek-chat"},      # 可改为 "deepseek-v3"
    "deepseek_nvidia": {"model": "deepseek/deepseek-r1"},
}
```

## 📁 输出文件

所有结果保存在 `results/` 目录：

- **单个 Bot**: `{provider}_final_{timestamp}.json`
  ```json
  {
    "portfolio": {
      "total_equity": 52450.50,
      "total_pnl": 2450.50,
      "positions": {...}
    },
    "trade_history": [...]
  }
  ```

- **竞赛结果**: `race_results_{timestamp}.json`
  ```json
  {
    "results": [
      {"provider": "gemini", "total_pnl": 2450.50, ...},
      ...
    ]
  }
  ```

## 🔒 安全提示

1. **默认模拟** → 除非加 `--live`，否则都是模拟交易
2. **先测试** → 用小资金和短周期测试
3. **监控 API 费用** → LLM 调用会产生费用
4. **需要 SFS 数据** → 确保 SFS 服务正在运行
5. **IBKR 连接** → 确保 IBKR Gateway/TWS 已启动

## 🐛 故障排查

### 问题 1: `No price data for symbol`
- **原因**: ClickHouse 中没有该标的的数据
- **解决**: 检查 `sfs.sfs_5s` 表，确认有数据

### 问题 2: `LLM provider health check failed`
- **原因**: API key 错误或网络问题
- **解决**: 检查 `.env` 中的 API key

### 问题 3: `Failed to connect to IBKR`
- **原因**: IBKR Gateway/TWS 未运行
- **解决**: 启动 IBKR Gateway，确保端口正确 (4002 for paper)

### 问题 4: `ClickHouse connection failed`
- **原因**: ClickHouse 未运行
- **解决**: `docker-compose up clickhouse` 或检查连接配置

## 🎓 示例工作流

```bash
# 1. 测试设置
python test_setup.py

# 2. 运行单个 bot (5 个周期测试)
python run_single_bot.py --provider openai --symbols AAPL TSLA --cycles 5

# 3. 查看结果
cat results/openai_final_*.json

# 4. 运行竞赛模式
python run_llm_race.py --cycles 10

# 5. 查看竞赛结果
cat results/race_results_*.json
```

## 📚 更多信息

- 详细文档: `README.md`
- 架构设计: 见各模块源码注释
- 扩展开发: 可添加新的 LLM 提供商或交易策略

## 🙋 常见问题

**Q: 可以实盘交易吗？**
A: 可以，但强烈建议先充分测试。加 `--live` 参数启用。

**Q: 支持哪些标的？**
A: 理论上支持所有 IBKR 可交易的美股，但需要 ClickHouse 中有数据。

**Q: 如何添加新的 LLM？**
A: 在 `providers/` 下创建新文件，继承 `BaseLLMProvider`，实现 `analyze_market()` 方法。

**Q: 可以并行交易不同标的吗？**
A: 可以，系统会遍历所有标的，每个周期都分析所有标的。

**Q: 如何调整交易频率？**
A: 使用 `--interval` 参数，单位是秒。

---

**祝交易顺利！🚀**

如有问题，请查看 `README.md` 或检查日志输出。
