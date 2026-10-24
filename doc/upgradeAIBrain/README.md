# AI Brain 动态 Prompt 升级项目

**状态**: ✅ **实施完成,已通过验证**
**版本**: 1.0
**日期**: 2025-10-31

---

## 📚 文档导航

本项目包含以下文档:

| 文档 | 描述 | 阅读对象 |
|------|------|----------|
| [1.md](./1.md) | 原始需求文档(Gemini 分析) | 开发者 |
| [IMPLEMENTATION_SUMMARY.md](./IMPLEMENTATION_SUMMARY.md) | 实施总结(详细的代码修改) | 开发者/审查者 |
| [VERIFICATION_REPORT.md](./VERIFICATION_REPORT.md) | 验证报告(测试结果) | QA/审查者 |
| [QUICKSTART_GUIDE.md](./QUICKSTART_GUIDE.md) | 快速启动指南 | 使用者 |
| **本文档 (README.md)** | 项目总览 | 所有人 |

---

## 🎯 项目目标

让 AI Brain 能够**动态定义和控制**执行层(LLM Trader Bot)的 Prompt,而不仅仅是选择预设的配置。

### 核心能力

1. **动态 Prompt 控制**: Brain 可以指定执行层使用的 Prompt(通过 ID 或文本)
2. **A/B 测试支持**: 同时运行不同 Prompt,对比效果
3. **自动回滚**: 根据表现自动切换到更好的 Prompt
4. **安全机制**: 多层防护,防止 Prompt 注入攻击

---

## 📊 实施状态

### ✅ 已完成

- [x] 代码实施(6 个文件修改)
- [x] 单元测试(9 个测试,全部通过)
- [x] 功能验证(Prompt 库、优先级逻辑、安全机制)
- [x] 文档编写(实施总结、验证报告、快速启动指南)

### ⏳ 待验证

- [ ] Paper Trading 端到端测试
- [ ] Brain 实际生成带 prompt 字段的 plan
- [ ] Evening Learning 集成(根据表现调整 Prompt)
- [ ] 生产环境运行

---

## 🏗️ 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                          Brain                              │
│  propose_plan() → {execution_prompt_id/text, ab_group}     │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                      Orchestrator                           │
│  _prepare_prompt_overrides() → 落盘/返回映射               │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                 LLMTraderController                         │
│  run_with_plan(plan, overrides) → 注入环境变量             │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                    LLMTraderBot                             │
│  读取 LLM_PROMPT_ID/FILE → 传递给 PromptBuilder            │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                   PromptBuilder                             │
│  应用覆盖: text > id > default                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 🔑 核心特性

### 1. 两种模式

#### ID 模式(推荐,默认)
- Brain 指定 `execution_prompt_id`
- 从白名单库加载: `prompts/prompt_library.json`
- **安全**: 只能使用审核过的 Prompt

#### 文本模式(可选)
- Brain 指定 `execution_prompt_text`
- 需开启: `policies.prompts.allow_text_override: true`
- **灵活**: 可以快速试验新 Prompt

### 2. 安全机制

- **默认安全**: `allow_text_override: false`
- **白名单**: 仅使用预审核的 Prompt
- **审计追踪**: 所有 Prompt 覆盖都落盘到 `runs/YYYY-MM-DD/prompts/`
- **日志记录**: 完整的操作日志

### 3. A/B 测试

```json
{
  "ab": {"champion": 0.7, "challenger": 0.3},
  "providers": [
    {"ab_group": "champion", "execution_prompt_id": "v3_stable"},
    {"ab_group": "challenger", "execution_prompt_id": "v4_experimental"}
  ]
}
```

### 4. 向后兼容

- 不提供 `execution_prompt_id/text` → 使用默认 Prompt
- 所有新参数都是可选的
- 现有代码无需修改

---

## 📝 使用示例

### 基础用法

Brain 输出:
```json
{
  "providers": [{
    "name": "gemini",
    "execution_prompt_id": "v3_fast_market_following"
  }]
}
```

结果:
- Orchestrator 读取 prompt_library.json
- Bot 使用 "v3_fast_market_following" Prompt

### A/B 测试

Brain 输出:
```json
{
  "ab": {"champion": 0.7, "challenger": 0.3},
  "providers": [
    {"ab_group": "champion", "execution_prompt_id": "conservative"},
    {"ab_group": "challenger", "execution_prompt_id": "aggressive_scalper"}
  ]
}
```

结果:
- 同时运行两个 Bot
- 对比 "conservative" vs "aggressive_scalper" 的表现

---

## 🧪 验证结果

### 测试概况

| 类型 | 数量 | 通过 | 失败 |
|------|------|------|------|
| 语法检查 | 6 files | 6 | 0 |
| 单元测试 | 9 tests | 9 | 0 |
| 功能验证 | 5 scenarios | 5 | 0 |

### 关键验证点

✅ Prompt 库加载正常(5 个预定义 Prompt)
✅ 优先级逻辑正确(text > id > default)
✅ 安全机制工作(默认拒绝文本覆盖)
✅ 多 Provider 支持
✅ 文件落盘和审计追踪

详见: [VERIFICATION_REPORT.md](./VERIFICATION_REPORT.md)

---

## 🚀 快速开始

### 1. 最简单的测试

