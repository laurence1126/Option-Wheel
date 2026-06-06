from __future__ import annotations

import os
import secrets
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import pandas as pd

from app.telegram_bot import TelegramBotService
from app.utils.telegram import answer_telegram_callback_query as answer_callback_query
from app.utils.telegram import edit_telegram_message_text
from trading.notification.telegram_callbacks import parse_strategy_callback
from trading.notification.telegram_consts import (
    BOT_COMMANDS,
    HELP_TEXT,
    EMPTY_INLINE_KEYBOARD,
    RESTART_ENV_VAR,
    SHORT_PUT_ACTION_ID,
    SHORT_PUT_CONFIRMATION_TIMEOUT_SECONDS,
    OPTION_WATCHER_APP_URL,
)
from trading.notification.telegram_status import build_status_message
from trading.notification.telegram_summary import replace_summary_prompt
from app.utils.logging import configure_logger

if TYPE_CHECKING:
    from trading.trading_engine.futu_trading_engine import FutuTradingEngine

logger = configure_logger(__name__)


@dataclass
class PendingApproval:
    event: threading.Event
    summary: str
    result: bool | None = None


@dataclass
class PendingShortPut:
    expires_at: pd.Timestamp
    strategy_ids: list[str]
    selected_strategy_id: str | None = None


