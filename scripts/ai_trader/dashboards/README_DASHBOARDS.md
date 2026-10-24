# AI Trader 三大屏监控系统 - 完整指南

## 📊 系统概览

AI Trader 提供三个专业大屏，从不同维度监控交易系统：

| 大屏 | 用途 | 访问方式 | 核心功能 |
|------|------|----------|----------|
| **大屏一：MLflow** | 策略学习与进化 | http://localhost:5001 | HPO 参数进化、RL 性能趋势、A/B 测试对比 |
| **大屏二：Grafana** | 系统实时状态 | http://localhost:3000 | 实时 P&L、LLM 决策、进程心跳、API 错误 |
| **大屏三：Daily Report** | 每日战报 | `view_daily_report.sh` | 今日诊断、处方、HPO/RL 结果、守护者活动 |

---

## 🚀 一键启动

### 最简单方式：统一启动脚本

```bash
cd .
bash scripts/ai_trader/dashboards/start_all_dashboards.sh
```

**这个脚本会自动：**
1. ✅ 检查 Docker 基础设施（ClickHouse、Prometheus、Grafana）
2. ✅ 启动 MLflow UI (端口 5000)
3. ✅ 配置 Grafana 数据源和仪表盘
4. ✅ 提供每日战报查看选项

---

## 📊 大屏一：MLflow - 策略学习与进化仪表盘

### 功能说明

MLflow 是策略参数进化的"历史博物馆"，记录每次学习周期的成果。

**核心指标**:
- **OOS 夏普率** (`hpo_oos_sharpe`): 样本外表现，HPO 优化目标
- **OOS 最大回撤** (`hpo_oos_mdd`): 样本外风险控制
- **RL 夏普率** (`rl_sharpe`): 强化学习模型表现
- **Champion vs Challenger**: A/B 测试对比

**关键场景**:
1. **追踪参数进化**: 查看每次 HPO 找到的最优参数变化
2. **性能趋势分析**: 比较不同日期的策略表现
3. **实验对比**: 筛选 OOS Sharpe > 1.1 的优秀实验
4. **决策文件归档**: 下载 `hpo_decision.json` / `daily_report.md`

### 启动方式

#### 方法一：独立启动
```bash
bash scripts/ai_trader/dashboards/start_mlflow.sh
```

#### 方法二：手动启动
```bash
cd .
mlflow ui --backend-store-uri file://./mlruns --host 0.0.0.0 --port 5000 &
```

### 使用指南

**步骤 1：打开浏览器**
```
http://localhost:5000
```

**步骤 2：查看实验**
- 点击 **"AI Trader Evolution"** 实验
- 看到所有 `evening_learning` 运行记录

**步骤 3：筛选优秀实验**
- 在搜索框输入: `metrics.hpo_oos_sharpe > 1.1`
- 按 OOS Sharpe 降序排列

**步骤 4：对比参数**
- 选中多个 run → 点击 **Compare**
- 查看 Parameters 标签页，对比参数差异

**步骤 5：下载决策文件**
- 点击某个 run → Artifacts 标签页
- 下载 `hpo_decision.json` / `daily_report.md`

### 数据目录

```
./mlruns/
├── 0/                          # 默认实验
├── 1/                          # AI Trader Evolution 实验
│   ├── run_id_1/
│   │   ├── meta.yaml           # run 元数据
│   │   ├── metrics/            # 指标时间序列
│   │   ├── params/             # 参数
│   │   └── artifacts/          # 决策文件
│   ├── run_id_2/
│   └── ...
└── models/                     # 注册的模型
```

### 常见问题

**Q: 为什么没有数据？**
- A: 需要先运行 `evening_learning` 生成数据
  ```bash
  PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/cli.py run --mode learn
  ```

**Q: 如何删除旧数据？**
- A: 直接删除对应的 run 目录
  ```bash
  rm -rf mlruns/1/run_id_xxx
  ```

