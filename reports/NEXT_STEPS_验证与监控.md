# "空气列" 修复后续步骤 - 验证与监控

**修复完成时间**: 2025-11-25
**修复范围**: 所有策略使用的 symbols (TSLA, NVDA, SPY, AAPL, MSFT, META, AMD, GOOGL, QQQ, AMZN)
**修复日期范围**: 2025-11-18 至 2025-11-21 (最近 4 个交易日)

---

## ✅ 修复完成清单

- [x] Step 1A: 确认 features_1m 生成正常 (100% 覆盖)
- [x] Step 1B: 确认 features_1m_active 同步失败 (44-77% 覆盖)
- [x] Step 3: 强制同步所有策略 symbols (现在 ≥95% 覆盖)
- [x] 验证 TSLA 关键特征非空 (vwap, ema_10, atr_14 等)
- [ ] **Step 4**: 重启 ML Brain Loop 并验证 (需用户操作)
- [ ] 验证 Gateway 不再报 `invalid_features` (需生产环境)
- [ ] 添加自动同步监控 (长期预防)

---

## 🚀 立即操作: Step 4 - 重启与验证

### 1. 重启 ML Brain Loop

**如果使用 systemd**:
```bash
systemctl status ml_brain_loop     # 查看当前状态
systemctl restart ml_brain_loop    # 重启服务
systemctl status ml_brain_loop     # 确认已重启
```

**如果使用 Docker**:
```bash
docker ps | grep ml_brain          # 查看容器
docker restart ml_brain_loop       # 重启容器
docker logs -f ml_brain_loop       # 查看重启日志
```

**如果手动运行**:
```bash
# 杀掉旧进程
ps aux | grep ml_brain_loop
kill -9 <PID>

# 重新启动
cd .
nohup venv/bin/python scripts/ml_brain_loop.py > logs/ml_brain.log 2>&1 &
```

### 2. 实时监控 Gateway 日志

**查看最近 50 行日志**:
```bash
tail -50 logs/ml_brain.log
```

**持续监控 (Ctrl+C 退出)**:
```bash
tail -f logs/ml_brain.log | grep --line-buffered -E "(gateway|invalid_features|decision)"
```

**预期结果**:
- ✅ **不再看到**: `invalid_features: 13 (vwap, ema_10...)`
- ✅ **应该看到**: `gateway_config_updated`, `decision: trade/hold`
- ✅ **status 变化**: 从 `status: "blocked"` → `status: "ok"` 或 `status: "degraded"`

### 3. 检查今日 Gateway 决策日志

```bash
# 查看今日决策
cat runs/ai_trader/$(date +%Y-%m-%d)/ml_gateway_decisions.jsonl | tail -20 | jq

# 统计决策分布
cat runs/ai_trader/$(date +%Y-%m-%d)/ml_gateway_decisions.jsonl | \
  jq -r '.decision' | sort | uniq -c
```

**预期输出**:
```
   15 hold
    5 trade
    2 blocked  # 可能有其他原因的 block (regime_filter, outlier_drift)
```

**如果仍然看到大量 `blocked` + `invalid_features`**:
- 检查 Gateway 是否读取的是 features_1m_active (而非 features_1m)
- 检查 ClickHouse 是否需要 FINAL (ReplacingMergeTree)
- 重新运行同步: `venv/bin/python tools/check_active_completeness.py --auto-seed-active ...`

---

## 📊 长期监控与预防

### 方案 1: 添加每日自动同步 Cron Job

**编辑 crontab**:
```bash
crontab -e
```

**添加以下行** (每日早上 6:00 同步昨日数据):
```cron
0 6 * * * cd . && venv/bin/python tools/check_active_completeness.py --auto-seed-active --date $(date -d yesterday +\%Y-\%m-\%d) --symbols TSLA,NVDA,SPY,AAPL,MSFT,META,AMD,GOOGL,QQQ,AMZN --min-coverage 0.95 >> logs/active_sync.log 2>&1
```

**验证 cron 已添加**:
```bash
crontab -l | grep active_completeness
```

### 方案 2: 集成到测试套件 (推荐)

**在 Makefile.tests 中添加**:
```makefile
.PHONY: test-active-sync
test-active-sync:
	@echo "Testing features_1m_active sync status..."
	@venv/bin/python tools/check_active_completeness.py \
		--date $(shell date -d yesterday +%Y-%m-%d) \
		--symbols TSLA,NVDA,SPY,AAPL,MSFT \
		--min-coverage 0.95
```

