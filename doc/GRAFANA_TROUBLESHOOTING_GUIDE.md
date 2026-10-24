# Grafana Dashboard 故障排查完整指南

**最后更新**: 2025-10-18  
**目的**: 让你能够独立排查和修复 Grafana Dashboard 问题

---

## 1. 系统架构概览

### 服务组成
```
┌─────────────────────────────────────────────────────────┐
│                   Docker Services                        │
├─────────────────────────────────────────────────────────┤
│  Grafana (3000)  ←─ 查询 ─→  Loki (3100)               │
│                  ←─ 查询 ─→  ClickHouse (8123)          │
│                  ←─ 查询 ─→  Prometheus (9090)          │
│                  ←─ 查询 ─→  Tempo (3200)               │
├─────────────────────────────────────────────────────────┤
│  Promtail        ─ 读取 ─→  /workspace/grafana-stack/logs│
└─────────────────────────────────────────────────────────┘
```

### 数据流
1. **交易系统** → 写日志到 `/workspace/grafana-stack/logs/trader.log`
2. **Promtail** → 读取日志 → 发送到 **Loki**
3. **Grafana** → 查询 Loki/ClickHouse/Prometheus/Tempo → 显示数据

---

## 2. 关键路径和文件位置

### 项目根目录
```
./
```

### Docker 配置
```bash
# Docker Compose 文件
./docker-compose.yml

# 查看所有服务状态
cd .
docker-compose ps

# 重启所有服务
docker-compose restart

# 查看服务日志
docker logs trading-grafana-1
docker logs trading-loki-1
docker logs trading-promtail-1
```

### Grafana 配置
```bash
# Dashboard JSON 文件目录
./grafana/dashboards/*.json

# Datasource 配置
./grafana/provisioning/datasources/ds.yml

# Dashboard Provider 配置
./grafana/provisioning/dashboards/provider.yml
```

### 日志文件
```bash
# 交易系统日志 (Promtail 读取这个)
/workspace/grafana-stack/logs/trader.log

# 查看最近日志
tail -100 /workspace/grafana-stack/logs/trader.log

# 查看错误日志
grep -i error /workspace/grafana-stack/logs/trader.log | tail -50
```

### ClickHouse 数据
```bash
# ClickHouse HTTP 端口
http://localhost:8123

# 测试连接 (无需密码)
curl -s "http://localhost:8123/" --data "SELECT 1"

# 列出所有表
curl -s "http://localhost:8123/" --data "SHOW TABLES FROM trading"

# 查询数据
curl -s "http://localhost:8123/" --data "SELECT * FROM trading.order_events LIMIT 10"
```

---

## 3. 常见错误类型和解决方法

### 错误 1: "Cannot read properties of undefined (reading 'split')"

**原因**: Template 变量查询返回空结果或格式错误

**排查步骤**:
```bash
# 1. 确认数据源是 ClickHouse 还是 Loki
# 在 dashboard JSON 中找到出错的 template variable

# 2. 如果是 ClickHouse 查询，测试数据是否存在
curl -s "http://localhost:8123/" --data "SELECT COUNT(*) FROM trading.runs"

# 3. 如果表为空或不存在，需要运行交易系统生成数据
cd .
python3 run_system.py --mode paper --exec
```

**修复方法** (在 dashboard JSON 中):
```json
{
  "type": "query",
  "name": "run_id",
  "query": "SELECT DISTINCT run_id FROM trading.runs ORDER BY run_id DESC",
  "includeAll": true,
  "allValue": ".*",              // ← 添加这个
  "current": {
    "text": "All",
    "value": ["$__all"],         // ← 改成数组格式
    "selected": true
  },
  "options": []                  // ← 添加这个
}
```

---

### 错误 2: "syntax error: unexpected IDENTIFIER"

