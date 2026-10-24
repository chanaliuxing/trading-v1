# 每日运维工具包

> 交易系统每日运维的完整工具和文档集合
>
> **目录**: `./docs/daily/`

---

## 📁 文件清单

### 📋 文档

1. **DAILY_OPERATIONS_CHECKLIST.md** (27 KB)
   - 完整的每日运维清单
   - 覆盖盘前、盘中、盘后、隔夜所有时段
   - 包含详细命令和故障排查指南
   - **用途**: 详细参考手册

2. **QUICK_REFERENCE.md** (5.5 KB)
   - 快速参考卡，一页纸总结
   - 常用命令速查
   - 故障排查流程
   - **用途**: 打印贴在桌面，日常快速查询

3. **AUTOMATION_ONE_CLICK.md**
   - 一键自动化（macOS）使用说明：双击启动与停止、日志位置、任务清单
   - **用途**: 非技术同学也可一键启用/停用全流程定时

4. **RUNBOOK_POLICY.md**（1页）
   - 三道门阈值、默认动作、固定命令、每日调度（ET）
   - **用途**: 上线/训练前的硬约束执行单
3. **RUNBOOK_POLICY.md**（1页）
   - 三道门阈值、默认动作、固定命令、每日调度（ET）
   - **用途**: 上线/训练前的硬约束执行单

### 🔧 自动化脚本

3. **system_health_check.py** (19 KB) ⭐ **新增**
   - 系统健康检查脚本（生成 JSON 报告）
   - 自动检查 7 大方面：Docker、run_system.py、IBKR、数据质量、Kafka、错误日志、系统资源
   - 报告保存到: `logs/system_health.json`（固定文件名，方便 AI 读取）
   - **运行时间**: 任意时间，建议每小时或盘前/盘后
   - **用法**:
     ```bash
     ./system_health_check.py          # 基本检查
     ./system_health_check.py -v       # 显示详细日志
     ```
   - **详细文档**: [HEALTH_CHECK_GUIDE.md](HEALTH_CHECK_GUIDE.md)

4. **morning_check.sh** (4.8 KB)
   - 盘前系统检查脚本
   - 检查基础设施、IBKR 连接、数据质量
   - 自动识别缺失数据
   - **运行时间**: 每日 07:00-09:00
   - **用法**: `./morning_check.sh`

5. **evening_review.sh** (7.7 KB)
   - 盘后复盘脚本
   - 导出 LLM 决策、统计交易表现
   - 可选运行 Alpha Loop 优化
   - **运行时间**: 每日 16:30-20:00
   - **用法**: `./evening_review.sh`

6. **trigger_backfill.py** (4.8 KB)
   - 手动触发数据回补工具
   - 支持指定日期、股票、Bar 大小
   - **用法**:
     ```bash
     ./trigger_backfill.py                    # 回补昨天
     ./trigger_backfill.py --date 2025-10-26  # 指定日期
     ./trigger_backfill.py --symbols AAPL TSLA
     ```

### 🧩 模板（可直接复制填写）

7. **SCORECARD_TEMPLATE.md**
   - 统一 run_system 与 llm_trader 的 E/Sharpe/Sortino/MDD/执行质量/一致性分
   - 含切片、对比与回滚/晋升门槛

8. **PROMPT_CHANGE_TEMPLATE.md**
   - 仅战术/元认知层改动模板，附证据/适用/回退条件

9. **AB_ROLLOUT_TEMPLATE.md**
   - Champion–Challenger 70/30 小权重推进、门禁/评估/回滚/晋升步骤

### 📊 待生成的脚本

以下脚本会在首次运行时自动生成：

6. **check_data_quality.sh**
   - 数据质量综合检查
   - 自动生成于首次运行 `morning_check.sh` 时

7. **daily_summary.sh**
   - 每日总结报告
   - 自动生成于首次运行 `evening_review.sh` 时

---

## 🚀 快速开始

### 系统健康检查（任意时间）⭐ 推荐

```bash
cd ./docs/daily
./system_health_check.py
```

