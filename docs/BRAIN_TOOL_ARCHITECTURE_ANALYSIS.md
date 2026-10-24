# Brain 工具架构分析与 rebuild_features 集成方案

**创建时间**: 2025-11-26
**目的**: 回答"为什么 AI 大脑不能自己回补特征数据?"并推荐最佳实现方案

---

## 一、当前架构概览

### 1.1 两种Brain交互模式

系统中存在**两种不同的模式**让Brain与主机系统交互:

#### **模式 A: Tool Call Pattern (同步工具调用)**
- **用途**: **诊断性**工具,用于Brain在ML Review过程中**调试和验证**
- **执行方式**:
  - Brain输出: `tool_calls: [{"tool": "inspect_table_schema", "args": {...}}]`
  - 主机立即执行工具,将结果作为`tool_results`返回给Brain
  - Brain在**同一轮对话**中使用结果(最多3轮)
- **执行位置**: `scripts/ai_trader/control_event_consumer.py:1875-1912`
- **当前支持的工具**:
  - `inspect_table_schema` - 查看表结构
  - `dry_run_feature_expr` - 测试特征表达式

**代码示例 (control_event_consumer.py:1875-1912)**:
```python
for call in tool_calls:
    tool_name = str(call.get("tool") or "")
    args = call.get("args") or {}
    res = {"tool": tool_name, "args": args, "ok": False}

    if tool_name == "inspect_table_schema":
        from scripts.ai_trader.brain.tools_sql import inspect_table_schema
        table = str(args.get("table") or "trading.features_1m")
        out = inspect_table_schema(table, save=True)
        res["ok"] = True
        res["result"] = out
    elif tool_name == "dry_run_feature_expr":
        from scripts.ai_trader.brain.tools_sql import dry_run_feature_expr
        # ... 执行并返回结果
    else:
        res["error"] = "unknown_tool"

    tool_results.append(res)

# 将结果附加到下一轮输入
ml_ctx_cur["tool_results"] = tool_results
```

**特点**:
- ✅ 快速反馈循环 (同一轮对话)
- ✅ 适合轻量级、只读操作
- ❌ 只在ML Review流程中可用 (不在其他流程)
- ❌ 不适合长时间运行的任务 (会阻塞对话)

---

#### **模式 B: Action Queue Pattern (异步动作队列)**
- **用途**: **修复性**动作,用于Brain**修改系统状态或执行耗时操作**
- **执行方式**:
  - Brain写入: `runs/{date}/pending_actions.yml`
  - Orchestrator在下一次循环时读取并执行
  - 结果记录到日志文件: `runs/{date}/label_recompute_log.json`
  - Brain在**下一天**的morning review中看到结果
- **执行位置**: `scripts/ai_trader/orchestrator.py:5535-5634`
- **当前支持的动作**:
  - `label_recompute` - 重新计算标签 (调用 `DataHealer.ensure_labels_ready`)
  - `disable_rl` - 禁用强化学习
  - `increase_conf_floor` - 提高置信度阈值
  - `reduce_sizing_multiplier` - 降低仓位倍数

**代码示例 (orchestrator.py:5604-5630)**:
```python
elif act == "label_recompute":
    # 执行标签重新计算 via DataHealer
    symbols = params.get("symbols", [])
    horizons = params.get("horizons", [30])
    if symbols and self.data_healer:
        try:
            self.log.info("label_recompute_requested", symbols=symbols, horizons=horizons)
            heal_result = self.data_healer.ensure_labels_ready(
                symbols=symbols,
                horizons=horizons,
                lookback_days=params.get("lookback_days", 90),
                batch_size=params.get("batch_size", 5)
            )
            label_recompute_results.append({
                "symbols": symbols,
                "result": heal_result
            })
            self.log.info("label_recompute_completed",
                          symbols=symbols,
                          status=heal_result.get("status"),
                          generated_rows=heal_result.get("generated_rows", 0))
        except Exception as e:
            self.log.error("label_recompute_failed", symbols=symbols, err=str(e))
```

**特点**:
- ✅ 适合长时间运行的任务 (不阻塞)
- ✅ 可以修改系统状态 (数据库、配置)
- ✅ 全局可用 (不限于ML Review)
- ❌ 反馈延迟 (通常要到第二天)

---

### 1.2 为什么Brain不能直接回补特征?

#### 问题根因:
1. **`tool/load_hist_multi.py` 只下载原始Bar数据**
   - 功能: IBKR → `bars_1m_ibkr` (原始OHLCV数据)
   - **NOT**: 计算特征 (spy_ret_5, atr_14等)

2. **特征计算由 `pipelines/rebuild_features_1m.py` 完成**
   - 功能: `bars_1m_ibkr` → `features_1m` (90+特征列)
   - Brain当前**无法调用此pipeline**

