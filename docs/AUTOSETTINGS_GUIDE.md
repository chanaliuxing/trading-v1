# AI大脑 Autosettings 使用指南

**版本**: 1.0
**最后更新**: 2025-11-02
**目标**: 让AI大脑的风控策略建议安全落地到config.yml

---

## 概述

### 什么是 Autosettings?

Autosettings 是AI大脑(ControlBrain)的自动应用机制，允许LLM的风控策略建议直接修改`config.yml`中的风控参数，无需人工编辑配置文件。

### 工作流程

```
LLM决策 → Orchestrator._apply_risk_policy() → 白名单检查 → 审计记录 → config.yml更新
```

**关键特性**:
- ✅ **白名单保护**: 只能修改预定义的风控参数，禁止修改账户/broker等敏感配置
- ✅ **审批流程**: Paper环境可直接应用，Prod环境需要人工审批
- ✅ **审计追踪**: 每次应用都记录审计文件(applied/denied/errors)
- ✅ **自动备份**: 更新config.yml前自动备份到`backups/`目录

---

## 配置说明

### 配置位置

文件: `scripts/ai_trader/config/ai_trader.yaml`

```yaml
policies:
  autosettings:
    enabled: true              # 启用自动应用 (Paper: true, Prod: false)
    require_approval: false    # 是否需要人工审批 (Paper: false, Prod: true)
```

### 环境配置建议

| 环境 | enabled | require_approval | 说明 |
|------|---------|------------------|------|
| **Paper Trading** | `true` | `false` | 直接应用，快速迭代 |
| **Staging** | `true` | `true` | 启用但需审批，验证流程 |
| **Production** | `false` | `true` | 禁用自动应用，仅记录审计 |

---

## 白名单

### 当前白名单 (硬编码)

位置: `scripts/ai_trader/orchestrator.py:461-470`

```python
def _risk_policy_whitelist(self) -> set[str]:
    return {
        # Guardian 风控参数
        "risk_management.guardian.dd_intraday_pct",    # 日内最大回撤 (%)
        "risk_management.guardian.dd_5d_pct",          # 5日最大回撤 (%)
        "risk_management.guardian.vol_spike_mult",     # 波动率异常倍数

        # Hedge 对冲配置
        "risk_management.hedge.strategy_id",           # 对冲策略ID
        "risk_management.hedge.min_notional_usd",      # 最小对冲名义金额
        "risk_management.hedge.auto_execute",          # 自动执行对冲
        "risk_management.hedge.mode",                  # 对冲模式 (delta_neutral/beta_hedge等)
    }
```

### 禁止修改的参数 (示例)

- ❌ `broker.*` - 券商配置
- ❌ `account.*` - 账户配置
- ❌ `sizing.base_qty` - 基础仓位 (若需要可加入白名单)
- ❌ `sizing.max_qty` - 最大仓位
- ❌ `paths.*` - 路径配置

**原则**: 只允许修改风控阈值和对冲配置，禁止修改策略规模、账户、券商等敏感参数。

---

## 使用场景

### 场景1: Paper环境 - 自动应用

**配置**:
```yaml
policies:
  autosettings:
    enabled: true
    require_approval: false
```

**流程**:
1. Evening learning执行 → Brain决策 → 自动应用 → config.yml更新
2. 审计文件: `runs/ai_trader/YYYY-MM-DD/audits/risk_policy_HHMMSS.json`
   - `applied: [...]` - 成功应用的参数
   - `errors: []` - 无错误

**验证**:
```bash
# 查看最新审计
cat runs/ai_trader/$(date +%Y-%m-%d)/audits/risk_policy_*.json | jq '.applied'

# 验证config.yml已更新
grep -A 10 "^risk_management:" scripts/ai_trader/config/ai_trader.yaml
```

---

### 场景2: Production环境 - 人工审批

**配置**:
```yaml
policies:
  autosettings:
    enabled: true               # 启用审计链路
    require_approval: true      # 需要审批
```

