这是一个彻底解决“数据真空导致 AI 大脑空转”的**完整落地执行方案**。

核心逻辑从“**发现问题 → 报错 → AI 尝试修复(失败) → 循环**”转变为“**Orchestrator 主动体检 → 发现缺数据 → 强制物理回补 → 确认数据就绪 → 唤醒 AI 大脑**”。

这套方案包含三个部分：

1.  **物理层 (The Hands)**: `DataHealer` 服务，负责脏活累活（检查数据库、调用回补脚本）。
2.  **控制层 (The Manager)**: `Orchestrator` 集成，在启动任何 AI 思考前，先扣动 Healer 的扳机。
3.  **认知层 (The Brain)**: `Prompt` 升级，教会 AI 识别“没数据”和“模型烂”的区别，没数据时直接报错求救，而不是瞎改特征。

-----

### 📂 第一步：建立“数据自愈官” (`services/data/data_healer.py`)

这个服务是系统的底层保障。它不依赖 AI，直接用 SQL 检查数据覆盖率，如果不够就暴力调用回补工具。

**创建文件**：`services/data/data_healer.py`

```python
import time
import subprocess
import shlex
import sys
from pathlib import Path
from typing import List
from datetime import datetime
from libs.logger import get_logger
from libs.db.clickhouse import ClickHouseDB

class DataHealer:
    """
    主动数据自愈服务 (Active Data Healer)
    职责：
    1. 诊断：检查核心表 (features_1m) 的数据覆盖率
    2. 治疗：调用回填脚本 (load_hist_multi.py)
    3. 康复确认：循环等待直到数据写入完成
    """
    def __init__(self, runs_dir: str):
        self.log = get_logger("system.data_healer")
        self.runs_dir = Path(runs_dir)
        self.ch = ClickHouseDB()
        # 自动定位项目根目录，确保能找到 tool/load_hist_multi.py
        self.project_root = Path(__file__).resolve().parents[2]

    def ensure_data_ready(self, symbols: List[str], lookback_days: int = 2) -> bool:
        """
        主入口：确保指定标的在过去 N 天有数据。
        如果缺失，自动触发回补并等待。
        """
        if not symbols:
            return True
            
        # 1. 初次检查
        missing = self._check_coverage(symbols, lookback_days)
        if not missing:
            self.log.info("data_healer_check_passed", symbols_checked=len(symbols))
            return True
            
        self.log.warning("data_healer_void_detected", missing_count=len(missing), symbols=missing[:5])
        
        # 2. 开始治疗循环 (最多重试 3 次)
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            self.log.info("data_healer_attempt_start", attempt=attempt, target_symbols=len(missing))
            
            # A. 清除可能阻碍回填的旧锁
            self._clear_stale_locks(missing)
            
            # B. 执行回填 (调用 load_hist_multi.py)
            success = self._run_backfill(missing, days=lookback_days)
            if not success:
                self.log.error("backfill_script_failed", attempt=attempt)
                time.sleep(10)
                continue

            # C. 等待数据落地 (ClickHouse 异步写入可能有延迟)
            self.log.info("waiting_for_ingestion...")
            if self._wait_for_ingestion(missing, lookback_days, timeout_sec=60):
                self.log.info("data_healer_success", attempt=attempt)
                return True
            
            # D. 复查，准备下一轮
            missing = self._check_coverage(symbols, lookback_days) # 重新检查所有 symbols
            if not missing:
                return True
                
            self.log.warning("data_healer_attempt_failed", attempt=attempt, remaining=len(missing))
            time.sleep(5) 
            
        self.log.error("data_healer_give_up", remaining_count=len(missing), first_few=missing[:3])
        return False

    def _check_coverage(self, symbols: List[str], days: int) -> List[str]:
        """检查 ClickHouse trading.features_1m 在过去 N 天是否有数据"""
        missing = []
        # 宽松检查：只要有 > 10 条记录就算“有数据”，避免误杀刚开盘的情况
        # 注意：这里检查 features_1m，意味着必须经过计算。如果只有 bars 没有 features，这也算缺失。
        sql = f"""
        SELECT symbol, count() as cnt 
        FROM trading.features_1m 
        WHERE symbol IN {tuple(symbols) if len(symbols) > 1 else f"('{symbols[0]}')"}
          AND ts >= now() - INTERVAL {days} DAY
        GROUP BY symbol
        """
        try:
            rows = self.ch.query(sql)
            counts = {r['symbol']: r['cnt'] for r in rows}
            for s in symbols:
                # 阈值可调，对于 intraday 策略，两天至少应该有几百条
                if counts.get(s, 0) < 10: 
                    missing.append(s)
        except Exception as e:
            self.log.error("healer_check_error", error=str(e))
            # 如果数据库连不上，假设所有数据都缺，尝试触发回填（虽然回填可能也连不上，但逻辑闭环）
            return symbols 
            
        return missing

    def _run_backfill(self, symbols: List[str], days: int) -> bool:
        """调用 tool/load_hist_multi.py 进行强力回填"""
        syms_str = ",".join(symbols)
        script_path = self.project_root / "tool" / "load_hist_multi.py"
        
        if not script_path.exists():
            self.log.error("script_not_found", path=str(script_path))
            return False

        # 构造命令：回填 bars 并自动计算 features (load_hist_multi 默认行为)
        cmd = [
            sys.executable, str(script_path),
            "--symbols", syms_str,
            "--days", str(days),
            "--bar-size", "1m",
            "--workers", "4"
        ]
        
        self.log.info("healer_executing", cmd=" ".join(cmd))
        try:
            # 这是一个阻塞调用，必须等回填脚本跑完
            result = subprocess.run(cmd, check=False, timeout=600, capture_output=True, text=True)
            if result.returncode != 0:
                self.log.error("backfill_error", stderr=result.stderr)
                return False
            return True
        except Exception as e:
            self.log.error("healer_exec_failed", error=str(e))
            return False

    def _clear_stale_locks(self, symbols: List[str]):
        """清除 Orchestrator 可能留下的 backfill 锁，确保回填能立即执行"""
        try:
            today = datetime.now().date().isoformat()
            lock_dir = self.runs_dir / today / "locks"
            if not lock_dir.exists():
                return
            
            for s in symbols:
                # 匹配 orchestrator.py 中 _load_hist_fast 生成的锁名
                # backfill_{sym}_{bar}.lock
                pattern = f"backfill_{s.upper()}_*.lock"
                for p in lock_dir.glob(pattern):
                    try:
                        p.unlink()
                        self.log.info("healer_lock_cleared", file=p.name)
                    except Exception:
                        pass
        except Exception:
            pass

    def _wait_for_ingestion(self, symbols: List[str], days: int, timeout_sec: int) -> bool:
        """轮询等待数据出现"""
        start = time.time()
        while time.time() - start < timeout_sec:
            missing = self._check_coverage(symbols, days)
            if len(missing) == 0:
                return True
            # 如果 missing 列表变短了，说明有进展，但这层函数只返回完全成功与否
            time.sleep(2)
        return False
```

