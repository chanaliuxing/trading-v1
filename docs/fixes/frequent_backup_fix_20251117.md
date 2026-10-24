# 数据库频繁备份问题修复

**日期:** 2025-11-17
**问题:** AI 大脑 auto_loop 运行时,每 15-30 分钟就触发一次数据库备份,而不是预期的每天一次

---

## 问题症状

### 备份频率异常
- **11月6日**: 21次备份 (平均每25分钟一次)
- **11月17日**: 12次备份 (平均每25分钟一次)
- **预期行为**: 每天1次 (下午 4:01 PM)

### 备份时间示例 (11月6日)
```
16:22 → 16:45 (23分钟)
16:45 → 17:13 (28分钟)
17:13 → 17:40 (27分钟)
17:40 → 18:06 (26分钟)
18:06 → 18:28 (22分钟)
18:28 → 18:52 (24分钟)
```

---

## 根本原因

### 1. 触发链路
```
ml_brain_loop.py / auto_loop.py (每 ~25 分钟循环一次)
  └→ _should_trigger_recon() (检查是否应该执行 recon)
      └→ _trigger_recon_all() (触发)
          └→ make recon-all
              ├→ make backup-pg-full  ⚠️ PostgreSQL 全量备份
              └→ make backup-ch-daily ⚠️ ClickHouse 全量备份
```

### 2. 锁机制失效原因

**文件:** `scripts/ai_trader/auto_loop.py`

#### 问题 1: 锁文件写入时机错误
**原代码 (第 356-367 行):**
```python
def _trigger_recon_all(now: datetime.datetime) -> None:
    # 先触发 recon
    subprocess.Popen(["make", "recon-all"], ...)

    # 后写入锁文件 (异步问题!)
    lock_file.write_text(str(int(now.timestamp())))
```

**问题:**
- `Popen()` 立即返回,recon 在后台运行
- 下次循环时锁文件可能还没写入
- 导致每次都认为可以触发

#### 问题 2: 缺少配置项
- `config.yml` 中没有 `policies.recon` 配置
- 代码使用默认值,但用户无法控制

#### 问题 3: 日志不足
- 无法看到锁检查的详细信息
- 无法诊断为什么锁失效

---

## 修复方案

### 修复 1: 先写锁,再触发 recon
**文件:** `scripts/ai_trader/auto_loop.py:356-381`

```python
def _trigger_recon_all(now: datetime.datetime) -> None:
    """Fire-and-forget `make recon-all` using project venv env and cwd.

    IMPORTANT: Write lock file BEFORE launching recon to prevent race conditions.
    """
    try:
        # ✅ 先写锁 (防止竞态条件)
        dstr = now.date().isoformat()
        day_dir = Path("runs/ai_trader") / dstr
        lock_dir = day_dir / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_file = lock_dir / "recon_daily.lock"

        ts = int(now.timestamp())
        lock_file.write_text(str(ts))
        print(f"[auto_loop] Recon lock written: {lock_file} (ts={ts})")

        # ✅ 然后触发 recon
        env, cwd = _get_project_env()
        print("[auto_loop] Triggering recon-all (Makefile)...")
        subprocess.Popen(["make", "recon-all"], start_new_session=True, cwd=cwd, env=env)

    except Exception as e:
        print(f"[auto_loop] Error launching recon-all: {e}")
        import traceback
        traceback.print_exc()
```

### 修复 2: 增强日志
**文件:** `scripts/ai_trader/auto_loop.py:321-377`

```python
def _should_trigger_recon(o: Orchestrator, now: datetime.datetime) -> bool:
    # ... (省略配置检查)

    if lock.exists():
        age = ts - int(lock.stat().st_mtime)
        if age < min_iv:
            # ✅ 日志: 锁未过期,跳过
            print(f"[auto_loop] Recon skipped: lock age={age}s, min_interval={min_iv}s")
            return False
        else:
            # ✅ 日志: 锁已过期
            print(f"[auto_loop] Recon lock expired: age={age}s > min_interval={min_iv}s")
            return True
    else:
        # ✅ 日志: 锁不存在
        print(f"[auto_loop] Recon lock not found: {lock}")
        return True
```

### 修复 3: 添加配置项
**文件:** `config.yml`

```yaml
policies:
  recon:
    # Daily reconciliation + backup trigger (auto_loop)
    enabled: true                # 是否启用 auto_loop 中的 recon 触发
    run_time_et: '16:10:00'      # 最早触发时间 (东部时间)
    min_interval_sec: 21600      # 最小间隔 (6小时) - 防止重复触发
```

---

## 验证测试

### 测试脚本
```bash
python3 /tmp/test_recon_lock.py
```

### 测试结果
```
[Check 1] Should trigger recon?
[auto_loop] Recon lock not found: .../recon_daily.lock
Result: True  ✅ 没有锁时应该触发

[Check 3] Creating test lock...
Lock written: .../recon_daily.lock

[Check 4] Should trigger after creating lock?
[auto_loop] Recon skipped: lock age=0s, min_interval=21600s
Result: False (should be False)  ✅ 有锁时不应该触发
```

---

## 生产部署建议

### 方案 A: 使用 launchd (推荐)
依赖系统定时任务,更可靠:
```bash
# 已配置: ~/Library/LaunchAgents/com.trading.recon.daily.plist
# 每天 16:01 自动运行 make recon-all
launchctl list | grep com.trading.recon.daily
```

**优点:**
- 独立于 auto_loop
- 系统级可靠性
- 日志清晰

**如果使用此方案,禁用 auto_loop 中的 recon:**
```yaml
policies:
  recon:
    enabled: false  # 禁用 auto_loop 触发
```

