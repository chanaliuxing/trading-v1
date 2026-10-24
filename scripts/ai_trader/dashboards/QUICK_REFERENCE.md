# AI Trader 三大屏 - 快速参考卡片

## 🚀 一键启动

```bash
cd .
bash scripts/ai_trader/dashboards/start_all_dashboards.sh
```

---

## 📊 三大屏快速访问

| 大屏 | URL / 命令 | 用途 |
|------|------------|------|
| **MLflow** | http://localhost:5001 | 策略学习与进化 |
| **Grafana** | http://localhost:3000 (admin/admin) | 系统实时状态 |
| **Daily Report** | `bash scripts/ai_trader/dashboards/view_daily_report.sh` | 每日战报 |

---

## 💡 常用命令

### 查看服务状态
```bash
# 所有 Docker 容器
docker ps --filter name=trading_

# MLflow 是否运行
lsof -i :5000

# Grafana 是否运行
curl -s http://localhost:3000/api/health
```

### 查看日志
```bash
# MLflow
tail -f /tmp/mlflow_startup.log

# Grafana
docker logs -f trading_grafana

# ClickHouse
docker logs -f trading_clickhouse
```

### 重启服务
```bash
# MLflow
pkill -f 'mlflow ui' && bash scripts/ai_trader/dashboards/start_mlflow.sh

# Grafana
docker restart trading_grafana

# 所有基础设施
cd infra && docker-compose restart
```

---

## 📈 监控工作流

### 早上开盘前 (8:00-9:30)
```bash
# 1. 查看昨晚学习结果
open http://localhost:5000

# 2. 查看每日战报
bash scripts/ai_trader/dashboards/view_daily_report.sh

# 3. 打开实时监控
open http://localhost:3000
```

### 盘中监控 (9:30-16:00)
- ✅ Grafana 自动刷新 (10s 间隔)
- ✅ 关注 P&L 曲线
- ✅ 监控 LLM API 错误率
- ✅ 检查进程心跳 (< 180s)

### 盘后复盘 (16:00-20:00)
```bash
# 查看今日表现
bash scripts/ai_trader/dashboards/view_daily_report.sh --auto
```

---

## 🔧 故障排查

### MLflow 无法访问
```bash
# 检查进程
lsof -i :5000

# 重启
bash scripts/ai_trader/dashboards/start_mlflow.sh
```

### Grafana "No Data"
```bash
# 检查 ClickHouse 数据
clickhouse-client --query "SELECT count() FROM llm.llm_sessions WHERE trading_day = today()"

# 测试连接
curl http://localhost:8123/ping
```

### 每日战报不存在
```bash
# 检查文件
ls -la runs/ai_trader/$(date +%Y-%m-%d)/

# 手动运行 evening_learning
PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/cli.py run --mode learn
```

---

## 📞 获取帮助

- **完整文档**: `scripts/ai_trader/dashboards/README_DASHBOARDS.md`
- **监控指南**: `scripts/ai_trader/MONITORING_GUIDE.md`
- **快速启动**: `scripts/ai_trader/QUICK_START.md`

**最后更新**: 2025-10-29