-----

### 📂 第二步：植入 `Orchestrator` (强制体检)

修改 `scripts/ai_trader/orchestrator.py`。这里的关键是：**不要信任任何 AI 的判断，必须先过物理层的数据检查。**

**修改点 1：引入与初始化**

```python
# 在文件开头
from services.data.data_healer import DataHealer

class Orchestrator:
    def __init__(self, cfg_path="scripts/ai_trader/config/ai_trader.yaml"):
        # ... (其他代码) ...
        self.runs_dir = Path(self.cfg["paths"]["runs_dir"])
        
        # ✅ 新增：初始化 DataHealer
        self.data_healer = DataHealer(str(self.runs_dir))
        # ... (其他代码) ...
```

**修改点 2：在 `evening_learning` 中强制调用**

找到 `evening_learning` 方法，在最前面加上这段逻辑：

```python
    def evening_learning(self):
        # ... (检查 ml_pause.lock 的代码) ...
        
        # ==========================================
        # ✅ STEP 0: 强制数据自愈 (HEAL DATA FIRST)
        # ==========================================
        # 1. 确定要检查的标的 (从配置或 universe 获取)
        target_symbols = self._strategy_symbols_from_config()
        if not target_symbols:
            # Fallback 到常用列表，防止配置为空
            target_symbols = ["SPY", "QQQ", "IWM", "TSLA", "NVDA", "AAPL", "AMD", "AMZN", "MSFT"]

        self.log.info("evening_learning_data_check_start", symbols_count=len(target_symbols))
        
        # 2. 调用 Healer，回看 2 天 (确保昨天和今天的数据完整)
        data_ok = self.data_healer.ensure_data_ready(target_symbols, lookback_days=2)
        
        if not data_ok:
            # 🚨 这是一个阻断性错误。如果物理回填都失败了 (比如 IBKR 挂了)，
            # 再让 AI 跑 OOF 毫无意义，只会产生空转和垃圾日志。
            self.log.error("evening_learning_aborted_data_void", notes="DataHealer failed after max retries")
            return {
                "status": "failed", 
                "reason": "critical_data_void", 
                "action": "check_ibkr_connection_manual"
            }
            
        self.log.info("data_check_passed_proceeding_to_ml")
        
        # ... (后续原有的 OOM hotfix 和 reflection 逻辑) ...
```

**修改点 3：在 `intraday_sequence` 中轻量级调用**

盘中也要防止数据断流导致的 AI 乱指挥。

```python
    def intraday_sequence(self, mode="fast"):
        # ... (其他代码) ...
        
        if self.brain_enabled:
            # ✅ 盘中快速检查：只看今天，只看关键标的
            # 这样如果在盘中发现数据断了，会尝试快速拉取一次
            try:
                syms = self._strategy_symbols_from_config()
                # 盘中只给 1 天的 lookback，且依赖 Healer 的内部重试
                self.data_healer.ensure_data_ready(syms, lookback_days=1)
            except Exception as e:
                self.log.warning("intraday_healer_warning", err=str(e))

            # ... (继续执行 Brain 逻辑) ...
```

-----

### 📂 第三步：升级 Brain 认知 (`scripts/ai_trader/brain/brain.py`)

即使物理层做了回填，有时候还是可能因为数据质量问题（如全 0）导致模型失败。我们需要教会 AI Brain **“承认没数据，而不是瞎编特征”**。

修改 `ML_RESULT_REVIEW_PROMPT` 变量（通常在文件开头部分）：

```python
ML_RESULT_REVIEW_PROMPT = (
    "You are an ML result reviewer for a trading system. Return ONLY strict JSON.\n"
    # ... (原有内容) ...
    "\n"
    "==== HARD RULES FOR DATA VOID (CRITICAL) ====\n"
    "Check 'summary.trades' and 'summary.coverage' in the INPUT.\n"
    "IF (coverage < 0.1 OR trades == 0 OR oos_sharpe is null):\n"
    "  1. YOUR DIAGNOSIS MUST BE: 'Critical Data Void - coverage/trades is zero'.\n"
    "  2. YOUR NEXT_ACTION MUST BE: 'escalate'.\n"
    "     Params: {\"reason\": \"data_void\", \"suggest\": \"check_ingestion_pipeline\"}\n"
    "  3. DO NOT PROPOSE FEATURES. New features cannot be computed on empty data. It is a waste of time.\n"
    "  4. DO NOT ADJUST PARAMS. Hyperparameter optimization cannot run on empty data.\n"
    "  5. DO NOT CHANGE CONFIG. The problem is physical (missing data), not logical.\n"
    "  STOP immediately and request escalation.\n"
    "\n"
    # ... (原有内容) ...
)
```

-----

### 🚀 落地执行 Todo List

