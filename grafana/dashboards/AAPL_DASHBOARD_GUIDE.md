# AAPL DividendWave Strategy Dashboard 使用指南

## 📊 Dashboard 概览

这个 Grafana 大屏专门为 AAPL DividendWave 策略设计，实时监控策略的每个决策环节。

---

## 🎯 核心指标面板 (第一行)

### 1. **Strategy Status** (策略状态)
- **显示**: 🟢 RUNNING / 🔴 STOPPED
- **含义**: 策略是否成功加载并运行
- **异常**: 如果没有显示"RUNNING"，说明策略未启动或崩溃

### 2. **Daily Trades Count** (今日交易次数)
- **显示**: 0-3 (最大3次)
- **颜色**: 绿(0-1) → 黄(2) → 红(3)
- **含义**: 今天已经执行了几次交易
- **关键**: 达到3次后，当日不再交易

### 3. **PnL Today** (今日盈亏)
- **显示**: 美元金额
- **颜色**: 红(负) → 黄(0) → 绿(正)
- **含义**: 今天累计盈亏
- **说明**: 包括已实现和未实现盈亏

### 4. **Current Position** (当前持仓)
- **显示**: 持仓数量
- **颜色**: 灰(0) → 绿(>0)
- **含义**: 当前持有的AAPL股票数
- **说明**: 0=空仓，>0=持仓中

### 5. **Trend Status** (趋势状态)
- **显示**: 🟢 ALLOW / 🔴 BLOCK
- **含义**: 大趋势(1小时)是否允许做多
- **条件**: EMA20_1h > EMA60_1h 且 Close > VWAP
- **关键**: BLOCK状态下不会开新仓

### 6. **Consecutive Losses** (连续亏损)
- **显示**: 0-2+
- **颜色**: 绿(0) → 黄(1) → 红(2+)
- **含义**: 连续亏损次数
- **关键**: 达到2次后暂停交易

---

## 📈 趋势分析面板 (第二行)

### 7. **Trend Filters Status** (趋势过滤状态)
- **图表类型**: 时序图
- **两条线**:
  - **蓝线**: Daily Trend (1小时) - True=允许，False=阻止
  - **橙线**: Rolling Trend (15分钟) - True=上升，False=无趋势
- **用途**: 查看两层趋势过滤的历史变化
- **关键**: 两条线都为True时才可能开仓

### 8. **Price vs EMA (1h Trend)** (价格与均线对比)
- **图表类型**: 多线时序图
- **四条线**:
  - **实线**: Current Price (当前价格)
  - **虚线1**: EMA20 (20期均线)
  - **虚线2**: EMA60 (60期均线)
  - **虚线3**: VWAP (成交量加权均价)
- **用途**: 可视化大趋势判定条件
- **理想状态**: Price > VWAP，EMA20 > EMA60

---

## 🔍 信号分析面板 (第三行) - 最重要!

### 9. **Entry Signal Analysis (Why No Entry?)** (为什么没有开仓?)
- **表格内容**:
  - **Time**: 检查时间
  - **Breakout**: 是否突破前高 (True/False)
  - **VolSurge**: 成交量是否放大 (True/False)
  - **TopFilter**: 是否被顶部过滤 (True/False)
  - **Reason**: 过滤原因

- **如何使用**:
  1. 看最新一行的数据
  2. 如果 `breakout=False` → 等待突破
  3. 如果 `volume_surge=False` → 等待成交量放大
  4. 如果 `top_filtered=True` → 查看Reason (RSI过高或距VWAP太远)

- **示例解读**:
  ```
  breakout=True | volume_surge=False | top_filtered=False
  → 意思: 价格突破了，但成交量不够，等待放量

  breakout=True | volume_surge=True | top_filtered=True | Reason="RSI overbought: 82 > 80"
  → 意思: 条件都满足，但RSI过高(追高风险)，拒绝开仓
  ```

### 10. **Recent Entry Orders** (最近下单记录)
- **表格内容**:
  - **时间**: 下单时刻
  - **Qty**: 数量
  - **Price**: 入场价格(含滑点)
  - **Stop**: 止损价
  - **Take**: 止盈价

