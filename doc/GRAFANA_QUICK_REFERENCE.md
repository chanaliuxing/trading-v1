# Grafana 快速参考卡

## 关键路径 (记住这些!)

```bash
项目根目录: ./
Dashboard(现役):   ./grafana/dashboards/active/*.json
Dashboard(归档):   ./grafana/archive_dashboards/
日志:       /workspace/grafana-stack/logs/trader.log
配置:       ./docker-compose.yml
```

## 服务地址

```
Grafana:    http://localhost:3000 (admin/admin)
Loki:       http://localhost:3100
ClickHouse: http://localhost:8123 (无密码)
Prometheus: http://localhost:9090
node_exporter:   http://localhost:9100/metrics
cadvisor:        http://localhost:8080/metrics
kafka_exporter:  http://localhost:9308/metrics
redis_exporter:  http://localhost:9121/metrics
postgres_exporter:http://localhost:9187/metrics
```

## 最常用命令

### 重启服务
```bash
cd .
docker-compose restart
# 或一键启动所有监控组件（含 Prometheus）
docker-compose up -d grafana loki promtail tempo node_exporter cadvisor kafka_exporter redis_exporter postgres_exporter prometheus
```

### 查看日志
```bash
docker logs trading-grafana-1
docker logs trading-loki-1 | grep error
```

### 测试数据源
```bash
# Loki
curl -s http://localhost:3100/ready

# ClickHouse
curl -s "http://localhost:8123/" --data "SELECT 1"

# 查看 Loki 有什么 labels
curl -s 'http://localhost:3100/loki/api/v1/labels' | jq '.data'
```

### 上传 Dashboard
```bash
cd ./grafana/dashboards/active

python3 << 'PYEOF'
import json, subprocess, glob
for f in glob.glob('*.json'):
    dashboard = json.load(open(f))
    result = subprocess.run(['curl', '-s', '-X', 'POST',
        'http://admin:admin@localhost:3000/api/dashboards/db',
        '-H', 'Content-Type: application/json',
        '-d', json.dumps({'dashboard': dashboard, 'overwrite': True})
    ], capture_output=True, text=True)
    print(f"✅ {f}" if 'success' in result.stdout else f"❌ {f}")
PYEOF
```

> 历史 JSON 如果需要回滚，先从 `../../archive_dashboards` 复制回 `active/`，再运行上述脚本或重启 Grafana。

### Alert 联系点

联系点和策略在 `grafana/provisioning/alerting/` 下管理：

- `contact-points.yaml` — Slack / Email Webhook (使用环境变量 `SLACK_WEBHOOK_URL`, `ALERT_EMAIL_RECIPIENTS`)
- `notification-policies.yaml` — 默认路由 (目前全部指向 Slack)
- `rules.yaml` — Loki / Prometheus 规则

## 常见错误速查

### "Cannot read properties of undefined (reading 'split')"
→ Template 变量查询返回空  
→ 检查: `curl "http://localhost:8123/" --data "SELECT COUNT(*) FROM trading.runs"`  
→ 修复: 在 JSON 中添加 `"allValue": ".*"` 和 `"options": []`

### "syntax error: unexpected IDENTIFIER"
→ Loki 查询用了反引号  
→ 查找: `grep -n '\`' *.json | grep -v line_format`  
→ 修复: 替换 `` |~ `pattern` `` 为 `|~ "pattern"`

### "No Data"
→ 数据源没数据  
→ Loki: 检查 Promtail 是否运行 `docker ps | grep promtail`  
→ ClickHouse: 检查表有数据 `curl "http://localhost:8123/" --data "SELECT COUNT(*) FROM trading.order_events"`

### Dashboard 重启后消失
→ 没有持久化  
→ 确认 docker-compose.yml 有:
```yaml
grafana:
  volumes:
    - grafana-data:/var/lib/grafana
volumes:
  grafana-data:
```

## JSON 修复模板

### Template 变量 (空数据友好)
```json
{
  "type": "query",
  "name": "variable_name",
  "query": "SELECT DISTINCT column FROM table",
  "includeAll": true,
  "allValue": ".*",
  "current": {"text": "All", "value": ["$__all"], "selected": true},
  "options": []
}
```

### ClickHouse Panel Target
```json
{
  "query": "SELECT ts, value FROM trading.table WHERE $__timeFilter(ts)",
  "format": "time_series",
  "queryType": "sql",
  "datasource": {"type": "grafana-clickhouse-datasource", "uid": "ch"}
}
```

### Loki Panel Target
```json
{
  "expr": "{job=\"trader\"} |= \"event_name\" | json",
  "legendFormat": "{{label}}"
}
```

## 完整文档
详细内容请看: `./doc/GRAFANA_TROUBLESHOOTING_GUIDE.md`