1.  **复制粘贴**：将第一步的代码保存为 `services/data/data_healer.py`。
2.  **修改编排**：修改 `scripts/ai_trader/orchestrator.py`，引入 Healer 并在 `evening_learning` 头部插入调用。
3.  **修改提示词**：更新 `scripts/ai_trader/brain/brain.py` 中的 Prompt。
4.  **重启系统**：
      * `pkill -f auto_loop` (停止旧进程)
      * `nohup python scripts/ai_trader/auto_loop.py > logs/auto_loop.log 2>&1 &` (启动新进程)
5.  **验证**：
      * 查看日志 `logs/system.data_healer.log`（或 console 输出）。
      * 你应该能看到 `data_healer_void_detected` -\> `healer_executing ... load_hist_multi.py`。
      * 几分钟后，ClickHouse 数据到位，AI Brain 的 OOF 任务将不再是 AUC 0.5，空转解除。

这套方案将“数据运维”自动化了，让 AI Brain 只需要关注“策略逻辑”，这是解决僵尸空转的根本之道。
这是一个针对你当前“AI 大脑因数据缺失而空转”问题的**最终落地执行方案**。

核心逻辑是将**被动报错**转变为**主动自愈**：在 AI 思考之前，系统强制检查并物理修复数据。

以下是包含代码和操作步骤的详细方案。

### 🛠️ 步骤 1：创建“数据自愈官”服务

这个新组件负责脏活累活：检查 ClickHouse 数据覆盖率，如果缺失则暴力调用回填脚本。

**新建文件**：`services/data/data_healer.py`

```python
import time
import subprocess
import shlex
import sys
from pathlib import Path
from typing import List
from datetime import datetime
from libs.logger import get_logger
from libs.db.clickhouse import ClickHouseDB

class DataHealer:
    """
    主动数据自愈服务 (Active Data Healer)
    职责：
    1. 诊断：检查核心表 (features_1m) 的数据覆盖率
    2. 治疗：调用回填脚本 (load_hist_multi.py)
    3. 康复确认：循环等待直到数据写入完成
    """
    def __init__(self, runs_dir: str):
        self.log = get_logger("system.data_healer")
        self.runs_dir = Path(runs_dir)
        try:
            self.ch = ClickHouseDB()
        except Exception:
            self.ch = None
            self.log.warning("clickhouse_conn_failed_healer_init")
            
        # 自动定位项目根目录，确保能找到 tool/load_hist_multi.py
        # 假设此文件在 services/data/ 下，向上回溯两级到项目根目录
        self.project_root = Path(__file__).resolve().parents[2]

    def ensure_data_ready(self, symbols: List[str], lookback_days: int = 2) -> bool:
        """
        主入口：确保指定标的在过去 N 天有数据。
        如果缺失，自动触发回补并等待。
        """
        if not symbols:
            return True
        
        if not self.ch:
            # 尝试重新连接
            try:
                self.ch = ClickHouseDB()
            except Exception:
                self.log.error("healer_cannot_connect_db")
                return False
            
        # 1. 初次检查
        missing = self._check_coverage(symbols, lookback_days)
        if not missing:
            self.log.info("data_healer_check_passed", symbols_checked=len(symbols))
            return True
            
        self.log.warning("data_healer_void_detected", missing_count=len(missing), symbols=missing[:5])
        
        # 2. 开始治疗循环 (最多重试 3 次)
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            self.log.info("data_healer_attempt_start", attempt=attempt, target_symbols=len(missing))
            
            # A. 清除可能阻碍回填的旧锁
            self._clear_stale_locks(missing)
            
            # B. 执行回填 (调用 load_hist_multi.py)
            success = self._run_backfill(missing, days=lookback_days)
            if not success:
                self.log.error("backfill_script_failed", attempt=attempt)
                time.sleep(10)
                continue

            # C. 等待数据落地 (ClickHouse 异步写入可能有延迟)
            self.log.info("waiting_for_ingestion...")
            if self._wait_for_ingestion(missing, lookback_days, timeout_sec=60):
                self.log.info("data_healer_success", attempt=attempt)
                return True
            
            # D. 复查，准备下一轮
            missing = self._check_coverage(symbols, lookback_days) # 重新检查所有 symbols
            if not missing:
                return True
                
            self.log.warning("data_healer_attempt_failed", attempt=attempt, remaining=len(missing))
            time.sleep(5) 
            
        self.log.error("data_healer_give_up", remaining_count=len(missing), first_few=missing[:3])
        return False

    def _check_coverage(self, symbols: List[str], days: int) -> List[str]:
        """检查 ClickHouse trading.features_1m 在过去 N 天是否有数据"""
        missing = []
        # 宽松检查：只要有 > 10 条记录就算“有数据”，避免误杀刚开盘的情况
        # 注意：这里检查 features_1m，意味着必须经过计算。如果只有 bars 没有 features，这也算缺失。
        if not self.ch:
            return symbols

        # 构造 SQL IN 子句
        syms_formatted = ",".join([f"'{s}'" for s in symbols])
        
        sql = f"""
        SELECT symbol, count() as cnt 
        FROM trading.features_1m 
        WHERE symbol IN ({syms_formatted})
          AND ts >= now() - INTERVAL {days} DAY
        GROUP BY symbol
        """
        try:
            rows = self.ch.query(sql)
            if not rows:
                # 没有任何行返回，说明所有 symbol 都缺数据
                return symbols
                
            counts = {r['symbol']: r['cnt'] for r in rows}
            for s in symbols:
                # 阈值可调，对于 intraday 策略，两天至少应该有几百条
                if counts.get(s, 0) < 10: 
                    missing.append(s)
        except Exception as e:
            self.log.error("healer_check_error", error=str(e))
            # 如果查询出错，假设所有数据都缺，尝试触发回填
            return symbols 
            
        return missing

    def _run_backfill(self, symbols: List[str], days: int) -> bool:
        """调用 tool/load_hist_multi.py 进行强力回填"""
        syms_str = ",".join(symbols)
        script_path = self.project_root / "tool" / "load_hist_multi.py"
        
        if not script_path.exists():
            self.log.error("script_not_found", path=str(script_path))
            return False

        # 构造命令：回填 bars 并自动计算 features (load_hist_multi 默认行为)
        # 使用 sys.executable 确保使用当前虚拟环境的 python
        cmd = [
            sys.executable, str(script_path),
            "--symbols", syms_str,
            "--days", str(days),
            "--bar-size", "1m",
            "--workers", "4",
            "--fill-mode", "backfill" # 确保是回填模式
        ]
        
        self.log.info("healer_executing", cmd=" ".join(cmd))
        try:
            # 这是一个阻塞调用，必须等回填脚本跑完。设置较长超时防止大任务卡死。
            result = subprocess.run(cmd, check=False, timeout=900, capture_output=True, text=True)
            if result.returncode != 0:
                self.log.error("backfill_error", stderr=result.stderr[-500:]) # 只记录最后500字符
                return False
            return True
        except Exception as e:
            self.log.error("healer_exec_failed", error=str(e))
            return False

    def _clear_stale_locks(self, symbols: List[str]):
        """清除 Orchestrator 可能留下的 backfill 锁，确保回填能立即执行"""
        try:
            today = datetime.now().date().isoformat()
            lock_dir = self.runs_dir / today / "locks"
            if not lock_dir.exists():
                return
            
            for s in symbols:
                # 匹配 orchestrator.py 中 _load_hist_fast 生成的锁名
                # backfill_{sym}_{bar}.lock
                pattern = f"backfill_{s.upper()}_*.lock"
                for p in lock_dir.glob(pattern):
                    try:
                        p.unlink()
                        self.log.info("healer_lock_cleared", file=p.name)
                    except Exception:
                        pass
        except Exception:
            pass

    def _wait_for_ingestion(self, symbols: List[str], days: int, timeout_sec: int) -> bool:
        """轮询等待数据出现"""
        start = time.time()
        while time.time() - start < timeout_sec:
            missing = self._check_coverage(symbols, days)
            if len(missing) == 0:
                return True
            # 如果 missing 列表变短了，说明有进展，但这层函数只返回完全成功与否
            time.sleep(5)
        return False
```