- **用途**: 查看实际执行的订单
- **关键**: 对比止损止盈价格，判断风险回报比

---

## 📊 统计分析面板 (第四行)

### 11. **Signal Rejection Reasons** (信号拒绝原因统计)
- **图表类型**: 横向柱状图
- **显示**: 过去6小时，哪些原因最常导致信号被拒绝
- **常见原因**:
  - "RSI overbought" → RSI > 80
  - "Price too far from VWAP" → 距VWAP > 2×ATR
- **用途**: 了解策略为什么不开仓

### 12. **Volume Analysis** (成交量分析)
- **图表类型**: 时序图
- **两条线**:
  - **实线**: Current Volume (当前成交量)
  - **虚线**: Avg Volume (20-bar平均)
- **用途**: 判断是否有成交量突增
- **信号**: Current > Avg × 1.5 才算放量

---

## 🚪 出场分析面板 (第五行)

### 13. **Exit/Stop Events** (为什么平仓?)
- **表格内容**:
  - **时间**: 平仓时刻
  - **Qty**: 平仓数量
  - **Reason**: 平仓原因

- **常见原因**:
  - `STOP_LOSS` → 触发止损 (price <= entry - 1.5×ATR)
  - `TAKE_PROFIT` → 触发止盈 (price >= entry + 1.8×ATR)
  - `FALSE_BREAKOUT` → 假突破 (EMA5斜率<0 或 连续2根<VWAP)
  - `EOD` → 收盘前强制平仓 (15:55)

- **用途**: 分析每次平仓是盈利退出还是止损

### 14. **Fill History** (成交历史)
- **表格内容**:
  - **时间**: 成交时刻
  - **Side**: BUY / SELL
  - **价格**: 成交价
  - **PnL**: 当时的累计盈亏
  - **Pos**: 成交后的持仓

- **用途**: 完整的交易流水
- **示例**: `BUY 10@230.50 | PnL=0 | Pos=10` → 买入10股，持仓10股

---

## 🔴 实时诊断面板 (第六行) - 最有用!

### 15. **Why No Trade? (Live Diagnosis)** (实时诊断)
- **显示**: 一行文字说明当前状态
- **可能的输出**:
  - ✅ `Strategy Running` → 策略正常运行
  - 🚫 `Max trades reached (3)` → 今日交易次数已满
  - 🚫 `Consecutive losses >= 2` → 连续亏损，已暂停
  - ⛔ `Daily Trend: Block (EMA20<EMA60 or Close<VWAP)` → 大趋势不允许
  - ⛔ `Rolling Trend: No uptrend (15m)` → 15分钟无上升趋势
  - ⏳ `Waiting for breakout (price > prev_high)` → 等待突破
  - ⏳ `Waiting for volume surge (>1.5x avg)` → 等待放量
  - 🔴 `Top Filter: RSI overbought: 82 > 80` → 顶部过滤拒绝
  - 🟢 `All conditions OK, waiting for signal` → 所有条件满足，等待信号

- **用途**: 一眼看出策略为什么没有交易
- **最重要**: 这是你最应该盯的面板!

---

## 📜 日志面板 (最后一行)

### 16. **Live Strategy Logs** (实时日志)
- **显示**: 所有策略日志 (info/warning/error)
- **用途**:
  - 调试问题
  - 查看详细的执行过程
  - 追踪异常情况
- **技巧**:
  - 点击某行展开查看完整JSON
  - 使用搜索框过滤关键词

---

## 🎓 使用场景示例

### 场景1: "为什么一整天都没交易?"

1. 先看 **"Why No Trade?"** 面板
   - 如果显示 `Daily Trend: Block` → 大趋势不允许，查看面板8确认EMA位置
   - 如果显示 `Rolling Trend: No uptrend` → 15分钟无上升趋势，正常
   - 如果显示 `Max trades reached` → 已经交易3次了，查看面板2

