import unittest
from unittest.mock import Mock, patch

from app.telegram_bot import TelegramBotService
from app.utils.telegram_utils import TelegramConfig


class TelegramBotServiceTest(unittest.TestCase):
    def make_config(self, enabled: bool = True) -> TelegramConfig:
        return TelegramConfig(
            bot_token="token",
            chat_id="123",
            webhook_base_url="https://telegram-option-wheel.ubuntu-nuc.com/telegram/webhook",
            webhook_path_secret="test-webhook-path",
            webhook_secret_token="test-webhook-secret",
            enabled=enabled,
        )

    def test_start_registers_commands_menu_webhook_and_startup_message(self) -> None:
        config = self.make_config()
        service = TelegramBotService(config_path=".test_config")
        commands = [{"command": "status", "description": "Show status"}]

        with (
            patch("app.telegram_bot.get_telegram_config", return_value=config),
            patch("app.telegram_bot.set_telegram_commands", return_value=True) as set_commands,
            patch("app.telegram_bot.set_telegram_commands_menu", return_value=True) as set_menu,
            patch("app.telegram_bot.set_telegram_webhook", return_value=True) as set_webhook,
            patch("app.telegram_bot.send_telegram_message", return_value=(True, 1)) as send_message,
        ):
            service.start(startup_message="Bot started", commands=commands)

        self.assertTrue(service.enabled)
        self.assertTrue(service._running)
        set_commands.assert_called_once_with(config, commands)
        set_menu.assert_called_once_with(config)
        set_webhook.assert_called_once_with(
            config,
            url=config.webhook_url,
            secret_token=config.webhook_secret_token,
            drop_pending_updates=True,
        )
        send_message.assert_called_once_with(config, "Bot started")

    def test_shutdown_deletes_webhook(self) -> None:
        config = self.make_config()
        service = TelegramBotService()
        service.config = config
        service.enabled = True
        service._running = True

        with patch("app.telegram_bot.delete_telegram_webhook", return_value=True) as delete_webhook:
            service.shutdown()

        delete_webhook.assert_called_once_with(config, drop_pending_updates=True)
        self.assertFalse(service.enabled)
        self.assertFalse(service._running)

    def test_webhook_update_dispatches_allowed_message_and_callback(self) -> None:
        handler = Mock()
        service = TelegramBotService(handler=handler)
        service.config = self.make_config()
        service.enabled = True
        service._running = True
        message = {"chat": {"id": "123"}, "text": "/status"}
        callback_query = {"id": "callback-1", "message": {"chat": {"id": "123"}, "message_id": 1}, "data": "ok"}

        service.handle_webhook_update({"message": message})
        service.handle_webhook_update({"callback_query": callback_query})

        handler.handle_message.assert_called_once_with(message)
        handler.handle_callback_query.assert_called_once_with(callback_query)

    def test_webhook_update_ignores_disallowed_chat(self) -> None:
        handler = Mock()
        service = TelegramBotService(handler=handler)
        service.config = self.make_config()
        service.enabled = True
        service._running = True

        service.handle_webhook_update({"message": {"chat": {"id": "999"}, "text": "/status"}})

        handler.handle_message.assert_not_called()
        handler.handle_callback_query.assert_not_called()

    def test_disabled_service_ignores_updates_and_send_attempts(self) -> None:
        handler = Mock()
        service = TelegramBotService(handler=handler)
        service.config = self.make_config(enabled=False)

        with patch("app.telegram_bot.send_telegram_message") as send_message:
            sent = service.send_message("hello")
            service.handle_webhook_update({"message": {"chat": {"id": "123"}, "text": "/status"}})

        self.assertFalse(sent)
        send_message.assert_not_called()
        handler.handle_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