**Q: 如何导出所有实验数据？**
- A: 使用 MLflow CLI
  ```bash
  mlflow experiments csv -x 1 -o experiments.csv
  ```

---

## 📈 大屏二：Grafana - 系统实时状态监控

### 功能说明

Grafana 是系统"生命体征监控仪"，实时展示交易系统健康状况。

**核心面板**:
1. **AI Brain Status**: 今日会话数量
2. **LLM Sessions Today**: 会话列表（session ID、provider、symbols、状态）
3. **Today's P&L by Symbol**: 各 symbol 累计盈亏曲线
4. **LLM Decision Distribution**: BUY/SELL/HOLD 分布饼图
5. **Process Heartbeats**: 进程心跳延迟（< 180s 正常）
6. **LLM API Errors**: API 错误率趋势
7. **Brain Decisions Log**: 决策日志流
8. **Strategy Performance**: 各 symbol 决策统计和信心度

**数据源**:
- **ClickHouse**: `llm.llm_sessions`, `llm.llm_decisions`, `llm.llm_fills`
- **Prometheus**: 进程心跳、API 错误计数器
- **Loki**: 日志流（可选）

### 启动方式

Grafana 随基础设施启动：
```bash
cd ./infra
docker-compose up -d grafana
```

### 使用指南

**步骤 1：打开浏览器**
```
http://localhost:3000
```

**默认登录**:
- 用户名: `admin`
- 密码: `admin`
- 首次登录会提示修改密码

**步骤 2：打开 AI Trader 仪表盘**
- 左侧菜单 → **Dashboards**
- 搜索: `AI Trader Brain`
- 点击打开

**步骤 3：调整时间范围**
- 右上角时间选择器 → 选择 `Last 6 hours` 或 `Today`

**步骤 4：设置自动刷新**
- 右上角刷新按钮 → 选择 `10s` 或 `30s`

**步骤 5：钉到侧边栏**
- 仪表盘右上角 ⭐ 图标 → **Add to favorites**

### 配置数据源

如果 ClickHouse 数据源未配置，手动添加：

**步骤 1：进入数据源配置**
```
Configuration → Data Sources → Add data source
```

**步骤 2：搜索 ClickHouse**
- 类型: `ClickHouse`
- 如果没有，需要安装插件:
  ```bash
  docker exec trading_grafana grafana cli plugins install vertamedia-clickhouse-datasource
  docker restart trading_grafana
  ```

**步骤 3：配置连接**
- **URL**: `http://host.docker.internal:8123`
- **Default Database**: `llm`
- **Use POST requests**: ✅ 启用

**步骤 4：测试连接**
- 点击 **Save & Test** → 应该显示 ✅ Success

### 告警配置

Grafana 支持配置告警规则，自动通知异常：

**示例：LLM API 错误率告警**
```yaml
# 在仪表盘编辑模式下添加告警
conditions:
  - type: query
    query: A  # LLM API 错误率
    reducer: last
    threshold: 0.1  # 错误率 > 10% 触发
notification:
  - Slack / Email / PagerDuty
```

### 常见问题

**Q: 仪表盘显示 "No Data"？**
- A: 检查 ClickHouse 数据源是否配置正确
- A: 确认 `llm.llm_sessions` 表有今日数据
  ```bash
  clickhouse-client --query "SELECT count() FROM llm.llm_sessions WHERE trading_day = today()"
  ```

**Q: 如何添加自定义面板？**
- A: 点击仪表盘右上角 **Add panel** → **Add new panel**
- A: 编写 ClickHouse SQL 查询
- A: 选择可视化类型（时间序列、表格、饼图等）

**Q: 如何导出仪表盘？**
- A: 仪表盘设置 → **JSON Model** → 复制 JSON 配置
- A: 或使用 API:
  ```bash
  curl -u admin:admin http://localhost:3000/api/dashboards/uid/ai-trader-brain | jq .dashboard > backup.json
  ```

---

## 📋 大屏三：每日战报 - 高层管理者总结

### 功能说明

