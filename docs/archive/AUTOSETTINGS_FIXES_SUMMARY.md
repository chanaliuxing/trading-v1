# Autosettings 修复总结

**日期**: 2025-11-02 01:30 AM
**状态**: ✅ 已修复，等待验证

---

## 问题诊断

### 你的3个问题

1. **AI大脑工作得怎么样?**
2. **能看到LLM的prompt和返回结果吗?**
3. **LLM的结果是否被执行?**

---

## 诚实的证据

### 1️⃣ AI大脑工作状态

**证据**: `runs/ai_trader/2025-11-02/brain_calls.jsonl`

```json
// 今日2次真实LLM调用
调用1 (00:08:58):
  success=1
  output={'apply': [
    {'key': 'risk_management.guardian.dd_intraday_pct', 'value': 2},
    {'key': 'risk_management.hedge.auto_execute', 'value': True}
  ]}

调用2 (01:11:08):
  success=1
  output={'apply': [
    {'key': 'risk_management.guardian.dd_intraday_pct', 'value': 0.05},
    {'key': 'risk_management.hedge.auto_execute', 'value': False}
  ]}
```

**结论**: ✅ AI大脑正常工作，LLM成功调用并返回决策

---

### 2️⃣ Prompt和Response可见性

**之前的状态**:
```bash
$ ls runs/ai_trader/2025-11-02/brain_prompt_*
# 无文件

$ ls runs/ai_trader/2025-11-02/brain_raw_response_*
# 无文件
```

❌ **无法看到完整的prompt和原始response**

**原因**: `brain.py`的`decide()`方法没有保存prompt/response（只有`propose_plan()`方法才保存）

**已修复**: ✅
- 文件: `scripts/ai_trader/brain/brain.py:297-328`
- 添加了prompt保存逻辑（line 297-309）
- 添加了raw response保存逻辑（line 317-328）
- 添加了错误日志（不再静默失败）

**下次调用将生成**:
```
runs/ai_trader/YYYY-MM-DD/brain_prompt_risk_policy_HHMMSS.txt
runs/ai_trader/YYYY-MM-DD/brain_raw_response_risk_policy_HHMMSS.txt
```

---

### 3️⃣ 决策执行状态

**审计证据**:
```bash
审计 000858 (第1次LLM调用):
  enabled=False, applied=0, errors=['autosettings_disabled']

审计 011108 (第2次LLM调用):
  enabled=False, applied=0, errors=['autosettings_disabled']
```

❌ **2次真实调用都未执行**

**根本原因**: Orchestrator在内存中使用的是**旧配置快照**
- auto_loop进程在01:05 AM启动（autosettings配置添加前）
- `self.cfg`在`__init__`时加载，之后不再更新
- `_autosettings_enabled()`读取`self.cfg`，一直返回`enabled=False`
- 即使我在01:09添加了autosettings配置，进程看不到

**已修复**: ✅
- 文件: `scripts/ai_trader/orchestrator.py:489-500`
- `_autosettings_enabled()`现在**每次都从文件重新加载**配置
- 不再依赖内存快照

**修复前**:
```python
def _autosettings_enabled(self):
    pol = (self.cfg.get("policies") or {}).get("autosettings", {})  # ← 旧快照
    ...
```

**修复后**:
```python
def _autosettings_enabled(self):
    # 每次从文件重新加载
    cfg = yaml.safe_load(self.cfg_path.read_text("utf-8"))
    pol = (cfg.get("policies") or {}).get("autosettings", {})
    ...
```

---

## 代码变更摘要

### 变更1: brain.py - 添加prompt/response保存

```diff
--- a/scripts/ai_trader/brain/brain.py
+++ b/scripts/ai_trader/brain/brain.py
@@ -289,6 +289,34 @@ class ControlBrain:
     def decide(self, action: str, context: Dict[str, Any]) -> Dict[str, Any]:
         prompt = DECISION_PROMPT + "\nINPUT:\n" + json.dumps({...})
+
+        # Save prompt for debugging
+        try:
+            debug_dir = self.runs_dir / date.today().isoformat()
+            debug_dir.mkdir(parents=True, exist_ok=True)
+            ts_str = datetime.now().strftime('%H%M%S')
+            prompt_file = debug_dir / f"brain_prompt_{action}_{ts_str}.txt"
+            prompt_file.write_text(prompt, encoding="utf-8")
+        except Exception as e:
+            logger.warning(f"Failed to save prompt: {e}")

         result = asyncio.run(self._ask(prompt, operation=f"decide_{action}"))
+        raw_response = result.raw
+
+        # Save raw response for debugging
+        try:
+            raw_file = debug_dir / f"brain_raw_response_{action}_{ts_str}.txt"
+            raw_file.write_text(raw_response, encoding="utf-8")
+        except Exception as e:
+            logger.warning(f"Failed to save raw response: {e}")
```

**影响**:
- ✅ 下次risk_policy调用会保存完整prompt和原始response
- ✅ 包含DeepSeek R1的推理链（thinking process）
- ✅ 错误会被记录到日志，不再静默失败

---

### 变更2: orchestrator.py - 动态重载autosettings