**流程**:
1. Evening learning执行 → Brain决策 → **仅记录审计，不修改config.yml**
2. 审计文件: `runs/ai_trader/YYYY-MM-DD/audits/risk_policy_HHMMSS.json`
   - `applied: []` - 零项应用
   - `errors: ["pending_approval"]` - 等待审批

**人工审批步骤**:
```bash
# 1. 查看LLM建议
cat runs/ai_trader/$(date +%Y-%m-%d)/audits/risk_policy_*.json | jq '.received.apply'

# 示例输出:
# [
#   {"key": "risk_management.guardian.dd_intraday_pct", "value": 2},
#   {"key": "risk_management.hedge.auto_execute", "value": true}
# ]

# 2. 人工审核决策 (评估合理性)

# 3. 手动应用 (编辑config.yml)
vim scripts/ai_trader/config/ai_trader.yaml

# 添加:
risk_management:
  guardian:
    dd_intraday_pct: 2      # LLM建议
  hedge:
    auto_execute: true      # LLM建议

# 4. 重启orchestrator生效
pkill -f auto_loop.py
nohup python scripts/ai_trader/auto_loop.py > logs/auto_loop_$(date +%Y%m%d).log 2>&1 &
```

---

### 场景3: 临时绕过审批 (测试用)

**环境变量**: `AI_TRADER_APPROVE_AUTOS=1`

```bash
# 临时启用自动应用 (即使require_approval=true)
AI_TRADER_APPROVE_AUTOS=1 python scripts/ai_trader/auto_loop.py

# 或在启动脚本中设置
export AI_TRADER_APPROVE_AUTOS=1
nohup python scripts/ai_trader/auto_loop.py > logs/auto_loop.log 2>&1 &
```

**警告**: 仅用于测试/CI环境，生产环境禁止使用。

---

## 审计文件说明

### 审计文件路径

```
runs/ai_trader/YYYY-MM-DD/audits/risk_policy_HHMMSS.json
```

### 审计文件格式

```json
{
  "received": {
    "apply": [
      {"key": "risk_management.guardian.dd_intraday_pct", "value": 2},
      {"key": "risk_management.hedge.auto_execute", "value": true}
    ],
    "notes": "conservative init: no pnl history, zero track metrics"
  },
  "enabled": true,            // autosettings是否启用
  "require_approval": false,  // 是否需要审批
  "applied": [                // ✅ 成功应用的参数
    {
      "key": "risk_management.guardian.dd_intraday_pct",
      "from": null,
      "to": 2
    }
  ],
  "denied": [                 // ❌ 被拒绝的参数 (不在白名单)
    {
      "key": "broker.account_id",
      "reason": "not_whitelisted"
    }
  ],
  "errors": []                // ❌ 错误列表
                              // - "autosettings_disabled" (未启用)
                              // - "pending_approval" (等待审批)
                              // - "read_config_failed:..." (读取配置失败)
                              // - "write_failed:..." (写入失败)
}
```

### 成功标准

✅ **成功应用**:
- `enabled: true`
- `applied.length > 0`
- `errors: []`
- `config.yml`已更新

❌ **失败/未应用**:
- `enabled: false` → 错误: `"autosettings_disabled"`
- `require_approval: true` → 错误: `"pending_approval"`
- 参数不在白名单 → 记录到`denied`

---

## 触发时机

### 自动触发

Orchestrator在以下时机调用Brain的`risk_policy`决策:

1. **Evening Learning** (每日报告阶段)
   - 文件: `scripts/ai_trader/orchestrator.py:1844-1860`
   - 时机: 晚间学习流程的最后阶段
   - 输入: `{"risk_config": {...}, "facts": {...}}`

### 手动触发 (测试)

```bash
# 运行完整的evening_learning流程
python3 -c "
from scripts.ai_trader.orchestrator import Orchestrator
orch = Orchestrator()
result = orch.evening_learning()
print('Result:', result)
"

# 或者直接测试_apply_risk_policy
python /tmp/test_autosettings_simple.py
```

---

## 回滚操作

### 自动备份