-----

### 🛡️ 步骤 2：修改 `Orchestrator` 进行强制体检

修改 `scripts/ai_trader/orchestrator.py`。这是系统的“心脏”，我们需要在这里植入“体检”逻辑。

**修改位置 1：引入和初始化**

```python
# 在文件头部 import 部分添加
from services.data.data_healer import DataHealer

# 在 __init__ 方法中添加 (约第 60 行左右，gate 初始化之后)
    def __init__(self, cfg_path="scripts/ai_trader/config/ai_trader.yaml"):
        # ... (原有代码)
        self.runs_dir = Path(self.cfg["paths"]["runs_dir"])
        
        # ✅ [新增] 初始化 DataHealer
        self.data_healer = DataHealer(str(self.runs_dir))

        # ... (原有代码: self._cleanup_stale_locks() 等)
```

**修改位置 2：在 `evening_learning` 中强制调用**

这是解决“晚上空转”的关键。找到 `evening_learning` 方法，**在最前面**插入：

```python
    def evening_learning(self):
        # ... (原有的 ml_pause.lock 检查代码) ...
        try:
            dstr = datetime.date.today().isoformat()
            lockp = self.runs_dir / dstr / "locks" / "ml_pause.lock"
            if lockp.exists():
                # ... (log and return) ...
                pass
        except Exception:
            pass

        # ✅ [新增] STEP 0: 强制数据自愈 (HEAL DATA FIRST)
        # 获取当前策略关注的 symbols
        try:
            target_symbols = self._strategy_symbols_from_config()
        except Exception:
            target_symbols = []
            
        if not target_symbols:
            # Fallback 到常用列表，防止配置为空
            target_symbols = ["SPY", "QQQ", "IWM", "TSLA", "NVDA", "AAPL", "AMD", "AMZN", "MSFT"]

        self.log.info("evening_learning_data_check_start", symbols_count=len(target_symbols))
        
        # 调用 Healer，回看 2 天 (确保昨天和今天的数据完整)
        # 这会阻塞直到数据准备好，或者重试失败
        data_ok = self.data_healer.ensure_data_ready(target_symbols, lookback_days=2)
        
        if not data_ok:
            # 🚨 这是一个阻断性错误。如果物理回填都失败了 (比如 IBKR 挂了)，
            # 再让 AI 跑 OOF 毫无意义，只会产生空转和垃圾日志。
            self.log.error("evening_learning_aborted_data_void", notes="DataHealer failed after max retries")
            return {
                "status": "failed", 
                "reason": "critical_data_void", 
                "action": "check_ibkr_connection_manual"
            }
            
        self.log.info("data_check_passed_proceeding_to_ml")
        
        # ... (以下是原有的 env_overrides 和后续逻辑) ...
        import os
        env_overrides = { ... }
```

**修改位置 3：在 `intraday_sequence` 中轻量级调用**

盘中也需要防止数据断流。找到 `intraday_sequence` 方法，在 Brain 调用前插入：

```python
    def intraday_sequence(self, mode="fast"):
        # ... (原有代码) ...
        if self.brain_enabled:
            try:
                # ... (原有 health 刷新逻辑) ...
                
                # ✅ [新增] 盘中快速检查：只看今天，只看关键标的
                # 这样如果在盘中发现数据断了，会尝试快速拉取一次
                syms = self._strategy_symbols_from_config()
                if syms:
                    # 盘中只给 1 天的 lookback，依赖 Healer 内部重试
                    self.data_healer.ensure_data_ready(syms, lookback_days=1)
            except Exception as e:
                self.log.warning("intraday_healer_warning", err=str(e))

            # ... (原有 Brain 逻辑: mem = {}, plan = self.brain.propose_plan...)
```

