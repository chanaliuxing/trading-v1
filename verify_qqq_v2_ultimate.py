import os
import sys
import asyncio
from datetime import datetime, time
from unittest.mock import MagicMock, AsyncMock, patch
from zoneinfo import ZoneInfo

# Setup path
sys.path.append(os.getcwd())

from libs.strategy import StrategyParams
from strategies.qqq_v2.strategy import QQQIntradayStrategyV2

ET = ZoneInfo("America/New_York")

async def run_ultimate_check():
    print("=== QQQ V2 ULTIMATE INTEGRATION CHECK ===")
    
    # 1. Setup Mock Context
    ctx = MagicMock()
    ctx.log = MagicMock()
    ctx.strategy_id = "qqq_v2_ultimate"
    ctx.engine = MagicMock()
    ctx.position = AsyncMock(return_value=None)
    ctx.open_orders = AsyncMock(return_value=[])
    ctx.submit_intent = AsyncMock()
    
    params = StrategyParams()
    params.extras = {"notional_usd": 10000.0, "loop_interval_sec": 0.01}
    
    strategy = QQQIntradayStrategyV2(ctx, params)
    
    with patch("strategies.qqq_v2.strategy.StateMemex") as mock_memex_cls, \
         patch("strategies.qqq_v2.strategy.HistoryProvider"), \
         patch("strategies.qqq_v2.strategy.QQQDataFabric") as mock_qqq_fabric_cls:
        
        # Setup mocks
        mock_memex_cls.return_value.initialize = AsyncMock()
        mock_memex_cls.return_value.save = AsyncMock()
        mock_qqq_fabric_cls.return_value.initialize = AsyncMock()
        
        print("[1/3] Initializing Strategy with StateLock...")
        await strategy.initialize()
        
        # 2. Test Sampling Density (The "Accumulator" Test)
        print("[2/3] Testing Tick-by-Tick Sampling Density...")
        # Mocking StrategicAnalyzer to track calls
        strategy.analyzer.collect_seed_sample = MagicMock()
        
        # PREVENT REAL INGEST: We want to control the ticks
        strategy.fabric.ingest = AsyncMock() 
        
        # Inject 5 ticks into fabric manually (Set timestamps within 09:30-09:45 ET)
        base_dt = datetime.now(ET).replace(hour=9, minute=35, second=0)
        base_ts = int(base_dt.timestamp() * 1000)
        
        strategy.fabric.ticks_1s["QQQ"].append((base_ts - 4000, 400.0))
        strategy.fabric.ticks_1s["QQQ"].append((base_ts - 3000, 400.1))
        strategy.fabric.ticks_1s["QQQ"].append((base_ts - 2000, 400.2))
        strategy.fabric.ticks_1s["QQQ"].append((base_ts - 1000, 400.3))
        strategy.fabric.ticks_1s["QQQ"].append((base_ts, 400.4))
        
        # Ensure watermark is zero to pick up all 5
        strategy.fabric._logic_watermark_ms["QQQ"] = 0
        
        # Run one loop step using the same logical time
        await strategy._run_loop_step(base_dt)
        
        # VERIFY: StrategicAnalyzer should have been called 5 TIMES
        call_count = strategy.analyzer.collect_seed_sample.call_count
        if call_count == 5:
            print(f"  SUCCESS: 5 ticks replayed. call_count={call_count}")
        else:
            print(f"  FAILURE: Sampling lost! call_count={call_count} (Expected 5)")
            return

        # 3. Test Anchored VWAP
        print("[3/3] Testing Anchored VWAP Consistency...")
        # Manually set some volume to accumulators
        strategy.fabric._day_pv_sum["QQQ"] = 400.0 * 100.0
        strategy.fabric._day_vol_sum["QQQ"] = 100.0
        
        vwap = strategy.fabric.get_anchored_vwap("QQQ")
        if vwap == 400.0:
            print(f"  SUCCESS: Anchored VWAP calculated correctly: {vwap}")
        else:
            print(f"  FAILURE: Anchored VWAP drift! vwap={vwap}")
            return

    print("\n--- ULTIMATE CHECK PASSED ---")
    if hasattr(strategy, "_main_task"): strategy._main_task.cancel()
    await strategy.auditor.stop()

if __name__ == "__main__":
    asyncio.run(run_ultimate_check())