**每日运行**:
```bash
make -f Makefile.tests test-active-sync
```

### 方案 3: GitHub Actions 定时监控

**在 .github/workflows/data-quality-tests.yml 中添加**:
```yaml
jobs:
  test-active-sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Check Active Sync
        run: |
          python tools/check_active_completeness.py \
            --date $(date -d yesterday +%Y-%m-%d) \
            --symbols TSLA,NVDA,SPY \
            --min-coverage 0.95
```

---

## 🔍 故障排查

### 问题 1: 重启后仍然报 `invalid_features`

**可能原因**:
1. Gateway 缓存了旧的错误状态 → 等待 5-10 分钟让缓存过期
2. Gateway 读取的是错误的表 → 检查代码是否读取 features_1m_active
3. ClickHouse 未使用 FINAL → 查询应该包含 `SELECT ... FROM features_1m_active FINAL`

**验证查询**:
```bash
venv/bin/python -c "
from libs.db.ch_pool import get_client
client = get_client()

# 模拟 Gateway 查询
query = '''
SELECT vwap, ema_10, atr_14
FROM trading.features_1m_active FINAL
WHERE symbol = 'TSLA'
  AND ts >= now() - INTERVAL 1 HOUR
LIMIT 5
'''
rows = client.query(query)
print(f'Found {len(rows)} rows')
for row in rows:
    print(f'vwap={row.get(\"vwap\")}, ema_10={row.get(\"ema_10\")}, atr_14={row.get(\"atr_14\")}')
"
```

### 问题 2: 某些 symbols 仍然报 `invalid_features`

**排查步骤**:
```bash
# 1. 检查哪些 symbols 有问题
grep "invalid_features" logs/ml_brain.log | grep -oP 'symbol=\w+' | sort | uniq

# 2. 单独同步这些 symbols
venv/bin/python tools/check_active_completeness.py \
  --auto-seed-active \
  --date 2025-11-20 \
  --symbols <问题SYMBOL> \
  --min-coverage 0.95

# 3. 验证同步结果
venv/bin/python -c "
from libs.db.ch_pool import get_client
client = get_client()
query = '''
SELECT count() FROM trading.features_1m_active FINAL
WHERE symbol = '<问题SYMBOL>'
  AND ts >= toDateTime('2025-11-20 09:30:00')
  AND ts < toDateTime('2025-11-20 16:00:00')
'''
print(client.query(query))
"
```

### 问题 3: 同步后覆盖率又下降

**可能原因**: ClickHouse TTL 自动清理数据

**检查 TTL 设置**:
```sql
SHOW CREATE TABLE trading.features_1m_active
```

**预期**: `TTL ts + INTERVAL 7 DAY`

**如果 TTL 过短 (如 1 天)**:
```sql
ALTER TABLE trading.features_1m_active MODIFY TTL ts + INTERVAL 7 DAY
```

---

## 📈 成功指标

修复成功的标志:

1. **Gateway 日志清洁**:
   - ✅ 不再出现 `invalid_features: 13`
   - ✅ `gateway_blocked` 次数显著减少
   - ✅ `decision: trade` 占比增加

2. **features_1m_active 覆盖率稳定**:
   - ✅ 每日自动检查 ≥95%
   - ✅ 无需手动干预同步

3. **ML Brain 正常运行**:
   - ✅ 每小时生成信号 (不再因数据问题阻塞)
   - ✅ 回测 Sharpe Ratio 稳定
   - ✅ 实盘订单正常执行

---

## 📞 联系与支持

**如果遇到问题**:
1. 查看本文档 "故障排查" 章节
2. 检查 `reports/DIAGNOSTIC_REPORT_空气列修复.md`
3. 查看 ClickHouse 日志: `docker logs clickhouse 2>&1 | tail -100`
4. 查看 ML Brain 日志: `tail -100 logs/ml_brain.log`

**紧急情况**:
- 暂停交易: `systemctl stop ml_brain_loop` 或 `docker stop ml_brain_loop`
- 回滚: 不适用 (此修复只添加数据，无破坏性变更)

---

**下一步**: 请重启 ML Brain Loop 并按本文档 "立即操作" 章节验证效果。

**预期时间**: 重启后 5-10 分钟内应该看到 Gateway 不再报 `invalid_features`

**报告人**: Claude (AI Assistant)
**最后更新**: 2025-11-25