3. **`label_recompute` 动作只处理标签,不处理特征**
   - `DataHealer.ensure_labels_ready()` → 调用 `pipelines/generate_labels.py`
   - **NOT**: `pipelines/rebuild_features_1m.py`

#### 结论:
Brain可以修复**标签缺失** (通过`label_recompute`动作),但不能修复**特征缺失**。

---

## 二、添加 rebuild_features 能力的两种方案

### 方案 A: Tool Call Pattern (不推荐)

#### 实现步骤:
1. 在 `scripts/ai_trader/brain/tools_sql.py` 添加:
```python
def rebuild_features(
    symbols: List[str],
    start_date: str,
    end_date: str,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Rebuild features for specified symbols and date range

    Args:
        symbols: List of symbols (e.g., ["TSLA", "AAPL"])
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        dry_run: If True, only check coverage without writing

    Returns:
        {"ok": bool, "symbols_processed": int, "rows_written": int, "coverage_pct": float}
    """
    import subprocess
    cmd = [
        "venv/bin/python", "pipelines/rebuild_features_1m.py",
        "--symbols"] + symbols + [
        "--start", start_date,
        "--end", end_date
    ]
    if dry_run:
        cmd.append("--dry-run")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    # Parse output and return summary
    return {"ok": result.returncode == 0, ...}
```

2. 在 `control_event_consumer.py:1908` 添加:
```python
elif tool_name == "rebuild_features":
    from scripts.ai_trader.brain.tools_sql import rebuild_features
    symbols = args.get("symbols", [])
    start = args.get("start_date", "")
    end = args.get("end_date", "")
    dry_run = bool(args.get("dry_run", False))
    out = rebuild_features(symbols, start, end, dry_run)
    res["ok"] = True
    res["result"] = out
```

#### 优点:
- ✅ 快速反馈 (同一轮对话)
- ✅ Brain可以立即使用结果决定下一步

#### 缺点:
- ❌ **阻塞ML Review流程** (rebuild可能需要5-30分钟)
- ❌ **只在ML Review流程中可用** (其他流程无法使用)
- ❌ **超时风险** (control_event_consumer可能有超时限制)
- ❌ **不符合设计意图** (Tool Call Pattern设计用于轻量级诊断,非长时间数据处理)

**推荐度**: ⚠️ **不推荐** - 违反架构设计原则

---

### 方案 B: Action Queue Pattern (推荐) ⭐

#### 实现步骤:

**Step 1: 在 DataHealer 添加特征回补方法**

文件: `services/data/data_healer.py`

```python
def ensure_features_ready(
    self,
    symbols: List[str],
    start_date: str,  # YYYY-MM-DD
    end_date: str,    # YYYY-MM-DD
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Ensure features are available for specified symbols and date range

    Similar to ensure_labels_ready(), but rebuilds features instead of labels.

    Args:
        symbols: List of symbols (e.g., ["TSLA", "AAPL", "SPY"])
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        dry_run: If True, only check coverage without rebuilding

    Returns:
        {
            "status": "fixed" | "partial" | "failed" | "ok",
            "symbols_processed": int,
            "rows_written": int,
            "errors": List[str]
        }

    Example:
        >>> healer = DataHealer(runs_dir="runs/ai_trader")
        >>> result = healer.ensure_features_ready(
        ...     symbols=["TSLA", "AAPL"],
        ...     start_date="2025-10-01",
        ...     end_date="2025-11-26"
        ... )
        >>> if result["status"] == "fixed":
        >>>     print(f"Rebuilt {result['rows_written']} feature rows")
    """
    import subprocess
    from datetime import datetime

    self.log.info(
        "ensure_features_ready_start",
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        dry_run=dry_run
    )

    # Validate date format
    try:
        datetime.strptime(start_date, "%Y-%m-%d")
        datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError as e:
        return {"status": "failed", "errors": [f"Invalid date format: {e}"]}

    # Build command
    cmd = [
        "venv/bin/python",
        "pipelines/rebuild_features_1m.py",
        "--symbols"
    ] + symbols + [
        "--start", start_date,
        "--end", end_date
    ]

    if dry_run:
        cmd.append("--dry-run")

    # Execute rebuild
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=7200,  # 2 hours max
            cwd=Path(__file__).parent.parent.parent  # Project root
        )

        if result.returncode == 0:
            # Parse output to extract metrics (look for "✓" markers)
            rows_written = 0
            for line in result.stdout.split('\n'):
                if 'rows written' in line.lower():
                    # Extract number from log line
                    import re
                    match = re.search(r'(\d+)\s+rows', line)
                    if match:
                        rows_written += int(match.group(1))

            self.log.info(
                "ensure_features_ready_success",
                symbols=symbols,
                rows_written=rows_written
            )
            return {
                "status": "fixed",
                "symbols_processed": len(symbols),
                "rows_written": rows_written,
                "errors": []
            }
        else:
            error_msg = result.stderr or result.stdout
            self.log.error(
                "ensure_features_ready_failed",
                symbols=symbols,
                error=error_msg
            )
            return {
                "status": "failed",
                "symbols_processed": 0,
                "rows_written": 0,
                "errors": [error_msg[:500]]  # Truncate for safety
            }

    except subprocess.TimeoutExpired:
        self.log.error("ensure_features_ready_timeout", symbols=symbols)
        return {
            "status": "failed",
            "errors": ["Rebuild timed out after 2 hours"]
        }
    except Exception as e:
        self.log.error("ensure_features_ready_error", symbols=symbols, error=str(e), exc_info=True)
        return {
            "status": "failed",
            "errors": [str(e)]
        }
```