每次更新config.yml前，自动备份到:

```
backups/config.yml.YYYYMMDD-HHMMSS.yaml
```

### 回滚步骤

```bash
# 1. 查看备份列表
ls -lh backups/config.yml.*.yaml

# 2. 选择备份恢复
cp backups/config.yml.20251102-010901.yaml scripts/ai_trader/config/ai_trader.yaml

# 3. 验证
grep -A 10 "^risk_management:" scripts/ai_trader/config/ai_trader.yaml

# 4. 重启orchestrator
pkill -f auto_loop.py
nohup python scripts/ai_trader/auto_loop.py > logs/auto_loop.log 2>&1 &
```

---

## 可观测性

### LLM交互日志

**Prompt 和 Response** (已内置):

- 位置: `runs/ai_trader/YYYY-MM-DD/brain_prompt_*.txt`
- 位置: `runs/ai_trader/YYYY-MM-DD/brain_raw_response_*.txt`
- 来源: `scripts/ai_trader/brain/brain.py:237-258`

**验证**:
```bash
# 查看最新的brain prompt
ls -lh runs/ai_trader/$(date +%Y-%m-%d)/brain_prompt_*

# 查看最新的原始response
ls -lh runs/ai_trader/$(date +%Y-%m-%d)/brain_raw_response_*
```

**注意**: 如果文件不存在，检查`brain.py`中的异常处理(line 237-258)是否被触发。

### Brain调用记录

**结构化日志**:
- 位置: `runs/ai_trader/YYYY-MM-DD/brain_calls.jsonl`
- 格式: 每行一个JSON (ts, action, provider, model, output, duration_ms, success)

```bash
# 查看今天的所有brain调用
cat runs/ai_trader/$(date +%Y-%m-%d)/brain_calls.jsonl | jq

# 过滤risk_policy调用
cat runs/ai_trader/$(date +%Y-%m-%d)/brain_calls.jsonl | jq 'select(.action == "risk_policy")'
```

### Grafana监控 (建议添加)

```yaml
# Prometheus指标 (需要在orchestrator中添加)
brain_decisions_received{action="risk_policy"} 1
brain_decisions_applied{action="risk_policy"} 1
brain_decisions_denied{action="risk_policy"} 0
brain_decisions_errors{action="risk_policy", reason="autosettings_disabled"} 0

# Grafana告警规则
alert: AutosettingsNotApplied
expr: (brain_decisions_received{action="risk_policy"} - brain_decisions_applied{action="risk_policy"}) > 0 for 1h
severity: warning
message: "AI大脑风控决策未应用，检查autosettings配置或审计文件"
```

---

## 故障排查

### 问题1: 决策未应用 (`applied: []`)

**症状**:
```json
{
  "enabled": false,
  "applied": [],
  "errors": ["autosettings_disabled"]
}
```

**原因**: `policies.autosettings.enabled: false` 或配置段缺失

**解决**:
```bash
# 检查配置
grep -A 5 "autosettings:" scripts/ai_trader/config/ai_trader.yaml

# 修复: 添加或启用autosettings
vim scripts/ai_trader/config/ai_trader.yaml
# 设置 enabled: true
```

---

### 问题2: 等待审批 (`pending_approval`)

**症状**:
```json
{
  "enabled": true,
  "require_approval": true,
  "applied": [],
  "errors": ["pending_approval"]
}
```

**原因**: Production环境需要人工审批

**解决**:
1. 查看LLM建议: `cat audits/risk_policy_*.json | jq '.received.apply'`
2. 人工审核并手动应用到config.yml
3. 或临时绕过: `AI_TRADER_APPROVE_AUTOS=1` (仅测试)

---

### 问题3: 参数被拒绝 (`denied`)

**症状**:
```json
{
  "denied": [
    {"key": "sizing.base_qty", "reason": "not_whitelisted"}
  ]
}
```

**原因**: 参数不在白名单中

**解决**:
1. 检查白名单: `scripts/ai_trader/orchestrator.py:461-470`
2. 如需添加参数到白名单 → 修改`_risk_policy_whitelist()`方法
3. 评估安全性 (避免将敏感参数加入白名单)

