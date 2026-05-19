import threading
import time
import unittest
import datetime as dt
from unittest.mock import patch

import pandas as pd

from trading.notification.telegram_bot import RESTART_ENV_VAR, TelegramBotService
from trading.notification.telegram_consts import BOT_COMMANDS, HELP_TEXT
from trading.notification.telegram_summary import (
    build_assignment_summary,
    build_cut_loss_summary,
    build_execution_result_summary,
    build_sell_put_summary,
    replace_summary_prompt,
)
from trading.utils.telegram_utils import TelegramConfig


class FakeStrategy:
    def __init__(self) -> None:
        self.execute_short_put_calls = 0
        self.lock = threading.RLock()
        self._pending_assignment_actions = {
            "token-1": {
                "order_id": "assignment-1",
                "code": "US.SPY",
                "qty": 100,
                "price": 723.0,
                "matched_strike": 723.0,
            }
        }
        self.assignment_calls = []

    def execute_short_put_strategy(self) -> None:
        self.execute_short_put_calls += 1

    def get_strategy_actions(self):
        return {
            "execute_short_put": self.execute_short_put_strategy,
            "execute_short_put_strategy": self.execute_short_put_strategy,
            "execute_underlying_assignment": self.execute_underlying_assignment,
        }

    def execute_underlying_assignment(self, method: str, assignment_token: str) -> None:
        self.assignment_calls.append({"method": method, "assignment_token": assignment_token})


class FakeEngine:
    def __init__(self) -> None:
        self._running = True
        self._closed = False
        self._started_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=65)
        self.strategy = {"short_put": FakeStrategy()}

    def close(self) -> None:
        self._closed = True
        self._running = False


class FakeExecutionResult:
    def __init__(
        self,
        target_qty: int,
        filled_qty: int,
        order_id: str,
        execution_status: str,
        message: str = "",
    ) -> None:
        self.target_qty = target_qty
        self.filled_qty = filled_qty
        self.order_id = order_id
        self.execution_status = execution_status
        self.message = message


