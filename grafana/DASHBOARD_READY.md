# ✅ Grafana Dashboard 已就绪!

**日期**: 2025-10-30
**状态**: ✅ 所有组件正常运行

---

## 🎯 立即访问 Dashboard

### 主 Dashboard (包含所有 21 个指标)

**直接打开**:
```bash
open http://localhost:3000/d/ed2b85c5-7396-492d-8306-596a6c0977df/ai-trader-complete-dashboard-all-metrics
```

或在浏览器访问:
```
http://localhost:3000/d/ed2b85c5-7396-492d-8306-596a6c0977df/ai-trader-complete-dashboard-all-metrics
```

**登录信息**:
- Username: `admin`
- Password: `admin`

---

## ✅ 验证结果

### 1. 容器状态
```
✅ Grafana:    running (healthy)
✅ ClickHouse: running (healthy)
```

### 2. Datasources
```
✅ ClickHouse (uid: ch)         - 默认数据源
✅ Prometheus (uid: prometheus) - 正常
✅ Postgres   (uid: pg)         - 正常
```

### 3. Dashboard
```
✅ AI Trader Complete Dashboard - 已加载
   UID: ed2b85c5-7396-492d-8306-596a6c0977df
   Title: AI Trader Complete Dashboard (All Metrics)
   Created: 2025-10-30 09:04:07
```

### 4. 数据验证
```
✅ ClickHouse 中有 105 条 metrics 记录
✅ 所有 21 个指标都存在:
   1. oos_sharpe                      ✅
   2. oos_mdd                         ✅
   3. oos_trades                      ✅
   4. rl_gate_ok                      ✅
   5. dq_ok                           ✅
   6. dq_failed_count                 ✅
   7. dq_passed_count                 ✅
   8. guardian_events                 ✅
   9. guardian_max_dd_pct             ✅
   10. hedging_events                 ✅
   11. hedging_notional_usd           ✅
   12. pending_count                  ✅
   13. pending_disable_rl             ✅
   14. pending_increase_conf_floor    ✅
   15. pending_reduce_sizing_multiplier ✅
   16. overlay_beta                   ✅
   17. overlay_need_hedge             ✅
   18. tools_kill                     ✅
   19. tools_restart                  ✅
   20. tools_backoff                  ✅
   21. tools_running                  ✅
```

---

## 📊 Dashboard 结构

### Section 1: Today Overview (8 KPI Cards)
- OOS Sharpe
- OOS Max Drawdown
- OOS Trades
- RL Gate OK
- DQ OK
- Guardian Max DD
- Hedging Notional
- Tools Running

### Section 2: Data Quality
- DQ Failed Count
- DQ Passed Count
- DQ Trend (7 days)

### Section 3: Risk & Hedging
- Guardian Events
- Hedging Events
- Guardian DD Trend
- Hedging Notional Trend
- Tools Ledger (Event Table)

### Section 4: Strategy & Deployment
- Performance Trend (7 days)
- Pending Actions (Bar Gauge)

### Section 5: Portfolio & Overlay
- Overlay Beta
- Overlay Need Hedge
- Overlay Beta Trend

### Section 6: LLM Tools Health
- Tools Kill Count
- Tools Restart Count
- Tools Backoff Count
- Tools Events Trend (7 days)

### Section 7: Brain Calls
- Brain Calls (Today)
- Brain Success Rate
- Brain Calls Trend (7 days)
- Brain Calls Detail (Table)

---

## 🔧 Dashboard 功能

### 自动刷新
- 默认: 30 秒
- 可在右上角调整

### 时间范围
- 默认: Last 7 days
- 可选: 6h, 12h, 24h, 7d, 30d, 90d

### 面板操作
- 点击面板标题可以查看详情、编辑、全屏
- 可导出数据为 CSV/Excel

---

## 📁 文件位置

### Dashboard JSON (源文件)
```
./dashboards/AI_Trader_Complete_Dashboard.json
```

### 已部署的 Dashboard
```
./grafana/dashboards/active/ai_trader_complete.json
```

### Datasource 配置
```
./grafana/provisioning/datasources/ds.yml
```

---

## 🚀 快速开始

### 如果看到 "No data"

**原因 1: 时间范围不对**
- 点击右上角时间选择器
- 选择 "Last 7 days" 或 "Last 30 days"

**原因 2: 今天还没运行 AI Trader**
```bash
cd .
python scripts/ai_trader/auto_loop.py
```

**原因 3: 查看历史数据**
```bash
# 查询最新的数据
curl -s "http://localhost:8123/?database=trading&query=SELECT%20date,%20name,%20value%20FROM%20ai_trader_dash_metrics%20ORDER%20BY%20ts%20DESC%20LIMIT%2010%20FORMAT%20Pretty"
```

---

## 📊 其他可用的 Dashboards

### 1. Trading System Operations
```
http://localhost:3000/d/trading-system-ops/trading-system-operations
```

### 2. Strategy & Decision Pipeline Insight
```
http://localhost:3000/d/strategy-pipeline/strategy-and-decision-pipeline-insight
```

### 3. Financial & Market Overview
```
http://localhost:3000/d/financial-market-overview/financial-and-market-overview
```

### 4. Infrastructure & Platform Health
```
http://localhost:3000/d/infra-platform-health/infrastructure-and-platform-health
```

---

## 🛠️ 维护命令

### 重启 Grafana
```bash
docker restart trading_grafana
```

### 重新加载 Dashboards (无需重启)
```bash
curl -X POST http://admin:admin@localhost:3000/api/admin/provisioning/dashboards/reload
```

### 查看 Grafana 日志
```bash
docker logs trading_grafana -f
```

### 备份 Dashboard JSON
```bash
cp ./grafana/dashboards/active/ai_trader_complete.json \
   ~/ai_trader_dashboard_backup_$(date +%Y%m%d).json
```

---

## 📞 故障排查

### Dashboard 显示错误
1. 检查 Grafana 日志: `docker logs trading_grafana --tail 50`
2. 检查 ClickHouse 连接: `curl http://localhost:8123/ping`
3. 测试 datasource: Grafana UI → Configuration → Data Sources → ClickHouse → Test

### 数据不更新
1. 检查 ClickHouse 数据:
   ```bash
   curl -s "http://localhost:8123/?database=trading&query=SELECT%20max(ts)%20FROM%20ai_trader_dash_metrics%20FORMAT%20Pretty"
   ```
2. 确保 AI Trader 正在运行
3. 检查 events exporter 是否运行

---

## 📝 相关文档

- **完整配置报告**: `./grafana/GRAFANA_RECONFIGURATION_COMPLETE.md`
- **快速启动指南**: `./grafana/QUICK_START.md`
- **指标说明**: `./scripts/ai_trader/dashboards/打点清单.md`
- **Brain 监控架构**: `./doc/AI_BRAIN_TACTICAL_MANUAL.md`

---

## ✅ 一切就绪!

你的 Grafana Dashboard 已经完全配置好了,包含:

- ✅ 所有 21 个监控指标
- ✅ 7 大监控区块
- ✅ 30+ 可视化面板
- ✅ 自动刷新和时间范围选择
- ✅ ClickHouse 数据连接正常
- ✅ 105 条历史数据记录

**立即访问**: http://localhost:3000/d/ed2b85c5-7396-492d-8306-596a6c0977df/ai-trader-complete-dashboard-all-metrics

🎉 **开始监控你的 AI Trader 吧!**