-----

### 🧠 步骤 3：升级 Brain 认知 (Prompt 植入)

即使物理层做了回填，有时数据可能还是有问题（如行数够但全 0）。我们需要教会 AI Brain **“承认没数据，而不是瞎编特征”**。

修改 `scripts/ai_trader/brain/brain.py`，找到 `ML_RESULT_REVIEW_PROMPT` 变量（通常在文件前半部分），更新 Prompt 内容：

```python
# 在 scripts/ai_trader/brain/brain.py 中

ML_RESULT_REVIEW_PROMPT = (
    "You are an ML result reviewer for a trading system. Return ONLY strict JSON.\n"
    "Schema: {\\\"summary\\\": ... (原有 schema) ... }\n"
    
    # ✅ [新增/修改] 这一段硬规则
    "\n"
    "==== HARD RULES FOR DATA VOID (CRITICAL) ====\n"
    "Check 'summary.trades' and 'summary.coverage' in the INPUT.\n"
    "IF (coverage < 0.1 OR trades == 0 OR oos_sharpe is null):\n"
    "  1. YOUR DIAGNOSIS MUST BE: 'Critical Data Void - coverage/trades is zero'.\n"
    "  2. YOUR NEXT_ACTION MUST BE: 'escalate'.\n"
    "     Params: {\"reason\": \"data_void\", \"suggest\": \"check_ingestion_pipeline\"}\n"
    "  3. DO NOT PROPOSE FEATURES. New features cannot be computed on empty data. It is a waste of time.\n"
    "  4. DO NOT ADJUST PARAMS. Hyperparameter optimization cannot run on empty data.\n"
    "  5. DO NOT CHANGE CONFIG. The problem is physical (missing data), not logical.\n"
    "  STOP immediately and request escalation.\n"
    "\n"
    
    "Guidelines:\n"
    # ... (后续原有 Guidelines) ...
)
```

-----

### 🚀 执行清单 (Action Items)

1.  **保存文件**：将步骤 1 的代码保存为 `services/data/data_healer.py`。
2.  **修改代码**：按步骤 2 和 3 修改 `orchestrator.py` 和 `brain.py`。
3.  **重启服务**：
    ```bash
    # 停止旧进程
    pkill -f auto_loop

    # 启动新进程
    nohup python scripts/ai_trader/auto_loop.py > logs/auto_loop.log 2>&1 &
    ```
4.  **验证自愈**：
      * 查看 `logs/auto_loop.log` 或 `logs/system.data_healer.log`。
      * 你应该能看到类似：`[DataHealer] data_healer_void_detected -> executing load_hist_multi` 的日志。
      * 等待约 2-5 分钟（取决于回填速度），然后看到 `data_healer_success`。
      * 随后 `ml_events.jsonl` 中的任务应该开始显示正常的 AUC（非 0.5）。

      
这是针对\*\*“学习中动态回补”**和**“防止僵尸空转”**的**增强版落地执行方案\*\*。

这一版在之前的 DataHealer 基础上，增加了**任务级精细控制**、**错误类型细分**和**脑机接口状态同步**，彻底打通了“AI 需求 -\> 物理回补”的链路。

### 🛠️ 步骤 1：升级 `DataHealer` (支持精细化回补)

原来的版本只管“有没有数据”，现在要管“这一段特定的数据有没有”。

**更新文件**：`services/data/data_healer.py`