### 方案 B: 使用 auto_loop (已修复)
继续使用 auto_loop 的 recon 触发:
```yaml
policies:
  recon:
    enabled: true            # 启用
    run_time_et: '16:10:00'  # 下午 4:10 之后才检查
    min_interval_sec: 21600  # 6小时防重复
```

**优点:**
- 统一管理
- 可以与其他 auto_loop 逻辑协同

**缺点:**
- 依赖 auto_loop 稳定运行

---

## 监控建议

### 1. 检查备份频率
```bash
# 每天备份应该只有 1-2 次
cat backup/pg/backup_history.log | grep "$(date +"%b %d")" | wc -l

# 如果超过 3 次,说明有问题
```

### 2. 检查锁文件
```bash
# 查看今天的 recon 锁
cat runs/ai_trader/$(date +%Y-%m-%d)/locks/recon_daily.lock

# 计算锁的年龄
python3 -c "
import time
from pathlib import Path
lock = Path('runs/ai_trader/$(date +%Y-%m-%d)/locks/recon_daily.lock')
if lock.exists():
    age = int(time.time()) - int(lock.read_text())
    print(f'Lock age: {age}s ({age/3600:.2f} hours)')
"
```

### 3. 查看 auto_loop 日志
```bash
# 搜索 recon 触发日志
grep "Triggering recon-all\|Recon skipped\|Recon lock" logs/auto_loop.log
```

---

## 预期效果

### 修复前 (11月6日)
```
Backup completed at Thu Nov  6 16:22:30 EST 2025
Backup completed at Thu Nov  6 16:45:27 EST 2025  ❌ 23分钟后又备份
Backup completed at Thu Nov  6 17:13:55 EST 2025  ❌ 28分钟后又备份
...
共 21 次备份
```

### 修复后 (预期)
```
Backup completed at Mon Nov 18 16:01:00 EST 2025  ✅ launchd 触发
或
Backup completed at Mon Nov 18 16:10:30 EST 2025  ✅ auto_loop 触发

[6小时内不会再次触发]

次日:
Backup completed at Tue Nov 19 16:01:00 EST 2025  ✅ 一天一次
```

---

## 相关文件

### 修改的文件
1. `scripts/ai_trader/auto_loop.py` - 修复锁机制和日志
2. `config.yml` - 添加 `policies.recon` 配置

### 相关配置
1. `~/Library/LaunchAgents/com.trading.recon.daily.plist` - launchd 定时任务
2. `Makefile` - `recon-all` 目标定义

### 备份脚本
1. `scripts/backup/postgres_full_backup.sh` - PostgreSQL 备份
2. `scripts/backup/clickhouse_daily_backup.sh` - ClickHouse 备份

---

## 回滚方案

如果修复后出现问题:

### 1. 禁用 auto_loop 中的 recon
```yaml
# config.yml
policies:
  recon:
    enabled: false
```

### 2. 手动触发备份
```bash
make backup-pg-full
make backup-ch-daily
```

### 3. 恢复旧代码 (如果需要)
```bash
git diff HEAD scripts/ai_trader/auto_loop.py
git checkout HEAD -- scripts/ai_trader/auto_loop.py
```

---

## 重启保护机制

### Q: 如果强制停止程序后重启,锁会造成问题吗?

**答案: 不会! ✅**

### 保护机制:

1. **锁文件白名单保护**
   ```python
   # orchestrator.py:1713-1716
   PRESERVE_LOCKS = {
       "recon_daily.lock",  # 6-hour TTL, 重启时不清理
       "ml_pause.lock",     # User-controlled pause
   }
   ```

2. **按日期隔离**
   - 锁文件路径: `runs/ai_trader/YYYY-MM-DD/locks/recon_daily.lock`
   - 昨天的锁不影响今天
   - 每天都是新的开始

3. **6小时有效期**
   - 锁文件记录触发时间戳
   - 6小时内重启,锁依然有效
   - 不会重复触发 recon

### 重启场景分析:

**场景1: 短时间重启 (1小时内)**
```
16:10 - 触发recon,写锁
16:30 - 强制停止程序 (Ctrl+C)
16:35 - 重启程序
✅ 锁依然有效 (年龄25分钟 < 6小时)
✅ 不会重复触发recon
```

**场景2: 长时间停止 (超过6小时)**
```
16:10 - 触发recon,写锁
18:00 - 强制停止程序
23:00 - 重启程序
✅ 锁已过期 (年龄6小时50分钟 > 6小时)
✅ 会触发新的recon (这是预期行为,因为确实该备份了)
```

**场景3: 跨天重启**
```
昨天16:10 - 触发recon,写锁到 2025-11-16/locks/
今天09:00 - 重启程序
✅ 检查今天的锁 2025-11-17/locks/ (不存在)
✅ 等到16:10后会触发今天的recon
```

---

## 总结

**问题根源:**
1. 锁文件在 recon 触发之后才写入,导致异步竞态条件
2. Orchestrator 启动时会清理所有锁,包括长期有效的锁

**修复方法:**
1. 先写锁文件,再触发 recon (防止竞态)
2. 添加锁文件白名单保护 (防止误清理)
3. 增强日志输出 (便于诊断)

**预期效果:**
- 备份频率: 从 20+次/天 → 1次/天
- 重启安全: 6小时内重启不会重复备份
- 跨天正常: 每天独立的备份周期

**监控方式:**
- 检查备份历史日志和锁文件年龄
- 查看 auto_loop 日志中的锁检查信息

---

**修复人:** Claude
**修复日期:** 2025-11-17
**验证状态:** ✅ 测试通过
**重启测试:** ✅ 锁文件保护正常
