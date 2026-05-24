from __future__ import annotations

import os
import sys
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import pandas as pd

from trading.notification.telegram_callbacks import parse_assignment_callback, parse_strategy_callback
from trading.notification.telegram_consts import (
    BOT_COMMANDS,
    HELP_TEXT,
    EMPTY_INLINE_KEYBOARD,
    RESTART_ENV_VAR,
    SHORT_PUT_ACTION_ID,
    SHORT_PUT_CONFIRMATION_TIMEOUT_SECONDS,
)
from trading.notification.telegram_status import build_status_message
from trading.notification.telegram_summary import replace_summary_prompt
from trading.utils.logging_utils import configure_logger
from trading.utils.telegram_utils import (
    TelegramConfig,
    answer_callback_query,
    edit_telegram_message_text,
    get_telegram_config,
    get_telegram_updates,
    send_telegram_document,
    send_telegram_message,
    set_telegram_commands,
    set_telegram_commands_menu,
)

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


class TelegramBotService:
    def __init__(
        self,
        config_path: str = ".config",
        poll_timeout_seconds: int = 30,
        error_backoff_seconds: int = 5,
    ) -> None:
        self.config_path = config_path
        self.poll_timeout_seconds = poll_timeout_seconds
        self.error_backoff_seconds = error_backoff_seconds

        self.config: TelegramConfig | None = None
        self.engine: FutuTradingEngine | None = None
        self.enabled = False

        self._running = False
        self._shortput_running = False
        self._offset: int | None = None
        self._shutdown_event = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._pending_shortput_confirmations: dict[str, PendingShortPut] = {}
        self._lock = threading.RLock()

    ####################################################################################################
    # Public Service API
    ####################################################################################################

    def send_message(self, text: str, reply_markup: dict | None = None, parse_mode: str | None = None) -> bool:
        if not self.enabled or self.config is None:
            logger.warning("Telegram message unavailable because Telegram bot service is disabled.")
            return False

        sent, _ = send_telegram_message(config=self.config, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        return sent

    def start(self, engine: FutuTradingEngine) -> None:
        if self._running:
            return

        self.engine = engine
        self.config = get_telegram_config(self.config_path)
        was_restarted = os.environ.pop(RESTART_ENV_VAR, None) == "1"
        if not self.config.enabled:
            self.enabled = False
            logger.info("Telegram bot service disabled by config.")
            return

        self.enabled = True
        set_telegram_commands(self.config, BOT_COMMANDS)
        set_telegram_commands_menu(self.config)
        self._discard_pending_updates()
        if was_restarted:
            send_telegram_message(self.config, "Trading engine restart complete 🎉")
        else:
            send_telegram_message(self.config, "Trading engine started 🎊")

        self._shutdown_event.clear()
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, name="telegram-bot-poller", daemon=True)
        self._poll_thread.start()
        if was_restarted:
            restart_recovery_thread = threading.Thread(
                target=self._run_restart_recovery_actions,
                name="telegram-restart-recovery",
                daemon=True,
            )
            restart_recovery_thread.start()
        logger.info("Telegram bot service started.")

    def shutdown(self) -> None:
        self._shutdown_event.set()
        with self._lock:
            pending_approvals = list(self._pending_approvals.values())
            self._pending_approvals.clear()
            self._pending_shortput_confirmations.clear()
        for approval in pending_approvals:
            approval.result = False
            approval.event.set()

        if self._poll_thread and self._poll_thread.is_alive() and threading.current_thread() is not self._poll_thread:
            self._poll_thread.join(timeout=self.poll_timeout_seconds + 2)
        self._running = False
        logger.info("Telegram bot service stopped.")

    def request_trade_approval(self, summary: str, timeout_seconds: int) -> bool:
        if not self.enabled or self.config is None:
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
        sent, message_id = send_telegram_message(self.config, summary, reply_markup=reply_markup, parse_mode="HTML")
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
                self.config,
                chat_id=self.config.chat_id,
                message_id=message_id,
                text=replace_summary_prompt(summary, "⚠️ Trade approval timed out."),
                parse_mode="HTML",
                reply_markup=EMPTY_INLINE_KEYBOARD,
            )
            return False
        return approval.result is True

    ####################################################################################################
    # Update / Callback Dispatch Handlers
    ####################################################################################################

    def _handle_update(self, update: dict) -> None:
        if "message" in update:
            self._handle_message(update["message"])
            return

        if "callback_query" in update:
            self._handle_callback_query(update["callback_query"])

    def _handle_message(self, message: dict) -> None:
        chat = message.get("chat", {})
        chat_id = str(chat.get("id"))
        if not self._is_allowed_chat(chat_id):
            return

        text = str(message.get("text", "")).strip()
        command = text.split(maxsplit=1)[0].lower()
        if command == "/start":
            send_telegram_message(self.config, "Hello! Quant bot is online.")
        elif command == "/help":
            send_telegram_message(self.config, HELP_TEXT, parse_mode="HTML")
        elif command == "/status":
            send_telegram_message(self.config, build_status_message(self.engine), parse_mode="HTML")
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
                send_telegram_message(self.config, "☹️ No log files found.")
            elif not send_telegram_document(self.config, latest_log, caption=f"Latest log file: {latest_log.name}"):
                send_telegram_message(self.config, "😰 Failed to send latest log file.")
        elif command == "/shortput":
            self._request_shortput_confirmation()
        elif command == "/shutdown":
            self._request_shutdown_confirmation()
        elif command == "/restart":
            self._request_restart_confirmation()

    def _handle_callback_query(self, callback_query: dict) -> None:
        message = callback_query.get("message", {})
        chat = message.get("chat", {})
        chat_id = str(chat.get("id"))
        if not self._is_allowed_chat(chat_id):
            return

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
        elif data.startswith("assignment:"):
            self._handle_assignment_callback(data, callback_query_id, chat_id, message_id, str(message.get("text", "")))

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

    def _handle_assignment_callback(self, data: str, callback_query_id: str, chat_id: str, message_id: int, message_text: str) -> None:
        callback = parse_assignment_callback(data)
        if callback is None:
            answer_callback_query(self.config, callback_query_id, "Assignment action unavailable")
            return

        strategies = getattr(self.engine, "strategy", {}) if self.engine is not None else {}
        strategy = strategies.get(callback.strategy_id) if isinstance(strategies, dict) else None
        if strategy is None:
            answer_callback_query(self.config, callback_query_id, "Assignment action unavailable")
            return

        pending_actions = getattr(strategy, "_pending_assignment_actions", {})
        pending_action = pending_actions.get(callback.assignment_token) if isinstance(pending_actions, dict) else None
        if pending_action is None:
            answer_callback_query(self.config, callback_query_id, "Assignment action expired")
            return
        lock = getattr(strategy, "lock", None)
        if lock is None:
            expires_at = pending_action.get("expires_at")
            if expires_at is not None and pd.Timestamp.now() >= pd.Timestamp(expires_at):
                pending_actions.pop(callback.assignment_token, None)
                answer_callback_query(self.config, callback_query_id, "Assignment action expired")
                return
        else:
            with lock:
                pending_action = pending_actions.get(callback.assignment_token)
                if pending_action is None:
                    answer_callback_query(self.config, callback_query_id, "Assignment action expired")
                    return
                expires_at = pending_action.get("expires_at")
                if expires_at is not None and pd.Timestamp.now() >= pd.Timestamp(expires_at):
                    pending_actions.pop(callback.assignment_token, None)
                    answer_callback_query(self.config, callback_query_id, "Assignment action expired")
                    return

        if callback.action_type == "liquidate":
            if lock is None:
                if pending_action.get("action_status") not in {None, "liquidating"}:
                    answer_callback_query(self.config, callback_query_id, "Assignment action already selected")
                    return
                pending_action["action_status"] = "liquidating"
            else:
                with lock:
                    pending_action = pending_actions.get(callback.assignment_token)
                    if pending_action is None:
                        answer_callback_query(self.config, callback_query_id, "Assignment action expired")
                        return
                    if pending_action.get("action_status") not in {None, "liquidating"}:
                        answer_callback_query(self.config, callback_query_id, "Assignment action already selected")
                        return
                    pending_action["action_status"] = "liquidating"
            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": "📈 Market Order", "callback_data": f"assignment:{callback.strategy_id}:market_order:{callback.assignment_token}"},
                        {"text": "🪜 Price Ladder", "callback_data": f"assignment:{callback.strategy_id}:price_ladder:{callback.assignment_token}"},
                    ]
                ]
            }
            answer_callback_query(self.config, callback_query_id, "Choose liquidation method")
            edit_telegram_message_text(
                self.config,
                chat_id=chat_id,
                message_id=message_id,
                text=replace_summary_prompt(message_text, "🚬 Choose liquidation method."),
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        elif callback.action_type == "market_order":
            if lock is None:
                if pending_action.get("action_status") not in {"liquidating", "market_order_failed"}:
                    answer_callback_query(self.config, callback_query_id, "Assignment action already selected")
                    return
                pending_action["action_status"] = "market_order_selected"
            else:
                with lock:
                    pending_action = pending_actions.get(callback.assignment_token)
                    if pending_action is None:
                        answer_callback_query(self.config, callback_query_id, "Assignment action expired")
                        return
                    if pending_action.get("action_status") not in {"liquidating", "market_order_failed"}:
                        answer_callback_query(self.config, callback_query_id, "Assignment action already selected")
                        return
                    pending_action["action_status"] = "market_order_selected"
            answer_callback_query(self.config, callback_query_id, "Market order selected")
            edit_telegram_message_text(
                self.config,
                chat_id=chat_id,
                message_id=message_id,
                text=replace_summary_prompt(message_text, "📈 Liquidating with market order..."),
                parse_mode="HTML",
                reply_markup=EMPTY_INLINE_KEYBOARD,
            )
            self._execute_assignment_action(callback.strategy_id, strategy, "market_order", callback.assignment_token)
        elif callback.action_type == "price_ladder":
            if lock is None:
                if pending_action.get("action_status") not in {"liquidating", "price_ladder_failed"}:
                    answer_callback_query(self.config, callback_query_id, "Assignment action already selected")
                    return
                pending_action["action_status"] = "price_ladder_selected"
            else:
                with lock:
                    pending_action = pending_actions.get(callback.assignment_token)
                    if pending_action is None:
                        answer_callback_query(self.config, callback_query_id, "Assignment action expired")
                        return
                    if pending_action.get("action_status") not in {"liquidating", "price_ladder_failed"}:
                        answer_callback_query(self.config, callback_query_id, "Assignment action already selected")
                        return
                    pending_action["action_status"] = "price_ladder_selected"
            answer_callback_query(self.config, callback_query_id, "Price ladder selected")
            edit_telegram_message_text(
                self.config,
                chat_id=chat_id,
                message_id=message_id,
                text=replace_summary_prompt(message_text, "🪜 Liquidating with price ladder..."),
                parse_mode="HTML",
                reply_markup=EMPTY_INLINE_KEYBOARD,
            )
            self._execute_assignment_action(callback.strategy_id, strategy, "price_ladder", callback.assignment_token)
        elif callback.action_type == "cancel":
            if lock is None:
                pending_actions.pop(callback.assignment_token, None)
            else:
                with lock:
                    pending_actions.pop(callback.assignment_token, None)
            answer_callback_query(self.config, callback_query_id, "Liquidation canceled")
            edit_telegram_message_text(
                self.config,
                chat_id=chat_id,
                message_id=message_id,
                text=replace_summary_prompt(message_text, "❌ Liquidation canceled."),
                parse_mode="HTML",
                reply_markup=EMPTY_INLINE_KEYBOARD,
            )
        else:
            answer_callback_query(self.config, callback_query_id, "Assignment action unavailable")

    ####################################################################################################
    # Polling
    ####################################################################################################

    def _poll_loop(self) -> None:
        while not self._shutdown_event.is_set():
            updates = get_telegram_updates(
                self.config,
                offset=self._offset,
                timeout_seconds=self.poll_timeout_seconds,
            )
            if updates is None:
                logger.warning("Telegram polling failed. If this is HTTP 409 Conflict, stop other getUpdates pollers using the same bot token.")
                self._shutdown_event.wait(self.error_backoff_seconds)
                continue

            for update in updates:
                if self._shutdown_event.is_set():
                    break
                self._offset = int(update["update_id"]) + 1
                self._handle_update(update)

    def _discard_pending_updates(self) -> None:
        if self.config is None:
            return

        updates = get_telegram_updates(self.config, offset=None, timeout_seconds=0)
        if updates is None:
            logger.warning("Telegram startup update drain failed; continuing without discarding pending updates.")
            return
        if not updates:
            return

        self._offset = max(int(update["update_id"]) for update in updates) + 1
        logger.info("Discarded pending Telegram updates on startup: count=%s, next_offset=%s.", len(updates), self._offset)

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
        send_telegram_message(
            self.config,
            "⚠️ Confirm trading engine shutdown?",
            reply_markup=reply_markup,
        )

    def _confirm_shutdown(self, callback_query_id: str, chat_id: str, message_id: int) -> None:
        answer_callback_query(self.config, callback_query_id, "Shutdown confirmed")
        edit_telegram_message_text(self.config, chat_id=chat_id, message_id=message_id, text="Trading engine shutdown confirmed.")
        self._shutdown_process()

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
        send_telegram_message(
            self.config,
            "⚠️ Confirm trading engine restart?",
            reply_markup=reply_markup,
        )

    def _confirm_restart(self, callback_query_id: str, chat_id: str, message_id: int) -> None:
        answer_callback_query(self.config, callback_query_id, "Restart confirmed")
        edit_telegram_message_text(self.config, chat_id=chat_id, message_id=message_id, text="Trading engine restart confirmed. Restarting now...")
        self._restart_process()

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
            send_telegram_message(self.config, unavailable_reason or "Short put strategy unavailable.")
            return

        token = secrets.token_urlsafe(8)
        expires_at = pd.Timestamp.now() + pd.Timedelta(seconds=SHORT_PUT_CONFIRMATION_TIMEOUT_SECONDS)
        strategy_ids = [strategy_id for strategy_id, _ in matches]
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
            send_telegram_message(
                self.config,
                "Which strategy ID would you want to execute short put?",
                reply_markup=reply_markup,
            )
            return

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Confirm", "callback_data": f"shortput:confirm:{token}"},
                    {"text": "❌ Cancel", "callback_data": f"shortput:cancel:{token}"},
                ]
            ]
        }
        send_telegram_message(
            self.config,
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
            args=(strategy_id, shortput_action),
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

    def _find_shortput_actions(self) -> tuple[list[tuple[str, Callable[[], None]]], str | None]:
        if self.engine is None:
            return [], "Trading engine unavailable."

        strategies = getattr(self.engine, "strategy", {})
        if not isinstance(strategies, dict) or not strategies:
            return [], "Short put strategy unavailable."

        matches: list[tuple[str, Callable[[], None]]] = []
        for strategy_id, strategy in strategies.items():
            get_actions = getattr(strategy, "get_strategy_actions", None)
            if not callable(get_actions):
                continue
            actions = get_actions()
            if not isinstance(actions, dict):
                continue
            action = actions.get(SHORT_PUT_ACTION_ID)
            if callable(action):
                matches.append((strategy_id, action))

        if not matches:
            return [], "Short put strategy unavailable."
        return matches, None

    def _resolve_shortput_action(self, strategy_id: str) -> Callable[[], None] | None:
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
        return action if callable(action) else None

    def _run_shortput_action(self, strategy_id: str, shortput_action: Callable[[], None]) -> None:
        try:
            shortput_action()
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
            target=self._run_strategy_retry, args=(strategy_id, action_id, retry_action), name=f"{strategy_id}-{action_id}-retry", daemon=True
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

    def _run_strategy_retry(self, strategy_id: str, action_id: str, retry_action) -> None:
        try:
            retry_action()
        except Exception:
            logger.exception("Strategy retry failed: strategy_id=%s, action_id=%s.", strategy_id, action_id)

    def _execute_assignment_action(self, strategy_id: str, strategy: object, method: str, assignment_token: str) -> None:
        assignment_actions = strategy.get_strategy_actions()
        assignment_action = assignment_actions.get("execute_underlying_assignment")
        if not callable(assignment_action):
            logger.error("Assignment action unavailable because execute_underlying_assignment is not registered: strategy_id=%s.", strategy_id)
            return

        assignment_thread = threading.Thread(
            target=self._run_assignment_action,
            args=(strategy_id, method, assignment_token, assignment_action),
            name=f"{strategy_id}-assignment-{method}",
            daemon=True,
        )
        assignment_thread.start()

    def _run_assignment_action(self, strategy_id: str, method: str, assignment_token: str, assignment_action) -> None:
        try:
            assignment_action(method, assignment_token)
        except Exception:
            logger.exception("Assignment action failed: strategy_id=%s, method=%s, assignment_token=%s.", strategy_id, method, assignment_token)

    def _run_restart_recovery_actions(self) -> None:
        strategies = getattr(self.engine, "strategy", {}) if self.engine is not None else {}
        if not isinstance(strategies, dict):
            logger.error("Restart recovery failed because engine strategies are unavailable.")
            self.send_message("🚨 Restart recovery failed. Check logs.")
            return

        matched_strategy_count = 0
        recovery_failed = False
        for strategy_id, strategy in strategies.items():
            get_actions = getattr(strategy, "get_strategy_actions", None)
            if not callable(get_actions):
                continue
            try:
                strategy_actions = get_actions()
            except Exception:
                recovery_failed = True
                logger.exception("Restart recovery failed because strategy actions are unavailable: strategy_id=%s.", strategy_id)
                continue
            if not isinstance(strategy_actions, dict):
                logger.error("Restart recovery skipped strategy because get_strategy_actions did not return a dict: strategy_id=%s.", strategy_id)
                continue

            update_maturing_put_strikes = strategy_actions.get("update_maturing_put_strikes")
            setup_cut_loss_monitor = strategy_actions.get("setup_cut_loss_monitor")
            if not callable(update_maturing_put_strikes) or not callable(setup_cut_loss_monitor):
                continue

            matched_strategy_count += 1
            logger.info("Restart recovery starting: strategy_id=%s, action=update_maturing_put_strikes.", strategy_id)
            try:
                updated = update_maturing_put_strikes()
            except Exception:
                recovery_failed = True
                logger.exception("Restart recovery failed: strategy_id=%s, action=update_maturing_put_strikes.", strategy_id)
            else:
                if updated is False:
                    recovery_failed = True
                    logger.error("Restart recovery failed: strategy_id=%s, action=update_maturing_put_strikes returned False.", strategy_id)
                else:
                    logger.info("Restart recovery completed: strategy_id=%s, action=update_maturing_put_strikes.", strategy_id)

            logger.info("Restart recovery starting: strategy_id=%s, action=setup_cut_loss_monitor.", strategy_id)
            try:
                setup_cut_loss_monitor()
            except Exception:
                recovery_failed = True
                logger.exception("Restart recovery failed: strategy_id=%s, action=setup_cut_loss_monitor.", strategy_id)
            else:
                logger.info("Restart recovery completed: strategy_id=%s, action=setup_cut_loss_monitor.", strategy_id)

        if matched_strategy_count == 0:
            logger.error("Restart recovery failed because no strategy exposes update_maturing_put_strikes and setup_cut_loss_monitor.")
            self.send_message("🚨 Restart recovery failed. Check logs.")
            return

        if recovery_failed:
            self.send_message("🚨 Restart recovery failed. Check logs.")
            return

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

    ####################################################################################################
    # Internal Utilities
    ####################################################################################################

    def _is_allowed_chat(self, chat_id: str) -> bool:
        return self.config is not None and chat_id == self.config.chat_id
