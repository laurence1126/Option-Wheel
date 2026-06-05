from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.utils.logging import configure_logger
from app.utils.telegram import (
    TelegramConfig,
    answer_telegram_callback_query,
    delete_telegram_webhook,
    edit_telegram_message_text,
    get_telegram_config,
    is_allowed_telegram_chat,
    send_telegram_document,
    send_telegram_message,
    set_telegram_commands,
    set_telegram_commands_menu,
    set_telegram_webhook,
)

logger = configure_logger(__name__)


class TelegramUpdateHandler(Protocol):
    def handle_message(self, message: dict) -> None:
        pass

    def handle_callback_query(self, callback_query: dict) -> None:
        pass


class TelegramBotService:
    def __init__(self, config_path: str = ".config", handler: TelegramUpdateHandler | None = None) -> None:
        self.config_path = config_path
        self.handler = handler

        self.config: TelegramConfig | None = None
        self.enabled = False
        self._running = False

    ####################################################################################################
    # Public Service API
    ####################################################################################################

    def start(self, startup_message: str | None = None, commands: list[dict[str, str]] | None = None) -> None:
        if self._running:
            return

        self.config = get_telegram_config(self.config_path)
        if not self.config.enabled:
            self.enabled = False
            logger.info("Telegram bot service disabled by config.")
            return

        if commands is not None:
            set_telegram_commands(self.config, commands)
            set_telegram_commands_menu(self.config)

        if not set_telegram_webhook(
            self.config,
            url=self.config.webhook_url,
            secret_token=self.config.webhook_secret_token,
            drop_pending_updates=True,
        ):
            raise RuntimeError("Telegram webhook registration failed.")

        if startup_message is not None:
            send_telegram_message(self.config, startup_message)

        self.enabled = True
        self._running = True
        logger.info("Telegram bot service started with webhook: url=%s.", self.config.webhook_url)

    def shutdown(self) -> None:
        if self.config is not None and self.enabled:
            delete_telegram_webhook(self.config, drop_pending_updates=True)
        self._running = False
        self.enabled = False
        logger.info("Telegram bot service stopped.")

    def send_message(self, text: str, reply_markup: dict | None = None, parse_mode: str | None = None) -> bool:
        sent, _ = self.send_message_with_id(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return sent

    def send_message_with_id(self, text: str, reply_markup: dict | None = None, parse_mode: str | None = None) -> tuple[bool, int | None]:
        if not self.enabled or self.config is None:
            logger.warning("Telegram message unavailable because Telegram bot service is disabled.")
            return False, None

        if reply_markup is None and parse_mode is None:
            return send_telegram_message(self.config, text)
        if parse_mode is None:
            return send_telegram_message(self.config, text, reply_markup=reply_markup)
        return send_telegram_message(self.config, text, reply_markup=reply_markup, parse_mode=parse_mode)

    def send_document(self, file_path: str | Path, caption: str | None = None) -> bool:
        if not self.enabled or self.config is None:
            logger.warning("Telegram document unavailable because Telegram bot service is disabled.")
            return False

        return send_telegram_document(self.config, file_path, caption=caption)

    def answer_callback_query(self, callback_query_id: str, text: str) -> bool:
        if not self.enabled or self.config is None:
            logger.warning("Telegram callback answer unavailable because Telegram bot service is disabled.")
            return False

        return answer_telegram_callback_query(self.config, callback_query_id, text)

    def edit_message_text(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> bool:
        if not self.enabled or self.config is None:
            logger.warning("Telegram message edit unavailable because Telegram bot service is disabled.")
            return False

        return edit_telegram_message_text(
            self.config,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )

    def handle_webhook_update(self, update: dict) -> None:
        if not self.enabled or self.config is None:
            logger.warning("Telegram webhook update ignored because Telegram bot service is disabled.")
            return
        if self.handler is None:
            logger.warning("Telegram webhook update ignored because no handler is attached.")
            return

        if "message" in update:
            message = update["message"]
            if isinstance(message, dict) and is_allowed_telegram_chat(self.config, message.get("chat", {}).get("id")):
                self.handler.handle_message(message)
            return

        if "callback_query" in update:
            callback_query = update["callback_query"]
            if not isinstance(callback_query, dict):
                return
            message = callback_query.get("message", {})
            chat = message.get("chat", {}) if isinstance(message, dict) else {}
            if is_allowed_telegram_chat(self.config, chat.get("id")):
                self.handler.handle_callback_query(callback_query)