class TelegramTradingHandler:
    def __init__(
        self,
        config_path: str = ".config",
    ) -> None:
        self.bot = TelegramBotService(config_path=config_path, handler=self)
        self.engine: FutuTradingEngine | None = None

        self._shortput_running = False
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._pending_shortput_confirmations: dict[str, PendingShortPut] = {}
        self._lock = threading.RLock()

    ####################################################################################################
    # Public Service API
    ####################################################################################################

    def send_message(self, text: str, reply_markup: dict | None = None, parse_mode: str | None = None) -> bool:
        return self.bot.send_message(text, reply_markup=reply_markup, parse_mode=parse_mode)

    def start(self, engine: FutuTradingEngine) -> None:
        if self.bot._running:
            return

        self.engine = engine

        was_restarted = os.environ.pop(RESTART_ENV_VAR, None) == "1"
        startup_message = "Trading engine restart complete 🎉" if was_restarted else "Trading engine started 🎊"
        self.bot.start(startup_message=startup_message, commands=BOT_COMMANDS)

    def shutdown(self) -> None:
        with self._lock:
            pending_approvals = list(self._pending_approvals.values())
            self._pending_approvals.clear()
            self._pending_shortput_confirmations.clear()
        for approval in pending_approvals:
            approval.result = False
            approval.event.set()

        self.bot.shutdown()

    def request_trade_approval(self, summary: str, timeout_seconds: int) -> bool:
        if not self.bot.enabled or self.bot.config is None:
            logger.warning("Telegram trade approval unavailable because Telegram bot service is disabled.")
            return False

        approval_id = secrets.token_urlsafe(8)
        approval = PendingApproval(event=threading.Event(), summary=summary)
        with self._lock:
            self._pending_approvals[approval_id] = approval

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"approve:{approval_id}"},
                    {"text": "❌ Reject", "callback_data": f"reject:{approval_id}"},
                ]
            ]
        }
        sent, message_id = self.bot.send_message_with_id(summary, reply_markup=reply_markup, parse_mode="HTML")
        if not sent:
            with self._lock:
                self._pending_approvals.pop(approval_id, None)
            logger.warning("Telegram trade approval request failed to send.")
            return False

        approved_in_time = approval.event.wait(timeout_seconds)
        with self._lock:
            self._pending_approvals.pop(approval_id, None)
        if not approved_in_time:
            logger.warning("Telegram trade approval timed out: approval_id=%s, timeout_seconds=%s.", approval_id, timeout_seconds)
            edit_telegram_message_text(
                self.bot.config,
                chat_id=self.bot.config.chat_id,
                message_id=message_id,
                text=replace_summary_prompt(summary, "⚠️ Trade approval timed out."),
                parse_mode="HTML",
                reply_markup=EMPTY_INLINE_KEYBOARD,
            )
            return False
        return approval.result is True

    @property
    def config(self):
        return self.bot.config

    @config.setter
    def config(self, value) -> None:
        self.bot.config = value

    @property
    def enabled(self) -> bool:
        return self.bot.enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self.bot.enabled = value

    @property
    def _running(self) -> bool:
        return self.bot._running

    @_running.setter
    def _running(self, value: bool) -> None:
        self.bot._running = value

    def handle_webhook_update(self, update: dict) -> None:
        self.bot.handle_webhook_update(update)

    ####################################################################################################
    # Update / Callback Dispatch Handlers
    ####################################################################################################

    def handle_message(self, message: dict) -> None:
        text = str(message.get("text", "")).strip()
        command = text.split(maxsplit=1)[0].lower()
        if command == "/start":
            self.bot.send_message("Hello! 🤖 Quant bot is online.")
        elif command == "/help":
            self.bot.send_message(HELP_TEXT, parse_mode="HTML")
        elif command == "/status":
            self.bot.send_message(build_status_message(self.engine), parse_mode="HTML")
        elif command == "/watcher":
            watcher_url = OPTION_WATCHER_APP_URL
            reply_markup = {"inline_keyboard": [[{"text": "📲 Open Option Watcher", "web_app": {"url": watcher_url}}]]}
            self.bot.send_message("Click the button below:", reply_markup=reply_markup)
        elif command == "/log":
            latest_log = None
            latest_key = None
            for log_file in (Path(__file__).resolve().parents[1] / "log").glob("*.log"):
                try:
                    current_key = (log_file.stat().st_mtime_ns, log_file.name)
                except OSError:
                    continue
                if latest_key is None or current_key > latest_key:
                    latest_key = current_key
                    latest_log = log_file
            if latest_log is None:
                self.bot.send_message("☹️ No log files found.")
            elif not self.bot.send_document(latest_log, caption=f"Latest log file: {latest_log.name}"):
                self.bot.send_message("😰 Failed to send latest log file.")
        elif command == "/shortput":
            self._request_shortput_confirmation()
        elif command == "/shutdown":
            self._request_shutdown_confirmation()
        elif command == "/restart":
            self._request_restart_confirmation()

    def handle_callback_query(self, callback_query: dict) -> None:
        message = callback_query.get("message", {})
        chat_id = str(message.get("chat", {}).get("id"))
        callback_query_id = str(callback_query.get("id"))
        message_id = int(message.get("message_id"))
        data = str(callback_query.get("data", ""))

        if data.startswith("approve:"):
            self._resolve_approval(data.removeprefix("approve:"), True, callback_query_id, chat_id, message_id)
        elif data.startswith("reject:"):
            self._resolve_approval(data.removeprefix("reject:"), False, callback_query_id, chat_id, message_id)
        elif data == "shutdown:confirm":
            self._confirm_shutdown(callback_query_id, chat_id, message_id)
        elif data == "shutdown:cancel":
            self._cancel_shutdown(callback_query_id, chat_id, message_id)
        elif data == "restart:confirm":
            self._confirm_restart(callback_query_id, chat_id, message_id)
        elif data == "restart:cancel":
            self._cancel_restart(callback_query_id, chat_id, message_id)
        elif data.startswith("shortput:confirm:"):
            self._confirm_shortput(data.removeprefix("shortput:confirm:"), callback_query_id, chat_id, message_id)
        elif data.startswith("shortput:select:"):
            self._select_shortput(data.removeprefix("shortput:select:"), callback_query_id, chat_id, message_id)
        elif data.startswith("shortput:cancel:"):
            self._cancel_shortput(data.removeprefix("shortput:cancel:"), callback_query_id, chat_id, message_id)
        elif data.startswith("strategy:"):
            self._handle_strategy_callback(data, callback_query_id, chat_id, message_id, str(message.get("text", "")))

    def _handle_strategy_callback(self, data: str, callback_query_id: str, chat_id: str, message_id: int, message_text: str) -> None:
        callback = parse_strategy_callback(data)
        if callback is None:
            answer_callback_query(self.config, callback_query_id, "Strategy action unavailable")
            return

        if callback.action_type == "retry" and callback.action_id is not None:
            self._retry_strategy(callback.strategy_id, callback.action_id, callback_query_id, chat_id, message_id, message_text)
        elif callback.action_type == "cancel":
            self._cancel_strategy_retry(callback_query_id, chat_id, message_id, message_text)
        else:
            answer_callback_query(self.config, callback_query_id, "Strategy action unavailable")

    ####################################################################################################
    # Trade Approval Callback Handling
    ####################################################################################################

    def _resolve_approval(self, approval_id: str, approved: bool, callback_query_id: str, chat_id: str, message_id: int) -> None:
        with self._lock:
            approval = self._pending_approvals.get(approval_id)

        if approval is None:
            answer_callback_query(self.config, callback_query_id, "Approval expired")
            return

        approval.result = approved
        approval.event.set()
        if approved:
            answer_callback_query(self.config, callback_query_id, "Trade approved")
            edit_telegram_message_text(
                self.config,
                chat_id=chat_id,
                message_id=message_id,
                text=replace_summary_prompt(approval.summary, "✅ Trade approved."),
                parse_mode="HTML",
                reply_markup=EMPTY_INLINE_KEYBOARD,
            )
        else:
            answer_callback_query(self.config, callback_query_id, "Trade rejected")
            edit_telegram_message_text(
                self.config,
                chat_id=chat_id,
                message_id=message_id,
                text=replace_summary_prompt(approval.summary, "❌ Trade rejected."),
                parse_mode="HTML",
                reply_markup=EMPTY_INLINE_KEYBOARD,
            )

    ####################################################################################################
    # Engine Control Commands
    ####################################################################################################

    def _request_shutdown_confirmation(self) -> None:
        if self.config is None:
            return

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Confirm", "callback_data": "shutdown:confirm"},
                    {"text": "❌ Cancel", "callback_data": "shutdown:cancel"},
                ]
            ]
        }
        self.bot.send_message("⚠️ Confirm trading engine shutdown?", reply_markup=reply_markup)

    def _confirm_shutdown(self, callback_query_id: str, chat_id: str, message_id: int) -> None:
        answer_callback_query(self.config, callback_query_id, "Shutdown confirmed")
        edit_telegram_message_text(self.config, chat_id=chat_id, message_id=message_id, text="Trading engine shutdown confirmed.")
        self._run_process_control_in_background(self._shutdown_process, "telegram-shutdown")

    def _cancel_shutdown(self, callback_query_id: str, chat_id: str, message_id: int) -> None:
        answer_callback_query(self.config, callback_query_id, "Shutdown cancelled")
        edit_telegram_message_text(self.config, chat_id=chat_id, message_id=message_id, text="Shutdown cancelled.")

    def _request_restart_confirmation(self) -> None:
        if self.config is None:
            return

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Confirm", "callback_data": "restart:confirm"},
                    {"text": "❌ Cancel", "callback_data": "restart:cancel"},
                ]
            ]
        }
        self.bot.send_message("⚠️ Confirm trading engine restart?", reply_markup=reply_markup)

    def _confirm_restart(self, callback_query_id: str, chat_id: str, message_id: int) -> None:
        answer_callback_query(self.config, callback_query_id, "Restart confirmed")
        edit_telegram_message_text(self.config, chat_id=chat_id, message_id=message_id, text="Trading engine restart confirmed. Restarting now...")
        self._run_process_control_in_background(self._restart_process, "telegram-restart")

    def _cancel_restart(self, callback_query_id: str, chat_id: str, message_id: int) -> None:
        answer_callback_query(self.config, callback_query_id, "Restart cancelled")
        edit_telegram_message_text(self.config, chat_id=chat_id, message_id=message_id, text="Restart cancelled.")

    ####################################################################################################
    # Short Put Command Handling
    ####################################################################################################

    def _request_shortput_confirmation(self) -> None:
        if self.config is None:
            return

        matches, unavailable_reason = self._find_shortput_actions()
        if unavailable_reason is not None or not matches:
            self.bot.send_message(unavailable_reason or "Short put strategy unavailable.")
            return

        token = secrets.token_urlsafe(8)
        expires_at = pd.Timestamp.now() + pd.Timedelta(seconds=SHORT_PUT_CONFIRMATION_TIMEOUT_SECONDS)
        strategy_ids = [strategy_id for strategy_id, _, _ in matches]
        with self._lock:
            self._pending_shortput_confirmations[token] = PendingShortPut(
                expires_at=expires_at,
                strategy_ids=strategy_ids,
                selected_strategy_id=strategy_ids[0] if len(strategy_ids) == 1 else None,
            )

        if len(strategy_ids) > 1:
            reply_markup = {
                "inline_keyboard": [
                    [{"text": strategy_id, "callback_data": f"shortput:select:{token}:{index}"}] for index, strategy_id in enumerate(strategy_ids)
                ]
                + [[{"text": "❌ Cancel", "callback_data": f"shortput:cancel:{token}"}]]
            }
            self.bot.send_message("Which strategy ID would you want to execute short put?", reply_markup=reply_markup)
            return

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Confirm", "callback_data": f"shortput:confirm:{token}"},
                    {"text": "❌ Cancel", "callback_data": f"shortput:cancel:{token}"},
                ]
            ]
        }
        self.bot.send_message(
            f"⚠️ Confirm short put strategy execution?\n💸 Strategy ID: {strategy_ids[0]}",
            reply_markup=reply_markup,
        )

    def _select_shortput(self, payload: str, callback_query_id: str, chat_id: str, message_id: int) -> None:
        try:
            token, index_text = payload.rsplit(":", 1)
            index = int(index_text)
        except (ValueError, TypeError):
            self._expire_shortput_callback(callback_query_id, chat_id, message_id)
            return

        pending = self._get_shortput_confirmation(token)
        if pending is None:
            self._expire_shortput_callback(callback_query_id, chat_id, message_id)
            return
        if index < 0 or index >= len(pending.strategy_ids):
            answer_callback_query(self.config, callback_query_id, "Short put strategy unavailable.")
            edit_telegram_message_text(
                self.config,
                chat_id=chat_id,
                message_id=message_id,
                text="Short put strategy unavailable.",
                reply_markup=EMPTY_INLINE_KEYBOARD,
            )
            return

        strategy_id = pending.strategy_ids[index]
        with self._lock:
            current = self._pending_shortput_confirmations.get(token)
            if current is not None:
                current.selected_strategy_id = strategy_id

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Confirm", "callback_data": f"shortput:confirm:{token}"},
                    {"text": "❌ Cancel", "callback_data": f"shortput:cancel:{token}"},
                ]
            ]
        }
        answer_callback_query(self.config, callback_query_id, "Short put strategy selected")
        edit_telegram_message_text(
            self.config,
            chat_id=chat_id,
            message_id=message_id,
            text=f"⚠️ Confirm short put strategy execution?\n💸 Strategy ID: {strategy_id}",
            reply_markup=reply_markup,
        )

    def _confirm_shortput(self, token: str, callback_query_id: str, chat_id: str, message_id: int) -> None:
        pending = self._consume_shortput_confirmation(token)
        if pending is None:
            self._expire_shortput_callback(callback_query_id, chat_id, message_id)
            return

        if pending.selected_strategy_id is None:
            message = "Short put strategy unavailable."
            answer_callback_query(self.config, callback_query_id, message)
            edit_telegram_message_text(
                self.config,
                chat_id=chat_id,
                message_id=message_id,
                text=message,
                reply_markup=EMPTY_INLINE_KEYBOARD,
            )
            return
        strategy_id = pending.selected_strategy_id

        with self._lock:
            if self._shortput_running:
                answer_callback_query(self.config, callback_query_id, "Short put execution already running")
                edit_telegram_message_text(
                    self.config,
                    chat_id=chat_id,
                    message_id=message_id,
                    text="Short put execution already running.",
                    reply_markup=EMPTY_INLINE_KEYBOARD,
                )
                return
            self._shortput_running = True

        shortput_action = self._resolve_shortput_action(strategy_id)
        if shortput_action is None:
            with self._lock:
                self._shortput_running = False
            answer_callback_query(self.config, callback_query_id, "Short put strategy unavailable.")
            edit_telegram_message_text(
                self.config,
                chat_id=chat_id,
                message_id=message_id,
                text="Short put strategy unavailable.",
                reply_markup=EMPTY_INLINE_KEYBOARD,
            )
            return
        strategy, action = shortput_action

        answer_callback_query(self.config, callback_query_id, "Short put confirmed")
        edit_telegram_message_text(
            self.config,
            chat_id=chat_id,
            message_id=message_id,
            text="Short put execution confirmed. Starting now...",
            reply_markup=EMPTY_INLINE_KEYBOARD,
        )
        shortput_thread = threading.Thread(
            target=self._run_shortput_action,
            args=(strategy_id, strategy, action),
            name=f"{strategy_id}-shortput-command",
            daemon=True,
        )
        try:
            shortput_thread.start()
        except Exception:
            with self._lock:
                self._shortput_running = False
            raise

    def _cancel_shortput(self, token: str, callback_query_id: str, chat_id: str, message_id: int) -> None:
        if self._consume_shortput_confirmation(token) is None:
            self._expire_shortput_callback(callback_query_id, chat_id, message_id)
            return

        answer_callback_query(self.config, callback_query_id, "Short put execution cancelled")
        edit_telegram_message_text(
            self.config,
            chat_id=chat_id,
            message_id=message_id,
            text="Short put execution cancelled.",
            reply_markup=EMPTY_INLINE_KEYBOARD,
        )

    def _expire_shortput_callback(self, callback_query_id: str, chat_id: str, message_id: int) -> None:
        answer_callback_query(self.config, callback_query_id, "Short put confirmation expired")
        edit_telegram_message_text(
            self.config,
            chat_id=chat_id,
            message_id=message_id,
            text="Short put confirmation expired.",
            reply_markup=EMPTY_INLINE_KEYBOARD,
        )

    def _get_shortput_confirmation(self, token: str) -> PendingShortPut | None:
        with self._lock:
            pending = self._pending_shortput_confirmations.get(token)
            if pending is None:
                return None
            if pd.Timestamp.now() >= pd.Timestamp(pending.expires_at):
                self._pending_shortput_confirmations.pop(token, None)
                return None
            return pending

    def _consume_shortput_confirmation(self, token: str) -> PendingShortPut | None:
        with self._lock:
            pending = self._pending_shortput_confirmations.pop(token, None)
        if pending is None:
            return None
        if pd.Timestamp.now() >= pd.Timestamp(pending.expires_at):
            return None
        return pending

    def _find_shortput_actions(self) -> tuple[list[tuple[str, object, Callable[..., None]]], str | None]:
        if self.engine is None:
            return [], "Trading engine unavailable."

        strategies = getattr(self.engine, "strategy", {})
        if not isinstance(strategies, dict) or not strategies:
            return [], "Short put strategy unavailable."

        matches: list[tuple[str, object, Callable[..., None]]] = []
        for strategy_id, strategy in strategies.items():
            get_actions = getattr(strategy, "get_strategy_actions", None)
            if not callable(get_actions):
                continue
            actions = get_actions()
            if not isinstance(actions, dict):
                continue
            action = actions.get(SHORT_PUT_ACTION_ID)
            if callable(action):
                matches.append((strategy_id, strategy, action))

        if not matches:
            return [], "Short put strategy unavailable."
        return matches, None

    def _resolve_shortput_action(self, strategy_id: str) -> tuple[object, Callable[..., None]] | None:
        if self.engine is None:
            return None

        strategies = getattr(self.engine, "strategy", {})
        if not isinstance(strategies, dict):
            return None
        strategy = strategies.get(strategy_id)
        if strategy is None:
            return None

        get_actions = getattr(strategy, "get_strategy_actions", None)
        if not callable(get_actions):
            return None
        actions = get_actions()
        if not isinstance(actions, dict):
            return None
        action = actions.get(SHORT_PUT_ACTION_ID)
        return (strategy, action) if callable(action) else None

    def _run_shortput_action(self, strategy_id: str, strategy: object, shortput_action: Callable[..., None]) -> None:
        try:
            shortput_action(strategy)
        except Exception as exc:
            logger.exception("Short put command failed: strategy_id=%s.", strategy_id)
            self.send_message(f"Short put execution failed: {exc}")
        finally:
            with self._lock:
                self._shortput_running = False

    ####################################################################################################
    # Strategy Retry Callback Handling
    ####################################################################################################

    def _retry_strategy(self, strategy_id: str, action_id: str, callback_query_id: str, chat_id: str, message_id: int, message_text: str) -> None:
        strategies = getattr(self.engine, "strategy", {}) if self.engine is not None else {}
        strategy = strategies.get(strategy_id) if isinstance(strategies, dict) else None
        if strategy is None:
            answer_callback_query(self.config, callback_query_id, "Strategy unavailable")
            return

        retry_actions = strategy.get_strategy_actions()
        retry_action = retry_actions.get(action_id)
        if not callable(retry_action):
            answer_callback_query(self.config, callback_query_id, "Retry unavailable")
            return

        if action_id == SHORT_PUT_ACTION_ID:
            try:
                acc_id = getattr(strategy, "acc_id")
            except Exception:
                logger.exception("Strategy retry unavailable because account id lookup failed: strategy_id=%s, action_id=%s.", strategy_id, action_id)
                answer_callback_query(self.config, callback_query_id, "Retry unavailable")
                return
            if acc_id is None or self.engine is None or not self.engine.cancel_open_orders(acc_id=acc_id):
                logger.error("Strategy retry skipped because open order cancellation failed: strategy_id=%s, action_id=%s.", strategy_id, action_id)
                answer_callback_query(self.config, callback_query_id, "Open order cancellation failed")
                return

        answer_callback_query(self.config, callback_query_id, "Retrying strategy...")
        edit_telegram_message_text(
            self.config,
            chat_id=chat_id,
            message_id=message_id,
            text=message_text or "Strategy retry requested.",
            reply_markup=EMPTY_INLINE_KEYBOARD,
        )
        retry_thread = threading.Thread(
            target=self._run_strategy_retry, args=(strategy_id, strategy, action_id, retry_action), name=f"{strategy_id}-{action_id}-retry", daemon=True
        )
        retry_thread.start()

    def _cancel_strategy_retry(self, callback_query_id: str, chat_id: str, message_id: int, message_text: str) -> None:
        answer_callback_query(self.config, callback_query_id, "Retry cancelled")
        edit_telegram_message_text(
            self.config,
            chat_id=chat_id,
            message_id=message_id,
            text=message_text or "Strategy retry cancelled.",
            reply_markup=EMPTY_INLINE_KEYBOARD,
        )

    def _run_strategy_retry(self, strategy_id: str, strategy: object, action_id: str, retry_action) -> None:
        try:
            retry_action(strategy)
        except Exception:
            logger.exception("Strategy retry failed: strategy_id=%s, action_id=%s.", strategy_id, action_id)

    ####################################################################################################
    # Process Control Helpers
    ####################################################################################################

    def _restart_process(self) -> None:
        logger.info("Restarting trading engine process.")
        os.environ[RESTART_ENV_VAR] = "1"
        if self.engine is not None:
            self.engine.close()
        os.execv(sys.executable, [sys.executable, *sys.argv])

    def _shutdown_process(self) -> None:
        logger.info("Shutting down trading engine process.")
        if self.engine is not None:
            self.engine.close()
        os._exit(0)

    def _run_process_control_in_background(self, target: Callable[[], None], name: str) -> None:
        threading.Thread(target=target, name=name, daemon=True).start()