**原因**: Loki 查询语法错误（通常是用了反引号 `` ` ``）

**错误示例**:
```json
"expr": "{job=\"trader\"} |~ `order_fill` | json"  // ❌ 错误：反引号
```

**正确写法**:
```json
"expr": "{job=\"trader\"} |~ \"order_fill\" | json"  // ✅ 正确：转义双引号
```

**查找所有反引号**:
```bash
cd ./grafana/dashboards
grep -n '`' *.json | grep -v 'line_format'  # line_format 中的反引号是正常的
```

**批量修复**:
```bash
# 用 sed 替换 (macOS 需要加 '')
sed -i '' 's/|~ `/|~ "/g' filename.json
sed -i '' 's/|= `/|= "/g' filename.json
sed -i '' 's/` |/" |/g' filename.json
```

---

### 错误 3: Dashboard 重启后消失

**原因**: Grafana 数据没有持久化

**检查持久化**:
```bash
# 检查 docker-compose.yml 是否有 grafana-data volume
cat ./docker-compose.yml | grep -A 3 "grafana:"

# 应该看到:
#   volumes:
#     - grafana-data:/var/lib/grafana
```

**修复 docker-compose.yml**:
```yaml
services:
  grafana:
    volumes:
      - ./grafana/provisioning:/etc/grafana/provisioning
      - ./grafana/dashboards:/var/lib/grafana/dashboards
      - grafana-data:/var/lib/grafana  # ← 添加这行

# 文件末尾添加
volumes:
  grafana-data:  # ← 添加这个
```

**应用更改**:
```bash
docker-compose down
docker-compose up -d
```

---

### 错误 4: Panel 显示 "No Data"

**原因**: 数据源没有数据或查询错误

**排查步骤**:

#### A. Loki Panel 无数据
```bash
# 1. 检查 Loki 是否运行
curl -s http://localhost:3100/ready

# 2. 检查 Loki 有哪些 labels
curl -s 'http://localhost:3100/loki/api/v1/labels' | jq '.data'

# 3. 检查具体 label 的值
curl -s 'http://localhost:3100/loki/api/v1/label/symbol/values' | jq '.data'

# 4. 检查 Promtail 是否在采集日志
docker logs trading-promtail-1 2>&1 | tail -20

# 5. 检查 Promtail 配置的日志路径
cat ./promtail/config.yml | grep __path__
```

#### B. ClickHouse Panel 无数据
```bash
# 1. 测试 ClickHouse 连接
curl -s "http://localhost:8123/" --data "SELECT 1"

# 2. 检查表是否存在
curl -s "http://localhost:8123/" --data "SHOW TABLES FROM trading"

# 3. 检查表是否有数据
curl -s "http://localhost:8123/" --data "SELECT COUNT(*) FROM trading.order_events"

# 4. 查看最近的数据
curl -s "http://localhost:8123/" --data "SELECT * FROM trading.order_events ORDER BY ts DESC LIMIT 5" | jq '.'
```

#### C. Prometheus Panel 无数据
```bash
# 1. 检查 Prometheus 是否运行
curl -s http://localhost:9090/-/healthy

# 2. 检查是否有 metrics
curl -s 'http://localhost:9090/api/v1/targets' | jq '.data.activeTargets'

# 3. 交易系统是否暴露 metrics (默认 9108 端口)
curl -s http://localhost:9108/metrics | head -20
```

---

### 错误 5: ClickHouse query 缺少 format 字段

**症状**: Panel 显示错误 "format is required"

**修复方法** (在 panel targets 中):
```json
{
  "refId": "A",
  "query": "SELECT ts, value FROM trading.table",
  "format": "time_series",     // ← 添加这个 (timeseries panel)
  "queryType": "sql",           // ← 添加这个
  "datasource": {
    "type": "grafana-clickhouse-datasource",
    "uid": "ch"
  }
}
```

**Format 类型**:
- `"time_series"` - 用于 timeseries, graph panel
- `"table"` - 用于 table, stat, gauge, bargauge panel

---

## 4. 数据源配置详解

### Loki
```yaml
# 文件: grafana/provisioning/datasources/ds.yml
- name: Loki
  uid: loki
  type: loki
  url: http://loki:3100        # ← Docker 内部地址
  isDefault: true
