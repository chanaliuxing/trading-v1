# AI Brain 动态 Prompt - 快速启动指南

本指南帮助你快速测试新实现的动态 Prompt 功能。

---

## 🚀 方式 1: 最简单测试(推荐)

### Step 1: 手动创建测试 Plan

创建一个测试文件 `test_plan.json`:

```json
{
  "session": "morning",
  "providers": [
    {
      "name": "gemini",
      "interval_sec": 300,
      "cycles": 3,
      "execution_prompt_id": "v3_fast_market_following",
      "wait_for": "premarket",
      "close_at": "09:35"
    }
  ],
  "llm_trader_flags": {
    "forbid_new_before_min": 9
  }
}
```

### Step 2: 修改 Orchestrator 加载测试 Plan

临时修改 `scripts/ai_trader/orchestrator.py` 的 `morning_sequence()`:

```python
def morning_sequence(self):
    # ... 现有代码 ...

    # 临时测试: 加载手动 plan
    test_plan_path = Path("test_plan.json")
    if test_plan_path.exists():
        self.log.info("TESTING: Loading manual test plan")
        plan = json.loads(test_plan_path.read_text())
    else:
        # 原有逻辑
        plan = self.brain.propose_plan("morning", context)

    plan = self._normalize_or_build_plan("morning", plan, mem)
    overrides = self._prepare_prompt_overrides(plan)
    self.llm.run_with_plan(plan, prompt_overrides=overrides)
```

### Step 3: 运行

```bash
python scripts/ai_trader/run_system.py morning --paper
```

### Step 4: 检查日志

查看以下内容:

1. **Orchestrator 日志**:
   ```
   [prompt_override_saved] index=0, provider=gemini, file=...
   ```

2. **子进程日志**:
   ```
   [Prompt Override] Using prompt ID: v3_fast_market_following
   ```

3. **文件输出**:
   ```bash
   ls runs/$(date +%Y-%m-%d)/prompts/
   # 应该看到: p0_gemini_*.txt (如果启用了 text override)
   ```

---

## 🔧 方式 2: 启用 Brain 自动生成

### Step 1: 确认 Brain 配置

检查 `scripts/ai_trader/config/ai_trader.yaml`:

```yaml
brain:
  enabled: true
  provider: deepseek_nvidia
  model: deepseek-ai/deepseek-r1
```

### Step 2: 让 Brain 使用 Prompt 字段

Brain 现在可以在输出中包含:

```json
{
  "providers": [{
    "name": "gemini",
    "execution_prompt_id": "v3_stable"
  }]
}
```

**注意**: Brain 需要理解新的 schema。可能需要在第一次运行时提供示例。

### Step 3: 运行

```bash
python scripts/ai_trader/run_system.py morning --paper
```

### Step 4: 检查 Brain 输出

```bash
cat runs/$(date +%Y-%m-%d)/brain_plan_morning.json
```

查看是否包含 `execution_prompt_id` 字段。

---

## 🧪 方式 3: 测试文本覆盖(可选)

**警告**: 仅用于测试,生产环境保持默认关闭。

### Step 1: 开启文本覆盖

编辑 `scripts/ai_trader/config/ai_trader.yaml`:

```yaml
policies:
  prompts:
    allow_text_override: true  # 改为 true
```

### Step 2: 创建测试 Plan

```json
{
  "providers": [
    {
      "name": "gemini",
      "execution_prompt_text": "You are a TEST trading agent. This is for testing only. Follow all constraints and output JSON."
    }
  ]
}
```

### Step 3: 运行并检查

```bash
python scripts/ai_trader/run_system.py morning --paper
```

检查文件:
```bash
cat runs/$(date +%Y-%m-%d)/prompts/p0_gemini_*.txt
```

**完成后记得改回**:
```yaml
allow_text_override: false
```

---

## 📊 验证清单

运行测试后,检查以下项目:

### ✅ Orchestrator 层

- [ ] `_prepare_prompt_overrides()` 被调用
- [ ] 如果有 `execution_prompt_id`,应该看到日志: `"Using prompt ID: ..."`
- [ ] 如果启用了文本覆盖,应该创建文件: `runs/YYYY-MM-DD/prompts/`

### ✅ Controller 层

- [ ] 子进程启动命令包含正确的环境变量
- [ ] 查看进程元数据: `cat runs/YYYY-MM-DD/procs/llm_*.json`

### ✅ Bot 层

- [ ] Bot 日志显示: `[Prompt Override] Loaded prompt from file` 或 `Using prompt ID`
- [ ] PromptBuilder 使用了覆盖的 Prompt