每日战报是"高层汇报材料"，用人类易读的格式总结一天的关键事件。

**内容结构**:
```markdown
# AI Trader Daily Report - 2025-10-29

## 🔍 Diagnosis (诊断)
- 策略健康状态
- 数据质量评估
- LLM 可用性检查

## 💊 Prescription (处方)
- 大脑决策：今日运行哪些 session
- 参数调整：cycles、interval、symbols
- 风险控制：close_at、TTL

## 📊 HPO Results (超参数优化)
- 是否运行 HPO
- OOS 夏普率：1.23 ✅
- OOS 最大回撤：0.08
- 是否应用新参数

## 🤖 RL Results (强化学习)
- 是否训练 RL 模型
- 训练 episode 数量
- 最终夏普率
- 是否部署新模型

## 🛡️ Guardian Activity (守护者活动)
- Watchdog 重启次数
- 进程异常事件
- 数据流健康检查

## 📈 Performance Summary
- 今日总 P&L
- 各 symbol 表现
- LLM 决策质量
```

### 启动方式

#### 交互式菜单
```bash
bash scripts/ai_trader/dashboards/view_daily_report.sh
```

**菜单选项**:
- **[1]** 查看今日战报
- **[2]** 选择历史战报
- **[3]** 在浏览器中打开 (HTML 版本)
- **[4]** 显示相关文件（HPO/RL 决策文件）
- **[5]** 查看最近 5 天汇总
- **[q]** 退出

#### 自动模式（脚本调用）
```bash
bash scripts/ai_trader/dashboards/view_daily_report.sh --auto
```

直接输出今日战报到终端。

### 增强体验

**推荐安装工具**:

1. **glow** - Markdown 终端渲染器
   ```bash
   brew install glow
   ```
   效果：彩色高亮、表格格式化、代码块语法高亮

2. **bat** - 带高亮的 cat
   ```bash
   brew install bat
   ```
   效果：语法高亮、行号

3. **pandoc** - Markdown 转 HTML
   ```bash
   brew install pandoc
   ```
   效果：生成专业的 HTML 报告

### 相关文件位置

```
runs/ai_trader/YYYY-MM-DD/
├── daily_report.md           # 主战报
├── reflection.json           # 反思日志（原始数据）
├── hpo_decision.json         # HPO 决策
├── rl_decision.json          # RL 决策
├── brain_plan_*.json         # 大脑计划文件
├── tools_ledger.jsonl        # 工具事件日志
├── morning_plan.json         # 早盘计划
├── intraday_plan.json        # 盘中计划
├── afterhours_plan.json      # 盘后计划
└── overnight_plan.json       # 夜间计划
```

### 常见问题

**Q: 今天没有战报？**
- A: 需要先运行 `evening_learning`:
  ```bash
  PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/cli.py run --mode learn
  ```

**Q: 如何定期生成战报？**
- A: 配置 cron 定时任务:
  ```bash
  # 每天晚上 8 点运行
  0 20 * * * cd . && PYTHONPATH=$(pwd) venv/bin/python scripts/ai_trader/cli.py run --mode learn >> logs/evening_learning.log 2>&1
  ```

**Q: 如何自动发送战报到邮箱/Slack？**
- A: 在 `evening_learning` 完成后，添加通知脚本:
  ```bash
  # 示例：发送到 Slack
  curl -X POST -H 'Content-type: application/json' \
    --data "{\"text\":\"$(cat runs/ai_trader/$(date +%Y-%m-%d)/daily_report.md)\"}" \
    YOUR_SLACK_WEBHOOK_URL
  ```

---

## 🔧 维护与故障排查

### 服务状态检查

**查看所有 Docker 容器**:
```bash
docker ps --filter name=trading_
```

**查看 MLflow 日志**:
```bash
tail -f /tmp/mlflow_startup.log
```

**查看 Grafana 日志**:
```bash
docker logs -f trading_grafana
```

**查看 ClickHouse 日志**:
```bash
docker logs -f trading_clickhouse
```