```

**测试 Loki**:
```bash
# 从浏览器访问 (Docker 外部)
open http://localhost:3100/ready

# 从 Grafana 容器访问 (Docker 内部)
docker exec trading-grafana-1 curl -s http://loki:3100/ready
```

### ClickHouse
```yaml
- name: ClickHouse
  uid: ch
  type: grafana-clickhouse-datasource
  jsonData:
    defaultDatabase: trading
    port: 8123
    protocol: http
    host: host.docker.internal  # ← 允许访问宿主机
    username: default
  secureJsonData:
    password: ""                # ← 密码为空
```

**重要**: ClickHouse 在宿主机运行，不在 Docker 中，所以用 `host.docker.internal`

### Prometheus
```yaml
- name: Prometheus
  uid: prometheus
  type: prometheus
  url: http://host.docker.internal:9090  # ← 宿主机地址
```

---

## 5. Dashboard JSON 文件结构

### 基本结构
```json
{
  "uid": "dashboard-uid",           // ← 唯一标识符
  "title": "Dashboard Title",
  "templating": {                   // ← 变量定义
    "list": [...]
  },
  "panels": [                       // ← 所有 panels
    {
      "id": 1,
      "type": "timeseries",
      "datasource": {...},
      "targets": [...]
    }
  ]
}
```

### Template 变量
```json
{
  "type": "query",                  // 类型: query, datasource, textbox, interval
  "name": "symbol",                 // 变量名 (用 $symbol 引用)
  "label": "Symbol",                // UI 显示名称
  "datasource": {
    "type": "loki",
    "uid": "loki"
  },
  "query": "label_values({job=\"trader\"}, symbol)",  // ← 查询语句
  "refresh": 2,                     // 刷新频率 (1=加载时, 2=时间范围变化时)
  "multi": true,                    // 允许多选
  "includeAll": true,               // 显示 "All" 选项
  "allValue": ".*"                  // "All" 对应的正则表达式
}
```

### Panel Target (Loki)
```json
{
  "refId": "A",
  "expr": "{job=\"trader\"} |= \"order_fill\" | json",  // ← LogQL 查询
  "legendFormat": "{{symbol}}"      // 图例格式
}
```

### Panel Target (ClickHouse)
```json
{
  "refId": "A",
  "query": "SELECT ts, value FROM trading.table",      // ← SQL 查询
  "format": "time_series",          // ← 必须: time_series 或 table
  "queryType": "sql",               // ← 必须
  "datasource": {
    "type": "grafana-clickhouse-datasource",
    "uid": "ch"
  }
}
```

---

## 6. 常用操作命令

### 重启服务
```bash
cd .

# 重启单个服务
docker-compose restart grafana
docker-compose restart loki
docker-compose restart promtail

# 重启所有服务
docker-compose restart

# 完全重建 (清除网络问题)
docker-compose down
docker-compose up -d
```

### 查看日志
```bash
# 实时跟踪日志
docker logs -f trading-grafana-1
docker logs -f trading-loki-1
docker logs -f trading-promtail-1

# 查看最近错误
docker logs trading-loki-1 2>&1 | grep -i error | tail -20
```

### 验证 JSON 文件
```bash
cd ./grafana/dashboards

# 验证单个文件
jq '.' trading_observability.json

# 验证所有文件
for f in *.json; do 
  echo "Checking $f..."
  jq '.' "$f" > /dev/null || echo "ERROR in $f"
done
```

### 上传 Dashboard 到 Grafana
```bash
cd ./grafana/dashboards

python3 << 'PYEOF'
import json
import subprocess
import glob