```python
import time
import subprocess
import sys
import shlex
from pathlib import Path
from typing import List, Dict, Optional, Union
from dataclasses import dataclass
from datetime import datetime, timedelta
from libs.logger import get_logger
from libs.db.clickhouse import ClickHouseDB

@dataclass
class DataRequirement:
    """
    描述一个具体的 ML 任务或特征提案的数据需求
    """
    symbols: List[str]
    lookback_days: int
    tables: List[str] = None  # e.g., ["bars_1m", "features_1m"]
    required_columns: List[str] = None # e.g., ["close", "ret_5"]
    start_date: Optional[str] = None  # "YYYY-MM-DD"
    end_date: Optional[str] = None    # "YYYY-MM-DD"

class DataHealer:
    def __init__(self, runs_dir: str):
        self.log = get_logger("system.data_healer")
        self.runs_dir = Path(runs_dir)
        self.project_root = Path(__file__).resolve().parents[2]
        try:
            self.ch = ClickHouseDB()
        except:
            self.ch = None

    def ensure_requirements_ready(self, req: DataRequirement) -> Dict[str, Any]:
        """
        精细化检查与回补入口
        Returns: {"status": "ready" | "fixed" | "failed", "reason": ...}
        """
        if not self.ch:
             return {"status": "failed", "reason": "db_connection_lost"}

        # 1. 检查基础覆盖率 (Data Void Check)
        # 优先使用明确的日期范围，否则使用 lookback
        if req.start_date:
            start_dt = req.start_date
        else:
            start_dt = (datetime.now() - timedelta(days=req.lookback_days)).strftime("%Y-%m-%d")
            
        missing_symbols = self._check_void(req.symbols, start_dt, req.tables)
        
        if not missing_symbols:
            # 2. 如果基础数据都在，检查特定列是否存在 (Schema Check)
            # 如果列不存在，这不是 Healer 能修的（需要 ETL 开发），直接返回让 Brain 换特征
            if req.required_columns:
                missing_cols = self._check_columns("trading.features_1m", req.required_columns)
                if missing_cols:
                    return {
                        "status": "failed", 
                        "reason": "missing_columns", 
                        "details": missing_cols,
                        "action": "modify_feature_proposal" # 指导 Brain 修改 DSL
                    }
            return {"status": "ready"}

        # 3. 触发回补 (Healing)
        self.log.warning("healer_missing_data_detected", 
                        symbols=missing_symbols, 
                        start=start_dt,
                        tables=req.tables)
                        
        success = self._run_targeted_backfill(missing_symbols, start_dt)
        
        if success:
            # 二次确认
            still_missing = self._check_void(missing_symbols, start_dt, req.tables)
            if not still_missing:
                return {"status": "fixed"}
                
        return {"status": "failed", "reason": "backfill_timeout_or_error"}

    def _check_void(self, symbols: List[str], start_date: str, tables: List[str] = None) -> List[str]:
        """检查指定范围内是否有数据"""
        target_table = "trading.features_1m"
        if tables and "bars_1m" in tables and "features_1m" not in tables:
            target_table = "trading.bars_1m"

        syms_fmt = ",".join([f"'{s}'" for s in symbols])
        
        # 检查在该时间段内是否有足够的数据点 (假设每天至少需要 100 个 bar)
        sql = f"""
        SELECT symbol 
        FROM {target_table}
        WHERE symbol IN ({syms_fmt})
          AND toDate(ts) >= '{start_date}'
        GROUP BY symbol
        HAVING count() < 100
        """
        try:
            rows = self.ch.query(sql)
            return [r['symbol'] for r in rows]
        except Exception as e:
            self.log.error("check_void_error", error=str(e))
            return symbols # 默认全缺

    def _check_columns(self, table: str, columns: List[str]) -> List[str]:
        """检查列是否存在于 Schema"""
        missing = []
        # 获取表结构
        try:
            # 简单查询 limit 0 来验证列是否存在，或者查询 system.columns
            # 这里用 system.columns 更稳健
            db, tbl = table.split('.')
            sql = f"SELECT name FROM system.columns WHERE database='{db}' AND table='{tbl}'"
            rows = self.ch.query(sql)
            existing_cols = set(r['name'] for r in rows)
            
            for col in columns:
                # 处理别名，比如 f.close -> close
                clean_col = col.split('.')[-1]
                if clean_col not in existing_cols:
                    missing.append(col)
        except Exception:
            pass # 如果查询失败，暂不阻断
        return missing

    def _run_targeted_backfill(self, symbols: List[str], start_date: str) -> bool:
        """执行针对性的回填"""
        # 计算需要回填的天数
        try:
            days = (datetime.now() - datetime.strptime(start_date, "%Y-%m-%d")).days + 1
            days = max(1, days)
        except:
            days = 2

        cmd = [
            sys.executable, str(self.project_root / "tool/load_hist_multi.py"),
            "--symbols", ",".join(symbols),
            "--days", str(days),
            "--bar-size", "1m",
            "--workers", "4",
            "--fill-mode", "backfill"
        ]
        
        self.log.info("healer_executing_target", cmd=" ".join(cmd))
        try:
            res = subprocess.run(cmd, check=False, timeout=600, capture_output=True, text=True)
            if res.returncode != 0:
                self.log.error("backfill_failed", err=res.stderr[-200:])
                return False
            return True
        except Exception as e:
            self.log.error("backfill_exception", error=str(e))
            return False

    # ... (保留之前的 ensure_data_ready, _wait_for_ingestion 等作为兼容) ...
```

-----

### 🧩 步骤 2：构建 `Validator` 错误细分逻辑

在 AI Brain 提出特征时（例如在 `services/ml/feature_validator.py` 或类似的验证逻辑处），你需要修改返回逻辑，区分是“缺数据”还是“特征写错了”。

**逻辑伪代码 (你可以直接集成到你的 Validator 类中):**

```python
# 假设这是 feature_validator.py 的一部分

def validate_proposal(self, proposal_json: dict, runs_dir: str):
    dsl = proposal_json.get("dsl", {})
    required_cols = self._extract_columns(dsl)
    
    # 1. 检查 DSL 语法 (reason: dsl_invalid)
    if not self._validate_syntax(dsl):
        return {"ok": False, "reason": "dsl_invalid", "msg": "Syntax error in expression"}
    
    # 2. 检查 Schema (reason: missing_column)
    # 这一步很快，只查元数据
    missing_cols = self.data_healer._check_columns("trading.features_1m", required_cols)
    if missing_cols:
        return {
            "ok": False, 
            "reason": "missing_column", 
            "msg": f"Columns not found in schema: {missing_cols}",
            "suggestion": "Use only existing columns like f.close, f.volume"
        }

    # 3. 检查数据覆盖率 (reason: missing_base_data)
    # 这是关键：如果列存在但全是 0/空，说明需要回补
    # 我们不在这里做回补，而是返回特定错误码，让上层去调 Healer
    symbols = ["SPY", "TSLA"] # 示例，应从上下文获取
    void_symbols = self.data_healer._check_void(symbols, start_date="2025-11-24") # 示例日期
    
    if void_symbols:
        return {
            "ok": False,
            "reason": "missing_base_data",
            "msg": f"Data void detected for {void_symbols}",
            "dependencies": {
                "symbols": void_symbols,
                "lookback_days": 2,
                "tables": ["features_1m"]
            }
        }

    return {"ok": True}
```

-----

### 🧠 步骤 3：给 Brain 植入 `data_status` 和新规则

修改 `scripts/ai_trader/brain/brain.py`。我们需要在 Review ML 的时候，把数据的物理状态直接喂给 AI，并用 Prompt 锁死它的行为。

**修改点 1：Review Prompt 更新**

