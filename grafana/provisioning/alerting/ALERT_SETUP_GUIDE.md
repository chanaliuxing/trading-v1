# Grafana Alerts Setup Guide

**Created**: 2025-11-01 18:00 EST
**Status**: ✅ Alert Rules Created | ⚠️ Contact Points Need Configuration

---

## 📋 **已创建的告警规则**

### P0 Critical Alerts (立即告警)

| Alert ID | Name | Threshold | For | Severity |
|----------|------|-----------|-----|----------|
| `data-freshness-critical` | Data Staleness > 1 Hour | >60 min | 5m | 🔴 critical |
| `system-status-critical` | Trading System Critical | status<1 | 10m | 🔴 critical |

### P1 Warning Alerts (性能/健康告警)

| Alert ID | Name | Threshold | For | Severity |
|----------|------|-----------|-----|----------|
| `zombie-sessions-high` | High Zombie Sessions | >10 sessions | 15m | 🟠 warning |
| `brain-latency-high` | AI Brain P95 Latency > 60s | >60s | 30m | 🟡 warning |
| `slow-calls-spike` | Slow AI Brain Calls Spike | >20 calls/hour | 15m | 🟡 warning |
| `no-fills-market-hours` | No Fills During Market Hours | 0 fills/2h | 2h | 🟠 warning |

---

## 🚀 **快速启用步骤**

### Step 1: 重启 Grafana 加载告警规则

```bash
cd ./infra
docker-compose restart grafana

# 等待 10 秒
sleep 10

# 验证 Grafana 启动
curl -s http://localhost:3000/api/health | jq .
```

### Step 2: 验证告警规则已加载

在浏览器打开:
```
http://localhost:3000/alerting/list
```

**预期结果**:
- 看到 "trading-clickhouse-alerts" 规则组
- 包含 6 个告警规则
- 状态显示为 "Normal" 或 "Pending"

### Step 3: 配置通知渠道 (Contact Points)

**当前状态**: ⚠️ 仅配置了示例邮件 (ops@example.com)

**推荐配置顺序**:

#### 3.1 配置 Slack (推荐 - 最快见效)

**方法 1: 通过 Grafana UI 配置** (推荐)

1. 打开 Grafana: http://localhost:3000
2. 登录 (admin/admin)
3. 导航到: **Alerting** → **Contact points** → **New contact point**
4. 配置:
   ```
   Name: slack-trading-alerts
   Integration: Slack
   Webhook URL: https://hooks.slack.com/services/YOUR/SLACK/WEBHOOK
   ```
5. 点击 **Test** 验证
6. 点击 **Save contact point**

