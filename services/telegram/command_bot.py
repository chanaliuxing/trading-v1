from __future__ import annotations

import asyncio
import json
import os
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, Iterable, Optional, Tuple
from urllib import parse, request

from libs.logger import get_logger

logger = get_logger(__name__)


@dataclass
class CommandResult:
    ok: bool
    code: int
    duration_sec: float
    output: str
    command: str


class TelegramCommandBot:
    """Lightweight in-process Telegram command listener (long polling)."""

    def __init__(
        self,
        *,
        bot_token: str,
        allowed_chat_ids: Iterable[str],
        repo_root: Path,
        trading_mode: str,
        ib_host: str,
        ib_port: int,
        ib_client_id: int,
        poll_timeout_sec: int = 25,
        flatten_cooldown_sec: int = 60,
    ):
        self.bot_token = str(bot_token or "").strip()
        self.allowed_chat_ids = {str(x).strip() for x in allowed_chat_ids if str(x).strip()}
        self.repo_root = Path(repo_root)
        self.trading_mode = str(trading_mode or "paper").lower()
        self.ib_host = str(ib_host)
        self.ib_port = int(ib_port)
        self.ib_client_id = int(ib_client_id)
        self.poll_timeout_sec = max(5, int(poll_timeout_sec))
        self.flatten_cooldown_sec = max(1, int(flatten_cooldown_sec))

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._offset: Optional[int] = None
        self._flatten_lock = asyncio.Lock()
        self._last_flatten_ts = 0.0
        self._poll_failures = 0
        self._flatten_handler: Optional[Callable[[str, Optional[str], Optional[str]], Awaitable[str]]] = None

    def set_flatten_handler(self, handler: Callable[[str, Optional[str], Optional[str]], Awaitable[str]]) -> None:
        self._flatten_handler = handler

    async def start(self) -> None:
        if self._running:
            return
        if not self.bot_token or not self.allowed_chat_ids:
            logger.info("telegram_command_bot_disabled_missing_config")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="telegram-command-bot")
        logger.info(
            "telegram_command_bot_started",
            allowed_chat_ids=sorted(self.allowed_chat_ids),
            poll_timeout_sec=self.poll_timeout_sec,
            trading_mode=self.trading_mode,
            ib_port=self.ib_port,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("telegram_command_bot_stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                updates = await asyncio.to_thread(self._get_updates_sync, self._offset)
                if self._poll_failures > 0:
                    logger.info("telegram_command_poll_recovered", failures=self._poll_failures)
                    self._poll_failures = 0
                for upd in updates:
                    try:
                        uid = int(upd.get("update_id"))
                        self._offset = uid + 1
                    except Exception:
                        pass
                    await self._handle_update(upd)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._poll_failures += 1
                retriable = self._is_retriable_poll_error(exc)
                # Network jitter/long-poll timeout is expected; avoid warning spam.
                if retriable and self._poll_failures <= 3:
                    logger.debug(
                        "telegram_command_poll_retrying",
                        error=str(exc),
                        failures=self._poll_failures,
                    )
                else:
                    logger.warning(
                        "telegram_command_poll_failed",
                        error=str(exc),
                        failures=self._poll_failures,
                        retriable=retriable,
                    )
                await asyncio.sleep(min(10.0, 1.5 * self._poll_failures))

    @staticmethod
    def _is_retriable_poll_error(exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, socket.timeout, ssl.SSLError)):
            return True
        msg = str(exc).lower()
        retry_keywords = (
            "timed out",
            "temporary failure",
            "connection reset",
            "connection refused",
            "network is unreachable",
            "tlsv1 alert",
            "handshake",
        )
        return any(k in msg for k in retry_keywords)

    def _get_updates_sync(self, offset: Optional[int]) -> list:
        params = {
            "timeout": self.poll_timeout_sec,
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            params["offset"] = offset
        q = parse.urlencode(params)
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates?{q}"
        with request.urlopen(url, timeout=self.poll_timeout_sec + 5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if not data.get("ok"):
            return []
        return list(data.get("result") or [])

    async def _handle_update(self, update: Dict) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return

        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = str(chat.get("id") or "").strip()
        if chat_id not in self.allowed_chat_ids:
            return

        text = str(message.get("text") or "").strip()
        cmd = self._parse_command(text)
        if cmd is None:
            await self._send_text(chat_id, self._help_text())
            return

        if cmd == "flatten":
            strategy_id, source_trigger_id = self._parse_flatten_target(text)
            await self._handle_flatten(
                chat_id,
                int(message.get("message_id") or 0),
                strategy_id=strategy_id,
                source_trigger_id=source_trigger_id,
            )
            return

        if cmd == "account":
            await self._handle_account(chat_id)
            return

        if cmd == "help":
            await self._send_text(chat_id, self._help_text())
            return

    @staticmethod
    def _parse_command(text: str) -> Optional[str]:
        t = str(text or "").strip().lower()
        if not t:
            return None

        if t.startswith("/"):
            t = t.split("@", 1)[0]

        flatten_aliases = {
            "清仓", "一键清仓", "sell all", "flatten", "flatten all", "liquidate", "liquidate all",
            "sell all shares", "liquidate all shares",
        }
        flatten_phrases = ("sell all", "flatten all", "liquidate all")
        account_aliases = {
            "账号", "账户", "account", "acc", "balance",
        }
        help_aliases = {
            "帮助", "help", "/help", "h",
        }

        if t in flatten_aliases or "清仓" in t or any(p in t for p in flatten_phrases):
            return "flatten"
        if t in account_aliases or "账号" in t or "账户" in t:
            return "account"
        if t in help_aliases:
            return "help"
        return None

    @staticmethod
    def _help_text() -> str:
        return (
            "📘 Telegram 指令帮助\n"
            "\n"
            "可用指令\n"
            "1. 清仓\n"
            "2. 清仓 策略 <strategy_id>\n"
            "3. 清仓 触发 <trigger_id>\n"
            "4. 账号\n"
            "5. 帮助\n"
            "\n"
            "说明\n"
            "• 清仓 = 全账户紧急清仓\n"
            "• 清仓 策略 = 清掉该策略下全部持仓\n"
            "• 清仓 触发 = 按 trigger-net capped close 执行，不是 lot-exact close\n"
            "\n"
            "示例\n"
            "• 清仓\n"
            "• 清仓 策略 qqq_intraday_001\n"
            "• 清仓 触发 b04866d59221f6f9\n"
            "• 账号"
        )

    @staticmethod
    def _parse_flatten_target(text: str) -> tuple[Optional[str], Optional[str]]:
        raw = str(text or "").strip()
        if not raw:
            return None, None
        parts = [p for p in raw.replace("/", " ").split() if p]
        if len(parts) < 3:
            return None, None
        key = parts[1].strip().lower()
        val = parts[2].strip()
        if (not val) or (val in {"-", "_"}):
            return None, None
        if key in {"策略", "strategy", "sid", "strategy_id"}:
            return val, None
        if key in {"触发", "trigger", "trigger_id", "tid"}:
            return None, val
        return None, None

    async def _handle_flatten(
        self,
        chat_id: str,
        message_id: int,
        *,
        strategy_id: Optional[str],
        source_trigger_id: Optional[str],
    ) -> None:
        now = time.time()
        remain = self.flatten_cooldown_sec - (now - self._last_flatten_ts)
        if remain > 0:
            await self._send_text(chat_id, f"清仓命令冷却中，请 {int(remain)} 秒后再试")
            return

        if self._flatten_lock.locked():
            await self._send_text(chat_id, "已有清仓命令在执行，请稍后")
            return

        async with self._flatten_lock:
            self._last_flatten_ts = time.time()
            if self._flatten_handler is None:
                await self._send_text(chat_id, "清仓执行器未就绪，本次拒绝执行")
                return
            scope = "all"
            if strategy_id:
                scope = f"strategy:{strategy_id}"
            elif source_trigger_id:
                scope = f"trigger:{source_trigger_id}"
            await self._send_text(
                chat_id,
                f"收到清仓命令，开始执行统一平仓链路 (scope={scope}, mode={self.trading_mode}, port={self.ib_port})",
            )
            trigger_id = f"telegram:{chat_id}:{message_id or int(time.time())}"
            started = time.time()
            try:
                summary = await asyncio.wait_for(
                    self._flatten_handler(trigger_id, strategy_id, source_trigger_id),
                    timeout=300,
                )
                result = CommandResult(
                    ok=True,
                    code=0,
                    duration_sec=time.time() - started,
                    output=summary,
                    command=f"manual_flatten(trigger_id={trigger_id})",
                )
            except asyncio.TimeoutError:
                result = CommandResult(
                    ok=False,
                    code=124,
                    duration_sec=time.time() - started,
                    output="manual_flatten timeout",
                    command=f"manual_flatten(trigger_id={trigger_id})",
                )
            except Exception as exc:
                result = CommandResult(
                    ok=False,
                    code=1,
                    duration_sec=time.time() - started,
                    output=str(exc),
                    command=f"manual_flatten(trigger_id={trigger_id})",
                )
            await self._send_text(chat_id, self._format_result("清仓", result))

    async def _handle_account(self, chat_id: str) -> None:
        await self._send_text(chat_id, f"收到账号命令，开始查询 (port={self.ib_port})")
        cmd = [
            sys.executable,
            "tool/check_ibkr_acc.py",
            "--host",
            self.ib_host,
            "--port",
            str(self.ib_port),
            "--client-id",
            str(self.ib_client_id + 101),
        ]
        result = await self._run_subprocess(cmd, timeout_sec=240)
        await self._send_text(chat_id, self._format_result("账号", result))

    async def _run_subprocess(self, cmd: list[str], *, timeout_sec: int) -> CommandResult:
        started = time.time()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
            code = int(proc.returncode or 0)
        except asyncio.TimeoutError:
            proc.kill()
            stdout_b, stderr_b = await proc.communicate()
            code = 124
        elapsed = time.time() - started

        stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
        stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
        merged = (stdout + ("\n" if stdout and stderr else "") + stderr).strip()
        return CommandResult(
            ok=(code == 0),
            code=code,
            duration_sec=elapsed,
            output=merged,
            command=" ".join(cmd),
        )

    def _format_result(self, title: str, result: CommandResult) -> str:
        status = "成功" if result.ok else "失败"
        body = result.output.strip()
        if not body:
            body = "(无输出)"
        body = self._compact_output(body, max_chars=3000)
        return (
            f"{title}执行{status}\n"
            f"exit_code={result.code}\n"
            f"耗时={result.duration_sec:.1f}s\n"
            f"命令={result.command}\n"
            f"--- 输出 ---\n{body}"
        )

    @staticmethod
    def _compact_output(text: str, max_chars: int = 3000) -> str:
        t = str(text or "").strip()
        if len(t) <= max_chars:
            return t
        keep_head = max_chars // 2
        keep_tail = max_chars - keep_head - 40
        return f"{t[:keep_head]}\n... (truncated) ...\n{t[-keep_tail:]}"

    async def _send_text(self, chat_id: str, text: str) -> None:
        chunks = self._split_text(text, limit=3500)
        for ch in chunks:
            await asyncio.to_thread(self._send_text_sync, chat_id, ch)

    def _send_text_sync(self, chat_id: str, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": chat_id,
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
        with request.urlopen(req, timeout=15) as resp:
            _ = resp.read()

    @staticmethod
    def _split_text(text: str, limit: int = 3500) -> list[str]:
        t = str(text or "")
        if len(t) <= limit:
            return [t]
        out = []
        cur = 0
        while cur < len(t):
            out.append(t[cur:cur + limit])
            cur += limit
        return out


def build_telegram_command_bot(
    *,
    repo_root: Path,
    trading_mode: str,
    ib_host: str,
    ib_port: int,
    ib_client_id: int,
) -> Optional[TelegramCommandBot]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    enabled_raw = os.getenv("TELEGRAM_COMMAND_ENABLED", "1").strip().lower()
    enabled = enabled_raw not in {"0", "false", "no", "off"}
    if (not token) or (not enabled):
        return None

    ids_raw = os.getenv("TELEGRAM_COMMAND_ALLOWED_CHAT_IDS", "").strip()
    if not ids_raw:
        ids_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    allowed = [x.strip() for x in ids_raw.split(",") if x.strip()]
    if not allowed:
        return None

    poll_timeout = int(os.getenv("TELEGRAM_COMMAND_POLL_TIMEOUT_SEC", "25"))
    flatten_cooldown = int(os.getenv("TELEGRAM_COMMAND_FLATTEN_COOLDOWN_SEC", "60"))

    return TelegramCommandBot(
        bot_token=token,
        allowed_chat_ids=allowed,
        repo_root=repo_root,
        trading_mode=trading_mode,
        ib_host=ib_host,
        ib_port=ib_port,
        ib_client_id=ib_client_id,
        poll_timeout_sec=poll_timeout,
        flatten_cooldown_sec=flatten_cooldown,
    )