for filepath in glob.glob('*.json'):
    with open(filepath) as f:
        dashboard = json.load(f)
    
    upload_data = json.dumps({'dashboard': dashboard, 'overwrite': True})
    
    result = subprocess.run([
        'curl', '-s', '-X', 'POST',
        'http://admin:admin@localhost:3000/api/dashboards/db',
        '-H', 'Content-Type: application/json',
        '-d', upload_data
    ], capture_output=True, text=True)
    
    response = json.loads(result.stdout)
    if response.get('status') == 'success':
        print(f"✅ {filepath}")
    else:
        print(f"❌ {filepath}: {response}")
PYEOF
```

### 备份和恢复 Dashboards
```bash
# 备份 (导出所有 dashboards)
mkdir -p ~/grafana_backup
cd ~/grafana_backup

for uid in $(curl -s -u admin:admin http://localhost:3000/api/search | jq -r '.[].uid'); do
  curl -s -u admin:admin "http://localhost:3000/api/dashboards/uid/$uid" | \
    jq '.dashboard' > "${uid}.json"
  echo "✅ Exported: ${uid}.json"
done

# 恢复 (上传所有 dashboards)
cd ./grafana/dashboards
# 使用上面的 Python 脚本
```

---

## 7. Loki 查询语法 (LogQL)

### 基本语法
```logql
{job="trader"}                              # 基础 label 过滤
{job="trader"} |= "order_fill"              # 包含文本 (精确)
{job="trader"} |~ "order_fill|order_reject" # 正则匹配
{job="trader"} | json                       # 解析 JSON
{job="trader"} | json | symbol="AAPL"       # JSON 字段过滤
```

### 聚合函数
```logql
count_over_time({job="trader"} [5m])                    # 计数
rate({job="trader"} [5m])                               # 速率
sum by (symbol) (count_over_time(...))                  # 分组求和
topk(10, sum by (reason) (count_over_time(...)))        # Top N
```

### Unwrap (提取数值)
```logql
{job="trader"} | json | unwrap pnl_today | sum_over_time([1h])
```

---

## 8. ClickHouse 查询语法

### 时间过滤
```sql
-- Grafana 提供的宏
WHERE $__timeFilter(ts)                    -- 自动时间范围过滤

-- 手动写法
WHERE ts >= now() - INTERVAL 1 HOUR
```

### 时间分组
```sql
-- 按间隔分组 (使用 Grafana 变量)
SELECT 
  toStartOfInterval(ts, INTERVAL $__interval SECOND) AS t,
  sum(value) AS total
FROM trading.table
GROUP BY t
ORDER BY t
```

### 常用查询
```sql
-- 最新值
SELECT anyLast(value) FROM trading.table

-- 计数
SELECT count() FROM trading.table WHERE condition

-- 条件计数
SELECT 
  countIf(event_type='FILLED') AS fills,
  countIf(event_type='REJECTED') AS rejects
FROM trading.exec_events
```

---

## 9. 故障排查流程图

```
Dashboard 显示错误
    │
    ├─→ "Cannot read properties of undefined (reading 'split')"
    │   ├─→ 检查: Template 变量查询是否返回空结果
    │   ├─→ 测试: curl ClickHouse 查询
    │   └─→ 修复: 添加 allValue, options 字段
    │
    ├─→ "syntax error: unexpected IDENTIFIER"
    │   ├─→ 检查: Loki 查询是否用了反引号 `
    │   └─→ 修复: 替换为转义双引号 \"
    │
    ├─→ "No Data"
    │   ├─→ Loki Panel
    │   │   ├─→ 检查: Promtail 是否运行
    │   │   ├─→ 检查: 日志文件路径是否正确
    │   │   └─→ 检查: Loki 是否有数据
    │   │
    │   ├─→ ClickHouse Panel
    │   │   ├─→ 检查: ClickHouse 是否运行
    │   │   ├─→ 检查: 表是否存在
    │   │   ├─→ 检查: 表是否有数据
    │   │   └─→ 检查: query 是否有 format 字段
    │   │
    │   └─→ Prometheus Panel
    │       ├─→ 检查: Prometheus 是否运行
    │       └─→ 检查: 交易系统是否暴露 metrics (9108)
    │
    └─→ "format is required"
        └─→ 修复: 添加 format 和 queryType 字段
```

---

## 10. 快速检查清单

启动系统前检查:
```bash
# ✅ 1. Docker 服务是否都在运行?
docker-compose ps

# ✅ 2. Loki 是否 ready?
curl -s http://localhost:3100/ready

# ✅ 3. ClickHouse 是否响应?
curl -s "http://localhost:8123/" --data "SELECT 1"

# ✅ 4. Grafana 是否可访问?
curl -s http://localhost:3000/api/health

# ✅ 5. 日志文件是否存在?
ls -lh /workspace/grafana-stack/logs/trader.log

# ✅ 6. Promtail 是否在读取日志?
docker logs trading-promtail-1 2>&1 | grep "tail routine: started"

# ✅ 7. Dashboard JSON 是否有效?
cd ./grafana/dashboards
for f in *.json; do jq '.' "$f" > /dev/null && echo "✅ $f" || echo "❌ $f"; done
```

---

## 11. 常见问题 FAQ

### Q: Dashboard 为什么每次 Docker 重启后都消失?
**A**: 没有配置持久化。确保 `docker-compose.yml` 中有:
```yaml
grafana:
  volumes:
    - grafana-data:/var/lib/grafana

volumes:
  grafana-data:
```

### Q: Loki panel 显示 "No Data" 但日志文件有数据?
**A**: 检查 Promtail 的日志路径配置:
```yaml
# promtail/config.yml
__path__: /var/log/trader/*.log  # 容器内路径

# docker-compose.yml
promtail:
  volumes:
    - /workspace/grafana-stack/logs:/var/log/trader:ro  # 宿主机:容器
```

### Q: ClickHouse 查询报错 "authentication failed"?
**A**: ClickHouse 默认无密码。去掉 `-u default:clickhouse`，直接用:
```bash
curl -s "http://localhost:8123/" --data "SELECT 1"
```

### Q: 如何知道 Loki 里有哪些 log events?
**A**: 查询所有 event 值:
```bash
curl -s 'http://localhost:3100/loki/api/v1/label/event/values' | jq '.data'
```

### Q: Template 变量下拉框为空?
**A**: 
1. 检查数据源是否有数据
2. 检查查询语法是否正确
3. 对于空结果，添加 `allValue` 和 `options: []`

---

## 12. 有用的资源

### Grafana API
```bash
# 列出所有 dashboards
curl -s -u admin:admin http://localhost:3000/api/search | jq '.'

# 获取 dashboard 详情
curl -s -u admin:admin http://localhost:3000/api/dashboards/uid/DASHBOARD_UID | jq '.'

# 列出所有 datasources
curl -s -u admin:admin http://localhost:3000/api/datasources | jq '.'
```

### Loki API
```bash
# 健康检查
curl -s http://localhost:3100/ready

# 列出所有 labels
curl -s 'http://localhost:3100/loki/api/v1/labels' | jq '.data'

# 查询 label 值
curl -s 'http://localhost:3100/loki/api/v1/label/LABEL_NAME/values' | jq '.data'

# 查询日志
curl -s 'http://localhost:3100/loki/api/v1/query' \
  --data-urlencode 'query={job="trader"}' | jq '.'
```

### ClickHouse
```bash
# 健康检查
curl -s "http://localhost:8123/ping"

# 列出数据库
curl -s "http://localhost:8123/" --data "SHOW DATABASES"

# 查看表结构
curl -s "http://localhost:8123/" --data "DESCRIBE TABLE trading.order_events"
```

---

## 13. 最终核心要点

### 修复 Dashboard 的标准流程:

1. **定位错误**
   - 在浏览器打开 Dashboard
   - 查看具体报错信息
   - 记录出错的 Panel 或 Template 变量名称

2. **检查数据源**
   - Loki: `curl http://localhost:3100/ready`
   - ClickHouse: `curl "http://localhost:8123/" --data "SELECT 1"`
   - 确认数据源有数据

3. **检查查询**
   - 找到出错 Panel 的查询语句 (在 JSON 文件中)
   - 手动测试查询 (用 curl 或浏览器)
   - 确认查询语法正确

4. **修改 JSON**
   - 用编辑器打开 `./grafana/dashboards/FILENAME.json`
   - 修复错误 (语法、字段缺失等)
   - 验证 JSON: `jq '.' FILENAME.json`

5. **上传测试**
   - 用 Python 脚本上传到 Grafana
   - 刷新浏览器验证修复

## 14. Unified Observability Dashboards (2025-10-20)

最新的 4 套主面板已经整合到 `grafana/dashboards/active/`，Grafana 通过 provisioning 自动加载：

| Dashboard | 文件 | 目的 | 关键数据源 |
| --- | --- | --- | --- |
| Infrastructure & Platform Health | `infrastructure_platform_health.json` | 核心服务健康、错误热区、Backfill 监控 | Prometheus (`service_up`), Loki, ClickHouse (`backfill_status`) |
| Trading System Operations | `trading_system_operations.json` | run_system 节奏、Throughput、Why Not 分析、Tempo Trace | Loki, Tempo, ClickHouse (`live_metrics`) |
| Strategy & Decision Pipeline Insight | `strategy_pipeline_insight.json` | 策略漏斗、Gate 诊断、PnL 排行 | Loki, ClickHouse (`pnl_day`) |
| Financial & Market Overview | `financial_market_overview.json` | 资金、仓位、成交、行情快照 | ClickHouse (`portfolio_snapshots`, `positions_snapshots`, `bars_*`) |

恢复/重建步骤：

1. 确认 `docker-compose.yml` 中依旧挂载 `./grafana/dashboards:/var/lib/grafana/dashboards` 与 `./grafana/provisioning:/etc/grafana/provisioning`。
2. 容器重启或重建后执行 `docker-compose restart grafana`，Grafana 会从 `/var/lib/grafana/dashboards/active` 自动加载上述 4 个面板。
3. 历史旧面板已迁移到 `grafana/archive_dashboards/`，需要时手动导入或 diff。
4. Alert 规则存放在 `grafana/provisioning/alerting/rules.yaml`，确保映射到容器后就绪。
5. Prometheus 现已抓取新增 exporter：`node_exporter`, `cadvisor`, `kafka_exporter`, `redis_exporter`, `postgres_exporter`（见主目录 `docker-compose.yml` + `infra/prometheus.yml`）。`docker-compose up -d prometheus` 即可启动监控端。
6. Grafana 联系点与策略在 `grafana/provisioning/alerting/contact-points.yaml`、`notification-policies.yaml` 中声明式管理；设置 `SLACK_WEBHOOK_URL` / `ALERT_EMAIL_RECIPIENTS` 后重启 Grafana 生效。

> 提示：Prometheus 位于 `infra/compose.yml`，如需手动启动请执行 `cd infra && docker-compose up -d prometheus`。

### 记住这些关键路径:
```
配置:      ./docker-compose.yml
Dashboard: ./grafana/dashboards/active/*.json
Archive:   ./grafana/archive_dashboards/
日志:      /workspace/grafana-stack/logs/trader.log
Grafana:   http://localhost:3000 (admin/admin)
Loki:      http://localhost:3100
ClickHouse:http://localhost:8123
Prometheus: http://localhost:9090
node_exporter: http://localhost:9100/metrics
cadvisor: http://localhost:8080/metrics
kafka_exporter: http://localhost:9308/metrics
redis_exporter: http://localhost:9121/metrics
postgres_exporter: http://localhost:9187/metrics
```

---

**完成时间**: 2025-10-18  
**作者**: Claude Code  
**版本**: 1.0