**获取 Slack Webhook URL**:
1. 访问: https://api.slack.com/apps
2. 创建新 App 或选择现有 App
3. 添加 "Incoming Webhooks" feature
4. 选择要通知的频道 (如 #trading-alerts)
5. 复制 Webhook URL

**方法 2: 通过配置文件** (如果你已有 Webhook URL)

编辑 `grafana/provisioning/alerting/contact-points.yaml`:

```yaml
apiVersion: 1
contactPoints:
  - orgId: 1
    name: slack-trading-alerts
    receivers:
      - uid: slack-trading
        type: slack
        settings:
          url: https://hooks.slack.com/services/YOUR/SLACK/WEBHOOK
          username: Grafana Alerts
          title: "{{ .GroupLabels.alertname }}"
          text: "{{ .Annotations.summary }}\n\n{{ .Annotations.description }}"
        disableResolveMessage: false

  - orgId: 1
    name: slack-engineering
    receivers:
      - uid: slack-eng
        type: slack
        settings:
          url: https://hooks.slack.com/services/YOUR/ENG/WEBHOOK
          username: Grafana Alerts
```

#### 3.2 配置邮件 (备用)

如果需要邮件告警，需要在 compose.yml 中配置 SMTP:

```yaml
# infra/compose.yml - grafana service
environment:
  # ... existing vars ...
  GF_SMTP_ENABLED: "true"
  GF_SMTP_HOST: smtp.gmail.com:587
  GF_SMTP_USER: your-email@gmail.com
  GF_SMTP_PASSWORD: your-app-password
  GF_SMTP_FROM_ADDRESS: alerts@trading.com
  GF_SMTP_FROM_NAME: Trading Alerts
```

#### 3.3 配置 PagerDuty (生产环境推荐)

**仅对 P0 Critical 告警使用**:

1. 获取 PagerDuty Integration Key
2. 在 Grafana UI 添加 PagerDuty contact point
3. 配置 Notification Policy 路由 severity=critical → PagerDuty

### Step 4: 配置通知策略 (Notification Policies)

编辑 `grafana/provisioning/alerting/notification-policies.yaml`:

```yaml
apiVersion: 1
policies:
  - orgId: 1
    receiver: slack-trading-alerts  # 默认接收者
    group_by:
      - alertname
      - severity
    group_wait: 30s
    group_interval: 5m
    repeat_interval: 4h
    routes:
      # P0 Critical → Slack + PagerDuty (如果已配置)
      - receiver: slack-trading-alerts
        matchers:
          - severity=critical
        continue: true  # 继续到下一个路由
        group_wait: 10s
        repeat_interval: 1h

      # Engineering Alerts → Slack #engineering
      - receiver: slack-engineering
        matchers:
          - team=engineering
        group_wait: 1m
        repeat_interval: 6h

      # Trading Alerts → Slack #trading-alerts
      - receiver: slack-trading-alerts
        matchers:
          - team=trading
        group_wait: 30s
        repeat_interval: 4h
```

### Step 5: 测试告警

#### 测试 1: 触发 Data Staleness Alert (模拟)

**方法**: 临时修改告警阈值为 1 分钟，等待触发

```bash
# 检查当前数据年龄
docker exec -i trading_clickhouse clickhouse-client --query "
SELECT round((now() - max(ts_ny)) / 60, 0) as minutes_ago
FROM llm.ibkr_account_realtime
"
```

如果结果 > 60，告警应在 5 分钟内触发。

#### 测试 2: 手动发送测试通知

在 Grafana UI:
1. 打开 **Alerting** → **Contact points**
2. 找到你的 contact point
3. 点击 **Test** 按钮
4. 检查 Slack/Email 是否收到测试消息

#### 测试 3: 查看告警历史

```
http://localhost:3000/alerting/history
```

---

## 📊 **告警规则详解**

### Alert #1: Data Staleness (数据过期)

**查询**:
```sql
SELECT round((now() - max(ts_ny)) / 60, 0) as minutes_ago
FROM llm.ibkr_account_realtime
```

**触发条件**: `minutes_ago > 60`

**For 时间**: 5 分钟 (连续超过阈值 5 分钟后才告警)

**意义**: 防止用户基于过期数据交易

**Runbook**: 检查数据导入服务 → 验证 IBKR 连接 → 重启服务

---

### Alert #2: System Status Critical

**查询**: 综合检查数据年龄 + 今日成交

**触发条件**:
- 数据 > 6 小时 old OR
- 市场时间内 0 fills

**For 时间**: 10 分钟

**意义**: 整体系统健康度检查

**Runbook**: 检查 Dashboard 1 → 验证策略执行 → 查看错误日志

---

### Alert #3: Zombie Sessions

**查询**:
```sql
SELECT count(*) FROM llm.llm_sessions
WHERE status = 'RUNNING' AND (now() - start_ts_ny) > 86400
```

**触发条件**: `zombie_count > 10`

**For 时间**: 15 分钟

**意义**: 防止资源泄漏

**Runbook**: 运行 `cleanup_zombie_sessions.py` → 调查会话未关闭原因

---

### Alert #4: AI Brain P95 Latency

**查询**: 最近 1 小时 P95 延迟

**触发条件**: `p95_latency > 60s`

**For 时间**: 30 分钟 (避免短暂尖峰)

**意义**: 性能下降检测

**Runbook**: 检查 LLM provider 状态 → 优化 prompt → 添加 fallback

---

### Alert #5: Slow Calls Spike

**查询**: 最近 1 小时内 >60s 的调用数量

**触发条件**: `slow_count > 20`

**For 时间**: 15 分钟

**意义**: 检测异常性能尖峰

**Runbook**: 检查网络 → 检查 provider 状态 → 临时降级服务

---

### Alert #6: No Fills During Market Hours

**查询**: 最近 2 小时成交数

**触发条件**: `fill_count = 0` (仅市场时间关心)

**For 时间**: 2 小时

**意义**: 检测交易执行停止

**Runbook**: 检查策略决策日志 → 验证订单提交 → 检查 IBKR 连接

---

## 🔧 **故障排查**

### 问题 1: 告警规则未出现

**检查**:
```bash
# 查看 Grafana 日志
docker logs trading_grafana | grep -i alert

# 验证文件挂载
docker exec trading_grafana ls -la /etc/grafana/provisioning/alerting/
```

**解决**:
```bash
# 重启 Grafana
docker-compose restart grafana
```

---

### 问题 2: 告警状态一直 "Pending"

**原因**: ClickHouse 数据源连接问题

**检查**:
```bash
# 测试 ClickHouse 连接
curl "http://localhost:8123/ping"

# 在 Grafana UI 测试查询
# Explore → ClickHouse datasource → 运行告警查询
```

---

### 问题 3: 告警触发但未收到通知

**检查**:
1. Grafana UI → **Alerting** → **Contact points** → Test
2. 查看 Notification History: http://localhost:3000/alerting/notifications
3. 检查 Grafana 日志: `docker logs trading_grafana | grep -i notif`

**常见原因**:
- Slack Webhook URL 错误
- SMTP 配置错误
- Contact Point 未绑定到 Notification Policy

---

## 📈 **监控告警系统本身**

### 查看告警状态

```bash
# 通过 API 查看所有告警状态
curl -s http://admin:admin@localhost:3000/api/v1/provisioning/alert-rules | jq .

# 查看告警历史
curl -s http://admin:admin@localhost:3000/api/v1/provisioning/alert-rules | jq '.[] | {title, state}'
```

### Grafana 自带指标

Prometheus 自动抓取 Grafana 指标:
```
http://localhost:3000/metrics
```

**关键指标**:
- `grafana_alerting_rule_evaluations_total` - 告警评估次数
- `grafana_alerting_notifications_sent_total` - 通知发送次数
- `grafana_alerting_notifications_failed_total` - 通知失败次数

---

## 🎯 **下一步优化**

### Phase 1 (立即) - 基础告警生效
- [x] 创建告警规则文件 ✅
- [ ] 配置 Slack Webhook
- [ ] 重启 Grafana
- [ ] 测试告警触发
- [ ] 验证通知送达

### Phase 2 (1 周内) - 完善通知渠道
- [ ] 添加 PagerDuty 集成 (P0 告警)
- [ ] 配置 Email 备用渠道
- [ ] 设置告警静默策略 (非工作时间)
- [ ] 配置告警升级策略

### Phase 3 (2 周内) - 告警调优
- [ ] 根据实际运行调整阈值
- [ ] 添加更多业务指标告警
- [ ] 实现告警聚合 (避免告警风暴)
- [ ] 建立告警响应 Runbook

---

## 📚 **参考文档**

**Grafana Alerting 官方文档**:
- Alerting: https://grafana.com/docs/grafana/latest/alerting/
- Provisioning: https://grafana.com/docs/grafana/latest/administration/provisioning/

**告警最佳实践**:
1. **设置合理的 For 时间** - 避免短暂尖峰触发告警
2. **避免告警疲劳** - 关键告警用 critical，非紧急用 warning
3. **提供可操作的描述** - 告警消息包含 runbook 链接
4. **测试告警路由** - 确保正确的人收到正确的告警
5. **定期回顾告警** - 删除无用告警，优化阈值

---

**Status**: ✅ Alert Rules Created
**Next Action**: Configure Slack Webhook → Restart Grafana → Test Alerts
**Time to Production**: ~30 minutes (if Slack webhook ready)