2. 查看 **"Entry Signal Analysis"** 表格
   - 看最近的检查记录
   - 如果 `breakout=False` 一直是False → AAPL今天没有突破信号
   - 如果 `volume_surge=False` → 有突破但成交量不够

### 场景2: "为什么有信号但没下单?"

1. 查看 **"Entry Signal Analysis"** 最新一行
2. 看 `top_filtered` 和 `top_reason`:
   - 如果是 `RSI overbought` → 说明追高风险太大
   - 如果是 `Price too far from VWAP` → 说明价格已经偏离太多
3. 这是策略的保护机制，避免追高

### 场景3: "为什么这么快就止损了?"

1. 查看 **"Recent Entry Orders"** 找到入场价和止损价
2. 查看 **"Exit/Stop Events"** 确认是否触发止损
3. 如果是 `FALSE_BREAKOUT` → 说明是假突破，策略提前保护
4. 如果是 `STOP_LOSS` → 说明价格跌破了 entry - 1.5×ATR

### 场景4: "想看历史表现"

1. 调整页面顶部的时间范围 (默认6小时)
2. 查看 **"Fill History"** 表格，看所有成交
3. 查看 **"PnL Today"** 面板的数值

---

## 🚀 导入Dashboard到Grafana

### 方法1: 通过Grafana UI导入 (推荐)

1. 打开 Grafana (http://localhost:3000)
2. 点击左侧菜单 `Dashboards` → `Import`
3. 点击 `Upload JSON file`
4. 选择文件: `/workspace/Documents/trading/grafana/dashboards/aapl_dividend_wave_monitor.json`
5. 点击 `Import`

### 方法2: 直接复制到Grafana目录 (如果配置了provisioning)

```bash
cp grafana/dashboards/aapl_dividend_wave_monitor.json ~/grafana-stack/grafana/provisioning/dashboards/
```

---

## ⚠️ 使用前提

1. **策略必须启用**:
   - 在 `config.yml` 中将 `aapl_dividend_wave_001` 的 `enabled` 设为 `true`
   - 重启 `run_system.py`

2. **Loki 和 Promtail 必须运行**:
   - 日志必须被收集到 Loki

3. **数据源配置**:
   - Grafana 中必须配置好 Loki 数据源 (uid: "loki")

---

## 📞 Troubleshooting

### 问题1: Dashboard显示"No data"

**原因**: 策略可能未启动，或者Loki没有收到日志

**解决**:
```bash
# 检查策略是否运行
ps aux | grep run_system.py

# 检查日志是否写入
tail -f ~/grafana-stack/logs/trader.log | grep "aapl_dividend_wave"

# 检查Loki是否收到日志
# 在Grafana Explore中执行:
{job="trader"} | json | strategy_id="aapl_dividend_wave_001"
```

### 问题2: "Why No Trade?" 面板不显示

**原因**: 日志解析表达式可能需要调整

**解决**: 在Grafana Explore中测试查询:
```logql
{job="trader"} | json | strategy_id="aapl_dividend_wave_001"
```

### 问题3: 面板显示解析错误

**原因**: 日志格式不是JSON，或者字段名不匹配

**解决**: 查看原始日志确认格式:
```bash
tail -100 ~/grafana-stack/logs/trader.log | grep aapl_dividend_wave | tail -5
```

---

## 🎯 最佳实践

1. **每天开盘后**: 检查 "Strategy Status" 和 "Trend Status"
2. **盯盘时**: 主要看 "Why No Trade?" 和 "Entry Signal Analysis"
3. **复盘时**: 查看 "Fill History" 和 "Exit/Stop Events"
4. **优化时**: 分析 "Signal Rejection Reasons" 找出改进点

---

## 📈 下一步

启用策略后，你应该能在大屏上看到:
1. 策略启动日志 (Strategy Status = RUNNING)
2. 每分钟的趋势检查 (Trend Filters Status)
3. 入场信号分析 (Entry Signal Analysis)
4. 实时诊断信息 (Why No Trade?)

祝交易顺利! 🚀