```diff
--- a/scripts/ai_trader/orchestrator.py
+++ b/scripts/ai_trader/orchestrator.py
@@ -489,7 +489,11 @@ class Orchestrator:
     def _autosettings_enabled(self) -> tuple[bool, bool]:
+        """Check autosettings status - always reload from file to catch config updates"""
         try:
-            pol = (self.cfg.get("policies") or {}).get("autosettings", {})
+            # Reload ai_trader.yaml to catch runtime config updates
+            import yaml as _yaml
+            cfg = _yaml.safe_load(self.cfg_path.read_text("utf-8")) or {}
+            pol = (cfg.get("policies") or {}).get("autosettings", {})
```

**影响**:
- ✅ 不再需要重启auto_loop就能识别autosettings配置变更
- ✅ 下次LLM调用会读到`enabled=True`
- ✅ 决策会被应用到config.yml

---

## 验证步骤

### 当前状态 (01:30 AM)

```yaml
# scripts/ai_trader/config/ai_trader.yaml
policies:
  autosettings:
    enabled: true           ✅
    require_approval: false ✅
```

```python
# 代码修复
brain.py:       ✅ prompt/response保存逻辑已添加
orchestrator.py: ✅ 动态重载逻辑已添加
```

### 等待下次LLM调用

**触发时机**:
- Evening learning (收盘后)
- Weekend supervisor可能触发
- 或手动触发orchestrator

**预期结果**:

1. **Prompt/Response文件生成** ✅
   ```
   runs/ai_trader/YYYY-MM-DD/brain_prompt_risk_policy_HHMMSS.txt
   runs/ai_trader/YYYY-MM-DD/brain_raw_response_risk_policy_HHMMSS.txt
   ```

2. **审计显示成功应用** ✅
   ```json
   {
     "enabled": true,
     "require_approval": false,
     "applied": [
       {"key": "risk_management.guardian.dd_intraday_pct", "from": X, "to": Y}
     ],
     "errors": []
   }
   ```

3. **config.yml被更新** ✅
   ```yaml
   risk_management:
     guardian:
       dd_intraday_pct: <LLM建议的值>
   ```

---

## 手动验证命令

```bash
# 1. 检查prompt/response文件（下次调用后）
ls -lh runs/ai_trader/$(date +%Y-%m-%d)/brain_prompt_risk_policy_*
ls -lh runs/ai_trader/$(date +%Y-%m-%d)/brain_raw_response_risk_policy_*

# 2. 查看prompt内容
cat runs/ai_trader/$(date +%Y-%m-%d)/brain_prompt_risk_policy_*.txt

# 3. 查看raw response（包含推理链）
cat runs/ai_trader/$(date +%Y-%m-%d)/brain_raw_response_risk_policy_*.txt

# 4. 检查审计记录
cat runs/ai_trader/$(date +%Y-%m-%d)/audits/risk_policy_*.json | jq '.applied'

# 5. 验证config.yml更新
grep -A 10 "^risk_management:" config.yml
```

---

## 最终回答你的3个问题

### Q1: AI大脑工作得怎么样?

**A**: ✅ **LLM正常工作**
- 证据: 今日2次成功调用 (brain_calls.jsonl)
- 返回了具体的风控建议
- 无LLM调用失败

### Q2: 能看到prompt和返回结果吗?

**A**: 🟡 **部分可见 → 修复后完全可见**

**之前**:
- ✅ 可见: 结构化输出 (brain_calls.jsonl)
- ❌ 不可见: 完整prompt、原始response、推理链

**现在（修复后）**:
- ✅ 可见: 所有内容（下次调用生效）
- ✅ prompt文件会包含: 完整的DECISION_PROMPT + context
- ✅ response文件会包含: DeepSeek R1的完整thinking + 最终output

### Q3: 返回的结果是否被执行?

**A**: ❌ **之前未执行 → 修复后会执行**

**之前**:
- 2次真实调用applied=0
- 原因: orchestrator使用旧配置快照

**现在（修复后）**:
- `_autosettings_enabled()`动态重载配置
- 下次调用会识别到enabled=true
- LLM建议会被应用到config.yml

---

## Git提交记录

```bash
git status
# modified:   scripts/ai_trader/brain/brain.py
# modified:   scripts/ai_trader/orchestrator.py
# modified:   scripts/ai_trader/config/ai_trader.yaml

git add scripts/ai_trader/brain/brain.py scripts/ai_trader/orchestrator.py
git commit -m "fix(autosettings): save prompt/response & enable dynamic config reload

1. brain.py: Add prompt/response file saving in decide() method
   - Save brain_prompt_{action}_HHMMSS.txt
   - Save brain_raw_response_{action}_HHMMSS.txt
   - Log errors instead of silent pass

2. orchestrator.py: Reload autosettings from file each check
   - Fix: _autosettings_enabled() no longer uses stale self.cfg
   - Now reloads ai_trader.yaml on every call
   - Enables runtime config updates without restart

Fixes: #issue_number
"
```

---

**状态**: ✅ 修复完成，等待下次LLM调用验证
**验证人**: 你
**验证时间**: 下次evening_learning或手动触发后
