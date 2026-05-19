import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trading.utils.telegram_utils import (
    TelegramConfig,
    answer_callback_query,
    edit_telegram_message_text,
    get_telegram_config,
    get_telegram_updates,
    send_telegram_message,
    set_telegram_commands,
    set_telegram_commands_menu,
)


class FakeTelegramResponse:
    def __init__(self, body: dict) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.body).encode("utf-8")


class TelegramUtilsTest(unittest.TestCase):
    def write_config(self, content: str) -> str:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        config_path = Path(temp_dir.name) / ".config"
        config_path.write_text(content)
        return str(config_path)

    def test_get_telegram_config_reads_config_file(self):
        config_path = self.write_config(
            """
[telegram]
bot_token = token123
chat_id = chat456
enabled = yes
"""
        )

        config = get_telegram_config(config_path)

        self.assertEqual(config.bot_token, "token123")
        self.assertEqual(config.chat_id, "chat456")
        self.assertTrue(config.enabled)

    def make_config(self, enabled: bool = True) -> TelegramConfig:
        return TelegramConfig(bot_token="token123", chat_id="chat456", enabled=enabled)

    def test_send_telegram_message_does_not_check_enabled_flag(self):
        config = self.make_config(enabled=False)

        with patch(
            "trading.utils.telegram_utils.urllib.request.urlopen",
            return_value=FakeTelegramResponse({"ok": True, "result": {"message_id": 11}}),
        ) as urlopen:
            sent = send_telegram_message(config, "hello")

        self.assertEqual(sent, (True, 11))
        urlopen.assert_called_once()

    def test_send_telegram_message_returns_true_when_api_returns_ok(self):
        config = self.make_config()

        with patch(
            "trading.utils.telegram_utils.urllib.request.urlopen",
            return_value=FakeTelegramResponse({"ok": True, "result": {"message_id": 11}}),
        ) as urlopen:
            sent = send_telegram_message(config, "hello")

        self.assertEqual(sent, (True, 11))
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertIn("/bottoken123/sendMessage", request.full_url)
        self.assertEqual(payload, {"chat_id": "chat456", "text": "hello"})
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 3)

    def test_send_telegram_message_includes_reply_markup(self):
        config = self.make_config()
        reply_markup = {"inline_keyboard": [[{"text": "Approve", "callback_data": "approve"}]]}

        with patch(
            "trading.utils.telegram_utils.urllib.request.urlopen",
            return_value=FakeTelegramResponse({"ok": True, "result": {"message_id": 11}}),
        ) as urlopen:
            sent = send_telegram_message(config, "choose", reply_markup=reply_markup)

        self.assertEqual(sent, (True, 11))
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["reply_markup"], reply_markup)

    def test_send_telegram_message_includes_parse_mode(self):
        config = self.make_config()

        with patch(
            "trading.utils.telegram_utils.urllib.request.urlopen",
            return_value=FakeTelegramResponse({"ok": True, "result": {"message_id": 11}}),
        ) as urlopen:
            sent = send_telegram_message(config, "<b>hello</b>", parse_mode="HTML")

        self.assertEqual(sent, (True, 11))
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["parse_mode"], "HTML")

    def test_send_telegram_message_returns_false_when_api_returns_error(self):
        config = self.make_config()

        with patch(
            "trading.utils.telegram_utils.urllib.request.urlopen",
            return_value=FakeTelegramResponse({"ok": False, "description": "Bad Request"}),
        ):
            sent = send_telegram_message(config, "hello")

        self.assertEqual(sent, (False, None))

    def test_send_telegram_message_returns_false_when_message_id_missing(self):
        config = self.make_config()

        with patch("trading.utils.telegram_utils.urllib.request.urlopen", return_value=FakeTelegramResponse({"ok": True, "result": {}})):
            sent = send_telegram_message(config, "hello")

        self.assertEqual(sent, (False, None))

    def test_get_telegram_updates_returns_update_list(self):
        config = self.make_config()
        api_response = {"ok": True, "result": [{"update_id": 7, "message": {"text": "/help"}}]}

        with patch("trading.utils.telegram_utils.urllib.request.urlopen", return_value=FakeTelegramResponse(api_response)) as urlopen:
            updates = get_telegram_updates(config, offset=3, timeout_seconds=10)

        self.assertEqual(updates, api_response["result"])
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertIn("/bottoken123/getUpdates", request.full_url)
        self.assertEqual(payload["offset"], 3)
        self.assertEqual(payload["timeout"], 10)
        self.assertEqual(payload["allowed_updates"], ["message", "callback_query"])
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 15)

    def test_answer_callback_query_posts_callback_id_and_text(self):
        config = self.make_config()

        with patch("trading.utils.telegram_utils.urllib.request.urlopen", return_value=FakeTelegramResponse({"ok": True})) as urlopen:
            answered = answer_callback_query(config, "callback-1", "Approved")

        self.assertTrue(answered)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertIn("/bottoken123/answerCallbackQuery", request.full_url)
        self.assertEqual(payload, {"callback_query_id": "callback-1", "text": "Approved"})

    def test_edit_telegram_message_text_includes_parse_mode(self):
        config = self.make_config()

        with patch("trading.utils.telegram_utils.urllib.request.urlopen", return_value=FakeTelegramResponse({"ok": True})) as urlopen:
            edited = edit_telegram_message_text(
                config,
                chat_id="123",
                message_id=10,
                text="<b>approved</b>",
                parse_mode="HTML",
                reply_markup={"inline_keyboard": []},
            )

        self.assertTrue(edited)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertIn("/bottoken123/editMessageText", request.full_url)
        self.assertEqual(payload["chat_id"], "123")
        self.assertEqual(payload["message_id"], 10)
        self.assertEqual(payload["text"], "<b>approved</b>")
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(payload["reply_markup"], {"inline_keyboard": []})

    def test_set_telegram_commands_posts_command_menu(self):
        config = self.make_config()
        commands = [{"command": "help", "description": "Show commands"}]

        with patch("trading.utils.telegram_utils.urllib.request.urlopen", return_value=FakeTelegramResponse({"ok": True})) as urlopen:
            configured = set_telegram_commands(config, commands)

        self.assertTrue(configured)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertIn("/bottoken123/setMyCommands", request.full_url)
        self.assertEqual(payload, {"commands": commands})

    def test_set_telegram_commands_menu_posts_commands_menu_button(self):
        config = self.make_config()

        with patch("trading.utils.telegram_utils.urllib.request.urlopen", return_value=FakeTelegramResponse({"ok": True})) as urlopen:
            configured = set_telegram_commands_menu(config)

        self.assertTrue(configured)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertIn("/bottoken123/setChatMenuButton", request.full_url)
        self.assertEqual(payload, {"chat_id": "chat456", "menu_button": {"type": "commands"}})


if __name__ == "__main__":
    unittest.main()