### 常见故障

#### 1. MLflow 无法访问 (localhost:5000)

**症状**: 浏览器无法打开 `http://localhost:5000`

**排查步骤**:
```bash
# 检查进程
lsof -i :5000

# 如果没有进程，重启
bash scripts/ai_trader/dashboards/start_mlflow.sh

# 查看日志
tail -f /tmp/mlflow_startup.log
```

#### 2. Grafana 显示 "No Data"

**症状**: 仪表盘面板显示 "No Data"

**排查步骤**:
```bash
# 1. 检查 ClickHouse 是否有数据
clickhouse-client --query "SELECT count() FROM llm.llm_sessions WHERE trading_day = today()"

# 2. 测试 ClickHouse 连接
curl http://localhost:8123/ping

# 3. 检查 Grafana 数据源配置
# 浏览器: http://localhost:3000 → Configuration → Data Sources → ClickHouse → Test

# 4. 查看 Grafana 日志
docker logs trading_grafana | tail -50
```

#### 3. 每日战报不存在

**症状**: `daily_report.md` 文件不存在

**排查步骤**:
```bash
# 1. 检查 evening_learning 是否运行
ls -la runs/ai_trader/$(date +%Y-%m-%d)/

# 2. 查看 evening_learning 日志
tail -100 logs/orchestrator.log

# 3. 手动运行 evening_learning
PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/cli.py run --mode learn

# 4. 检查 report_builder.py 是否有错误
grep -i error logs/orchestrator.log | tail -20
```

### 重启服务

**重启 MLflow**:
```bash
pkill -f 'mlflow ui'
bash scripts/ai_trader/dashboards/start_mlflow.sh
```

**重启 Grafana**:
```bash
docker restart trading_grafana
```

**重启所有基础设施**:
```bash
cd ./infra
docker-compose restart
```

---

## 🎯 最佳实践

### 监控工作流

**早上开盘前 (8:00-9:30)**:
1. 打开 Grafana → 查看昨日战报摘要
2. 打开 MLflow → 检查昨晚 HPO/RL 是否成功
3. 查看每日战报 → 确认今日策略配置

**盘中监控 (9:30-16:00)**:
1. Grafana 自动刷新 (10s 间隔)
2. 关注 P&L 曲线
3. 监控 LLM API 错误率
4. 检查进程心跳（确保 < 180s）

**盘后复盘 (16:00-20:00)**:
1. 查看今日战报 → 性能总结
2. 查看 Grafana → 各 symbol 表现
3. 分析决策质量 → 信心度分布

**晚间学习 (20:00-次日 4:00)**:
1. 自动运行 `evening_learning`
2. 次日早晨查看 MLflow → 新实验结果
3. 决定是否应用新参数

### 告警配置建议

**关键告警**:
1. **LLM API 错误率 > 10%** → Slack/Email 通知
2. **进程心跳超时 > 5 分钟** → 紧急通知
3. **单日回撤 > 5%** → 风控告警
4. **ClickHouse 磁盘使用 > 80%** → 运维告警

**Grafana 告警示例**:
```yaml
# alerting/rules.yaml
groups:
  - name: ai_trader_alerts
    interval: 1m
    rules:
      - alert: LLM_API_Error_Rate_High
        expr: rate(ai_trader_llm_errors_total[5m]) > 0.1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "LLM API error rate > 10%"
          description: "Provider {{ $labels.provider }} error rate: {{ $value }}"

      - alert: Process_Heartbeat_Timeout
        expr: time() - ai_trader_heartbeat_timestamp > 300
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "Process heartbeat timeout > 5 minutes"
          description: "Process {{ $labels.process }} last heartbeat: {{ $value }}s ago"
```

### 性能优化

**MLflow**:
- 定期清理旧实验（保留最近 90 天）
- 使用 SQLite 或 MySQL 作为后端存储（生产环境）
- 限制 artifact 文件大小（< 100MB）