```python
# scripts/ai_trader/brain/brain.py

ML_RESULT_REVIEW_PROMPT = (
    "You are an ML result reviewer for a trading system. Return ONLY strict JSON.\n"
    # ... (原有 Schema) ...
    
    "\n"
    "==== DATA STATUS AWARENESS (CRITICAL) ====\n"
    "You will receive a 'data_status' object in INPUT:\n"
    "  - can_train: bool (Is data sufficient to run ML?)\n"
    "  - missing_reason: 'missing_base_data' | 'missing_column' | 'none'\n"
    "  - coverage: float (0.0 - 1.0)\n"
    "\n"
    "HARD RULES FOR NEXT ACTION:\n"
    "1. IF data_status.can_train == false AND missing_reason == 'missing_base_data':\n"
    "   - SITUATION: The database is empty. It is a physical infra issue.\n"
    "   - ACTION: MUST be 'wait_for_data'.\n"
    "   - DO NOT propose features (they will fail validation).\n"
    "   - DO NOT adjust params (useless on empty data).\n"
    "   - RATIONALE: 'Waiting for DataHealer to backfill base data.'\n"
    "\n"
    "2. IF data_status.can_train == false AND missing_reason == 'missing_column':\n"
    "   - SITUATION: You asked for a column that doesn't exist in the table schema.\n"
    "   - ACTION: 'propose_features' (Correction).\n"
    "   - STRATEGY: Propose a SIMPLER feature spec using ONLY columns listed in 'AVAILABLE_COLUMNS'.\n"
    "\n"
    # ... (原有 Guidelines) ...
)
```

**修改点 2：构建 Context 时注入 `data_status`**

在 `brain.py` 或调用 `review_ml` 的地方（可能是 `orchestrator.py` 或 `ml_scheduler.py`），在构造 `context` 字典时，增加这一段逻辑：

```python
# 伪代码示例
def build_ml_review_context(self):
    # ...
    
    # 实时计算 data_status
    symbols = ["SPY", "TSLA"]
    voids = self.data_healer._check_void(symbols, days=2)
    
    context["data_status"] = {
        "can_train": len(voids) == 0,
        "missing_reason": "missing_base_data" if voids else "none",
        "coverage": 0.0 if voids else 1.0, # 简化指标
        "void_symbols": voids
    }
    
    return context
```

-----

### 🚀 落地执行清单

1.  **升级 DataHealer**：
      * 将 `services/data/data_healer.py` 更新为支持 `DataRequirement` 的版本。
2.  **接入 Pipeline**：
      * 在你的 OOF Runner (`scripts/ai_trader/ml/job_runner.py` 等) 启动前，加上：
        ```python
        req = DataRequirement(symbols=..., lookback_days=...)
        res = data_healer.ensure_requirements_ready(req)
        if res['status'] == 'failed':
            return # 终止任务，不要跑 AUC=0.5 的结果
        ```
3.  **更新 Prompt**：
      * 修改 `brain.py`，加入 `DATA STATUS AWARENESS` 规则。
4.  **重启验证**：
      * 清空今天可能产生的垃圾日志。
      * 重启服务。
      * 你会看到：Brain 第一次可能会说 "Waiting for data..." -\> Healer 介入回填 -\> 数据就绪 -\> Brain 下一次开始正常提特征。

这就彻底终结了“没有数据还在那儿瞎忙活”的僵尸状态。
我先给一句总评，然后拆开说细节：

> **这版方案方向是对的，而且已经非常接近你想要的“学习前 + 学习中都能动态回补、且不空转”的形态。**
>
> 现在更多是一些“工程层面的小坑”和“细节要对上你真实系统”的问题，而不是思路错误。

---

## 一、这版方案真正解决了哪几个核心痛点？

我按你最关心的两件事来对照：

### 1. 学习前：不再在“彻底没数据”的情况下乱跑

* `evening_learning / intraday_sequence` 之前加 `ensure_data_ready`（旧接口保留）
* 现在又有 `ensure_requirements_ready(DataRequirement)` 可以在 **具体训练任务 / OOF 任务之前** 做更细的检查。
* 数据真空、ClickHouse 里根本没 `features_1m` 时，不再让 AI 去跑 AUC=0.5 的模型，直接返回 `failed` / `critical_data_void`。

✅ 这基本堵死了你之前那种「coverage=0，AUC=0.5 一直刷」的情况。

### 2. 学习中：AI 想到新的数据需求，可以被“翻译成回补任务”

* Validator 不再只返回一本糊涂账的 `no_valid_specs`，而是拆成：

  * `dsl_invalid`（语法错）
  * `missing_column`（列不存在 → 特征/ETL问题）
  * `missing_base_data`（数据缺失 → 物理回补问题 ✅）
* 在 `missing_base_data` 的情况下，还会带上 `dependencies`：

  ```json
  "dependencies": {
    "symbols": ["TSLA"],
    "lookback_days": 2,
    "tables": ["features_1m"]
  }
  ```
* 上一层可以把这个 `dependencies` 包装成 `DataRequirement`，交给 `DataHealer.ensure_requirements_ready(req)` 去跑 **针对性的 backfill**。

✅ 这就打通了你说的那条链路：

> “AI 在学习中想到新特征 → 发现缺底层数据 → 不是瞎改特征，而是发起回补 → 数据到位后再继续学。”

### 3. Brain 端：知道什么时候“该闭嘴等数据”

* Prompt 里有 `data_status` + 硬规则：

  * `can_train == false && missing_reason == missing_base_data` → 只能 `wait_for_data`，不能提特征/调参
  * `missing_column` 才鼓励 Brain 去“改写特征，用现有列重写”

✅ 这样 Brain 本身不再制造“僵尸空转”，而是乖乖把“物理数据问题”交给 DataHealer / Orchestrator。

整体上，这三个改动配合起来，**逻辑闭环是成立的**。

---

## 二、我觉得需要注意 / 微调的几个点

这部分是我觉得你照着实现时，容易踩坑的地方。

### 1. DataHealer 的精细回补还可以再“对齐业务需求”

当前 `_check_void` 逻辑是：

```python
HAVING count() < 100
AND toDate(ts) >= start_date
```

问题：

* 100 这个阈值是个 **拍脑袋的常数**，对不同窗口和任务可能完全不够：

  * 如果是 30 天窗口，100 条记录可能只是 1–2 天的数据；
  * 如果只是检查“今天盘中是否有数据”，100 条又可能太多。
* 现在只看一张 `features_1m` 或 `bars_1m` 表，没有考虑：

  * label 表（未来收益）
  * 某些特征列的 **非空覆盖率**（比如 `atr_14` 全是 null 也没用）