**Step 2: 在 Orchestrator 添加动作处理**

文件: `scripts/ai_trader/orchestrator.py:5579`

```python
# 修改这一行:
supported_actions = {"disable_rl", "increase_conf_floor", "reduce_sizing_multiplier", "label_recompute", "feature_rebuild"}

# 在 orchestrator.py:5630 后添加:
elif act == "feature_rebuild":
    # Execute feature rebuild via DataHealer
    symbols = params.get("symbols", [])
    start_date = params.get("start_date", "")
    end_date = params.get("end_date", "")
    dry_run = bool(params.get("dry_run", False))

    if symbols and start_date and end_date and self.data_healer:
        try:
            self.log.info("feature_rebuild_requested",
                          symbols=symbols,
                          start_date=start_date,
                          end_date=end_date,
                          dry_run=dry_run)

            rebuild_result = self.data_healer.ensure_features_ready(
                symbols=symbols,
                start_date=start_date,
                end_date=end_date,
                dry_run=dry_run
            )

            # Log to dedicated file (similar to label_recompute)
            feature_rebuild_results.append({
                "symbols": symbols,
                "date_range": f"{start_date} to {end_date}",
                "result": rebuild_result
            })

            self.log.info("feature_rebuild_completed",
                          symbols=symbols,
                          status=rebuild_result.get("status"),
                          rows_written=rebuild_result.get("rows_written", 0))
        except Exception as e:
            self.log.error("feature_rebuild_failed",
                           symbols=symbols,
                           err=str(e),
                           exc_info=True)
            feature_rebuild_results.append({
                "symbols": symbols,
                "result": {"status": "error", "error": str(e)}
            })
    else:
        self.log.warning("feature_rebuild_skipped_invalid_params",
                         symbols=symbols,
                         start_date=start_date,
                         end_date=end_date)

# 在 orchestrator.py:5634 后添加结果日志:
# Log feature rebuild results
feature_rebuild_results = []  # 初始化 (在5582行附近)

if feature_rebuild_results:
    feature_rebuild_log_path = self.runs_dir / today.isoformat() / "feature_rebuild_log.json"
    feature_rebuild_log_path.parent.mkdir(parents=True, exist_ok=True)
    feature_rebuild_log_path.write_text(json.dumps(feature_rebuild_results, indent=2), "utf-8")
    self.log.info("feature_rebuild_log_written", path=str(feature_rebuild_log_path))
```

**Step 3: Brain使用示例**

Brain在morning review中发现特征缺失时,可以写入:

文件: `runs/2025-11-26/pending_actions.yml`
```yaml
pending:
  - action: feature_rebuild
    reason: "spy_ret_5 coverage is 0% for last 90 days, blocking HPO training"
    params:
      symbols: ["TSLA", "NVDA", "SPY", "AAPL", "MSFT", "AMD", "META", "AMZN", "GOOGL", "QQQ", "TQQQ", "SQQQ", "VOO", "ORCL", "INTC"]
      start_date: "2025-08-28"  # 90 days ago
      end_date: "2025-11-26"    # Today
      dry_run: false
```

下一天,Brain从 `runs/2025-11-26/feature_rebuild_log.json` 看到结果:
```json
[
  {
    "symbols": ["TSLA", "NVDA", "SPY", ...],
    "date_range": "2025-08-28 to 2025-11-26",
    "result": {
      "status": "fixed",
      "symbols_processed": 15,
      "rows_written": 2847520,
      "errors": []
    }
  }
]
```

#### 优点:
- ✅ **不阻塞ML Review** (异步执行)
- ✅ **全局可用** (任何流程都可触发)
- ✅ **符合架构设计** (Action Queue Pattern专为长时间修复性任务设计)
- ✅ **与现有 `label_recompute` 完全一致** (代码结构相同,易于维护)
- ✅ **支持批量重试** (Brain可以多次请求直到成功)
- ✅ **结果可审计** (所有操作记录在日志文件)

