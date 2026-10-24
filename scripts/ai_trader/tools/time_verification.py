#!/usr/bin/env python3
"""
Time Verification Tool - 时间正确性验证工具

用途：在修改时区代码之前，部署此工具验证所有时间来源的一致性和正确性。
作为交易系统，时间错误风险极大，必须建立完整的验证机制。

验证内容：
1. 多源时间对比 (ZoneInfo vs pytz vs UTC转换)
2. 外部NTP验证 (WorldTimeAPI)
3. 市场数据时间戳验证
4. 交易时段验证
5. 时间单调性检查

Usage:
    python scripts/ai_trader/tools/time_verification.py --full
    python scripts/ai_trader/tools/time_verification.py --quick
    python scripts/ai_trader/tools/time_verification.py --monitor  # 持续监控模式
"""

import asyncio
import datetime
import json
import sys
from pathlib import Path
from typing import Dict, Any, Optional
from zoneinfo import ZoneInfo
import pytz

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from libs.logger import get_logger

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    from libs.db.clickhouse import ClickHouseDB
except ImportError:
    ClickHouseDB = None

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None

log = get_logger("ai_trader.time_verification")

NY_TZ = ZoneInfo("America/New_York")
NY_PYTZ = pytz.timezone("America/New_York")


class TimeVerifier:
    """时间验证器 - 多源验证确保时间正确性"""

    def __init__(self):
        self.results: Dict[str, Any] = {}
        self.errors: list = []
        self.ch: Optional[ClickHouseDB] = None
        self.redis: Optional[Any] = None

    async def initialize(self):
        """初始化数据库连接"""
        try:
            if ClickHouseDB:
                self.ch = ClickHouseDB()
        except Exception as e:
            log.warning("ch_unavailable", err=str(e))

        try:
            if aioredis:
                self.redis = await aioredis.from_url("redis://localhost:6379")
        except Exception as e:
            log.warning("redis_unavailable", err=str(e))

    async def cleanup(self):
        """清理资源"""
        if self.redis:
            await self.redis.close()

    def check_1_multi_source_time(self) -> Dict[str, Any]:
        """验证1: 多源时间对比 - ZoneInfo vs pytz vs UTC转换"""
        log.info("check_1_start", name="multi_source_time")

        # Method 1: ZoneInfo
        now_zoneinfo = datetime.datetime.now(NY_TZ)

        # Method 2: pytz
        now_pytz = datetime.datetime.now(NY_PYTZ)

        # Method 3: UTC then convert
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_from_utc_zoneinfo = now_utc.astimezone(NY_TZ)
        now_from_utc_pytz = now_utc.astimezone(NY_PYTZ)

        # Compare all methods
        sources = {
            "zoneinfo": now_zoneinfo,
            "pytz": now_pytz,
            "utc_to_zoneinfo": now_from_utc_zoneinfo,
            "utc_to_pytz": now_from_utc_pytz,
        }

        # Calculate max difference
        times = list(sources.values())
        max_diff_sec = max(
            abs((t1 - t2).total_seconds()) for t1 in times for t2 in times
        )

        ok = max_diff_sec < 1.0  # All sources should agree within 1 second

        result = {
            "check": "multi_source_time",
            "ok": ok,
            "max_diff_sec": max_diff_sec,
            "threshold_sec": 1.0,
            "sources": {k: v.isoformat() for k, v in sources.items()},
            "utc_offset": now_zoneinfo.strftime("%z"),
            "tzname": now_zoneinfo.tzname(),
        }

        if not ok:
            err = f"Time source mismatch: max diff {max_diff_sec:.3f}s > 1.0s"
            self.errors.append(err)
            log.error("check_1_failed", **result)
        else:
            log.info("check_1_passed", **result)

        return result

    async def check_2_ntp_verification(self) -> Dict[str, Any]:
        """验证2: 外部NTP验证 - 使用WorldTimeAPI"""
        log.info("check_2_start", name="ntp_verification")

        if not aiohttp:
            return {
                "check": "ntp_verification",
                "ok": False,
                "skipped": True,
                "reason": "aiohttp not available",
            }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://worldtimeapi.org/api/timezone/America/New_York",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        raise Exception(f"HTTP {resp.status}")
                    data = await resp.json()

            ntp_time_str = data.get("datetime", "")
            if not ntp_time_str:
                raise Exception("No datetime in response")

            # Parse NTP time
            ntp_time = datetime.datetime.fromisoformat(
                ntp_time_str.replace("Z", "+00:00")
            )

            # Local time
            local_time = datetime.datetime.now(NY_TZ)

            # Calculate drift
            drift_sec = abs((local_time - ntp_time).total_seconds())
            ok = drift_sec < 5.0  # Allow 5 second drift

            result = {
                "check": "ntp_verification",
                "ok": ok,
                "drift_sec": drift_sec,
                "threshold_sec": 5.0,
                "ntp_time": ntp_time.isoformat(),
                "local_time": local_time.isoformat(),
                "ntp_source": "worldtimeapi.org",
            }

            if not ok:
                err = f"NTP drift too large: {drift_sec:.3f}s > 5.0s"
                self.errors.append(err)
                log.error("check_2_failed", **result)
            else:
                log.info("check_2_passed", **result)

            return result

        except Exception as e:
            result = {
                "check": "ntp_verification",
                "ok": False,
                "error": str(e),
            }
            self.errors.append(f"NTP check failed: {e}")
            log.error("check_2_error", **result)
            return result

    async def check_3_market_data_timestamp(self) -> Dict[str, Any]:
        """验证3: 市场数据时间戳验证"""
        log.info("check_3_start", name="market_data_timestamp")

        # Check Redis SFS data
        redis_result = await self._check_redis_sfs_time()

        # Check ClickHouse data
        ch_result = await self._check_clickhouse_time()

        # Overall OK if at least one source is healthy
        ok = redis_result.get("ok", False) or ch_result.get("ok", False)

        result = {
            "check": "market_data_timestamp",
            "ok": ok,
            "redis_sfs": redis_result,
            "clickhouse": ch_result,
        }

        if not ok:
            err = "Market data timestamps verification failed"
            self.errors.append(err)
            log.error("check_3_failed", **result)
        else:
            log.info("check_3_passed", **result)

        return result

    async def _check_redis_sfs_time(self) -> Dict[str, Any]:
        """检查Redis SFS数据的时间戳"""
        if not self.redis:
            return {"ok": False, "skipped": True, "reason": "redis unavailable"}

        try:
            # Get latest 1s bar for TSLA
            key = "sfs:1s:TSLA"
            raw = await self.redis.lindex(key, 0)
            if not raw:
                return {"ok": False, "reason": "no data in Redis"}

            bar = json.loads(raw)
            bar_ts_ms = bar.get("ts")
            if not bar_ts_ms:
                return {"ok": False, "reason": "no ts field in bar"}

            # Convert to datetime
            bar_time = datetime.datetime.fromtimestamp(
                bar_ts_ms / 1000.0, tz=NY_TZ
            )
            now = datetime.datetime.now(NY_TZ)
            age_sec = (now - bar_time).total_seconds()

            # Data should be fresh (< 60 seconds old)
            ok = 0 <= age_sec < 60

            return {
                "ok": ok,
                "source": "redis_sfs",
                "key": key,
                "bar_time": bar_time.isoformat(),
                "now": now.isoformat(),
                "age_sec": age_sec,
                "threshold_sec": 60,
            }

        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _check_clickhouse_time(self) -> Dict[str, Any]:
        """检查ClickHouse数据的时间戳"""
        if not self.ch:
            return {"ok": False, "skipped": True, "reason": "clickhouse unavailable"}

        try:
            # Get latest bar from trading.bars_1m
            result = self.ch.query(
                """
                SELECT
                    symbol,
                    ts,
                    now('America/New_York') as now_ny,
                    dateDiff('second', ts, now('America/New_York')) as age_sec
                FROM trading.bars_1m
                WHERE symbol = 'TSLA'
                ORDER BY ts DESC
                LIMIT 1
                """
            )

            if not result or len(result) == 0:
                return {"ok": False, "reason": "no data in ClickHouse"}

            row = result[0]
            age_sec = row.get("age_sec", 999999)

            # During trading hours: data should be < 5 min old
            # Outside trading hours: allow older data
            now = datetime.datetime.now(NY_TZ)
            hour = now.hour

            if 9 <= hour < 16:  # Trading hours
                threshold = 300  # 5 minutes
            else:
                threshold = 86400  # 24 hours

            ok = 0 <= age_sec < threshold

            return {
                "ok": ok,
                "source": "clickhouse",
                "table": "trading.bars_1m",
                "symbol": row.get("symbol"),
                "bar_time": str(row.get("ts")),
                "now": str(row.get("now_ny")),
                "age_sec": age_sec,
                "threshold_sec": threshold,
            }

        except Exception as e:
            return {"ok": False, "error": str(e)}

    def check_4_trading_session(self) -> Dict[str, Any]:
        """验证4: 交易时段验证"""
        log.info("check_4_start", name="trading_session")

        now = datetime.datetime.now(NY_TZ)
        hour = now.hour
        minute = now.minute
        time_str = now.strftime("%H:%M:%S")

        # Determine expected session
        if 20 <= hour or hour < 4:
            expected = "OVERNIGHT"
        elif 4 <= hour < 9 or (hour == 9 and minute < 30):
            expected = "PRE"
        elif (hour == 9 and minute >= 30) or (9 < hour < 16):
            expected = "RTH"
        elif 16 <= hour < 20:
            expected = "AFTERHOURS"
        else:
            expected = "UNKNOWN"

        # Cross-validate with time_manager logic if available
        try:
            from llm_trader.llm_trader.time_manager import TimeManager
            from llm_trader.llm_trader.ny_time import now_ny

            tm = TimeManager()
            actual_phase = tm.session_phase(now_ny())

            matches = actual_phase == expected

            result = {
                "check": "trading_session",
                "ok": matches,
                "now_ny": now.isoformat(),
                "time_str": time_str,
                "expected_phase": expected,
                "time_manager_phase": actual_phase,
                "matches": matches,
            }

            if not matches:
                err = f"Session mismatch: expected {expected}, got {actual_phase}"
                self.errors.append(err)
                log.error("check_4_failed", **result)
            else:
                log.info("check_4_passed", **result)

        except Exception as e:
            result = {
                "check": "trading_session",
                "ok": False,
                "error": str(e),
                "now_ny": now.isoformat(),
                "time_str": time_str,
            }
            self.errors.append(f"Session check error: {e}")
            log.error("check_4_error", **result)

        return result

    def check_5_monotonicity(self) -> Dict[str, Any]:
        """验证5: 时间单调性检查 - 检测时钟回退"""
        log.info("check_5_start", name="monotonicity")

        # Get current time
        now = datetime.datetime.now(NY_TZ)

        # Check if we have a previous timestamp saved
        marker_file = Path("/tmp/ai_trader_time_marker.json")

        if marker_file.exists():
            try:
                prev_data = json.loads(marker_file.read_text())
                prev_time_str = prev_data.get("timestamp")
                if prev_time_str:
                    prev_time = datetime.datetime.fromisoformat(prev_time_str)
                    delta_sec = (now - prev_time).total_seconds()

                    # Time should always move forward
                    ok = delta_sec >= -1.0  # Allow 1 second tolerance

                    result = {
                        "check": "monotonicity",
                        "ok": ok,
                        "now": now.isoformat(),
                        "prev_time": prev_time.isoformat(),
                        "delta_sec": delta_sec,
                        "clock_rollback": delta_sec < -1.0,
                    }

                    if not ok:
                        err = f"Clock rollback detected: {delta_sec:.3f}s"
                        self.errors.append(err)
                        log.error("check_5_failed", **result)
                    else:
                        log.info("check_5_passed", **result)

                    # Update marker
                    marker_file.write_text(
                        json.dumps({"timestamp": now.isoformat()})
                    )
                    return result

            except Exception as e:
                log.warning("monotonicity_check_error", err=str(e))

        # First run - save marker
        marker_file.write_text(json.dumps({"timestamp": now.isoformat()}))

        return {
            "check": "monotonicity",
            "ok": True,
            "first_run": True,
            "now": now.isoformat(),
        }

    async def run_full_verification(self) -> Dict[str, Any]:
        """运行完整验证"""
        log.info("verification_start", mode="full")

        await self.initialize()

        try:
            # Run all checks
            results = {
                "check_1_multi_source": self.check_1_multi_source_time(),
                "check_2_ntp": await self.check_2_ntp_verification(),
                "check_3_market_data": await self.check_3_market_data_timestamp(),
                "check_4_trading_session": self.check_4_trading_session(),
                "check_5_monotonicity": self.check_5_monotonicity(),
            }

            # Overall status
            all_ok = all(r.get("ok", False) for r in results.values())

            summary = {
                "timestamp": datetime.datetime.now(NY_TZ).isoformat(),
                "overall_ok": all_ok,
                "checks_passed": sum(1 for r in results.values() if r.get("ok", False)),
                "checks_total": len(results),
                "errors": self.errors,
                "results": results,
            }

            log.info(
                "verification_complete",
                ok=all_ok,
                passed=summary["checks_passed"],
                total=summary["checks_total"],
            )

            return summary

        finally:
            await self.cleanup()

    async def run_quick_verification(self) -> Dict[str, Any]:
        """运行快速验证 (仅本地检查)"""
        log.info("verification_start", mode="quick")

        results = {
            "check_1_multi_source": self.check_1_multi_source_time(),
            "check_4_trading_session": self.check_4_trading_session(),
            "check_5_monotonicity": self.check_5_monotonicity(),
        }

        all_ok = all(r.get("ok", False) for r in results.values())

        summary = {
            "timestamp": datetime.datetime.now(NY_TZ).isoformat(),
            "overall_ok": all_ok,
            "checks_passed": sum(1 for r in results.values() if r.get("ok", False)),
            "checks_total": len(results),
            "errors": self.errors,
            "results": results,
        }

        log.info(
            "verification_complete",
            ok=all_ok,
            passed=summary["checks_passed"],
            total=summary["checks_total"],
        )

        return summary


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="AI Trader Time Verification Tool")
    parser.add_argument(
        "--full", action="store_true", help="Run full verification (includes network)"
    )
    parser.add_argument(
        "--quick", action="store_true", help="Run quick verification (local only)"
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="Continuous monitoring mode (every 60s)",
    )
    parser.add_argument(
        "--output", type=str, help="Output file path for results (JSON)"
    )

    args = parser.parse_args()

    verifier = TimeVerifier()

    if args.monitor:
        log.info("monitor_mode_start", interval_sec=60)
        while True:
            try:
                result = await verifier.run_quick_verification()
                if args.output:
                    Path(args.output).write_text(json.dumps(result, indent=2))

                if not result["overall_ok"]:
                    log.error("monitor_alert", errors=result["errors"])

                await asyncio.sleep(60)

            except KeyboardInterrupt:
                log.info("monitor_stopped")
                break
            except Exception as e:
                log.error("monitor_error", err=str(e))
                await asyncio.sleep(60)

    elif args.full:
        result = await verifier.run_full_verification()
        print(json.dumps(result, indent=2))

        if args.output:
            Path(args.output).write_text(json.dumps(result, indent=2))

        sys.exit(0 if result["overall_ok"] else 1)

    elif args.quick:
        result = await verifier.run_quick_verification()
        print(json.dumps(result, indent=2))

        if args.output:
            Path(args.output).write_text(json.dumps(result, indent=2))

        sys.exit(0 if result["overall_ok"] else 1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