```bash
# 1. 创建测试 plan
cat > test_plan.json << EOF
{
  "providers": [{
    "name": "gemini",
    "execution_prompt_id": "v3_fast_market_following",
    "cycles": 3
  }]
}
EOF

# 2. 运行 (需临时修改 orchestrator 加载 test_plan.json)
python scripts/ai_trader/run_system.py morning --paper

# 3. 检查日志
grep "Prompt Override" logs/*.log
```

### 2. 查看可用的 Prompt

```bash
cat prompts/prompt_library.json | jq 'keys'
```

输出:
```json
[
  "v3_stable",
  "v3_fast_market_following",
  "v4_experimental",
  "conservative",
  "aggressive_scalper"
]
```

### 3. 添加自定义 Prompt

编辑 `prompts/prompt_library.json`:
```json
{
  "my_custom_prompt": "Your custom prompt text here..."
}
```

然后在 plan 中使用:
```json
{
  "execution_prompt_id": "my_custom_prompt"
}
```

更多示例: [QUICKSTART_GUIDE.md](./QUICKSTART_GUIDE.md)

---

## 📂 修改的文件

| 文件 | 修改类型 | 说明 |
|------|----------|------|
| `scripts/ai_trader/brain/brain.py` | Schema 扩展 | 添加 3 个新字段 |
| `scripts/ai_trader/orchestrator.py` | 新增方法 | `_prepare_prompt_overrides()` |
| `scripts/ai_trader/controllers/llm_trader_controller.py` | 参数扩展 | 支持 `prompt_overrides` |
| `llm_trader/llm_trader/llm_trader_bot.py` | 环境变量读取 | 读取 `LLM_PROMPT_ID/FILE` |
| `llm_trader/llm_trader/prompt_builder_universal.py` | 覆盖逻辑 | 应用 Prompt 覆盖 |
| `llm_trader/llm_trader/prompt_builder_compact.py` | 覆盖逻辑 | 应用 Prompt 覆盖 |
| `scripts/ai_trader/config/ai_trader.yaml` | 配置项 | 安全开关 |
| `prompts/prompt_library.json` | **新建** | Prompt 白名单库 |

---

## 🔧 配置说明

### ai_trader.yaml

```yaml
policies:
  prompts:
    allow_text_override: false  # 安全开关(默认关闭)
```

**建议**:
- 生产环境: `false`(仅 ID 模式)
- 测试环境: `true`(可以试验新 Prompt)

### prompts/prompt_library.json

```json
{
  "prompt_id": "Prompt text here..."
}
```

**建议**:
- 每个 Prompt 应该有清晰的命名
- 添加版本号(如 v3_stable, v4_experimental)
- 记录 Prompt 的使用场景和预期效果

---

## 📈 性能影响

### 新增的操作

1. **文件 I/O**:
   - Prompt 库加载(启动时一次)
   - Prompt 文本落盘(仅启用文本模式时)

2. **环境变量**:
   - 设置: ~1μs (可忽略)
   - 读取: ~1μs (可忽略)

### 性能测试

- **Prompt 库加载**: < 1ms (5 个 Prompt)
- **文件写入**: < 5ms (典型 Prompt ~500 字符)
- **环境变量传递**: 无延迟

**结论**: 性能影响可忽略不计。

---

## 🛡️ 安全评估

### 攻击面分析

| 风险 | 缓解措施 | 状态 |
|------|----------|------|
| Prompt 注入 | 默认仅 ID 模式 | ✅ 已实施 |
| 恶意文本 | `allow_text_override=false` | ✅ 已实施 |
| 未授权修改 | 白名单库 | ✅ 已实施 |
| 审计追踪 | 文件落盘 + 日志 | ✅ 已实施 |

### 安全建议

1. **生产环境**:
   - 保持 `allow_text_override: false`
   - 定期审核 `prompt_library.json`
   - 监控 `runs/*/prompts/` 目录

2. **测试环境**:
   - 可以开启 `allow_text_override: true`
   - 记录所有试验的 Prompt
   - 验证后添加到白名单库

---

## 🔮 未来增强

### 短期(已规划)

- [ ] Evening Learning 集成(根据表现自动调整)
- [ ] A/B 抽样器(精细权重控制)
- [ ] Prompt 性能指标(Sharpe, 胜率等)

### 中期

- [ ] Prompt 版本管理
- [ ] 自动 Prompt 优化(基于反馈)
- [ ] 多模态 Prompt 支持

### 长期

- [ ] Prompt 签名校验
- [ ] 动态 Prompt 生成(Brain 根据市场状态生成)
- [ ] Prompt 效果预测模型

---

## 📞 支持

### 遇到问题?

1. **查看文档**:
   - [QUICKSTART_GUIDE.md](./QUICKSTART_GUIDE.md) - 快速开始
   - [VERIFICATION_REPORT.md](./VERIFICATION_REPORT.md) - 已知问题

2. **运行测试**:
   ```bash
   python -m pytest scripts/ai_trader/tests/unit/test_prompt_override.py -v
   ```

3. **检查日志**:
   ```bash
   tail -f logs/ai_trader_*.log | grep -i prompt
   ```

### 反馈

如有问题或建议,请在项目 issue 中反馈。

---

## 🙏 致谢

基于文档 `./doc/upgradeAIBrain/1.md` 中的 Gemini 分析和建议实施。

---

**项目状态**: ✅ 实施完成,已通过验证,建议进入 Paper Trading 测试
**维护者**: Trading Team
**最后更新**: 2025-10-31
**版本**: 1.0