#### 缺点:
- ⚠️ 反馈延迟 (通常要到第二天)
  - **缓解**: 可以添加Slack通知或Prometheus指标实时反馈

**推荐度**: ⭐⭐⭐⭐⭐ **强烈推荐** - 完美符合系统架构

---

## 三、推荐方案总结

### 最终推荐: 方案 B (Action Queue Pattern)

**理由**:
1. **架构一致性**: 与现有 `label_recompute` 完全对称
2. **可扩展性**: 未来可以添加更多数据修复动作 (如 `raw_data_backfill`)
3. **可靠性**: 不会因为长时间运行导致超时或阻塞
4. **可观测性**: 所有操作记录在日志,方便排查问题

### 实现清单

- [ ] 在 `services/data/data_healer.py` 添加 `ensure_features_ready()` 方法
- [ ] 在 `scripts/ai_trader/orchestrator.py` 添加 `feature_rebuild` 动作处理
- [ ] 在 `scripts/ai_trader/orchestrator.py` 初始化 `feature_rebuild_results = []`
- [ ] 添加结果日志写入: `feature_rebuild_log.json`
- [ ] 更新 Brain prompt (告知新动作可用)
- [ ] 添加单元测试: `tests/services/data/test_data_healer_features.py`
- [ ] 添加集成测试: `tests/integration/test_feature_rebuild_action.py`
- [ ] 更新文档: `docs/BRAIN_ACTIONS.md`

### 预期工作量
- 开发: 2-3小时
- 测试: 1-2小时
- 文档: 30分钟
- **总计**: 3-6小时

---

## 四、对比表格

| 维度 | Tool Call Pattern | Action Queue Pattern | 推荐 |
|------|-------------------|----------------------|------|
| **执行速度** | 同步,立即反馈 | 异步,下一轮反馈 | - |
| **适用任务** | 轻量级诊断 (<1分钟) | 长时间修复 (>5分钟) | ✅ Action Queue |
| **阻塞风险** | 阻塞ML Review | 不阻塞 | ✅ Action Queue |
| **全局可用性** | 仅ML Review流程 | 所有流程 | ✅ Action Queue |
| **架构一致性** | 新模式 | 与label_recompute一致 | ✅ Action Queue |
| **可审计性** | 无持久化日志 | 完整日志文件 | ✅ Action Queue |
| **代码复杂度** | 简单 | 简单 (复用现有模式) | - |

---

## 五、澄清用户误解

### 问题: "为什么 ai 大脑不能自己用tool/load_hist_multi.py去回补数据?之前是可以的啊"

**澄清**:
1. **`tool/load_hist_multi.py` 不是特征回补工具**
   - 功能: 从IBKR下载原始Bar数据 → 写入 `bars_1m_ibkr` 表
   - **NOT**: 计算特征 (spy_ret_5, atr_14等)

2. **Brain可以下载原始数据,但不能计算特征**
   - Brain可能之前用过 `load_hist_multi.py` 下载原始Bar
   - 但特征计算由 `rebuild_features_1m.py` 完成,Brain当前无法调用

3. **`label_recompute` 只修复标签,不修复特征**
   - 标签 (labels): 未来收益率 (用于训练目标)
   - 特征 (features): 技术指标、市场信号 (用于模型输入)
   - 两者是不同的pipeline

### 结论:
Brain需要新的 `feature_rebuild` 动作来修复特征缺失问题。

---

## 六、后续优化建议

### 6.1 实时反馈优化
当前Action Queue Pattern反馈延迟到第二天,可以添加:
- Slack通知: 重建完成时发送消息
- Prometheus指标: `feature_rebuild_rows_written`, `feature_rebuild_duration_seconds`
- Brain主动检查: 在ML Review时查询 `feature_rebuild_log.json` (如果存在)

### 6.2 智能批量处理
Brain可以自动将大任务拆分:
```yaml
pending:
  - action: feature_rebuild
    params:
      symbols: ["TSLA", "NVDA", "SPY", "AAPL", "MSFT"]  # Batch 1
      start_date: "2025-10-27"
      end_date: "2025-11-26"
  - action: feature_rebuild
    params:
      symbols: ["AMD", "META", "AMZN", "GOOGL", "QQQ"]  # Batch 2
      start_date: "2025-10-27"
      end_date: "2025-11-26"
```

### 6.3 预检机制
在执行前先dry-run检查:
```yaml
- action: feature_rebuild
  params:
    symbols: ["TSLA"]
    start_date: "2025-10-01"
    end_date: "2025-11-26"
    dry_run: true  # 先检查覆盖率,不实际写入
```

---

**文档版本**: 1.0
**最后更新**: 2025-11-26
**维护者**: Trading Team