### ✅ 实际交易决策

- [ ] Bot 做出了交易决策(BUY/SELL/HOLD)
- [ ] 决策 JSON 格式正确
- [ ] 没有因 Prompt 改变而导致的错误

---

## 🐛 常见问题排查

### 问题 1: 看不到 "Using prompt ID" 日志

**可能原因**:
- Plan 中没有 `execution_prompt_id` 字段
- Brain 没有生成这个字段

**解决方案**:
- 使用方式 1 手动创建 plan 测试
- 检查 `runs/YYYY-MM-DD/brain_plan_morning.json`

### 问题 2: Prompt 文件没有创建

**可能原因**:
- `allow_text_override: false` (默认)
- Plan 中没有 `execution_prompt_text`

**解决方案**:
- 这是正常的!ID 模式不会创建文件
- 如需测试文本模式,参考方式 3

### 问题 3: 子进程没有使用新 Prompt

**可能原因**:
- 环境变量没有正确传递
- PromptBuilder 没有读取环境变量

**调试步骤**:
1. 检查进程元数据: `cat runs/YYYY-MM-DD/procs/llm_*.json`
2. 在 `llm_trader_bot.py` 添加调试日志:
   ```python
   print(f"DEBUG: LLM_PROMPT_ID={os.getenv('LLM_PROMPT_ID')}")
   print(f"DEBUG: LLM_PROMPT_FILE={os.getenv('LLM_PROMPT_FILE')}")
   ```

### 问题 4: Bot 报错 "prompt_id not found in library"

**可能原因**:
- Prompt ID 不在 `prompts/prompt_library.json` 中
- 库文件路径不正确

**解决方案**:
1. 检查 ID 是否存在:
   ```bash
   cat prompts/prompt_library.json | grep "v3_stable"
   ```
2. 如果不存在,添加到库中
3. 使用现有的 ID: `v3_stable`, `v3_fast_market_following`, 等

---

## 📈 A/B 测试示例

### 测试不同 Prompt 的表现

创建 A/B 测试 plan:

```json
{
  "ab": {"champion": 0.7, "challenger": 0.3},
  "providers": [
    {
      "name": "gemini",
      "ab_group": "champion",
      "execution_prompt_id": "v3_stable",
      "cycles": 10
    },
    {
      "name": "gemini",
      "ab_group": "challenger",
      "execution_prompt_id": "v4_experimental",
      "cycles": 10
    }
  ]
}
```

**这会同时运行两个 Bot**:
- 一个使用 `v3_stable` Prompt
- 一个使用 `v4_experimental` Prompt

### 对比结果

查看各自的表现:
```bash
# Champion 的交易
grep "ab_group=champion" logs/*.log

# Challenger 的交易
grep "ab_group=challenger" logs/*.log
```

---

## 🎯 成功标志

如果看到以下内容,说明功能正常工作:

1. ✅ Orchestrator 日志包含 "prompt_override_saved" 或 "Using prompt ID"
2. ✅ 子进程启动时有环境变量 `LLM_PROMPT_ID`
3. ✅ Bot 日志显示 "Prompt Override" 相关消息
4. ✅ Bot 正常做出交易决策
5. ✅ 没有新的异常或错误

---

## 📞 需要帮助?

如果遇到问题:

1. **检查日志**:
   ```bash
   tail -f logs/ai_trader_*.log
   tail -f logs/gemini_*.log
   ```

2. **查看进程状态**:
   ```bash
   ls runs/$(date +%Y-%m-%d)/procs/
   cat runs/$(date +%Y-%m-%d)/hb/llm_*.json
   ```

3. **验证配置**:
   ```bash
   cat scripts/ai_trader/config/ai_trader.yaml | grep -A5 prompts
   ```

4. **重新运行测试**:
   ```bash
   python -m pytest scripts/ai_trader/tests/unit/test_prompt_override.py -v
   ```

---

## 🔄 回滚方式

如果需要临时禁用动态 Prompt:

### 方法 1: 关闭 Brain
```yaml
brain:
  enabled: false
```

### 方法 2: Brain 不输出 prompt 字段

Brain 不提供 `execution_prompt_id/text` 字段时,系统会使用默认 Prompt。

### 方法 3: 使用默认配置

确保:
```yaml
policies:
  prompts:
    allow_text_override: false
```

---

**快速启动指南版本**: 1.0
**最后更新**: 2025-10-31
**适用版本**: AI Brain Dynamic Prompt v1.0
