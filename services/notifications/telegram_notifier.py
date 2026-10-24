from __future__ import annotations

import asyncio
import json
import os
import random
import re
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib import error, request
from zoneinfo import ZoneInfo

from libs.logger import get_logger

from .base import NotificationEvent, Notifier
from .circuit_breaker import CircuitBreaker

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")


@dataclass
class SendAttemptResult:
    ok: bool
    retriable: bool
    error: str = ""
    status_code: Optional[int] = None
    retry_after_sec: Optional[float] = None


class TelegramNotifier(Notifier):
    """Telegram Bot API notifier with retry and circuit-breaker protection."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        timeout_sec: float = 3.0,
        max_retries: int = 3,
        retry_base_delay_sec: float = 0.5,
        retry_max_delay_sec: float = 5.0,
        retry_jitter_sec: float = 0.25,
        failure_threshold: int = 5,
        breaker_open_timeout_sec: float = 60.0,
    ):
        self.bot_token = str(bot_token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.timeout_sec = max(0.5, float(timeout_sec))
        self.max_retries = max(0, int(max_retries))
        self.retry_base_delay_sec = max(0.05, float(retry_base_delay_sec))
        self.retry_max_delay_sec = max(self.retry_base_delay_sec, float(retry_max_delay_sec))
        self.retry_jitter_sec = max(0.0, float(retry_jitter_sec))
        self.breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            open_timeout_sec=breaker_open_timeout_sec,
            half_open_max_calls=1,
        )
        self.audit_enabled = str(os.getenv("TELEGRAM_PUSH_AUDIT_ENABLED", "1")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.audit_path = Path(
            str(os.getenv("TELEGRAM_PUSH_AUDIT_PATH", "runtime/audit/telegram_push_audit.jsonl")).strip()
            or "runtime/audit/telegram_push_audit.jsonl"
        ).expanduser()
        self._audit_lock = asyncio.Lock()

    async def send(self, event: NotificationEvent) -> bool:
        if not self.bot_token or not self.chat_id:
            await self._audit_push(
                event,
                message_text="",
                outcome="skipped",
                attempt=0,
                reason="missing_credentials",
            )
            return False

        text = self._format_text(event)

        if not self.breaker.allow_request():
            logger.warning(
                "telegram_notification_skipped_circuit_open",
                strategy_id=event.strategy_id,
                symbol=event.symbol,
            )
            await self._audit_push(
                event,
                message_text=text,
                outcome="skipped",
                attempt=0,
                reason="circuit_open",
                circuit_state=self.breaker.state,
            )
            return False

        for attempt in range(self.max_retries + 1):
            result = await self._send_once(text)
            await self._audit_push(
                event,
                message_text=text,
                outcome="sent" if result.ok else "attempt_failed",
                attempt=attempt + 1,
                error=result.error,
                status_code=result.status_code,
                retriable=result.retriable,
                retry_after_sec=result.retry_after_sec,
                circuit_state=self.breaker.state,
            )
            if result.ok:
                self.breaker.on_success()
                logger.info(
                    "telegram_notification_sent",
                    strategy_id=event.strategy_id,
                    symbol=event.symbol,
                    event_type=event.event_type,
                    status=event.status,
                    order_id=event.order_id,
                    perm_id=event.perm_id,
                    order_ref=event.order_ref,
                )
                return True

            is_last = attempt >= self.max_retries
            if is_last or not result.retriable:
                self.breaker.on_failure()
                logger.warning(
                    "telegram_notification_failed",
                    strategy_id=event.strategy_id,
                    symbol=event.symbol,
                    attempt=attempt + 1,
                    error=result.error,
                    status_code=result.status_code,
                    retriable=result.retriable,
                    circuit_state=self.breaker.state,
                )
                await self._audit_push(
                    event,
                    message_text=text,
                    outcome="failed",
                    attempt=attempt + 1,
                    error=result.error,
                    status_code=result.status_code,
                    retriable=result.retriable,
                    retry_after_sec=result.retry_after_sec,
                    circuit_state=self.breaker.state,
                )
                return False

            await asyncio.sleep(self._backoff_sec(attempt, result.retry_after_sec))

        self.breaker.on_failure()
        await self._audit_push(
            event,
            message_text=text,
            outcome="failed",
            attempt=self.max_retries + 1,
            reason="retry_exhausted",
            circuit_state=self.breaker.state,
        )
        return False

    async def _send_once(self, text: str) -> SendAttemptResult:
        return await asyncio.to_thread(self._send_once_sync, text)

    def _send_once_sync(self, text: str) -> SendAttemptResult:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")

        req = request.Request(
            url=url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self.timeout_sec) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                data = json.loads(body) if body else {}
                if bool(data.get("ok")):
                    return SendAttemptResult(ok=True, retriable=False, status_code=getattr(resp, "status", 200))
                err_text = str(data.get("description") or "telegram_api_not_ok")
                return SendAttemptResult(ok=False, retriable=False, error=err_text, status_code=getattr(resp, "status", 200))
        except error.HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            retry_after = None
            msg = f"http_{status}"
            try:
                raw = exc.read().decode("utf-8", errors="replace")
                if raw:
                    data = json.loads(raw)
                    msg = str(data.get("description") or msg)
                    params = data.get("parameters") or {}
                    if isinstance(params, dict) and params.get("retry_after") is not None:
                        retry_after = float(params.get("retry_after"))
            except Exception:
                pass
            retriable = (status >= 500) or (status == 429)
            return SendAttemptResult(ok=False, retriable=retriable, error=msg, status_code=status, retry_after_sec=retry_after)
        except (error.URLError, TimeoutError, OSError) as exc:
            return SendAttemptResult(ok=False, retriable=True, error=str(exc))
        except Exception as exc:  # pragma: no cover
            return SendAttemptResult(ok=False, retriable=False, error=str(exc))

    def _backoff_sec(self, attempt: int, retry_after_sec: Optional[float]) -> float:
        if retry_after_sec is not None and retry_after_sec > 0:
            base = min(retry_after_sec, self.retry_max_delay_sec)
        else:
            base = min(self.retry_base_delay_sec * (2 ** attempt), self.retry_max_delay_sec)

        if self.retry_jitter_sec > 0:
            base += random.uniform(0.0, self.retry_jitter_sec)

        return max(0.0, base)

    def _format_text(self, event: NotificationEvent) -> str:
        if event.event_type == "system_alert":
            return self._format_system_alert(event)

        if event.event_type == "daily_trade_summary":
            return self._format_daily_trade_summary(event)

        if event.event_type in {"reconciliation_summary", "reconciliation_alert"}:
            m = event.metadata or {}
            ts_utc = event.ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            ts_et = event.ts.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET")
            title = "Runtime Reconciliation Alert" if event.event_type == "reconciliation_alert" else "Runtime Reconciliation Summary"
            orphan_count = int(m.get('orphans_found', 0))
            orphan_detected = int(m.get('orphans_detected', orphan_count))
            orphan_self_healed = int(m.get('orphans_self_healed', max(0, orphan_detected - orphan_count)))
            orphan_line = f"Orphans: {orphan_count}"
            if orphan_detected != orphan_count or orphan_self_healed > 0:
                orphan_line = (
                    f"Orphans: {orphan_count} "
                    f"(Detected: {orphan_detected}, Self-healed: {orphan_self_healed})"
                )
            return (
                f"{title}\n"
                f"Time: {ts_et} ({ts_utc})\n"
                f"DB Open Orders: {int(m.get('db_open_orders', 0))}\n"
                f"IB Open Orders: {int(m.get('ib_open_orders', 0))}\n"
                f"IB PreSubmitted: {int(m.get('ib_pre_submitted_orders', 0))}\n"
                f"Ghosts: {int(m.get('ghosts_found', 0))}\n"
                f"{orphan_line}\n"
                f"Mismatches: {int(m.get('mismatches', 0))}"
            )

        ts_utc = event.ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        ts_et = event.ts.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET")
        order_id = f"\nOrder ID: {event.order_id}" if event.order_id is not None else ""
        perm_id = f"\nPerm ID: {event.perm_id}" if event.perm_id is not None else ""
        order_ref = f"\nOrder Ref: {event.order_ref}" if event.order_ref else ""
        order_role = f"\nTrade Type: {event.order_role}" if event.order_role else ""
        option_label = self._infer_option_label(event)
        option_contract = self._infer_option_contract(event)
        trade_action = self._build_trade_action(event.side, option_label)
        action_intent = self._infer_action_intent(event)
        trade_action_line = f"\nAction: {trade_action}" if trade_action else ""
        action_intent_line = f"\nAction Intent: {action_intent}" if action_intent else ""
        option_type_line = f"\nOption Type: {option_label}" if option_label else ""
        option_contract_line = f"\nOption Contract: {option_contract}" if option_contract else ""
        price = f"\nPrice: ${float(event.price):.4f}" if event.price is not None else ""
        fill_qty = f"\nFill Qty: {float(event.fill_qty):.4f}" if event.fill_qty is not None else ""
        amount = f"\nAmount: ${float(event.amount_usd):.2f}" if event.amount_usd is not None else ""
        reason = f"\nReason: {event.reason}" if event.reason else ""
        order_title = "🟢 订单成交" if event.event_type == "order_fill" else "📨 订单通知"
        return (
            f"{order_title}\n"
            "\n"
            "📌 基本信息\n"
            f"• Event: {event.event_type}\n"
            f"• Status: {event.status}\n"
            f"• Time: {ts_et}\n"
            f"• Time UTC: {ts_utc}\n"
            f"• Strategy: {event.strategy_id}\n"
            f"• Symbol: {event.symbol}\n"
            f"• Side: {event.side}"
            f"{trade_action_line}"
            f"{action_intent_line}"
            f"{option_type_line}"
            f"{option_contract_line}"
            f"\n\n"
            "📦 订单明细\n"
            f"• Qty: {float(event.qty):.4f}\n"
            f"• Order Type: {event.order_type}"
            f"{price}"
            f"{fill_qty}"
            f"{amount}"
            f"{order_role}"
            f"{order_id}"
            f"{perm_id}"
            f"{order_ref}"
            f"\n\n"
            "🧭 跟踪\n"
            f"• Decision: {event.decision_id}"
            f"{reason}"
        )

    def _format_system_alert(self, event: NotificationEvent) -> str:
        ts_utc = event.ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        ts_et = event.ts.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET")
        m = event.metadata or {}
        host = str(m.get("host") or "unknown")
        service = str(m.get("service") or event.symbol or "TRADING_SYSTEM")
        error_type = str(m.get("error_type") or "")
        error_text = str(m.get("error") or event.reason or "unknown")
        detail = str(m.get("detail") or "")
        pid = m.get("pid")
        cwd = str(m.get("cwd") or "")
        status = str(event.status or "").upper()
        if status in {"ERROR", "CRITICAL"}:
            title = "🚨 系统故障告警"
        elif status in {"WARNING", "WARN", "DEGRADED"}:
            title = "⚠️ 系统异常通知"
        else:
            title = "ℹ️ 系统恢复通知"
        lines = [
            title,
            f"Time: {ts_et} ({ts_utc})",
            f"Status: {event.status}",
            f"Host: {host}",
            f"Service: {service}",
        ]
        if pid is not None:
            lines.append(f"PID: {pid}")
        if error_type:
            lines.append(f"Error Type: {error_type}")
        lines.append(f"Error: {error_text}")
        if detail:
            lines.append(f"Detail: {detail}")
        if cwd:
            lines.append(f"CWD: {cwd}")
        return "\n".join(lines)

    @staticmethod
    def _build_trade_action(side: str, option_label: str) -> str:
        side_u = str(side or "").upper()
        if side_u not in {"BUY", "SELL"}:
            return ""
        if option_label in {"CALL", "PUT"}:
            return f"{side_u} {option_label}"
        return side_u

    @staticmethod
    def _infer_action_intent(event: NotificationEvent) -> str:
        role = str(event.order_role or "").strip().upper()
        if role in {"PROTECTIVE_STOP", "STOP_LOSS", "TAKE_PROFIT", "FLATTEN", "CLOSE_ONLY", "EXIT"}:
            return "CLOSE"
        if role in {"ENTRY", "PROBE", "ADD"}:
            return "OPEN"
        return ""

    @staticmethod
    def _infer_option_label(event: NotificationEvent) -> str:
        md = event.metadata if isinstance(event.metadata, dict) else {}
        right = str(md.get("right") or md.get("option_right") or "").strip().upper()
        if right in {"C", "CALL"}:
            return "CALL"
        if right in {"P", "PUT"}:
            return "PUT"

        symbol = str(event.symbol or "").strip().upper()
        # IB-style option localSymbol examples:
        # - QQQ  260311C00610000
        # - QQQ 20260311 610 C
        # - QQQ 20260311 610P
        if re.search(r"\bC\d{8}\b", symbol):
            return "CALL"
        if re.search(r"\bP\d{8}\b", symbol):
            return "PUT"
        if re.search(r"\b\d{8}\s+\d+(?:\.\d+)?\s*C\b", symbol):
            return "CALL"
        if re.search(r"\b\d{8}\s+\d+(?:\.\d+)?\s*P\b", symbol):
            return "PUT"
        m = re.search(r"\bOPT:[^:]+:\d{8}:[^:]+:([CP])\b", symbol)
        if m:
            return "CALL" if m.group(1) == "C" else "PUT"

        order_ref = str(event.order_ref or "").strip().upper()
        m = re.search(r"\bOPT:[^:]+:\d{8}:[^:]+:([CP])\b", order_ref)
        if m:
            return "CALL" if m.group(1) == "C" else "PUT"
        return ""

    @staticmethod
    def _infer_option_contract(event: NotificationEvent) -> str:
        symbol = str(event.symbol or "").strip().upper()
        m = re.search(r"\bOPT:([^:]+):(\d{8}):([^:]+):([CP])\b", symbol)
        if m:
            underlying, expiry, strike, right = m.groups()
            return f"{underlying} {expiry} {strike}{right}"

        m = re.search(r"\b([A-Z]{1,6})\s+(\d{6})([CP])(\d{8})\b", symbol)
        if m:
            underlying, yyMMdd, right, strike_int = m.groups()
            return f"{underlying} 20{yyMMdd} {int(strike_int)/1000:g}{right}"
        return ""

    def _format_daily_trade_summary(self, event: NotificationEvent) -> str:
        def _pnl_icon(v: float) -> str:
            if v > 1e-8:
                return "🟢"
            if v < -1e-8:
                return "🔴"
            return "⚪"

        def _fmt_usd(v: float) -> str:
            sign = "+" if v >= 0 else "-"
            return f"{sign}${abs(v):.2f}"

        m = event.metadata or {}
        report_date = str(m.get("report_date") or "")
        strategy_sections = list(m.get("strategy_sections") or [])
        strategy_sections.sort(key=lambda x: float(x.get("total_pnl_usd", 0.0) or 0.0), reverse=True)
        total_realized = float(m.get("realized_pnl_usd", 0.0))
        total_unrealized = float(m.get("unrealized_pnl_usd", 0.0))
        total_pnl = float(m.get("total_pnl_usd", 0.0))

        lines = [
            "📘 交易财务日报",
            f"🗓️ 交易日(ET): {report_date}",
            f"⏱️ 区间: {str(m.get('window_start_et') or '')} -> {str(m.get('window_end_et') or '')}",
            "",
            "💼 账户汇总",
                f"• 当日总损益: {_pnl_icon(total_pnl)} {_fmt_usd(total_pnl)}",
            f"• 已实现: {_fmt_usd(total_realized)} | 未实现: {_fmt_usd(total_unrealized)}",
            f"• 策略数量: {int(m.get('strategy_count', 0))} | 成交笔数: {int(m.get('fills_count', 0))}",
            f"• 期末持仓市值: ${float(m.get('position_market_value_usd', 0.0)):.2f}",
            f"• 风险敞口(绝对名义): ${float(m.get('position_notional_abs_usd', 0.0)):.2f}",
            "",
            "📈 策略盈亏排行（按总损益）",
        ]
        for idx, item in enumerate(strategy_sections[:12], start=1):
            open_positions = item.get("open_positions") or []
            has_open_position = any(abs(float(p.get("qty", 0.0))) > 1e-8 for p in open_positions)
            realized = float(item.get("realized_pnl_usd", 0.0))
            unrealized = float(item.get("unrealized_pnl_usd", 0.0))
            total = float(item.get("total_pnl_usd", 0.0))
            lines.append(
                (
                    f"{idx}. {_pnl_icon(total)} {item.get('strategy_id')} | 总{_fmt_usd(total)} | "
                    f"已{_fmt_usd(realized)} | 未{_fmt_usd(unrealized)} | "
                    f"成交{int(item.get('fills_count', 0))} | 触发{int(item.get('trigger_count', 0))} | "
                    f"未闭{int(item.get('open_trigger_count', 0))} | 持仓={'有仓' if has_open_position else '平仓'}"
                )
            )

        lines.extend(["", "⚠️ 风险关注（仅异常）"])
        risk_lines: list[str] = []
        for sec in strategy_sections[:6]:
            sid = str(sec.get("strategy_id") or "")
            open_positions = sec.get("open_positions") or []
            has_open_position = any(abs(float(p.get("qty", 0.0))) > 1e-8 for p in open_positions)
            open_trigger_count = int(sec.get("open_trigger_count", 0))
            if open_trigger_count > 0:
                open_ids: list[str] = []
                for chain in sec.get("chains") or []:
                    if not bool(chain.get("report_closed", chain.get("closed"))) and chain.get("trigger_id"):
                        open_ids.append(str(chain.get("trigger_id")))
                show_ids = ", ".join(open_ids[:3]) if open_ids else "-"
                risk_lines.append(f"• {sid}: 未闭合触发 {open_trigger_count} 个 | trigger_id: {show_ids}")
            if has_open_position:
                risk_lines.append(f"• {sid}: 账户当前仍有持仓")

        if risk_lines:
            lines.extend(risk_lines[:12])
        else:
            lines.append("• 无异常（无未闭合触发 / 无账户残留持仓）")

        lines.extend(["", "🔎 链路样本（每策略最多2条）"])
        for sec in strategy_sections[:6]:
            sid = str(sec.get("strategy_id") or "")
            chains = sec.get("chains") or []
            if not chains:
                continue
            for chain in chains[:2]:
                chain_closed = bool(chain.get("report_closed", chain.get("closed")))
                carry_close_only = bool(chain.get("carry_close_only"))
                chain_status = "✅闭合(跨日平仓)" if (chain_closed and carry_close_only) else ("✅闭合" if chain_closed else "⏳未闭合")
                lines.append(
                    f"• {sid} | trigger_id={chain.get('trigger_id','')} | "
                    f"触发时间={chain.get('first_trigger_ts_et','')} | "
                    f"已实现={_fmt_usd(float(chain.get('realized_pnl_usd', 0.0)))} | {chain_status}"
                )
            extra = len(chains) - 2
            if extra > 0:
                lines.append(f"  - ... {sid} 还有 {extra} 条触发链未展示")

        truncated = int(m.get("truncated_details", 0) or 0)
        if truncated > 0:
            lines.append("")
            lines.append(f"ℹ️ 明细截断: 另有 {truncated} 笔成交未展示")

        text = "\n".join(lines).strip()
        if len(text) <= 3900:
            return text

        # Telegram max message length is 4096; keep a conservative headroom.
        compact_lines = lines[:]
        while compact_lines and len("\n".join(compact_lines)) > 3900:
            compact_lines.pop()
        compact_lines.append("... (message truncated)")
        return "\n".join(compact_lines)

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): TelegramNotifier._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [TelegramNotifier._json_safe(v) for v in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    async def _audit_push(
        self,
        event: NotificationEvent,
        *,
        message_text: str,
        outcome: str,
        attempt: int,
        error: str = "",
        status_code: Optional[int] = None,
        retriable: Optional[bool] = None,
        retry_after_sec: Optional[float] = None,
        reason: str = "",
        circuit_state: str = "",
    ) -> None:
        if not self.audit_enabled:
            return

        record = {
            "ts": event.ts.astimezone(timezone.utc).isoformat(),
            "push_ts": datetime.now(timezone.utc).isoformat(),
            "outcome": outcome,
            "attempt": int(attempt),
            "reason": reason,
            "error": error,
            "status_code": status_code,
            "retriable": retriable,
            "retry_after_sec": retry_after_sec,
            "circuit_state": circuit_state,
            "event_type": event.event_type,
            "status": event.status,
            "strategy_id": event.strategy_id,
            "symbol": event.symbol,
            "side": event.side,
            "qty": event.qty,
            "order_type": event.order_type,
            "decision_id": event.decision_id,
            "order_id": event.order_id,
            "broker_order_id": event.broker_order_id,
            "perm_id": event.perm_id,
            "order_ref": event.order_ref,
            "message_text": message_text,
            "metadata": self._json_safe(event.metadata or {}),
        }
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(self._json_safe(record), ensure_ascii=False)
            async with self._audit_lock:
                await asyncio.to_thread(self._append_line_sync, line)
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "telegram_push_audit_write_failed",
                strategy_id=event.strategy_id,
                symbol=event.symbol,
                outcome=outcome,
                error=str(exc),
            )

    def _append_line_sync(self, line: str) -> None:
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