👉 建议小改一下思路：

* `DataRequirement` 里已经有：

  * `lookback_days`
  * `tables`
  * `required_columns`
* 你可以让 `_check_void` 接收整个 `req`，然后：

  * `min_rows = min_rows_per_day * lookback_days`（配置化，例如 200 * days）
  * 如果需要 label，就对 label 表也跑一遍统计
  * 额外加一个“required_columns 非空比例 > 某阈值”的检查（比如 0.8）

不用一次写到很复杂，**先加两个可配置参数**会比死写 `100` 要好非常多。

### 2. 要防止 DataHealer 被“并发炸死”

现在的 `_run_targeted_backfill` 是同步阻塞调用 `load_hist_multi.py`，如果：

* 多个 OOF 任务 / 多个 feature proposal 几乎同时触发 `missing_base_data`，
* 全部去调用 `ensure_requirements_ready`，就可能多次重复 backfill 同一批 symbol。

建议：

* 在 DataHealer 内部做一个简单的 **“软锁缓存”**，比如：

  * 用 `(tuple(sorted(symbols)), start_date)` 做 key，
  * 正在回补时记一条 in-memory 状态（或者写一个 `.healer_lock_xxx` 到 `runs_dir`），
  * 其他任务看到已有回补在跑，就直接 `status="waiting"` / 小 sleep + 重试，而不是再起新进程。

你现在已经有 `backfill_XXX.lock` 的机制，这里只是再加一层逻辑。

### 3. Validator 不要直接做“重型操作”

Gemini 给的伪代码里，Validator 调了：

```python
void_symbols = self.data_healer._check_void(...)
```

大方向 OK，但我会建议：

* Validator 层只做 **“便宜检查 + 结构化错误返回”**：

  * `dsl_invalid` / `missing_column` / `missing_base_data`
  * 对于 `missing_base_data`，构建好 `dependencies`（其实就是 DataRequirement 所需字段）
* 真正的 `ensure_requirements_ready()` 和回补逻辑，放在：

  * 任务调度器 / orchestrator / ml_job_runner 那一层

理由：

* `ensure_requirements_ready` 是一个 **可能阻塞几十秒甚至几分钟** 的操作；
* 如果你把它塞在一个本来只应该“快速判断” 的 validator 里，很容易出现：

  * LLM 调用链条被卡死
  * 整个 “提特征 → 验证” pipeline latency 爆炸

更推荐的 flow 是：

1. Brain 提案 → Validator 返回 `reason=missing_base_data, dependencies=...`
2. 上层 job handler 看到这个 reason：

   * `status="WAITING_FOR_DATA"` 写进 `ml_events.jsonl`
   * 调 `DataHealer.ensure_requirements_ready(req)`（异步或同步都行）
   * 等下一轮 scheduler 再重试这次 proposal / OOF。

### 4. `data_status` 必须由“真实世界信息”驱动，而不是 Brain 自己猜

现在 Prompt 里已经有规则了，但要确保：

* `data_status` 是你在 Orchestrator / job_runner 里**计算好的**：

  * 基于 `data_quality.json` + `DataHealer` 返回值 + 真实 coverage 统计；
* 而不是“Brain 通过 summary 自己瞎推断”。

简单的做法（你可以先实现一个 MVP）：

```python
data_status = {
    "can_train": (coverage > 0.5 and trades > 最低门槛),
    "missing_reason": "missing_base_data" if coverage == 0 else "none",
    "coverage": coverage,
    "void_symbols": void_symbols_from_healer
}
```

然后再喂给 Brain。
这样，**Brain 的行为是被你“锁死在现实里”的**，不会又跑出什么奇怪的解释。

---

## 三、如果让我给这版方案“打标签”

我会这么分层：

* **V1：只在 evening_learning 前跑 DataHealer，阻止在完全没数据时训练。**
  → 你之前那版已经是 V1。

* **V2：这次 Gemini 更新版：**

  * DataRequirement
  * Validator 错误细分 + dependencies
  * Brain 的 data_status 规则
    → ✅ 已经是一个 **成体系的“AI 需求 → 数据回补”闭环设计**，可以实施。

* **V2.5（我建议你再加的东西）：**

  1. `_check_void` 用配置化阈值 + lookback_days，而不是死写 100；
  2. DataHealer 内部做简单去重/软锁，防止并发爆仓；
  3. 把 `ensure_requirements_ready` 放在 job runner/ orchestrator，而不是 Validator 内部；
  4. 明确由 orchestrator 构造 `data_status` 喂给 Brain，而不是让 Brain自己猜。

---

## 四、下一步你可以怎么落地（优先顺序）

如果你现在就要动手，我会建议按这个顺序：

1. **先合并 DataHealer + ensure_requirements_ready + DataRequirement**（保留老的 ensure_data_ready，方便渐进迁移）。
2. **在你 OOF / 训练 job 的入口，把原来“直接开跑”的地方，改成：**

   * 先构造一个 DataRequirement（symbol list + lookback_days）
   * 调 `ensure_requirements_ready`，failed 就直接 `return`，不跑这次 job。
3. 等这一层稳定后，再逐步引入：

   * Validator 的 `missing_base_data / missing_column` 区分 + dependencies
   * Brain 的 `data_status` 规则。

这样你是以 **“最小可用单元 + 渐进式控制风险”** 的方式在升级，而不是一次性全改。

---

**一句话总结回答你：**

> 这版 Gemini 的设计思路我基本是认可的，方向完全对，已经解决了你之前吐槽的绝大部分问题。
> 我建议你按它的框架落地，但在阈值、并发控制、责任边界（Validator vs Orchestrator）、`data_status` 的来源上做一点工程级的强化，就能真正做到你要的那种：
> **“AI 想要什么数据，就能被翻译成回补任务，而不是瞎忙；没有数据的时候，系统自己会停下来等，不再空转。”**
