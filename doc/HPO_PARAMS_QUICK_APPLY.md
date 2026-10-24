# HPO参数应用 - 快速参考

> **最快速度将HPO优化参数部署到实盘**

---

## ⚡ 3步快速部署

### Step 1: 运行HPO优化
```bash
venv/bin/python hpo_cli_upgraded.py \
  --config config/hpo_tsla_v7_2.json \
  --study-name production-hpo \
  --runner wf_hpo:evaluate_trial \
  --backend optuna-tpe \
  --n-trials 50 \
  --n-jobs 4 \
  --seed 2025
```

✅ 输出: `production-hpo_best_params.json`

---

### Step 2: 应用参数
```bash
# 推荐: 生成新配置文件
venv/bin/python apply_hpo_params.py \
  --hpo-params production-hpo_best_params.json \
  --strategy-config strategies/configs/tsla_v7_2.json \
  --output strategies/configs/tsla_v7_2_optimized.json
```

✅ 输出: `strategies/configs/tsla_v7_2_optimized.json`

---

### Step 3: 更新系统配置

**修改 `config.yml`:**
```yaml
strategies:
  - id: "tsla_v7_2_001"
    params:
      config_file: "strategies/configs/tsla_v7_2_optimized.json"  # ← 改这里
```

**重启系统:**
```bash
docker-compose restart trading-bot
# 或
make down && make up
```

---

## 📊 参数对比（示例）

| 参数 | 优化前 | HPO优化后 | 变化 |
|------|--------|----------|------|
| `A1_thr_sigma` | 0.0130 | 0.0060 | ↓ 53.8% (更激进) |
| `A1_rvol_hi` | 1.8 | 1.6 | ↓ 11.1% (收紧) |
| `A2_grid_g` | 1.07 | 0.8 | ↓ 25.2% (网格更密) |
| `A2_oneway_thr` | 15.0 | 2.2 | ↓ 85.3% (降低门槛) |
| `k_atr` | 1.4 | 1.6 | ↑ 14.3% (止损放宽) |

---

## ✅ 验证清单

部署前必查:

- [ ] HPO Sharpe > 0
- [ ] 查看 `_hpo_metadata` 确认参数正确应用
- [ ] 回测新参数（可选但推荐）
- [ ] Paper Trading测试1天（强烈推荐）
- [ ] 监控首日实盘日志

---

## 🔄 回滚操作

如果新参数表现不佳:

```bash
# 方法1: 使用自动备份
cp strategies/configs/tsla_v7_2.json.backup_20251023_134759 \
   strategies/configs/tsla_v7_2.json

# 方法2: git回滚
git checkout HEAD~1 strategies/configs/tsla_v7_2.json

# 重启系统
docker-compose restart trading-bot
```

---

## 📞 相关命令

```bash
# 查看最佳参数
cat production-hpo_best_params.json | jq

# 对比新旧配置
diff <(jq . strategies/configs/tsla_v7_2.json) \
     <(jq . strategies/configs/tsla_v7_2_optimized.json)

# 查看应用历史
cat strategies/configs/tsla_v7_2_optimized.json | jq '._hpo_metadata'

# 验证参数范围
cat strategies/configs/tsla_v7_2_optimized.json | jq '{
  k_atr,
  "A1.thr_sigma": .A1.thr_sigma,
  "A2.grid_g": .A2.grid_g
}'
```

---

## 🎯 一键命令（完整流程）

```bash
# 优化 + 应用 + 验证
STUDY_NAME="hpo-$(date +%Y%m%d)"

# 1. 运行HPO
venv/bin/python hpo_cli_upgraded.py \
  --config config/hpo_tsla_v7_2.json \
  --study-name $STUDY_NAME \
  --runner wf_hpo:evaluate_trial \
  --backend optuna-tpe \
  --n-trials 50 \
  --n-jobs 4 \
  --seed 2025 && \

# 2. 应用参数
venv/bin/python apply_hpo_params.py \
  --hpo-params ${STUDY_NAME}_best_params.json \
  --strategy-config strategies/configs/tsla_v7_2.json \
  --output strategies/configs/tsla_v7_2_optimized.json && \

# 3. 显示结果
echo "✅ HPO完成!" && \
cat ${STUDY_NAME}_best_params.json | jq && \
echo "\n📝 下一步: 更新config.yml并重启系统"
```

---

**完整文档**: `HPO_DEPLOYMENT_GUIDE.md`