class TelegramBotServiceTest(unittest.TestCase):
    def make_service(self) -> TelegramBotService:
        return TelegramBotService(config_path=".test_config", poll_timeout_seconds=0, error_backoff_seconds=0)

    def wait_for_pending_approval(self, service: TelegramBotService, approval_id: str) -> None:
        deadline = time.time() + 1
        while time.time() < deadline:
            if approval_id in service._pending_approvals:
                return
            time.sleep(0.01)
        self.fail(f"Timed out waiting for pending approval {approval_id}")

    def test_start_registers_commands_menu_and_starts_polling_when_enabled(self):
        service = self.make_service()
        config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)

        with (
            patch("trading.notification.telegram_bot.get_telegram_config", return_value=config),
            patch("trading.notification.telegram_bot.set_telegram_commands", return_value=True) as set_commands,
            patch("trading.notification.telegram_bot.set_telegram_commands_menu", return_value=True) as set_menu,
            patch("trading.notification.telegram_bot.send_telegram_message", return_value=True),
            patch("trading.notification.telegram_bot.get_telegram_updates", return_value=[]),
            patch.dict("trading.notification.telegram_bot.os.environ", {}, clear=True),
        ):
            service.start(FakeEngine())
            self.assertTrue(service.enabled)
            self.assertTrue(service._running)
            self.assertIsNotNone(service._poll_thread)
            set_commands.assert_called_once()
            self.assertIs(set_commands.call_args.args[0], config)
            self.assertEqual(
                [command["command"] for command in set_commands.call_args.args[1]],
                ["status", "shortput", "restart", "shutdown", "help", "start"],
            )
            set_menu.assert_called_once_with(config)
            service.shutdown()

    def test_command_consts_include_shortput(self):
        self.assertIn("shortput", [command["command"] for command in BOT_COMMANDS])
        self.assertIn("/shortput - Run the short put strategy", HELP_TEXT)

    def test_start_sends_restarted_message_after_exec_restart(self):
        service = self.make_service()
        config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)

        with (
            patch("trading.notification.telegram_bot.get_telegram_config", return_value=config),
            patch("trading.notification.telegram_bot.set_telegram_commands", return_value=True),
            patch("trading.notification.telegram_bot.set_telegram_commands_menu", return_value=True),
            patch("trading.notification.telegram_bot.send_telegram_message", return_value=True) as send_message,
            patch("trading.notification.telegram_bot.get_telegram_updates", return_value=[]),
            patch.dict("trading.notification.telegram_bot.os.environ", {RESTART_ENV_VAR: "1"}, clear=True),
        ):
            service.start(FakeEngine())
            service.shutdown()

        self.assertEqual(send_message.call_args_list[0].args[1], "Trading engine restart complete 🎉")

    def test_start_discards_pending_updates_before_polling(self):
        service = self.make_service()
        config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        update_calls = 0

        def get_updates(*_, **__):
            nonlocal update_calls
            update_calls += 1
            if update_calls == 1:
                return [{"update_id": 10}, {"update_id": 12}]
            return []

        with (
            patch("trading.notification.telegram_bot.get_telegram_config", return_value=config),
            patch("trading.notification.telegram_bot.set_telegram_commands", return_value=True),
            patch("trading.notification.telegram_bot.set_telegram_commands_menu", return_value=True),
            patch("trading.notification.telegram_bot.send_telegram_message", return_value=True),
            patch("trading.notification.telegram_bot.get_telegram_updates", side_effect=get_updates) as get_updates_mock,
        ):
            service.start(FakeEngine())
            service.shutdown()

        self.assertEqual(service._offset, 13)
        self.assertEqual(get_updates_mock.call_args_list[0].kwargs["timeout_seconds"], 0)

    def test_start_disabled_config_does_not_start_polling(self):
        service = self.make_service()
        config = TelegramConfig(bot_token="token", chat_id="123", enabled=False)

        with patch("trading.notification.telegram_bot.get_telegram_config", return_value=config):
            service.start(FakeEngine())

        self.assertFalse(service.enabled)
        self.assertFalse(service._running)
        self.assertIsNone(service._poll_thread)

    def test_status_command_only_responds_to_configured_chat(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()

        with patch("trading.notification.telegram_bot.send_telegram_message", return_value=True) as send_message:
            service._handle_message({"chat": {"id": "999"}, "text": "/status"})
            service._handle_message({"chat": {"id": "123"}, "text": "/status"})

        self.assertEqual(send_message.call_count, 1)
        self.assertIs(send_message.call_args.args[0], service.config)
        self.assertIn("Trading engine connected", send_message.call_args.args[1])
        self.assertIn("Duration: 00:01:", send_message.call_args.args[1])
        self.assertIn("short_put: FakeStrategy", send_message.call_args.args[1])

    def test_shutdown_command_requests_inline_confirmation_without_closing_engine(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()

        with patch("trading.notification.telegram_bot.send_telegram_message", return_value=True) as send_message:
            service._handle_message({"chat": {"id": "123"}, "text": "/shutdown"})

        self.assertFalse(service.engine._closed)
        self.assertEqual(send_message.call_count, 1)
        self.assertEqual(send_message.call_args.args[1], "⚠️ Confirm trading engine shutdown?")
        reply_markup = send_message.call_args.kwargs["reply_markup"]
        self.assertEqual(reply_markup["inline_keyboard"][0][0]["callback_data"], "shutdown:confirm")
        self.assertEqual(reply_markup["inline_keyboard"][0][1]["callback_data"], "shutdown:cancel")

    def test_shutdown_confirm_callback_closes_engine_and_exits_process(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.os._exit") as exit_process,
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "shutdown:confirm",
                    "message": {"message_id": 10, "chat": {"id": "123"}},
                }
            )

        self.assertTrue(service.engine._closed)
        answer.assert_called_once_with(service.config, "callback-1", "Shutdown confirmed")
        self.assertEqual(edit_message.call_args.kwargs["text"], "Trading engine shutdown confirmed.")
        exit_process.assert_called_once_with(0)

    def test_shutdown_cancel_callback_does_not_close_engine(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True),
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True),
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "shutdown:cancel",
                    "message": {"message_id": 10, "chat": {"id": "123"}},
                }
            )

        self.assertFalse(service.engine._closed)

    def test_restart_command_requests_inline_confirmation_without_closing_engine(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()

        with patch("trading.notification.telegram_bot.send_telegram_message", return_value=True) as send_message:
            service._handle_message({"chat": {"id": "123"}, "text": "/restart"})

        self.assertFalse(service.engine._closed)
        self.assertEqual(send_message.call_count, 1)
        self.assertEqual(send_message.call_args.args[1], "⚠️ Confirm trading engine restart?")
        reply_markup = send_message.call_args.kwargs["reply_markup"]
        self.assertEqual(reply_markup["inline_keyboard"][0][0]["callback_data"], "restart:confirm")
        self.assertEqual(reply_markup["inline_keyboard"][0][1]["callback_data"], "restart:cancel")

    def test_restart_confirm_callback_closes_engine_and_execs_current_command(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.os.execv") as execv,
            patch("trading.notification.telegram_bot.sys.executable", "python"),
            patch("trading.notification.telegram_bot.sys.argv", ["run.py", "--live"]),
            patch.dict("trading.notification.telegram_bot.os.environ", {}, clear=True),
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "restart:confirm",
                    "message": {"message_id": 10, "chat": {"id": "123"}},
                }
            )

        self.assertTrue(service.engine._closed)
        answer.assert_called_once_with(service.config, "callback-1", "Restart confirmed")
        self.assertEqual(edit_message.call_args.kwargs["text"], "Trading engine restart confirmed. Restarting now...")
        self.assertEqual(execv.call_args.args[1], ["python", "run.py", "--live"])
        execv.assert_called_once_with("python", ["python", "run.py", "--live"])

    def test_restart_cancel_callback_does_not_restart(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.os.execv") as execv,
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "restart:cancel",
                    "message": {"message_id": 10, "chat": {"id": "123"}},
                }
            )

        self.assertFalse(service.engine._closed)
        answer.assert_called_once_with(service.config, "callback-1", "Restart cancelled")
        self.assertEqual(edit_message.call_args.kwargs["text"], "Restart cancelled.")
        execv.assert_not_called()

    def test_shortput_command_requests_inline_confirmation(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()

        with (
            patch("trading.notification.telegram_bot.secrets.token_urlsafe", return_value="short-token"),
            patch("trading.notification.telegram_bot.send_telegram_message", return_value=True) as send_message,
        ):
            service._handle_message({"chat": {"id": "123"}, "text": "/shortput"})

        self.assertIn("short-token", service._pending_shortput_confirmations)
        send_message.assert_called_once()
        self.assertIs(send_message.call_args.args[0], service.config)
        self.assertEqual(send_message.call_args.args[1], "Confirm short put strategy execution?")
        reply_markup = send_message.call_args.kwargs["reply_markup"]
        self.assertEqual(reply_markup["inline_keyboard"][0][0]["callback_data"], "shortput:confirm:short-token")
        self.assertEqual(reply_markup["inline_keyboard"][0][1]["callback_data"], "shortput:cancel:short-token")

    def test_shortput_command_ignores_disallowed_chat(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()

        with patch("trading.notification.telegram_bot.send_telegram_message", return_value=True) as send_message:
            service._handle_message({"chat": {"id": "999"}, "text": "/shortput"})

        send_message.assert_not_called()
        self.assertEqual(service._pending_shortput_confirmations, {})

    def test_shortput_cancel_callback_does_not_execute(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()
        service._pending_shortput_confirmations["short-token"] = pd.Timestamp.now() + pd.Timedelta(seconds=60)

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.threading.Thread") as shortput_thread,
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "shortput:cancel:short-token",
                    "message": {"message_id": 10, "chat": {"id": "123"}},
                }
            )

        self.assertEqual(service.engine.strategy["short_put"].execute_short_put_calls, 0)
        self.assertEqual(service._pending_shortput_confirmations, {})
        answer.assert_called_once_with(service.config, "callback-1", "Short put execution cancelled")
        self.assertEqual(edit_message.call_args.kwargs["text"], "Short put execution cancelled.")
        self.assertEqual(edit_message.call_args.kwargs["reply_markup"], {"inline_keyboard": []})
        shortput_thread.assert_not_called()

    def test_shortput_confirm_callback_starts_one_background_strategy_run(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()
        service._pending_shortput_confirmations["short-token"] = pd.Timestamp.now() + pd.Timedelta(seconds=60)
        started_threads = []

        class ImmediateThread:
            def __init__(self, target, args=(), name=None, daemon=None):
                self.target = target
                self.args = args
                self.name = name
                self.daemon = daemon
                started_threads.append(self)

            def start(self):
                self.target(*self.args)

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.threading.Thread", ImmediateThread),
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "shortput:confirm:short-token",
                    "message": {"message_id": 10, "chat": {"id": "123"}},
                }
            )

        self.assertEqual(service.engine.strategy["short_put"].execute_short_put_calls, 1)
        self.assertEqual(len(started_threads), 1)
        self.assertEqual(started_threads[0].name, "short_put-shortput-command")
        self.assertTrue(started_threads[0].daemon)
        self.assertFalse(service._shortput_running)
        self.assertEqual(service._pending_shortput_confirmations, {})
        answer.assert_called_once_with(service.config, "callback-1", "Short put confirmed")
        self.assertEqual(edit_message.call_args.kwargs["text"], "Short put execution confirmed. Starting now...")
        self.assertEqual(edit_message.call_args.kwargs["reply_markup"], {"inline_keyboard": []})

    def test_shortput_expired_confirm_does_not_execute(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()
        service._pending_shortput_confirmations["short-token"] = pd.Timestamp.now() - pd.Timedelta(seconds=1)

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.threading.Thread") as shortput_thread,
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "shortput:confirm:short-token",
                    "message": {"message_id": 10, "chat": {"id": "123"}},
                }
            )

        self.assertEqual(service.engine.strategy["short_put"].execute_short_put_calls, 0)
        self.assertEqual(service._pending_shortput_confirmations, {})
        answer.assert_called_once_with(service.config, "callback-1", "Short put confirmation expired")
        self.assertEqual(edit_message.call_args.kwargs["text"], "Short put confirmation expired.")
        self.assertEqual(edit_message.call_args.kwargs["reply_markup"], {"inline_keyboard": []})
        shortput_thread.assert_not_called()

    def test_shortput_confirm_rejects_duplicate_while_run_is_active(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()
        service._shortput_running = True
        service._pending_shortput_confirmations["short-token"] = pd.Timestamp.now() + pd.Timedelta(seconds=60)

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.threading.Thread") as shortput_thread,
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "shortput:confirm:short-token",
                    "message": {"message_id": 10, "chat": {"id": "123"}},
                }
            )

        self.assertEqual(service.engine.strategy["short_put"].execute_short_put_calls, 0)
        answer.assert_called_once_with(service.config, "callback-1", "Short put execution already running")
        self.assertEqual(edit_message.call_args.kwargs["text"], "Short put execution already running.")
        self.assertEqual(edit_message.call_args.kwargs["reply_markup"], {"inline_keyboard": []})
        shortput_thread.assert_not_called()

    def test_shortput_confirm_rejects_missing_engine(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = None
        service._pending_shortput_confirmations["short-token"] = pd.Timestamp.now() + pd.Timedelta(seconds=60)

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.threading.Thread") as shortput_thread,
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "shortput:confirm:short-token",
                    "message": {"message_id": 10, "chat": {"id": "123"}},
                }
            )

        answer.assert_called_once_with(service.config, "callback-1", "Trading engine unavailable.")
        self.assertEqual(edit_message.call_args.kwargs["text"], "Trading engine unavailable.")
        shortput_thread.assert_not_called()

    def test_shortput_confirm_rejects_missing_strategy_action(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()
        service.engine.strategy = {"short_put": object()}
        service._pending_shortput_confirmations["short-token"] = pd.Timestamp.now() + pd.Timedelta(seconds=60)

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.threading.Thread") as shortput_thread,
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "shortput:confirm:short-token",
                    "message": {"message_id": 10, "chat": {"id": "123"}},
                }
            )

        answer.assert_called_once_with(service.config, "callback-1", "Short put strategy unavailable.")
        self.assertEqual(edit_message.call_args.kwargs["text"], "Short put strategy unavailable.")
        shortput_thread.assert_not_called()

    def test_shortput_confirm_rejects_multiple_matching_strategies(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()
        service.engine.strategy["second_short_put"] = FakeStrategy()
        service._pending_shortput_confirmations["short-token"] = pd.Timestamp.now() + pd.Timedelta(seconds=60)

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.threading.Thread") as shortput_thread,
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "shortput:confirm:short-token",
                    "message": {"message_id": 10, "chat": {"id": "123"}},
                }
            )

        answer.assert_called_once_with(service.config, "callback-1", "Multiple short put strategies are registered.")
        self.assertEqual(edit_message.call_args.kwargs["text"], "Multiple short put strategies are registered.")
        shortput_thread.assert_not_called()

    def test_short_put_retry_callback_removes_keyboard_and_retries_in_thread(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()

        class ImmediateThread:
            def __init__(self, target, args=(), name=None, daemon=None):
                self.target = target
                self.args = args
                self.name = name
                self.daemon = daemon

            def start(self):
                self.target(*self.args)

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.threading.Thread", ImmediateThread),
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "strategy:short_put:retry:execute_short_put",
                    "message": {"message_id": 10, "chat": {"id": "123"}, "text": "SHORT PUT RESULT - FAILURE"},
                }
            )

        self.assertEqual(service.engine.strategy["short_put"].execute_short_put_calls, 1)
        answer.assert_called_once_with(service.config, "callback-1", "Retrying strategy...")
        self.assertEqual(edit_message.call_args.kwargs["text"], "SHORT PUT RESULT - FAILURE")
        self.assertEqual(edit_message.call_args.kwargs["reply_markup"], {"inline_keyboard": []})

    def test_short_put_cancel_callback_removes_keyboard_without_retry(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()

        class ImmediateThread:
            def __init__(self, target, args=(), name=None, daemon=None):
                self.target = target
                self.args = args
                self.name = name
                self.daemon = daemon

            def start(self):
                self.target(*self.args)

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.threading.Thread", ImmediateThread),
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "strategy:short_put:cancel",
                    "message": {"message_id": 10, "chat": {"id": "123"}, "text": "SHORT PUT RESULT - FAILURE"},
                }
            )

        self.assertEqual(service.engine.strategy["short_put"].execute_short_put_calls, 0)
        answer.assert_called_once_with(service.config, "callback-1", "Retry cancelled")
        self.assertEqual(edit_message.call_args.kwargs["text"], "SHORT PUT RESULT - FAILURE")
        self.assertEqual(edit_message.call_args.kwargs["reply_markup"], {"inline_keyboard": []})

    def test_short_put_retry_callback_ignores_disallowed_chat(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.threading.Thread") as retry_thread,
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "strategy:short_put:retry:execute_short_put",
                    "message": {"message_id": 10, "chat": {"id": "999"}, "text": "SHORT PUT RESULT - FAILURE"},
                }
            )

        self.assertEqual(service.engine.strategy["short_put"].execute_short_put_calls, 0)
        answer.assert_not_called()
        edit_message.assert_not_called()
        retry_thread.assert_not_called()

    def test_short_put_retry_callback_answers_when_strategy_unavailable(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()
        service.engine.strategy = {}

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.threading.Thread") as retry_thread,
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "strategy:short_put:retry:execute_short_put",
                    "message": {"message_id": 10, "chat": {"id": "123"}, "text": "SHORT PUT RESULT - FAILURE"},
                }
            )

        answer.assert_called_once_with(service.config, "callback-1", "Strategy unavailable")
        edit_message.assert_not_called()
        retry_thread.assert_not_called()

    def test_strategy_retry_callback_answers_when_retry_action_unavailable(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.threading.Thread") as retry_thread,
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "strategy:short_put:retry:missing",
                    "message": {"message_id": 10, "chat": {"id": "123"}, "text": "SHORT PUT RESULT - FAILURE"},
                }
            )

        answer.assert_called_once_with(service.config, "callback-1", "Retry unavailable")
        edit_message.assert_not_called()
        retry_thread.assert_not_called()

    def test_assignment_liquidate_callback_replaces_keyboard_with_method_choices(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()
        service.engine.strategy["short_put"]._pending_assignment_actions["token-1"]["action_status"] = "liquidating"
        summary = build_assignment_summary(
            code="US.SPY",
            side="BUY",
            price=723.0,
            qty=100,
            matched_strike=723.0,
            market_state="AFTER_HOURS_BEGIN",
            detected_at="2026-05-17 16:00:00",
        )

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "assignment:short_put:liquidate:token-1",
                    "message": {"message_id": 10, "chat": {"id": "123"}, "text": summary},
                }
            )

        answer.assert_called_once_with(service.config, "callback-1", "Choose liquidation method")
        self.assertEqual(edit_message.call_args.kwargs["text"], replace_summary_prompt(summary, "🚬 Choose liquidation method."))
        self.assertEqual(edit_message.call_args.kwargs["parse_mode"], "HTML")
        self.assertEqual(
            edit_message.call_args.kwargs["reply_markup"],
            {
                "inline_keyboard": [
                    [
                        {"text": "📈 Market Order", "callback_data": "assignment:short_put:market_order:token-1"},
                        {"text": "🪜 Price Ladder", "callback_data": "assignment:short_put:price_ladder:token-1"},
                    ]
                ]
            },
        )
        self.assertEqual(service.engine.strategy["short_put"]._pending_assignment_actions["token-1"]["action_status"], "liquidating")

    def test_assignment_market_order_callback_updates_message_prompt_and_removes_keyboard(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()
        service.engine.strategy["short_put"]._pending_assignment_actions["token-1"]["action_status"] = "liquidating"
        summary = build_assignment_summary(
            code="US.SPY",
            side="BUY",
            price=723.0,
            qty=100,
            matched_strike=723.0,
            market_state="AFTER_HOURS_BEGIN",
            detected_at="2026-05-17 16:00:00",
            final_line="🚬 Choose liquidation method.",
        )

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "assignment:short_put:market_order:token-1",
                    "message": {"message_id": 10, "chat": {"id": "123"}, "text": summary},
                }
            )

        answer.assert_called_once_with(service.config, "callback-1", "Market order selected")
        self.assertEqual(edit_message.call_args.kwargs["text"], replace_summary_prompt(summary, "📈 Liquidating with market order..."))
        self.assertEqual(edit_message.call_args.kwargs["parse_mode"], "HTML")
        self.assertEqual(edit_message.call_args.kwargs["reply_markup"], {"inline_keyboard": []})
        self.assertEqual(service.engine.strategy["short_put"]._pending_assignment_actions["token-1"]["action_status"], "market_order_selected")
        self.assertEqual(service.engine.strategy["short_put"].assignment_calls, [{"method": "market_order", "assignment_token": "token-1"}])

    def test_assignment_market_order_callback_does_not_reset_running_action(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()
        service.engine.strategy["short_put"]._pending_assignment_actions["token-1"]["action_status"] = "market_order_executing"

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.threading.Thread") as assignment_thread,
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "assignment:short_put:market_order:token-1",
                    "message": {"message_id": 10, "chat": {"id": "123"}, "text": "assignment"},
                }
            )

        answer.assert_called_once_with(service.config, "callback-1", "Assignment action already selected")
        edit_message.assert_not_called()
        assignment_thread.assert_not_called()
        self.assertEqual(service.engine.strategy["short_put"]._pending_assignment_actions["token-1"]["action_status"], "market_order_executing")

    def test_assignment_market_order_callback_retries_failed_action(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()
        service.engine.strategy["short_put"]._pending_assignment_actions["token-1"]["action_status"] = "market_order_failed"

        class ImmediateThread:
            def __init__(self, target, args=(), name=None, daemon=None):
                self.target = target
                self.args = args
                self.name = name
                self.daemon = daemon

            def start(self):
                self.target(*self.args)

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.threading.Thread", ImmediateThread),
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "assignment:short_put:market_order:token-1",
                    "message": {"message_id": 10, "chat": {"id": "123"}, "text": "ASSIGNMENT LIQUIDATION RESULT - FAILURE"},
                }
            )

        answer.assert_called_once_with(service.config, "callback-1", "Market order selected")
        self.assertEqual(edit_message.call_args.kwargs["reply_markup"], {"inline_keyboard": []})
        self.assertEqual(service.engine.strategy["short_put"].assignment_calls, [{"method": "market_order", "assignment_token": "token-1"}])

    def test_assignment_expired_callback_removes_token_without_execution(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()
        service.engine.strategy["short_put"]._pending_assignment_actions["token-1"]["expires_at"] = pd.Timestamp.now() - pd.Timedelta(seconds=1)

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.threading.Thread") as assignment_thread,
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "assignment:short_put:market_order:token-1",
                    "message": {"message_id": 10, "chat": {"id": "123"}, "text": "assignment"},
                }
            )

        answer.assert_called_once_with(service.config, "callback-1", "Assignment action expired")
        edit_message.assert_not_called()
        assignment_thread.assert_not_called()
        self.assertNotIn("token-1", service.engine.strategy["short_put"]._pending_assignment_actions)
        self.assertEqual(service.engine.strategy["short_put"].assignment_calls, [])

    def test_assignment_price_ladder_callback_updates_message_prompt_and_removes_keyboard(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()
        service.engine.strategy["short_put"]._pending_assignment_actions["token-1"]["action_status"] = "liquidating"
        summary = build_assignment_summary(
            code="US.SPY",
            side="BUY",
            price=723.0,
            qty=100,
            matched_strike=723.0,
            market_state="AFTER_HOURS_BEGIN",
            detected_at="2026-05-17 16:00:00",
            final_line="🚬 Choose liquidation method.",
        )

        class ImmediateThread:
            def __init__(self, target, args=(), name=None, daemon=None):
                self.target = target
                self.args = args
                self.name = name
                self.daemon = daemon

            def start(self):
                self.target(*self.args)

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
            patch("trading.notification.telegram_bot.threading.Thread", ImmediateThread),
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "assignment:short_put:price_ladder:token-1",
                    "message": {"message_id": 10, "chat": {"id": "123"}, "text": summary},
                }
            )

        answer.assert_called_once_with(service.config, "callback-1", "Price ladder selected")
        self.assertEqual(edit_message.call_args.kwargs["text"], replace_summary_prompt(summary, "🪜 Liquidating with price ladder..."))
        self.assertEqual(edit_message.call_args.kwargs["parse_mode"], "HTML")
        self.assertEqual(edit_message.call_args.kwargs["reply_markup"], {"inline_keyboard": []})
        self.assertEqual(service.engine.strategy["short_put"]._pending_assignment_actions["token-1"]["action_status"], "price_ladder_selected")
        self.assertEqual(service.engine.strategy["short_put"].assignment_calls, [{"method": "price_ladder", "assignment_token": "token-1"}])

    def test_assignment_cancel_callback_updates_message_prompt_and_removes_pending_action(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True
        service.engine = FakeEngine()
        summary = build_assignment_summary(
            code="US.SPY",
            side="BUY",
            price=723.0,
            qty=100,
            matched_strike=723.0,
            market_state="AFTER_HOURS_BEGIN",
            detected_at="2026-05-17 16:00:00",
        )

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "assignment:short_put:cancel:token-1",
                    "message": {"message_id": 10, "chat": {"id": "123"}, "text": summary},
                }
            )

        answer.assert_called_once_with(service.config, "callback-1", "Liquidation canceled")
        self.assertEqual(edit_message.call_args.kwargs["text"], replace_summary_prompt(summary, "❌ Liquidation canceled."))
        self.assertEqual(edit_message.call_args.kwargs["parse_mode"], "HTML")
        self.assertEqual(edit_message.call_args.kwargs["reply_markup"], {"inline_keyboard": []})
        self.assertNotIn("token-1", service.engine.strategy["short_put"]._pending_assignment_actions)

    def test_approve_callback_resolves_pending_approval_true(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True

        with (
            patch("trading.notification.telegram_bot.secrets.token_urlsafe", return_value="abc"),
            patch("trading.notification.telegram_bot.send_telegram_message", return_value=(True, 10)),
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True),
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
        ):
            result_holder = {}
            summary = "<b>Trade</b>\nDetails stay visible\n\n🫡 Approve this trade?"
            thread = threading.Thread(target=lambda: result_holder.setdefault("result", service.request_trade_approval(summary, 2)))
            thread.start()
            self.wait_for_pending_approval(service, "abc")
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "approve:abc",
                    "message": {"message_id": 10, "chat": {"id": "123"}},
                }
            )
            thread.join(timeout=1)

        self.assertEqual(result_holder["result"], True)
        self.assertEqual(edit_message.call_args.kwargs["text"], "<b>Trade</b>\nDetails stay visible\n\n✅ Trade approved.")
        self.assertEqual(edit_message.call_args.kwargs["parse_mode"], "HTML")
        self.assertEqual(edit_message.call_args.kwargs["reply_markup"], {"inline_keyboard": []})

    def test_reject_callback_resolves_pending_approval_false(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True

        with (
            patch("trading.notification.telegram_bot.secrets.token_urlsafe", return_value="abc"),
            patch("trading.notification.telegram_bot.send_telegram_message", return_value=(True, 10)),
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True),
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
        ):
            result_holder = {}
            summary = "<b>Cut Loss</b>\nOrder details stay visible\n\n🫡 Approve this cut-loss order?"
            thread = threading.Thread(target=lambda: result_holder.setdefault("result", service.request_trade_approval(summary, 2)))
            thread.start()
            self.wait_for_pending_approval(service, "abc")
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "reject:abc",
                    "message": {"message_id": 10, "chat": {"id": "123"}},
                }
            )
            thread.join(timeout=1)

        self.assertEqual(result_holder["result"], False)
        self.assertEqual(edit_message.call_args.kwargs["text"], "<b>Cut Loss</b>\nOrder details stay visible\n\n❌ Trade rejected.")
        self.assertEqual(edit_message.call_args.kwargs["parse_mode"], "HTML")
        self.assertEqual(edit_message.call_args.kwargs["reply_markup"], {"inline_keyboard": []})

    def test_approval_timeout_returns_false(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True

        with (
            patch("trading.notification.telegram_bot.secrets.token_urlsafe", return_value="abc"),
            patch("trading.notification.telegram_bot.send_telegram_message", return_value=(True, 10)) as send_message,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
        ):
            summary = "<b>Trade</b>\nOrder details stay visible\n\n🫡 Approve this trade?"
            approved = service.request_trade_approval(summary, 0)

        self.assertFalse(approved)
        self.assertEqual(service._pending_approvals, {})
        self.assertEqual(send_message.call_count, 1)
        self.assertEqual(edit_message.call_args.kwargs["chat_id"], "123")
        self.assertEqual(edit_message.call_args.kwargs["message_id"], 10)
        self.assertEqual(edit_message.call_args.kwargs["text"], "<b>Trade</b>\nOrder details stay visible\n\n⚠️ Trade approval timed out.")
        self.assertEqual(edit_message.call_args.kwargs["parse_mode"], "HTML")
        self.assertEqual(edit_message.call_args.kwargs["reply_markup"], {"inline_keyboard": []})

    def test_expired_approval_callback_only_answers_popup(self):
        service = self.make_service()
        service.config = TelegramConfig(bot_token="token", chat_id="123", enabled=True)
        service.enabled = True

        with (
            patch("trading.notification.telegram_bot.answer_callback_query", return_value=True) as answer,
            patch("trading.notification.telegram_bot.edit_telegram_message_text", return_value=True) as edit_message,
        ):
            service._handle_callback_query(
                {
                    "id": "callback-1",
                    "data": "approve:expired",
                    "message": {"message_id": 10, "chat": {"id": "123"}},
                }
            )

        answer.assert_called_once_with(service.config, "callback-1", "Approval expired")
        edit_message.assert_not_called()

    def test_stop_resolves_pending_approval_false(self):
        service = self.make_service()
        event = threading.Event()
        service._pending_approvals["abc"] = type("Approval", (), {"event": event, "summary": "summary", "result": None})()

        service.shutdown()

        self.assertTrue(event.is_set())
        self.assertEqual(service._pending_approvals, {})

    def test_build_trade_approval_summary_formats_readable_html(self):
        summary = build_sell_put_summary(
            code="US.SPY260527P723000",
            name="SPY <test>",
            snapshot={
                "implied_volatility": 31.234,
                "delta": -0.12345,
                "volume": 1000,
                "bid_volume": 100,
                "bid_price": 2.26,
                "ask_price": 2.40,
                "ask_volume": 120,
            },
            total_qty=80,
            child_qtys=[30, 30, 20],
            prices=[2.33, 2.26],
        )

        self.assertIn("<b>💸 SHORT PUT SUMMARY</b>", summary)
        self.assertIn("<b>📜 Contract</b>", summary)
        self.assertIn("Name: SPY &lt;test&gt;", summary)
        self.assertIn("<b>📷 Snapshot</b>", summary)
        self.assertIn("Implied Vol: 31.23%", summary)
        self.assertIn("Delta: -0.1235", summary)
        self.assertIn("Daily Volume: 1000", summary)
        self.assertIn("TOB: 100 @ 2.26 | 2.40 @ 120", summary)
        self.assertNotIn("Side:", summary)
        self.assertIn("Child Quantity: 30 + 30 + 20", summary)
        self.assertIn("Price Ladder: 2.33 -&gt; 2.26", summary)
        self.assertNotIn("<pre>", summary)
        self.assertNotIn("<code>", summary)

    def test_build_cut_loss_approval_summary_formats_readable_html(self):
        summary = build_cut_loss_summary(
            code="US.SPY260527P723000",
            name="SPY <test>",
            total_qty=12,
            child_qtys=[10, 2],
            average_price=1.0,
            stop_price=1.5,
            order_book={
                "bid_price": 1.48,
                "bid_volume": 10,
                "ask_price": 1.51,
                "ask_volume": 12,
            },
            mid_signal_price=1.5,
            prices=[1.5, 1.51],
        )

        self.assertIn("<b>🚨 CUT LOSS SUMMARY</b>", summary)
        self.assertIn("Name: SPY &lt;test&gt;", summary)
        self.assertIn("Average Price: 1.00", summary)
        self.assertIn("Stop Price: 1.50", summary)
        self.assertIn("TOB: 10 @ 1.48 | 1.51 @ 12", summary)
        self.assertIn("Mid Signal: 1.50", summary)
        self.assertIn("Child Quantity: 10 + 2", summary)
        self.assertIn("Price Ladder: 1.50 -&gt; 1.51", summary)

    def test_build_execution_result_summary_formats_child_results(self):
        summary = build_execution_result_summary(
            title="SHORT PUT RESULT",
            code="US.SPY260527P723000",
            name="SPY <test>",
            requested_qty=30,
            target_mid=2.33,
            filled_qty=25,
            child_results=[
                FakeExecutionResult(target_qty=20, filled_qty=20, order_id="1", execution_status="success"),
                FakeExecutionResult(target_qty=10, filled_qty=5, order_id="2", execution_status="fail", message="partial <fill>"),
            ],
        )

        self.assertIn("<b>🚨 SHORT PUT RESULT - FAILURE</b>", summary)
        self.assertIn("Name: SPY &lt;test&gt;", summary)
        self.assertIn("Requested Quantity: <b>30</b>", summary)
        self.assertIn("Target Mid: 2.33", summary)
        self.assertIn("Filled Quantity: <b>25</b>", summary)
        self.assertIn("🟢 <b>Child 1</b>: 20 / 20 filled", summary)
        self.assertIn("🔴 <b>Child 2</b>: 5 / 10 filled - partial &lt;fill&gt;", summary)


if __name__ == "__main__":
    unittest.main()