**脚本会生成**:
- 📄 JSON 报告: `./logs/system_health.json`
- 📊 包含 7 大方面检查结果
- 🤖 固定文件名，方便 AI 读取
- ⏰ 包含时间戳，可追溯历史

**检查内容**:
- ✅ Docker 容器状态
- ✅ run_system.py 运行状态
- ✅ IBKR 连接
- ✅ 数据质量（Tick + Bar）
- ✅ Kafka Consumer Groups LAG
- ✅ 错误日志统计
- ✅ 系统资源使用（CPU/内存/磁盘）

### 每日早上（07:00）

```bash
cd ./docs/daily
./morning_check.sh
```

**脚本会检查**:
- ✅ Docker 容器状态（Kafka, ClickHouse, Redis）
- ✅ run_system.py 运行状态
- ✅ IBKR 连接
- ✅ 实时 Tick 数据
- ✅ 昨日 Bar 数据完整性
- ✅ Kafka Consumer Groups LAG

### 每日晚上（16:30）

```bash
cd ./docs/daily
./evening_review.sh
```

**脚本会生成**:
- 📊 今日 LLM 决策数据导出
- 📈 交易统计（胜率、盈亏、各模型表现）
- 🎯 最佳/最差决策分析
- 🔧 可选：Alpha Loop 参数优化

### 数据缺失时

```bash
cd ./docs/daily
./trigger_backfill.py --date 2025-10-26 --symbols AAPL TSLA
```

---

## 📅 每日工作流程

| 时间 | 任务 | 脚本/命令 |
|------|------|----------|
| **07:00** | 盘前检查 | `./morning_check.sh` |
| **07:30** | 启动盘前 LLM | `cd ../../llm_trader && ./run_premarket.sh` |
| **09:30** | 启动盘中 LLM | `cd ../../llm_trader && ./run_all_providers.sh market` |
| **16:30** | 盘后复盘 | `./evening_review.sh` |
| **17:00** | Alpha Loop 优化 | 在 `evening_review.sh` 中选择 |
| **20:00** | 启动隔夜 LLM | `cd ../../llm_trader && nohup python run_single_bot.py --trading-hours extended &` |

---

## 🔍 文件用途说明

### 适合打印

- **QUICK_REFERENCE.md**: 一页纸快速参考，适合打印贴在桌面

### 每日必读

- **DAILY_OPERATIONS_CHECKLIST.md**: 详细步骤和命令，第一次使用时仔细阅读

### 每日必跑

- **morning_check.sh**: 每天早上第一件事
- **evening_review.sh**: 每天盘后第一件事

### 按需使用

- **trigger_backfill.py**: 发现数据缺失时使用

---

## ⚙️ 配置说明

所有脚本已配置为可执行权限：

```bash
-rwxr-xr-x morning_check.sh
-rwxr-xr-x evening_review.sh
-rwxr-xr-x trigger_backfill.py
```

无需额外配置，直接运行即可。

---

## 🚨 故障排查

### 问题：脚本报错 "Permission denied"

```bash
chmod +x ./docs/daily/*.sh
chmod +x ./docs/daily/*.py
```

### 问题：无法连接 ClickHouse

```bash
# 检查容器
cd ./infra
docker-compose ps

# 重启
docker-compose restart clickhouse
```

### 问题：IBKR 连接失败

```bash
# 检查 TWS/Gateway 是否运行
# 重启 run_system.py
pkill -f run_system.py
cd .
nohup python run_system.py > logs/run_system.log 2>&1 &
```

---

## 📚 相关文档

- **IBKR 连接容错分析**: `./temp/ibkr_connection_resilience_analysis.md`
- **Kafka 修复总结**: `./temp/kafka_fix_summary.md`
- **LLM Trader 文档**: `./llm_trader/HOW_TO_USE.md`
- **代码修复规则**: `./CLAUDE.md`

---

## 🔄 更新历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0 | 2025-10-27 | 初始版本，包含 5 个核心文件 |

---

**维护者**: Trading Team
**最后更新**: 2025-10-27
**位置**: `./docs/daily/`
