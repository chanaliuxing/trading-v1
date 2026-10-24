# Alpha Loop - 快速启动指南

> **状态**: 已配置完成，测试运行中
> **配置**: `alpha_config_tsla_robust.json`
> **时间**: 2025-10-24

---

## ✅ 已完成配置

### 1. 创建的文件

| 文件 | 用途 | 状态 |
|------|------|------|
| `alpha_config_tsla_robust.json` | Alpha Loop 配置 (基于你的数据优化) | ✅ 已创建 |
| `ALPHA_LOOP_GUIDE.md` | 完整使用指南 | ✅ 已创建 |
| `ALPHA_LOOP_QUICK_START.md` | 本文件 - 快速参考 | ✅ 已创建 |

### 2. 关键改进

基于你的发现，配置已优化：

✅ **时间范围**: 使用 7-8 月低波动期 (避开 10 月 ATR spike)
✅ **k_atr 范围**: 2.0-4.5 (vs 原来 1.6-2.4)
✅ **多 Fold**: 3 个 walk-forward folds
✅ **约束合理**: min_trades=5, max_mdd=0.50
✅ **AI 配置**: GPT-4o-mini + Random Forest + Optuna TPE

---

## 🚀 当前运行状态

**测试运行** (正在后台执行):
```bash
Background Job ID: bbb3b9
Command: alpha_loop.py --provider optuna --gens 1
Output: alpha_out/quick_test/
```

### 监控进度

```bash
# 方法 1: 查看后台任务输出
# (使用 BashOutput tool with bash_id: bbb3b9)

# 方法 2: 查看日志文件
tail -f alpha_out/quick_test/alpha_loop.log

# 方法 3: 检查输出目录
ls -lh alpha_out/quick_test/
```

---

## 📋 运行命令速查

### 快速测试 (5-10分钟)

```bash
venv/bin/python alpha_loop.py \
  --config alpha_config_tsla_robust.json \
  --provider optuna \
  --gens 1 \
  --out_dir alpha_out/quick_test
```

### 完整优化 - 推荐 (3-6小时)

```bash
venv/bin/python alpha_loop.py \
  --config alpha_config_tsla_robust.json \
  --provider coop_optuna \
  --gens 3 \
  --out_dir alpha_out/production_run \
  --k-top 15
```

### 仅 LLM 驱动

```bash
venv/bin/python alpha_loop.py \
  --config alpha_config_tsla_robust.json \
  --provider llm \
  --gens 2 \
  --out_dir alpha_out/llm_run
```

---

## 📊 结果查看

### 查看最佳参数

```bash
# 快速测试结果
cat alpha_out/quick_test/gen_01/gen_summary.json | jq '.best_params'

# 完整运行结果
cat alpha_out/production_run/gen_03/gen_summary.json | jq '.stage2.best_params'
```

### 查看 OOS 性能

```bash
cat alpha_out/quick_test/gen_01/gen_summary.json | jq '.oos_summary'
```

### 查看所有代汇总

```bash
cat alpha_out/production_run/index.json | jq '.'
```

---

## 🎯 下一步行动

### 测试完成后

1. **检查快速测试结果**
```bash
cat alpha_out/quick_test/gen_01/gen_summary.json
```

2. **如果结果满意，运行完整优化**
```bash
venv/bin/python alpha_loop.py \
  --config alpha_config_tsla_robust.json \
  --provider coop_optuna \
  --gens 3 \
  --out_dir alpha_out/production_run \
  --k-top 15
```

3. **应用最佳参数**
```bash
# 提取最佳参数
cat alpha_out/production_run/gen_03/gen_summary.json | \
  jq '.stage2.best_params' > alpha_final_params.json

# 应用到策略
venv/bin/python apply_hpo_params.py \
  --hpo-params alpha_final_params.json \
  --strategy-config strategies/configs/tsla_v7_2.json \
  --output strategies/configs/tsla_v7_2_alpha.json
```

---

## 🔧 故障排查

### 如果快速测试失败

**检查日志**:
```bash
tail -100 alpha_out/quick_test/alpha_loop.log
```

**常见问题**:

1. **OpenAI API Key 错误**
```bash
export OPENAI_API_KEY="sk-xxx"
```

2. **数据库连接失败**
```bash
# 检查 ClickHouse
curl http://localhost:8123/ping

# 检查 PostgreSQL (for Optuna storage)
psql -h localhost -U postgres -d test -c "SELECT 1"
```

3. **所有 trials pruned**
   - 编辑配置，放宽约束:
     ```json
     {
       "objective": {
         "min_trades": 2,
         "max_mdd": 0.60
       }
     }
     ```

---

## 📈 预期结果

### 快速测试 (Optuna, 1 代)

```
运行时间: 5-10 分钟
Trials: 50
输出:
  - best_params: {...}
  - IS Sharpe: 0.8-1.5
  - OOS Sharpe: 0.5-1.2
  - Trades: 10-30
```

### 完整优化 (Coop+Optuna, 3 代)

```
运行时间: 3-6 小时
总 Trials: ~150-200
输出:
  - Gen 1 (Coop): 探索广度
    → 发现潜在优势区域
  - Gen 2 (Optuna): 在最优区域精细搜索
    → IS Sharpe: 1.2-1.8
  - Gen 3 (Optuna): 深度挖掘
    → OOS Sharpe: 1.0-1.5
    → Trades: 20-40
    → 稳健参数 (3 fold 验证)
```

---

## 📚 完整文档

详细使用请参考:
- **`ALPHA_LOOP_GUIDE.md`** - 完整指南
- **`HPO_IMPROVED_GUIDE.md`** - 基础 HPO 改进
- **`HPO_PROCESS_EXPLAINED.md`** - HPO 原理

---

**当前状态**: ✅ 配置完成, 快速测试运行中

**等待测试完成后**, 查看结果并决定是否运行完整优化！

**预计完成时间**: ~10分钟 (快速测试)

**监控命令**:
```bash
# 实时查看进度
tail -f alpha_out/quick_test/alpha_loop.log | grep -E "Trial|Best|Sharpe"
```
