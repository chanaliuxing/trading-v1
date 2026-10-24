from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from libs.logger import get_logger
from services.notifications import NotificationEvent, NotificationManager

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")


@dataclass
class DailySummaryConfig:
    report_time_et: str = "16:01"
    include_weekends: bool = False
    max_detail_rows: int = 40

    @classmethod
    def from_env(cls) -> "DailySummaryConfig":
        return cls(
            report_time_et=str(os.getenv("DAILY_TRADE_SUMMARY_TIME_ET", "16:01")).strip() or "16:01",
            include_weekends=str(os.getenv("DAILY_TRADE_SUMMARY_INCLUDE_WEEKENDS", "0")).strip().lower() in {"1", "true", "yes", "on"},
            max_detail_rows=max(1, int(str(os.getenv("DAILY_TRADE_SUMMARY_MAX_ROWS", "40")).strip() or "40")),
        )


class DailyTradeSummaryService:
    def __init__(
        self,
        *,
        pg: Any,
        notification_manager: NotificationManager,
        cfg: Optional[DailySummaryConfig] = None,
    ):
        self.pg = pg
        self.notification_manager = notification_manager
        self.cfg = cfg or DailySummaryConfig.from_env()

    async def run_forever(self) -> None:
        logger.info(
            "daily_trade_summary_service_started",
            report_time_et=self.cfg.report_time_et,
            include_weekends=self.cfg.include_weekends,
            max_detail_rows=self.cfg.max_detail_rows,
        )
        while True:
            now_et = datetime.now(ET)
            next_run = self._next_run_dt_et(now_et, self.cfg.report_time_et)
            sleep_sec = max(1.0, (next_run - now_et).total_seconds())
            logger.info(
                "daily_trade_summary_next_run_scheduled",
                next_run_et=next_run.isoformat(),
                sleep_sec=round(sleep_sec, 2),
            )
            await asyncio.sleep(sleep_sec)
            report_day = datetime.now(ET).date()
            try:
                if (not self.cfg.include_weekends) and report_day.weekday() >= 5:
                    logger.info("daily_trade_summary_skipped_weekend", report_date=str(report_day))
                    continue
                await self.publish_for_date(report_day)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("daily_trade_summary_publish_failed", error=str(exc), exc_info=True)

    async def publish_for_date(self, report_date: date) -> bool:
        start_et = datetime.combine(report_date, time(0, 0), tzinfo=ET)
        end_et = start_et + timedelta(days=1)
        rows = await self._load_fill_rows(start_et, end_et)
        position_rows = await self._load_position_rows()
        metadata = self._build_summary_metadata(
            rows=rows,
            position_rows=position_rows,
            report_date=report_date,
            start_et=start_et,
            end_et=end_et,
        )
        event = NotificationEvent(
            event_type="daily_trade_summary",
            status="SUMMARY",
            strategy_id="system_reconciler",
            symbol="ALL",
            side="N/A",
            qty=0.0,
            order_type="N/A",
            decision_id=f"daily_trade_summary:{report_date.isoformat()}",
            metadata=metadata,
        )
        ok = bool(self.notification_manager.publish(event))
        logger.info(
            "daily_trade_summary_published",
            report_date=str(report_date),
            fills_count=int(metadata.get("fills_count", 0)),
            strategy_count=int(metadata.get("strategy_count", 0)),
            published=ok,
        )
        return ok

    async def _load_fill_rows(self, start_et: datetime, end_et: datetime) -> list[dict[str, Any]]:
        start_utc = start_et.astimezone(timezone.utc)
        end_utc = end_et.astimezone(timezone.utc)
        query = """
            SELECT
                f.order_id::text AS db_order_id,
                f.ts AS fill_ts,
                f.qty AS fill_qty,
                f.price AS fill_price,
                COALESCE(f.commission_usd, 0) AS commission_usd,
                o.strategy_id,
                o.symbol,
                o.side,
                o.order_type,
                o.order_ref,
                o.note,
                COALESCE(
                    o.parent_order_id::text,
                    substring(COALESCE(o.note, '') FROM '"parent_order_id"\\s*:\\s*"([^"]+)"')
                ) AS parent_order_id,
                COALESCE(
                    ev_submit.decision_id,
                    ev_intent.decision_id,
                    parent_ev_submit.decision_id,
                    parent_ev_intent.decision_id,
                    substring(COALESCE(parent_o.note, '') FROM '"decision_id"\\s*:\\s*"([^"]+)"'),
                    substring(COALESCE(o.note, '') FROM '"decision_id"\\s*:\\s*"([^"]+)"'),
                    ''
                ) AS decision_id,
                COALESCE(
                    ev_submit.trigger_id,
                    ev_intent.trigger_id,
                    substring(COALESCE(o.note, '') FROM '"trigger_id"\\s*:\\s*"([^"]+)"'),
                    parent_ev_submit.trigger_id,
                    parent_ev_intent.trigger_id,
                    substring(COALESCE(parent_o.note, '') FROM '"trigger_id"\\s*:\\s*"([^"]+)"'),
                    ''
                ) AS trigger_id,
                COALESCE(parent_ev_submit.trigger_id, parent_ev_intent.trigger_id, '') AS parent_trigger_id,
                COALESCE(ev_submit.event_ts, ev_intent.event_ts) AS trigger_ts
            FROM core.core_fill f
            JOIN core.core_order o ON o.order_id = f.order_id
            LEFT JOIN core.core_order parent_o
              ON parent_o.order_id::text = COALESCE(
                    o.parent_order_id::text,
                    substring(COALESCE(o.note, '') FROM '"parent_order_id"\\s*:\\s*"([^"]+)"')
                 )
            LEFT JOIN LATERAL (
                SELECT trigger_id, decision_id, event_ts
                FROM core.core_order_event
                WHERE order_id = o.order_id
                  AND event_type = 'order_submitted'
                ORDER BY event_ts ASC
                LIMIT 1
            ) ev_submit ON TRUE
            LEFT JOIN LATERAL (
                SELECT trigger_id, decision_id, event_ts
                FROM core.core_order_event
                WHERE order_id = o.order_id
                  AND event_type = 'intent_received'
                ORDER BY event_ts ASC
                LIMIT 1
            ) ev_intent ON TRUE
            LEFT JOIN LATERAL (
                SELECT trigger_id, decision_id, event_ts
                FROM core.core_order_event
                WHERE order_id = parent_o.order_id
                  AND event_type = 'order_submitted'
                ORDER BY event_ts ASC
                LIMIT 1
            ) parent_ev_submit ON TRUE
            LEFT JOIN LATERAL (
                SELECT trigger_id, decision_id, event_ts
                FROM core.core_order_event
                WHERE order_id = parent_o.order_id
                  AND event_type = 'intent_received'
                ORDER BY event_ts ASC
                LIMIT 1
            ) parent_ev_intent ON TRUE
            WHERE f.ts >= $1 AND f.ts < $2
            ORDER BY f.ts ASC
        """
        try:
            return await self.pg.fetchall(query, start_utc, end_utc)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "daily_trade_summary_query_failed",
                error=str(exc),
                start_utc=start_utc.isoformat(),
                end_utc=end_utc.isoformat(),
            )
            return []

    async def _load_position_rows(self) -> list[dict[str, Any]]:
        query = """
            SELECT strategy_id, symbol, pos_qty, avg_px
            FROM core.core_position
            WHERE abs(pos_qty) > 0.000001
        """
        try:
            return await self.pg.fetchall(query)
        except Exception as exc:  # noqa: BLE001
            logger.error("daily_trade_summary_position_query_failed", error=str(exc))
            return []

    def _build_summary_metadata(
        self,
        *,
        rows: list[dict[str, Any]],
        position_rows: list[dict[str, Any]],
        report_date: date,
        start_et: datetime,
        end_et: datetime,
    ) -> Dict[str, Any]:
        gross_buy = 0.0
        gross_sell = 0.0
        total_commission = 0.0
        strategy_stats: Dict[str, Dict[str, Any]] = {}
        details: list[dict[str, Any]] = []
        strategy_trigger_groups: Dict[str, Dict[str, Dict[str, Any]]] = {}
        latest_fill_px_by_sid_sym: Dict[tuple[str, str], float] = {}

        for row in rows[: self.cfg.max_detail_rows]:
            strategy_id = str(row.get("strategy_id") or "unknown")
            symbol = str(row.get("symbol") or "")
            side = str(row.get("side") or "").upper()
            qty = float(row.get("fill_qty") or 0.0)
            price = float(row.get("fill_price") or 0.0)
            commission = float(row.get("commission_usd") or 0.0)
            amount = qty * price
            note_obj = self._safe_json_obj(row.get("note"))
            action = self._classify_action(
                side=side,
                order_type=str(row.get("order_type") or ""),
                order_ref=str(row.get("order_ref") or ""),
                note_obj=note_obj,
            )
            protection = self._protection_detail(
                order_type=str(row.get("order_type") or ""),
                order_ref=str(row.get("order_ref") or ""),
                note_obj=note_obj,
            )

            if side == "BUY":
                gross_buy += amount
            elif side == "SELL":
                gross_sell += amount
            total_commission += commission

            agg = strategy_stats.setdefault(
                strategy_id,
                {
                    "strategy_id": strategy_id,
                    "fills": 0,
                    "gross_buy_usd": 0.0,
                    "gross_sell_usd": 0.0,
                    "commission_usd": 0.0,
                    "net_cashflow_usd": 0.0,
                },
            )
            agg["fills"] += 1
            agg["commission_usd"] += commission
            if side == "BUY":
                agg["gross_buy_usd"] += amount
            elif side == "SELL":
                agg["gross_sell_usd"] += amount
            agg["net_cashflow_usd"] = agg["gross_sell_usd"] - agg["gross_buy_usd"] - agg["commission_usd"]

            fill_ts = row.get("fill_ts")
            trigger_ts = row.get("trigger_ts")
            fill_ts_et = fill_ts.astimezone(ET).strftime("%H:%M:%S") if isinstance(fill_ts, datetime) else ""
            trigger_ts_et = trigger_ts.astimezone(ET).strftime("%H:%M:%S") if isinstance(trigger_ts, datetime) else ""
            details.append(
                {
                    "fill_ts_et": fill_ts_et,
                    "trigger_ts_et": trigger_ts_et,
                    "strategy_id": strategy_id,
                    "symbol": symbol,
                    "side": side,
                    "qty": round(qty, 6),
                    "price": round(price, 6),
                    "amount_usd": round(amount, 4),
                    "commission_usd": round(commission, 4),
                    "action": action,
                    "trigger_id": str(row.get("trigger_id") or ""),
                    "parent_trigger_id": str(row.get("parent_trigger_id") or ""),
                    "decision_id": str(row.get("decision_id") or ""),
                    "order_ref": str(row.get("order_ref") or ""),
                    "order_id": str(row.get("db_order_id") or ""),
                    "parent_order_id": str(row.get("parent_order_id") or ""),
                    "protection": protection,
                }
            )
            if price > 0:
                latest_fill_px_by_sid_sym[(strategy_id, symbol)] = price

            trigger_id = str(row.get("trigger_id") or "").strip()
            if not trigger_id:
                trigger_id = str(row.get("parent_trigger_id") or "").strip()
            if not trigger_id:
                trigger_id = str(row.get("decision_id") or "").strip()
            if not trigger_id:
                trigger_id = str(row.get("order_ref") or "").strip()[:64]
            if not trigger_id:
                trigger_id = f"unknown:{len(details)}"

            trig_map = strategy_trigger_groups.setdefault(strategy_id, {})
            chain = trig_map.setdefault(
                trigger_id,
                {
                    "trigger_id": trigger_id,
                    "events": [],
                    "buy_qty": 0.0,
                    "sell_qty": 0.0,
                    "gross_buy_usd": 0.0,
                    "gross_sell_usd": 0.0,
                    "commission_usd": 0.0,
                    "net_cashflow_usd": 0.0,
                    "realized_pnl_usd": 0.0,
                    "closed": False,
                    "has_entry_action": False,
                    "carry_close_only": False,
                    "report_closed": False,
                    "first_trigger_ts_et": trigger_ts_et,
                    "first_fill_ts_et": fill_ts_et,
                    "last_fill_ts_et": fill_ts_et,
                },
            )
            chain["events"].append(details[-1])
            chain["commission_usd"] += commission
            if side == "BUY":
                chain["buy_qty"] += qty
                chain["gross_buy_usd"] += amount
            elif side == "SELL":
                chain["sell_qty"] += qty
                chain["gross_sell_usd"] += amount
            chain["net_cashflow_usd"] = chain["gross_sell_usd"] - chain["gross_buy_usd"] - chain["commission_usd"]
            net_qty = float(chain["buy_qty"]) - float(chain["sell_qty"])
            chain["closed"] = abs(net_qty) <= 1e-6
            chain["has_entry_action"] = bool(chain.get("has_entry_action")) or (action == "ENTRY")
            chain["realized_pnl_usd"] = chain["net_cashflow_usd"] if chain["closed"] else 0.0
            if fill_ts_et:
                chain["last_fill_ts_et"] = fill_ts_et

        strategies = sorted(strategy_stats.values(), key=lambda x: x["fills"], reverse=True)
        for item in strategies:
            for key in ("gross_buy_usd", "gross_sell_usd", "commission_usd", "net_cashflow_usd"):
                item[key] = round(float(item[key]), 4)

        strategy_position_stats: Dict[str, Dict[str, Any]] = {}
        for p in position_rows:
            sid = str(p.get("strategy_id") or "unknown")
            sym = str(p.get("symbol") or "")
            pos_qty = float(p.get("pos_qty") or 0.0)
            avg_px = float(p.get("avg_px") or 0.0)
            mark_px = float(latest_fill_px_by_sid_sym.get((sid, sym)) or avg_px or 0.0)
            market_value = pos_qty * mark_px
            notional_abs = abs(pos_qty * mark_px)
            unrealized_pnl = pos_qty * (mark_px - avg_px) if avg_px > 0 and mark_px > 0 else 0.0
            agg = strategy_position_stats.setdefault(
                sid,
                {
                    "position_market_value_usd": 0.0,
                    "position_notional_abs_usd": 0.0,
                    "unrealized_pnl_usd": 0.0,
                    "open_positions": [],
                },
            )
            agg["position_market_value_usd"] += market_value
            agg["position_notional_abs_usd"] += notional_abs
            agg["unrealized_pnl_usd"] += unrealized_pnl
            agg["open_positions"].append(
                {
                    "symbol": sym,
                    "qty": round(pos_qty, 6),
                    "avg_px": round(avg_px, 6),
                    "mark_px": round(mark_px, 6),
                    "market_value_usd": round(market_value, 4),
                    "unrealized_pnl_usd": round(unrealized_pnl, 4),
                }
            )

        strategy_sections: list[dict[str, Any]] = []
        for s in strategies:
            sid = str(s.get("strategy_id") or "")
            chains = list((strategy_trigger_groups.get(sid) or {}).values())
            chains.sort(key=lambda x: str(x.get("first_fill_ts_et") or ""))
            open_positions = (strategy_position_stats.get(sid) or {}).get("open_positions", [])
            has_open_position = any(abs(float(p.get("qty", 0.0))) > 1e-8 for p in open_positions)
            for c in chains:
                c["events"] = sorted(c.get("events") or [], key=lambda e: str(e.get("fill_ts_et") or ""))
                c["trigger_count"] = 1
                c["fills_count"] = len(c["events"])
                c["open_qty"] = round(float(c.get("buy_qty", 0.0)) - float(c.get("sell_qty", 0.0)), 6)
                for key in ("gross_buy_usd", "gross_sell_usd", "commission_usd", "net_cashflow_usd", "realized_pnl_usd"):
                    c[key] = round(float(c.get(key, 0.0)), 4)
                # Close-only chains from carry-in positions are not intraday chain-risk.
                c["carry_close_only"] = bool(
                    (not bool(c.get("closed")))
                    and (not has_open_position)
                    and (not bool(c.get("has_entry_action")))
                )
                c["report_closed"] = bool(c.get("closed")) or bool(c.get("carry_close_only"))
            realized_pnl = sum(float(c.get("realized_pnl_usd") or 0.0) for c in chains)
            open_chain_count = sum(1 for c in chains if not bool(c.get("report_closed")))

            strategy_sections.append(
                {
                    "strategy_id": sid,
                    "fills_count": int(s.get("fills") or 0),
                    "trigger_count": len(chains),
                    "open_trigger_count": open_chain_count,
                    "gross_buy_usd": float(s.get("gross_buy_usd") or 0.0),
                    "gross_sell_usd": float(s.get("gross_sell_usd") or 0.0),
                    "commission_usd": float(s.get("commission_usd") or 0.0),
                    "net_cashflow_usd": float(s.get("net_cashflow_usd") or 0.0),
                    "realized_pnl_usd": round(realized_pnl, 4),
                    "position_market_value_usd": round(float((strategy_position_stats.get(sid) or {}).get("position_market_value_usd", 0.0)), 4),
                    "position_notional_abs_usd": round(float((strategy_position_stats.get(sid) or {}).get("position_notional_abs_usd", 0.0)), 4),
                    "unrealized_pnl_usd": round(float((strategy_position_stats.get(sid) or {}).get("unrealized_pnl_usd", 0.0)), 4),
                    "total_pnl_usd": round(
                        realized_pnl + float((strategy_position_stats.get(sid) or {}).get("unrealized_pnl_usd", 0.0)),
                        4,
                    ),
                    "equity_like_delta_usd": round(
                        float(s.get("net_cashflow_usd") or 0.0)
                        + float((strategy_position_stats.get(sid) or {}).get("position_market_value_usd", 0.0)),
                        4,
                    ),
                    "open_positions": open_positions,
                    "chains": chains,
                }
            )

        total_position_market_value = sum(float((x.get("position_market_value_usd") or 0.0)) for x in strategy_sections)
        total_position_notional_abs = sum(float((x.get("position_notional_abs_usd") or 0.0)) for x in strategy_sections)
        total_realized_pnl = sum(float((x.get("realized_pnl_usd") or 0.0)) for x in strategy_sections)
        total_unrealized_pnl = sum(float((x.get("unrealized_pnl_usd") or 0.0)) for x in strategy_sections)

        return {
            "report_date": report_date.isoformat(),
            "window_start_et": start_et.isoformat(),
            "window_end_et": end_et.isoformat(),
            "fills_count": len(rows),
            "strategy_count": len(strategy_stats),
            "gross_buy_usd": round(gross_buy, 4),
            "gross_sell_usd": round(gross_sell, 4),
            "commission_usd": round(total_commission, 4),
            "net_cashflow_usd": round(gross_sell - gross_buy - total_commission, 4),
            "position_market_value_usd": round(total_position_market_value, 4),
            "position_notional_abs_usd": round(total_position_notional_abs, 4),
            "realized_pnl_usd": round(total_realized_pnl, 4),
            "unrealized_pnl_usd": round(total_unrealized_pnl, 4),
            "total_pnl_usd": round(total_realized_pnl + total_unrealized_pnl, 4),
            "equity_like_delta_usd": round((gross_sell - gross_buy - total_commission) + total_position_market_value, 4),
            "top_strategy_id": strategies[0]["strategy_id"] if strategies else "__daily_summary__",
            "strategies": strategies,
            "strategy_sections": strategy_sections,
            "events": details,
            "truncated_details": max(0, len(rows) - len(details)),
        }

    def _classify_action(self, *, side: str, order_type: str, order_ref: str, note_obj: Dict[str, Any]) -> str:
        order_ref_up = order_ref.upper()
        extras = note_obj.get("extras")
        extras = extras if isinstance(extras, dict) else {}

        if bool(extras.get("flatten_action")) or bool(extras.get("manual_intervention")) or "MANUAL_FLATTEN" in order_ref_up:
            return "FLATTEN"
        if ":TP" in order_ref_up or "TAKE_PROFIT" in order_ref_up:
            return "TP"
        if (
            ":STOP" in order_ref_up
            or ":TRAIL" in order_ref_up
            or str(order_type).upper().replace(" ", "_") in {"STP", "STP_LMT", "STOP", "TRAIL", "TRAIL_LIMIT"}
        ):
            return "SL"
        return "ENTRY" if side == "BUY" else "EXIT"

    def _protection_detail(self, *, order_type: str, order_ref: str, note_obj: Dict[str, Any]) -> Dict[str, Any]:
        order_type_u = str(order_type or "").strip().upper().replace(" ", "_")
        order_ref_u = str(order_ref or "").strip().upper()
        if order_type_u not in {"STP", "STP_LMT", "TRAIL", "TRAIL_LIMIT"} and not (
            ":STOP" in order_ref_u or ":TRAIL" in order_ref_u
        ):
            return {}
        extras = note_obj.get("extras")
        extras = extras if isinstance(extras, dict) else {}
        style = note_obj.get("order_style")
        style = style if isinstance(style, dict) else {}
        return {
            "kind": order_type_u or str(style.get("kind") or "").strip().upper().replace(" ", "_"),
            "stop_px": self._optional_float(style.get("stop_px") or extras.get("stop_px") or note_obj.get("stop_px")),
            "trail_amount": self._optional_float(style.get("trail_amount") or extras.get("trail_amount")),
            "trail_percent": self._optional_float(style.get("trail_percent") or extras.get("trail_percent")),
            "trail_stop_px": self._optional_float(style.get("trail_stop_px") or extras.get("trail_stop_px")),
            "lmt_price_offset": self._optional_float(style.get("lmt_price_offset") or extras.get("lmt_price_offset")),
            "protection_spec_mode": str(extras.get("protection_spec_mode") or note_obj.get("protection_spec_mode") or ""),
            "protection_position_id": str(extras.get("protection_position_id") or note_obj.get("protection_position_id") or ""),
        }

    @staticmethod
    def _optional_float(value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _safe_json_obj(self, val: Any) -> Dict[str, Any]:
        if isinstance(val, dict):
            return val
        if isinstance(val, str) and val.strip():
            try:
                out = json.loads(val)
                return out if isinstance(out, dict) else {}
            except Exception:
                return {}
        return {}

    def _next_run_dt_et(self, now_et: datetime, hhmm: str) -> datetime:
        hh, mm = self._parse_hhmm(hhmm)
        target = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now_et:
            target += timedelta(days=1)
        return target

    def _parse_hhmm(self, hhmm: str) -> tuple[int, int]:
        raw = str(hhmm or "").strip()
        try:
            parts = raw.split(":", 1)
            hh = int(parts[0])
            mm = int(parts[1]) if len(parts) > 1 else 0
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return hh, mm
        except Exception:
            pass
        logger.warning("daily_trade_summary_invalid_time_et", value=raw, fallback="16:01")
        return 16, 1
