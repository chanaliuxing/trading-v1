from __future__ import annotations

"""
Realtime Feature Freshness Gate

检查内存中 RealtimeFeatureEngine 的特征新鲜度，适用于使用实时特征引擎的策略。

不同于旧的 FeatureFreshnessGate (检查 ClickHouse)，这个 Gate:
- 检查策略引擎中每个 symbol 的最后 bar 更新时间
- 从 StrategyEngine 注册表读取活跃策略
- 判断数据流是否 stale/dead

Config (policies.gates.realtime_feature_freshness):
  enabled: true
  threshold_stale_sec: 120   # 2分钟没更新 = stale
  threshold_dead_sec: 300    # 5分钟没更新 = dead
  check_strategy_ids: ["enhanced_ml_001"]  # 要检查的策略 ID 列表

Best-effort: 如果无法获取策略信息,返回 ok=True (advisory, not blocking)
"""

import time
from typing import Any, Dict, List, Optional
from pathlib import Path
import json

from libs.logger import get_logger


class RealtimeFeatureFreshnessGate:
    """检查内存实时特征引擎的新鲜度"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config or {}
        self.log = get_logger("ai_trader.realtime_feature_freshness_gate")

        self.th_stale = int(self.config.get("threshold_stale_sec", 120))  # 2分钟
        self.th_dead = int(self.config.get("threshold_dead_sec", 300))   # 5分钟

        # 要检查的策略 ID 列表
        self.strategy_ids = self.config.get("check_strategy_ids", [])

        # 策略状态文件路径 (StrategyEngine 会定期更新)
        self.status_dir = Path("runs/strategies/status")

        self.log.info("realtime_ff_gate_initialized",
                     th_stale=self.th_stale,
                     th_dead=self.th_dead,
                     strategy_ids=self.strategy_ids)

    def _get_strategy_status(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        """读取策略状态文件

        Args:
            strategy_id: 策略 ID

        Returns:
            策略状态字典,包含:
            - last_updated: 最后更新时间戳
            - symbols: {symbol: {last_bar_ts: timestamp, last_feature_ts: timestamp}}
        """
        try:
            status_file = self.status_dir / f"{strategy_id}_status.json"
            if not status_file.exists():
                return None

            with status_file.open("r") as f:
                return json.load(f)
        except Exception as e:
            self.log.warning("strategy_status_read_failed",
                           strategy_id=strategy_id,
                           err=str(e))
            return None

    def _check_symbol_freshness(
        self,
        symbol: str,
        last_update_ts: Optional[float],
        now: float
    ) -> tuple[str, Optional[int]]:
        """检查单个 symbol 的新鲜度

        Args:
            symbol: 标的符号
            last_update_ts: 最后更新时间戳
            now: 当前时间戳

        Returns:
            (status, age_sec) where status in ["ok", "stale", "dead"]
        """
        if last_update_ts is None:
            return ("dead", None)

        age = now - last_update_ts

        if age <= self.th_stale:
            return ("ok", int(age))
        elif age <= self.th_dead:
            return ("stale", int(age))
        else:
            return ("dead", int(age))

    def check(self, symbols: List[str]) -> Dict[str, Any]:
        """检查指定 symbols 的特征新鲜度

        Args:
            symbols: 要检查的标的列表

        Returns:
            {
                "ok": bool,                      # 是否所有 symbol 都 ok
                "ok_symbols": [str],             # ok 的 symbol 列表
                "stale": {symbol: {age_sec, strategy_id}},   # stale 的详情
                "dead": {symbol: {age_sec, strategy_id}},    # dead 的详情
                "advisory": bool                 # 是否仅为 advisory (无法获取数据)
            }
        """
        now = time.time()

        res: Dict[str, Any] = {
            "ok": True,
            "ok_symbols": [],
            "stale": {},
            "dead": {},
            "advisory": False
        }

        if not symbols:
            return res

        if not self.strategy_ids:
            self.log.warning("no_strategy_ids_configured")
            res["advisory"] = True
            return res

        # 收集所有策略中 symbols 的最后更新时间
        symbol_updates: Dict[str, Dict[str, Any]] = {s: {} for s in symbols}

        for strategy_id in self.strategy_ids:
            status = self._get_strategy_status(strategy_id)
            if not status:
                continue

            symbol_data = status.get("symbols", {})

            for symbol in symbols:
                if symbol not in symbol_data:
                    continue

                sdata = symbol_data[symbol]

                # 优先使用 last_feature_ts (特征计算完成时间)
                # 其次使用 last_bar_ts (最后 bar 时间)
                last_ts = sdata.get("last_feature_ts") or sdata.get("last_bar_ts")

                if last_ts:
                    # 取多个策略中最新的时间
                    if "last_ts" not in symbol_updates[symbol] or \
                       last_ts > symbol_updates[symbol].get("last_ts", 0):
                        symbol_updates[symbol] = {
                            "last_ts": last_ts,
                            "strategy_id": strategy_id
                        }

        # 检查每个 symbol 的新鲜度
        for symbol in symbols:
            update_info = symbol_updates[symbol]
            last_ts = update_info.get("last_ts")
            strategy_id = update_info.get("strategy_id", "unknown")

            status, age = self._check_symbol_freshness(symbol, last_ts, now)

            if status == "ok":
                res["ok_symbols"].append(symbol)
            elif status == "stale":
                res["ok"] = False
                res["stale"][symbol] = {
                    "age_sec": age,
                    "strategy_id": strategy_id
                }
            else:  # dead
                res["ok"] = False
                res["dead"][symbol] = {
                    "age_sec": age,
                    "strategy_id": strategy_id
                }

        # 如果没有任何策略数据,标记为 advisory
        if all(not v for v in symbol_updates.values()):
            res["advisory"] = True
            self.log.warning("no_strategy_data_available",
                           strategy_ids=self.strategy_ids)

        # 日志
        if not res["ok"] and not res["advisory"]:
            self.log.warning("realtime_feature_freshness_not_ok",
                           stale=len(res["stale"]),
                           dead=len(res["dead"]),
                           stale_symbols=list(res["stale"].keys()),
                           dead_symbols=list(res["dead"].keys()))
        else:
            self.log.info("realtime_feature_freshness_ok",
                        checked=len(symbols),
                        ok=len(res["ok_symbols"]),
                        advisory=res["advisory"])

        return res


# Self-test
if __name__ == "__main__":
    print("\n" + "="*80)
    print("RealtimeFeatureFreshnessGate Self-Test")
    print("="*80)

    # Test 1: 创建 gate
    print("\n[Test 1] Gate Initialization...")
    gate = RealtimeFeatureFreshnessGate({
        "threshold_stale_sec": 120,
        "threshold_dead_sec": 300,
        "check_strategy_ids": ["enhanced_ml_001"]
    })
    print(f"  ✓ Gate created")
    print(f"    Stale threshold: {gate.th_stale}s")
    print(f"    Dead threshold: {gate.th_dead}s")
    print(f"    Strategy IDs: {gate.strategy_ids}")

    # Test 2: 创建模拟状态文件
    print("\n[Test 2] Creating Mock Strategy Status...")
    import tempfile
    from pathlib import Path
    import json

    with tempfile.TemporaryDirectory() as tmpdir:
        status_dir = Path(tmpdir)
        gate.status_dir = status_dir

        # 模拟策略状态: AAPL 是 ok, TSLA 是 stale, NVDA 是 dead
        now = time.time()
        mock_status = {
            "last_updated": now,
            "symbols": {
                "AAPL": {
                    "last_bar_ts": now - 30,        # 30秒前 - OK
                    "last_feature_ts": now - 20     # 20秒前 - OK
                },
                "TSLA": {
                    "last_bar_ts": now - 150,       # 150秒前 - STALE
                    "last_feature_ts": now - 140
                },
                "NVDA": {
                    "last_bar_ts": now - 400,       # 400秒前 - DEAD
                    "last_feature_ts": None
                }
            }
        }

        status_file = status_dir / "enhanced_ml_001_status.json"
        with status_file.open("w") as f:
            json.dump(mock_status, f, indent=2)

        print(f"  ✓ Mock status file created: {status_file}")

        # Test 3: 检查新鲜度
        print("\n[Test 3] Checking Freshness...")
        result = gate.check(["AAPL", "TSLA", "NVDA"])

        print(f"  Overall OK: {result['ok']}")
        print(f"  OK symbols: {result['ok_symbols']}")
        print(f"  Stale symbols: {list(result['stale'].keys())}")
        print(f"  Dead symbols: {list(result['dead'].keys())}")

        # 验证结果
        assert not result["ok"], "Should not be OK (has stale/dead)"
        assert "AAPL" in result["ok_symbols"], "AAPL should be OK"
        assert "TSLA" in result["stale"], "TSLA should be STALE"
        assert "NVDA" in result["dead"], "NVDA should be DEAD"

        print("\n  ✓ Freshness detection working correctly")

        # Test 4: 详细信息
        print("\n[Test 4] Detailed Information...")
        if "TSLA" in result["stale"]:
            stale_info = result["stale"]["TSLA"]
            print(f"  TSLA stale info:")
            print(f"    Age: {stale_info['age_sec']}s")
            print(f"    Strategy: {stale_info['strategy_id']}")

        if "NVDA" in result["dead"]:
            dead_info = result["dead"]["NVDA"]
            print(f"  NVDA dead info:")
            print(f"    Age: {dead_info.get('age_sec', 'N/A')}s")
            print(f"    Strategy: {dead_info['strategy_id']}")

    print("\n" + "="*80)
    print("✅ ALL TESTS PASSED")
    print("="*80)

    print("\nUsage in orchestrator:")
    print("  1. StrategyEngine 定期写入 runs/strategies/status/{strategy_id}_status.json")
    print("  2. RealtimeFeatureFreshnessGate 读取状态文件")
    print("  3. 返回 ok/stale/dead 分类给 AI Brain")
    print("  4. Brain 根据新鲜度调整策略 (safe_mode, per_symbol_override)")