---

### 问题4: Prompt/Response未保存

**症状**: `runs/ai_trader/YYYY-MM-DD/brain_prompt_*.txt` 不存在

**原因**: `brain.py`中的异常处理跳过了保存逻辑

**调试**:
```python
# 检查brain.py:237-258的异常
# 添加日志:
except Exception as e:
    logger.error("prompt_save_failed", err=str(e), exc_info=True)
    pass
```

**临时解决**: 检查`runs_dir`权限、磁盘空间、路径是否存在。

---

## 安全最佳实践

### 1. 白名单管理

- ✅ **只加入风控阈值和对冲配置**
- ❌ **禁止加入账户、broker、sizing等敏感参数**
- ✅ **定期审计白名单** (git diff检查变更)

### 2. 环境隔离

| 环境 | 策略 |
|------|------|
| Paper | 自动应用 (快速迭代) |
| Staging | 审批流程 (验证流程) |
| Production | 禁用自动应用 (仅审计) |

### 3. 审计检查

```bash
# 每日检查applied历史
find runs/ai_trader -name "risk_policy_*.json" -mtime -7 | xargs cat | jq '.applied'

# 检查是否有异常denied
find runs/ai_trader -name "risk_policy_*.json" -mtime -7 | xargs cat | jq 'select(.denied | length > 0)'
```

### 4. 备份策略

- ✅ 自动备份 (每次更新前)
- ✅ 手动备份 (重要配置变更前)
- ✅ Git版本控制 (定期commit config.yml)

```bash
# Git追踪config.yml变更
git add scripts/ai_trader/config/ai_trader.yaml
git commit -m "autosettings: AI applied dd_intraday_pct=2"
git push
```

---

## 快速参考

### 启用Autosettings (Paper)

```bash
# 1. 编辑配置
vim scripts/ai_trader/config/ai_trader.yaml

# 添加:
policies:
  autosettings:
    enabled: true
    require_approval: false

# 2. 重启生效
pkill -f auto_loop.py
nohup python scripts/ai_trader/auto_loop.py > logs/auto_loop.log 2>&1 &
```

### 验证链路

```bash
# 1. 检查配置
grep -A 5 "autosettings:" scripts/ai_trader/config/ai_trader.yaml

# 2. 触发测试
python /tmp/test_autosettings_simple.py

# 3. 查看审计
cat runs/ai_trader/$(date +%Y-%m-%d)/audits/risk_policy_*.json | jq

# 4. 验证config.yml
grep -A 10 "^risk_management:" scripts/ai_trader/config/ai_trader.yaml
```

### 回滚配置

```bash
# 恢复备份
cp backups/config.yml.YYYYMMDD-HHMMSS.yaml scripts/ai_trader/config/ai_trader.yaml

# 重启
pkill -f auto_loop.py
nohup python scripts/ai_trader/auto_loop.py > logs/auto_loop.log 2>&1 &
```

---

## 附录

### 代码位置索引

| 功能 | 文件 | 行号 |
|------|------|------|
| 白名单定义 | `scripts/ai_trader/orchestrator.py` | 461-470 |
| 应用逻辑 | `scripts/ai_trader/orchestrator.py` | 498-560 |
| 触发时机 | `scripts/ai_trader/orchestrator.py` | 1844-1860 |
| Prompt保存 | `scripts/ai_trader/brain/brain.py` | 237-258 |
| Brain调用记录 | `scripts/ai_trader/brain/brain.py` | 多处 |

### 相关文档

- **AI大脑评估报告**: `AI_BRAIN_HONEST_ASSESSMENT.md`
- **快速修复指南**: `temp/AUTOSETTINGS_FIX.md`
- **Claude代码修复规则**: `CLAUDE.md`

---

**版本历史**:
- v1.0 (2025-11-02): 初始版本，覆盖Paper和Prod环境配置

**维护者**: Trading Team
**联系方式**: 参考项目README
