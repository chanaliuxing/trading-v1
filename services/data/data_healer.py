#!/usr/bin/env python3
from __future__ import annotations

"""
Active data self-healing service.

Goal:
- Before running expensive ML workflows, ensure that core feature tables
  actually contain data for the target symbols over a recent window.
- If coverage is missing, trigger backfill via tool/load_hist_multi.py
  and wait (with retries) until ClickHouse ingestion has caught up.

This service is intentionally dumb and robust: it does NOT use AI;
it uses direct SQL checks and the existing backfill tools.
"""

import time
import subprocess
import sys
from pathlib import Path
from typing import List
from datetime import datetime, timedelta

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

        self.log.warning(
            "data_healer_void_detected",
            missing_count=len(missing),
            symbols=missing[:5],
        )

        # 2. 开始治疗循环 (最多重试 3 次)
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            self.log.info(
                "data_healer_attempt_start",
                attempt=attempt,
                target_symbols=len(missing),
            )
            # A. 清除可能阻碍回填的旧锁
            self._clear_stale_locks(missing)
            # B. 执行回填 (调用 load_hist_multi.py)
            success = self._run_backfill(missing, days=lookback_days)
            if not success:
                self.log.error("backfill_script_failed", attempt=attempt)
                time.sleep(10)
                continue

            # C. 在 bars 回补之后，为缺失标的主动重建 features_1m 窗口。
            #    否则 features_1m 仍然为空，_check_coverage 永远无法通过。
            feat_ok = self._rebuild_features_1m(missing, lookback_days)
            if not feat_ok:
                self.log.warning(
                    "feature_rebuild_failed",
                    attempt=attempt,
                    symbols=missing[:5],
                )
                # 小退避后继续下一轮尝试；不要立即放弃
                time.sleep(5)
                missing = self._check_coverage(symbols, lookback_days)
                if not missing:
                    self.log.info("data_healer_success", attempt=attempt)
                    return True
                self.log.warning(
                    "data_healer_attempt_failed",
                    attempt=attempt,
                    remaining=len(missing),
                )
                continue

            # C. 等待数据落地 (ClickHouse 异步写入可能有延迟)
            self.log.info("data_healer_waiting_for_ingestion")
            if self._wait_for_ingestion(missing, lookback_days, timeout_sec=60):
                self.log.info("data_healer_success", attempt=attempt)
                # 在 features_1m 就绪后，同步最近若干个完整交易日的
                # baseline 特征到 features_1m_active，以便网关/ML 在
                # 热表上看到完整特征而不需要人工触发 seeding。
                try:
                    self._sync_active_from_baseline(symbols, lookback_days)
                except Exception as exc:
                    # 同步失败不应阻塞主流程；记录 warning 供排查。
                    self.log.warning(
                        "data_healer_active_sync_failed",
                        error=str(exc),
                    )
                return True

            # D. 复查，准备下一轮
            missing = self._check_coverage(symbols, lookback_days)  # 重新检查所有 symbols
            if not missing:
                return True

            self.log.warning(
                "data_healer_attempt_failed",
                attempt=attempt,
                remaining=len(missing),
            )
            time.sleep(5)

        self.log.error(
            "data_healer_give_up",
            remaining_count=len(missing),
            first_few=missing[:3],
        )
        return False

    def _check_coverage(self, symbols: List[str], days: int) -> List[str]:
        """检查 ClickHouse trading.features_1m 在过去 N 天是否有数据。"""
        missing: List[str] = []
        if not symbols:
            return missing
        days_int = int(days)

        # 宽松检查：只要有 > 10 条记录就算“有数据”，避免误杀刚开盘的情况
        # 注意：这里检查 features_1m，意味着必须经过计算。如果只有 bars 没有 features，这也算缺失。
        if len(symbols) == 1:
            sym_clause = f"('{symbols[0]}')"
        else:
            sym_clause = "(" + ",".join(f"'{s}'" for s in symbols) + ")"
        sql = f"""
        SELECT symbol, count() as cnt
        FROM trading.features_1m
        WHERE symbol IN {sym_clause}
          AND ts >= now() - INTERVAL {days_int} DAY
        GROUP BY symbol
        """
        try:
            rows = self.ch.query(sql)
            counts = {r["symbol"]: r["cnt"] for r in rows}
            hard_missing: List[str] = []
            for s in symbols:
                # 阈值可调，对于 intraday 策略，两天至少应该有几百条
                if counts.get(s, 0) < 10:
                    hard_missing.append(s)
            if hard_missing:
                # 周末 / 假期 / 跨周场景下，now()-INTERVAL N DAY 可能完全落在非交易日，
                # 这会导致即使最近一个交易日已经有数据也被误判为缺失。
                # 为了避免在这些场景下自愈陷入死循环，对疑似缺失的标的再用更宽松窗口复查。
                days_ext = days_int + 5  # 足以覆盖 3 日长周末
                sql_ext = f"""
                SELECT symbol, count() as cnt
                FROM trading.features_1m
                WHERE symbol IN {sym_clause}
                  AND ts >= now() - INTERVAL {days_ext} DAY
                GROUP BY symbol
                """
                try:
                    rows_ext = self.ch.query(sql_ext)
                    ext_counts = {r["symbol"]: r["cnt"] for r in rows_ext}
                except Exception:
                    ext_counts = {}
                for s in hard_missing:
                    if ext_counts.get(s, 0) < 10:
                        missing.append(s)
        except Exception as e:  # pragma: no cover - defensive
            self.log.error("healer_check_error", error=str(e))
        return missing

    def _run_backfill(self, symbols: List[str], days: int) -> bool:
        """调用 load_hist_multi.py 进行回补（阻塞直到脚本完成）。"""
        if not symbols:
            return True

        # 使用与你系统一致的 backfill CLI：tool/load_hist_multi.py
        script = self.project_root / "tool" / "load_hist_multi.py"
        if not script.exists():
            self.log.error("load_hist_script_missing", path=str(script))
            return False

        # IBKR 对 1m bar 的 days 有限制，外部已经有 chunk-days 逻辑；这里主要指定 symbols/days
        cmd = [
            sys.executable,
            str(script),
            "--symbols",
            *symbols,
            "--days",
            str(int(days)),
            "--bar-size",
            "1m",
            "--fill-mode",
            "backfill",
            "--chunk-days",
            "1",
            "--skip-covered-days",
        ]

        self.log.info("healer_executing", cmd=" ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                check=False,
                timeout=600,
                capture_output=True,
                text=True,
                cwd=str(self.project_root),
            )
            if result.returncode != 0:
                self.log.error("backfill_error", stderr=result.stderr[-400:])
                return False
            return True
        except Exception as e:  # pragma: no cover - defensive
            self.log.error("healer_exec_failed", error=str(e))
            return False

    def _rebuild_features_1m(self, symbols: List[str], days: int) -> bool:
        """
        从 bars_1m_ibkr 主动重建 features_1m 窗口。

        场景：
        - DataHealer 发现 features_1m 覆盖为 0
        - load_hist_multi.py 只负责写 bars_1m_ibkr，不会自动刷新 features_1m
        - 如果不补这一环节，ensure_data_ready 会在 _wait_for_ingestion 中反复失败
        """
        if not symbols or days <= 0:
            return True

        script = self.project_root / "pipelines" / "rebuild_features_1m.py"
        if not script.exists():
            # 在某些环境下可能尚未部署该脚本；记录 warning 但不中断主流程。
            self.log.warning("feature_rebuild_script_missing", path=str(script))
            return False

        # 根据 lookback_days 推导起始日期，多退 1 天作为滚动窗口预热。
        start_date = (datetime.now().date() - timedelta(days=int(days) + 1)).isoformat()

        # 复用 ClickHouseDB 的配置，确保 host/port 与线上一致。
        ch_cfg = getattr(self.ch, "config", {}) or {}
        host = str(ch_cfg.get("host", "localhost"))
        # 注意：ClickHouseDB 使用 clickhouse_connect (HTTP)，通常端口为 8123，
        # 而 rebuild_features_1m.py 使用 clickhouse_driver (native protocol)，默认 9000。
        # 因此这里优先读取 native_port，其次回退到 9000，避免将 HTTP 端口误传给 driver。
        native_port = ch_cfg.get("native_port", 9000)
        try:
            port = str(int(native_port))
        except Exception:
            port = "9000"

        cmd = [
            sys.executable,
            str(script),
            "--symbols",
            *symbols,
            "--start",
            start_date,
            "--host",
            host,
            "--port",
            port,
        ]
        self.log.info("healer_rebuild_features_1m", cmd=" ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                timeout=900,
                capture_output=True,
                text=True,
                cwd=str(self.project_root),
            )
            if proc.returncode != 0:
                self.log.error(
                    "feature_rebuild_error",
                    stderr=proc.stderr[-400:] if proc.stderr else "",
                )
                return False
            return True
        except Exception as e:  # pragma: no cover - defensive
            self.log.error("feature_rebuild_exec_failed", error=str(e))
            return False

    def _clear_stale_locks(self, symbols: List[str]) -> None:
        """清除 Orchestrator 可能留下的 backfill 锁，确保回填能立即执行。"""
        try:
            today = datetime.now().date().isoformat()
            lock_dir = self.runs_dir / today / "locks"
            if not lock_dir.exists():
                return

            for s in symbols:
                # 匹配 orchestrator 中 backfill 锁名，如 backfill_TSLA_1m.lock
                pattern = f"backfill_{s.upper()}_*.lock"
                for p in lock_dir.glob(pattern):
                    try:
                        p.unlink()
                        self.log.info("healer_lock_cleared", file=p.name)
                    except Exception:
                        continue
        except Exception:
            # 锁清理失败不应阻止后续逻辑
            pass

    def _wait_for_ingestion(self, symbols: List[str], days: int, timeout_sec: int) -> bool:
        """轮询等待数据出现。"""
        start = time.time()
        while time.time() - start < timeout_sec:
            missing = self._check_coverage(symbols, days)
            if not missing:
                return True
            time.sleep(2)
        return False

    def _sync_active_from_baseline(self, symbols: List[str], lookback_days: int) -> None:
        """
        将最近若干个完整交易日的 baseline 特征同步到 features_1m_active,
        并基于 active 表覆盖率自动更新 runtime feature_status。

        - 同步: 调用 tools/check_active_completeness.py --auto-seed-active
        - 状态: 调用 tools/auto_update_feature_status_runtime.py 写入
          runs/ai_trader/<date>/feature_status.runtime.json
        """
        if not symbols or lookback_days <= 0:
            return

        # 1) 同步 baseline → active（按天）
        sync_script = self.project_root / "tools" / "check_active_completeness.py"
        if not sync_script.exists():
            self.log.warning("active_sync_script_missing", path=str(sync_script))
        else:
            today = datetime.now().date()
            sym_arg = ",".join(sorted({s.upper() for s in symbols}))
            for offset in range(1, int(lookback_days) + 1):
                day = today - timedelta(days=offset)
                date_str = day.isoformat()
                cmd = [
                    sys.executable,
                    str(sync_script),
                    "--date",
                    date_str,
                    "--symbols",
                    sym_arg,
                    "--min-coverage",
                    "0.9",
                    "--auto-seed-active",
                ]
                self.log.info(
                    "data_healer_active_sync_start",
                    date=date_str,
                    symbols=sym_arg,
                    cmd=" ".join(cmd),
                )
                try:
                    proc = subprocess.run(
                        cmd,
                        check=False,
                        timeout=300,
                        capture_output=True,
                        text=True,
                        cwd=str(self.project_root),
                    )
                    if proc.returncode != 0:
                        self.log.warning(
                            "data_healer_active_sync_exit_nonzero",
                            date=date_str,
                            returncode=proc.returncode,
                            stderr=(proc.stderr[-300:] if proc.stderr else ""),
                        )
                    else:
                        self.log.info(
                            "data_healer_active_sync_ok",
                            date=date_str,
                            returncode=proc.returncode,
                        )
                except subprocess.TimeoutExpired:
                    self.log.warning(
                        "data_healer_active_sync_timeout",
                        date=date_str,
                        timeout_sec=300,
                    )
                except Exception as exc:
                    self.log.warning(
                        "data_healer_active_sync_exec_failed",
                        date=date_str,
                        error=str(exc),
                    )

        # 2) 基于 active 覆盖率自动更新 runtime feature_status
        status_script = self.project_root / "tools" / "auto_update_feature_status_runtime.py"
        if not status_script.exists():
            self.log.warning("feature_status_runtime_script_missing", path=str(status_script))
            return

        sym_arg = ",".join(sorted({s.upper() for s in symbols}))
        cmd_status = [
            sys.executable,
            str(status_script),
            "--symbols",
            sym_arg,
            "--days",
            str(int(lookback_days)),
        ]
        self.log.info(
            "data_healer_feature_status_runtime_start",
            symbols=sym_arg,
            days=lookback_days,
            cmd=" ".join(cmd_status),
        )
        try:
            proc = subprocess.run(
                cmd_status,
                check=False,
                timeout=300,
                capture_output=True,
                text=True,
                cwd=str(self.project_root),
            )
            if proc.returncode != 0:
                self.log.warning(
                    "data_healer_feature_status_runtime_exit_nonzero",
                    returncode=proc.returncode,
                    stderr=(proc.stderr[-300:] if proc.stderr else ""),
                )
            else:
                self.log.info(
                    "data_healer_feature_status_runtime_ok",
                    returncode=proc.returncode,
                )
        except subprocess.TimeoutExpired:
            self.log.warning(
                "data_healer_feature_status_runtime_timeout",
                timeout_sec=300,
            )
        except Exception as exc:
            self.log.warning(
                "data_healer_feature_status_runtime_exec_failed",
                error=str(exc),
            )

    def _sync_active_from_baseline(self, symbols: List[str], lookback_days: int) -> None:
        """
        将最近若干个完整交易日的 baseline 特征同步到 features_1m_active。

        实现方式：调用 tools/check_active_completeness.py --auto-seed-active
        对每个日期窗口执行一次 coverage 检查和自动 seeding。
        """
        if not symbols or lookback_days <= 0:
            return

        script = self.project_root / "tools" / "check_active_completeness.py"
        if not script.exists():
            self.log.warning("active_sync_script_missing", path=str(script))
            return

        # 只针对已经完成的交易日进行同步，跳过当天未完结的会话。
        today = datetime.now().date()
        sym_arg = ",".join(sorted({s.upper() for s in symbols}))

        for offset in range(1, int(lookback_days) + 1):
            day = today - timedelta(days=offset)
            date_str = day.isoformat()
            cmd = [
                sys.executable,
                str(script),
                "--date",
                date_str,
                "--symbols",
                sym_arg,
                "--min-coverage",
                "0.9",
                "--auto-seed-active",
            ]
            self.log.info(
                "data_healer_active_sync_start",
                date=date_str,
                symbols=sym_arg,
                cmd=" ".join(cmd),
            )
            try:
                proc = subprocess.run(
                    cmd,
                    check=False,
                    timeout=300,
                    capture_output=True,
                    text=True,
                    cwd=str(self.project_root),
                )
                if proc.returncode != 0:
                    self.log.warning(
                        "data_healer_active_sync_exit_nonzero",
                        date=date_str,
                        returncode=proc.returncode,
                        stderr=(proc.stderr[-300:] if proc.stderr else ""),
                    )
                else:
                    self.log.info(
                        "data_healer_active_sync_ok",
                        date=date_str,
                        returncode=proc.returncode,
                    )
            except subprocess.TimeoutExpired:
                self.log.warning(
                    "data_healer_active_sync_timeout",
                    date=date_str,
                    timeout_sec=300,
                )
            except Exception as exc:
                self.log.warning(
                    "data_healer_active_sync_exec_failed",
                    date=date_str,
                    error=str(exc),
                )

    def ensure_labels_ready(
        self,
        symbols: List[str],
        horizons: List[int] = [30],
        lookback_days: int = 90,
        batch_size: int = 5,
        max_retries: int = 2,
        max_null_ratio: float = 0.1,
        min_rows_per_symbol: int = 1000,
        force_symbols: List[str] | None = None,
    ) -> dict:
        """
        确保标签数据就绪 (Ensure labels are ready for ML training)

        Similar design to ensure_data_ready(), but for labels generation.
        Uses batched generation to avoid ClickHouse memory limits.

        Args:
            symbols: List of symbols to ensure labels for
            horizons: Label horizons (e.g., [30] for 30-minute forward returns)
            lookback_days: How many days of historical data to generate labels for
            batch_size: Number of symbols per batch (default 5, avoid memory overflow)
            max_retries: Maximum retry attempts per batch
            max_null_ratio: Maximum allowed ratio of null labels (default 0.1)
            min_rows_per_symbol: Minimum non-null rows required (align with preflight)
            force_symbols: Optional list of symbols to force regeneration even if coverage looks OK

        Returns:
            {
                "status": "fixed" | "failed" | "already_ok",
                "missing_symbols": [...],
                "generated_rows": 123456,
                "errors": [...],
                "batches_completed": 3
            }

        Example:
            >>> healer = DataHealer(runs_dir="runs/ai_trader")
            >>> result = healer.ensure_labels_ready(["TSLA", "AAPL", "MSFT"])
            >>> if result["status"] == "fixed":
            >>>     print(f"Generated {result['generated_rows']} label rows")
        """
        if not symbols:
            return {
                "status": "already_ok",
                "missing_symbols": [],
                "generated_rows": 0,
                "errors": [],
            }

        self.log.info(
            "ensure_labels_ready_start",
            symbols=symbols,
            horizons=horizons,
            lookback_days=lookback_days,
        )

        # 1. Check which symbols are missing or have insufficient labels
        missing_symbols = self._check_label_coverage(
            symbols,
            horizons,
            lookback_days,
            min_rows_per_symbol=min_rows_per_symbol,
            max_null_ratio=max_null_ratio,
        )
        if force_symbols:
            # Explicitly heal symbols flagged upstream (e.g., preflight high_null_ratio)
            force_set = {s for s in force_symbols if s}
            missing_symbols = list({*missing_symbols, *force_set})

        if not missing_symbols:
            self.log.info("labels_already_ready", symbols_checked=len(symbols))
            return {
                "status": "already_ok",
                "missing_symbols": [],
                "generated_rows": 0,
                "errors": [],
            }

        self.log.warning(
            "labels_missing_detected",
            missing_count=len(missing_symbols),
            symbols=missing_symbols[:5],
        )

        # 2. Generate labels in batches to avoid memory issues
        errors = []
        total_rows_generated = 0
        batches_completed = 0

        # Split missing symbols into batches
        for batch_idx in range(0, len(missing_symbols), batch_size):
            batch = missing_symbols[batch_idx : batch_idx + batch_size]
            batch_num = (batch_idx // batch_size) + 1
            total_batches = (len(missing_symbols) + batch_size - 1) // batch_size

            self.log.info(
                "label_batch_start",
                batch_num=batch_num,
                total_batches=total_batches,
                batch_symbols=batch,
            )

            # Try to generate labels for this batch
            for attempt in range(1, max_retries + 1):
                try:
                    rows_generated = self._generate_labels_batch(
                        batch, horizons, lookback_days
                    )

                    if rows_generated > 0:
                        total_rows_generated += rows_generated
                        batches_completed += 1
                        self.log.info(
                            "label_batch_success",
                            batch_num=batch_num,
                            rows=rows_generated,
                            attempt=attempt,
                        )
                        break  # Success, move to next batch
                    else:
                        self.log.warning(
                            "label_batch_zero_rows",
                            batch_num=batch_num,
                            attempt=attempt,
                        )
                        if attempt == max_retries:
                            errors.append(
                                {
                                    "batch": batch,
                                    "error": "Generated 0 rows after max retries",
                                }
                            )

                except Exception as e:
                    self.log.error(
                        "label_batch_failed",
                        batch_num=batch_num,
                        batch=batch,
                        attempt=attempt,
                        error=str(e),
                    )
                    if attempt == max_retries:
                        errors.append({"batch": batch, "error": str(e)})
                    time.sleep(5)  # Backoff before retry

        # 3. Final verification
        remaining_missing = self._check_label_coverage(
            symbols, horizons, lookback_days, max_null_ratio=max_null_ratio
        )

        if not remaining_missing:
            status = "fixed"
            self.log.info(
                "ensure_labels_ready_success",
                generated_rows=total_rows_generated,
                batches_completed=batches_completed,
            )
        elif len(remaining_missing) < len(missing_symbols):
            status = "partial"
            self.log.warning(
                "ensure_labels_ready_partial",
                generated_rows=total_rows_generated,
                remaining_missing=len(remaining_missing),
            )
        else:
            status = "failed"
            self.log.error(
                "ensure_labels_ready_failed",
                errors_count=len(errors),
                remaining_missing=len(remaining_missing),
            )

        return {
            "status": status,
            "missing_symbols": remaining_missing,
            "generated_rows": total_rows_generated,
            "errors": errors,
            "batches_completed": batches_completed,
        }

    def _check_label_coverage(
        self,
        symbols: List[str],
        horizons: List[int],
        lookback_days: int,
        min_rows_per_symbol: int = 1000,
        max_null_ratio: float = 0.1,  # Match preflight default
    ) -> List[str]:
        """
        Check which symbols have insufficient label coverage, now including a null-ratio check.

        Returns:
            List of symbols that need labels generated
        """
        label_cols = [f"label_h{h}" for h in horizons]
        cutoff_date = (datetime.now() - timedelta(days=lookback_days)).strftime(
            "%Y-%m-%d"
        )

        missing = []
        for symbol in symbols:
            try:
                # Query now gets total rows and null rows to calculate null_ratio
                query = f"""
                    SELECT
                        count() as total_rows,
                        countIf({label_cols[0]} IS NULL) as null_rows
                    FROM trading.features_1m_with_labels
                    WHERE symbol = %(symbol)s
                    AND toDate(ts) >= %(cutoff_date)s
                """
                result = self.ch.query(
                    query, {"symbol": symbol, "cutoff_date": cutoff_date}
                )

                if not result:
                    missing.append(symbol)
                    self.log.debug(
                        "label_coverage_no_data",
                        symbol=symbol,
                    )
                    continue

                stats = result[0]
                total_rows = stats.get("total_rows", 0)
                null_rows = stats.get("null_rows", 0)
                non_null_rows = total_rows - null_rows
                null_ratio = null_rows / total_rows if total_rows > 0 else 1.0

                # Condition 1: Not enough non-null rows (absolute check)
                if non_null_rows < min_rows_per_symbol:
                    missing.append(symbol)
                    self.log.debug(
                        "label_coverage_insufficient",
                        symbol=symbol,
                        rows=non_null_rows,
                        threshold=min_rows_per_symbol,
                    )
                    continue  # Already marked as missing, no need to check ratio

                # Condition 2: Too many nulls (ratio check)
                if null_ratio > max_null_ratio:
                    missing.append(symbol)
                    self.log.debug(
                        "label_coverage_high_null_ratio",
                        symbol=symbol,
                        null_ratio=null_ratio,
                        threshold=max_null_ratio,
                    )

            except Exception as e:
                self.log.warning(
                    "label_coverage_check_failed", symbol=symbol, error=str(e)
                )
                missing.append(symbol)  # Assume missing on error

        return missing

    def _generate_labels_batch(
        self,
        symbols: List[str],
        horizons: List[int],
        lookback_days: int
    ) -> int:
        """
        Generate labels for a batch of symbols using tools/recompute_labels_1m.py
        into a temporary wide table, then merge into features_1m_with_labels.

        Returns:
            Number of rows generated

        Raises:
            Exception if generation fails
        """
        # Use a temp table (fully qualified) to avoid dropping existing data
        temp_table_name = f"features_1m_with_labels_temp_{int(time.time())}"
        temp_table_qualified = f"trading.{temp_table_name}"

        # Compute time window for labels
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=lookback_days)
        start_ts = start_dt.strftime("%Y-%m-%d 00:00:00")
        end_ts = end_dt.strftime("%Y-%m-%d 23:59:59")
        horizons_str = ",".join(map(str, horizons))

        cmd = [
            sys.executable,
            "tools/recompute_labels_1m.py",
            "--symbols",
            ",".join(symbols),
            "--horizons",
            horizons_str,
            "--start",
            start_ts,
            "--end",
            end_ts,
            "--table",
            temp_table_qualified,
            "--source",
            "trading.features_1m",
        ]

        self.log.info(
            "label_generation_start",
            symbols=symbols,
            horizons=horizons,
            temp_table=temp_table_qualified,
        )

        try:
            proc = subprocess.run(
                cmd,
                check=True,
                timeout=600,  # 10 minutes max
                capture_output=True,
                text=True,
                cwd=str(self.project_root),
            )

            self.log.info(
                "label_generation_completed",
                returncode=proc.returncode,
                temp_table=temp_table_qualified,
            )

            # Count rows in temp table
            count_query = f"SELECT count() as cnt FROM {temp_table_qualified}"
            result = self.ch.query(count_query)
            rows_generated = result[0]["cnt"] if result else 0

            if rows_generated > 0:
                # Merge temp table into main table
                self._merge_label_table(temp_table_name)
            else:
                self.log.warning(
                    "label_generation_zero_rows",
                    temp_table=temp_table_qualified,
                )

            # Clean up temp table
            self.ch.execute(f"DROP TABLE IF EXISTS {temp_table_qualified}")

            return rows_generated

        except subprocess.TimeoutExpired:
            self.log.error("label_generation_timeout", symbols=symbols, timeout_sec=600)
            raise
        except subprocess.CalledProcessError as e:
            self.log.error(
                "label_generation_failed",
                symbols=symbols,
                returncode=e.returncode,
                stderr=e.stderr[-500:] if e.stderr else "",
            )
            raise
        except Exception as e:
            self.log.error("label_generation_exception", symbols=symbols, error=str(e))
            raise

    def _merge_label_table(self, temp_table: str) -> None:
        """
        Merge temp label table into main features_1m_with_labels table

        Creates main table if it doesn't exist, otherwise inserts new rows.
        """
        # Create main table if missing (idempotent IF NOT EXISTS to avoid races)
        create_query = f"""
            CREATE TABLE IF NOT EXISTS trading.features_1m_with_labels
            ENGINE = MergeTree()
            ORDER BY (symbol, ts)
            AS SELECT * FROM trading.{temp_table} WHERE 1 = 0
        """
        self.ch.execute(create_query)

        # Compare schemas and add any missing columns on the main table to handle schema evolution (e.g., updated_at)
        try:
            source_info = self.ch.query(f"DESCRIBE TABLE trading.{temp_table}") or []
            target_info = self.ch.query("DESCRIBE TABLE trading.features_1m_with_labels") or []
        except Exception as exc:
            self.log.warning("label_table_describe_failed", source=temp_table, error=str(exc))
            source_info, target_info = [], []

        source_cols = {row["name"]: row.get("type") for row in source_info}
        target_cols = {row["name"]: row.get("type") for row in target_info}

        # Add missing columns to the main table using types from the source (temp) table
        missing_cols = [col for col in source_cols if col not in target_cols]
        for col in missing_cols:
            col_type = source_cols.get(col, "Nullable(Float64)")
            try:
                alter_sql = f"ALTER TABLE trading.features_1m_with_labels ADD COLUMN IF NOT EXISTS {col} {col_type}"
                self.ch.execute(alter_sql)
                self.log.info("label_table_column_added", column=col, col_type=col_type)
            except Exception as exc:
                self.log.warning("label_table_column_add_failed", column=col, error=str(exc))

        # Insert from temp into main (non-destructive merge) using the common column set to avoid count mismatches
        common_cols = [col for col in source_cols.keys() if col in (target_cols.keys() | set(missing_cols))]
        if not common_cols:
            # Nothing to merge; avoid executing invalid SQL
            self.log.warning("label_table_merge_skipped_no_columns", source=temp_table)
            return

        cols_csv = ", ".join(common_cols)
        insert_query = f"""
            INSERT INTO trading.features_1m_with_labels ({cols_csv})
            SELECT {cols_csv} FROM trading.{temp_table}
        """

        self.ch.execute(insert_query)
        self.log.info(
            "label_table_merged",
            source=temp_table,
            columns=len(common_cols),
            missing_columns_added=len(missing_cols),
        )

    def ensure_features_ready(
        self,
        symbols: List[str],
        start_date: str,
        end_date: str,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        Ensure features are available for specified symbols and date range

        Similar to ensure_labels_ready(), but rebuilds features instead of labels.
        Calls pipelines/rebuild_features_1m.py to recompute all feature columns.

        Args:
            symbols: List of symbols (e.g., ["TSLA", "AAPL", "SPY"])
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            dry_run: If True, only check coverage without rebuilding

        Returns:
            {
                "status": "fixed" | "failed" | "skipped",
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
        from pathlib import Path

        if not symbols:
            return {
                "status": "skipped",
                "symbols_processed": 0,
                "rows_written": 0,
                "errors": ["No symbols provided"]
            }

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
            error_msg = f"Invalid date format: {e}"
            self.log.error("ensure_features_ready_invalid_dates", error=error_msg)
            return {
                "status": "failed",
                "symbols_processed": 0,
                "rows_written": 0,
                "errors": [error_msg]
            }

        # Build command to call rebuild_features_1m.py
        project_root = Path(__file__).parent.parent.parent
        cmd = [
            str(project_root / "venv" / "bin" / "python"),
            str(project_root / "pipelines" / "rebuild_features_1m.py"),
            "--symbols"
        ] + symbols + [
            "--start", start_date,
            "--end", end_date
        ]

        if dry_run:
            cmd.append("--dry-run")

        # Execute rebuild
        try:
            self.log.info(
                "ensure_features_ready_executing",
                command=" ".join(cmd),
                timeout_seconds=7200
            )

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=7200,  # 2 hours max
                cwd=str(project_root)
            )

            if result.returncode == 0:
                # Parse output to extract metrics
                rows_written = 0
                for line in result.stdout.split('\n'):
                    # Look for pattern like "✓ TSLA: 12345 rows written"
                    if 'rows written' in line.lower() or 'rows processed' in line.lower():
                        import re
                        match = re.search(r'(\d{1,3}(?:,\d{3})*|\d+)\s+rows', line)
                        if match:
                            # Remove commas from number
                            num_str = match.group(1).replace(',', '')
                            rows_written += int(num_str)

                self.log.info(
                    "ensure_features_ready_success",
                    symbols=symbols,
                    rows_written=rows_written,
                    dry_run=dry_run
                )
                return {
                    "status": "fixed" if not dry_run else "skipped",
                    "symbols_processed": len(symbols),
                    "rows_written": rows_written,
                    "errors": []
                }
            else:
                # Extract error message from stderr or stdout
                error_msg = result.stderr.strip() if result.stderr else result.stdout.strip()
                # Truncate very long errors
                if len(error_msg) > 1000:
                    error_msg = error_msg[:1000] + "... (truncated)"

                self.log.error(
                    "ensure_features_ready_failed",
                    symbols=symbols,
                    returncode=result.returncode,
                    error=error_msg
                )
                return {
                    "status": "failed",
                    "symbols_processed": 0,
                    "rows_written": 0,
                    "errors": [f"Command failed with code {result.returncode}: {error_msg}"]
                }

        except subprocess.TimeoutExpired:
            error_msg = "Rebuild timed out after 2 hours"
            self.log.error("ensure_features_ready_timeout", symbols=symbols)
            return {
                "status": "failed",
                "symbols_processed": 0,
                "rows_written": 0,
                "errors": [error_msg]
            }
        except Exception as e:
            error_msg = str(e)
            self.log.error(
                "ensure_features_ready_error",
                symbols=symbols,
                error=error_msg,
                exc_info=True
            )
            return {
                "status": "failed",
                "symbols_processed": 0,
                "rows_written": 0,
                "errors": [error_msg]
            }