**Grafana**:
- 限制查询时间范围（默认 6 小时）
- 使用缓存（ClickHouse query_cache）
- 避免高频自动刷新（< 5s 间隔）

**ClickHouse**:
- 配置 TTL 清理旧数据
- 定期 OPTIMIZE TABLE 合并分区
- 监控磁盘使用率

---

## 📚 进阶话题

### 自定义 Grafana 仪表盘

**示例：添加 "Provider 性能对比" 面板**

```sql
-- ClickHouse 查询
SELECT
  provider,
  avg(confidence) as avg_confidence,
  countIf(action = 'BUY') as buys,
  countIf(action = 'SELL') as sells,
  sum(realized_pnl) as total_pnl
FROM llm.llm_decisions d
LEFT JOIN llm.llm_fills f ON d.decision_id = f.decision_id
WHERE trading_day = today()
GROUP BY provider
ORDER BY total_pnl DESC
```

**可视化类型**: Bar Chart (柱状图)
**X 轴**: provider
**Y 轴**: total_pnl

### MLflow 实验管理

**注册最佳模型**:
```python
import mlflow

# 注册 Champion 策略
mlflow.register_model(
    model_uri=f"runs:/{run_id}/enhanced_ml_001",
    name="ChampionStrategy"
)

# 切换到 Production 阶段
client = mlflow.tracking.MlflowClient()
client.transition_model_version_stage(
    name="ChampionStrategy",
    version=1,
    stage="Production"
)
```

**自动化部署流水线**:
```bash
# 示例：CI/CD 集成
if [ $(mlflow models list -m ChampionStrategy --stage Production | wc -l) -gt 0 ]; then
    echo "Deploying new Champion strategy..."
    python apply_extras_params.py --from-mlflow --model ChampionStrategy --stage Production
fi
```

### 每日战报自动化

**定时任务 (crontab)**:
```bash
# 编辑 cron
crontab -e

# 添加任务
0 20 * * * cd . && PYTHONPATH=$(pwd) venv/bin/python scripts/ai_trader/cli.py run --mode learn && bash scripts/ai_trader/dashboards/view_daily_report.sh --auto | mail -s "AI Trader Daily Report" your_email@example.com
```

**Slack 通知**:
```bash
# 在 evening_learning 完成后
REPORT_FILE="runs/ai_trader/$(date +%Y-%m-%d)/daily_report.md"
if [ -f "$REPORT_FILE" ]; then
    REPORT_CONTENT=$(cat "$REPORT_FILE")
    curl -X POST -H 'Content-type: application/json' \
        --data "{\"text\":\"$REPORT_CONTENT\"}" \
        $SLACK_WEBHOOK_URL
fi
```

---

## 🆘 获取帮助

### 文档资源

- **MLflow 官方文档**: https://mlflow.org/docs/latest/index.html
- **Grafana 官方文档**: https://grafana.com/docs/
- **ClickHouse 官方文档**: https://clickhouse.com/docs/

### 项目文档

```
docs/
├── MONITORING_GUIDE.md           # 监控指南
├── QUICK_START.md                # 快速启动
├── SYSTEM_DESIGN.md              # 系统设计
└── HOW_TO_USE.md                 # 使用指南
```

### 日志位置

```
logs/
├── orchestrator.log              # Orchestrator 主日志
├── deepseek_nvidia_*.log         # LLM Trader 日志
├── deepseek_nvidia_*_api_detailed.txt  # API 详细日志
└── system_health.json            # 系统健康检查

/tmp/
├── mlflow_startup.log            # MLflow 启动日志
└── ai_trader_daily_report_*.html # 临时 HTML 报告
```

### 社区支持

- **GitHub Issues**: 提交 bug 或功能请求
- **团队内部**: Slack #ai-trader-support 频道

---

## 📝 更新日志

| 版本 | 日期 | 变更内容 |
|------|------|----------|
| 1.0 | 2025-10-29 | 初始版本：三大屏完整实施 |

**最后更新**: 2025-10-29
**维护者**: Trading Team
