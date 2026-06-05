from __future__ import annotations

import configparser
import json
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app.utils.logging import configure_logger

logger = configure_logger(__name__)


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str
    webhook_base_url: str
    webhook_path_secret: str
    webhook_secret_token: str
    enabled: bool

    @property
    def webhook_url(self) -> str:
        return f"{self.webhook_base_url.rstrip('/')}/{self.webhook_path_secret.strip('/')}"


def get_telegram_config(config_path: str = ".config") -> TelegramConfig:
    parser = configparser.ConfigParser()
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}. Create it with a [telegram] section.")

    parser.read(config_path)
    try:
        telegram_section = parser["telegram"]
        bot_token = telegram_section["bot_token"].strip()
        chat_id = telegram_section["chat_id"].strip()
        webhook_base_url = telegram_section["webhook_base_url"].strip()
        webhook_path_secret = telegram_section["webhook_path_secret"].strip().strip("/")
        webhook_secret_token = telegram_section["webhook_secret_token"].strip()
        enabled_raw = telegram_section["enabled"].strip()
    except KeyError as exc:
        raise KeyError(
            f"Missing [telegram] bot_token, chat_id, enabled, webhook_base_url, webhook_path_secret, or webhook_secret_token in {path}"
        ) from exc

    if not bot_token:
        raise ValueError(f"Missing non-empty [telegram] bot_token in {path}")
    if not chat_id:
        raise ValueError(f"Missing non-empty [telegram] chat_id in {path}")
    if not webhook_base_url:
        raise ValueError(f"Missing non-empty [telegram] webhook_base_url in {path}")
    if not webhook_path_secret:
        raise ValueError(f"Missing non-empty [telegram] webhook_path_secret in {path}")
    if not webhook_secret_token:
        raise ValueError(f"Missing non-empty [telegram] webhook_secret_token in {path}")

    return TelegramConfig(
        bot_token=bot_token,
        chat_id=chat_id,
        webhook_base_url=webhook_base_url,
        webhook_path_secret=webhook_path_secret,
        webhook_secret_token=webhook_secret_token,
        enabled=_parse_enabled(enabled_raw),
    )


def send_telegram_message(
    config: TelegramConfig,
    text: str,
    reply_markup: dict | None = None,
    parse_mode: str | None = None,
) -> tuple[bool, int | None]:
    payload = {"chat_id": config.chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode

    result = _post_telegram_api(config, "sendMessage", payload, timeout_seconds=3)
    if result is None:
        return False, None
    message = result.get("result")
    message_id = message.get("message_id") if isinstance(message, dict) else None
    if message_id is None:
        logger.error("Telegram notification failed: missing message_id in sendMessage response.")
        return False, None
    logger.info("Telegram notification sent.")
    return True, int(message_id)


def send_telegram_document(config: TelegramConfig, file_path: str | Path, caption: str | None = None) -> bool:
    path = Path(file_path)
    boundary = f"----TelegramBoundary{secrets.token_hex(16)}"
    body = bytearray()

    fields = {"chat_id": config.chat_id}
    if caption is not None:
        fields["caption"] = caption

    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    try:
        file_bytes = path.read_bytes()
    except OSError as exc:
        logger.error("Telegram document upload failed: unable to read %s: %s", path, exc)
        return False

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(f'Content-Disposition: form-data; name="document"; filename="{path.name}"\r\n'.encode("utf-8"))
    body.extend(b"Content-Type: text/plain\r\n\r\n")
    body.extend(file_bytes)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    request = urllib.request.Request(
        url=f"https://api.telegram.org/bot{config.bot_token}/sendDocument",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response_body = response.read().decode("utf-8")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.error("Telegram document upload failed: %s", exc)
        return False

    try:
        result = json.loads(response_body)
    except json.JSONDecodeError:
        logger.error("Telegram document upload failed: invalid JSON response.")
        return False

    if not result.get("ok"):
        logger.error("Telegram document upload failed: %s", result.get("description", "unknown error"))
        return False

    logger.info("Telegram document sent: %s", path)
    return True


def set_telegram_webhook(
    config: TelegramConfig,
    url: str,
    secret_token: str,
    drop_pending_updates: bool = True,
    allowed_updates: list[str] | None = None,
) -> bool:
    payload: dict[str, object] = {
        "url": url,
        "secret_token": secret_token,
        "drop_pending_updates": drop_pending_updates,
        "allowed_updates": allowed_updates or ["message", "callback_query"],
    }
    result = _post_telegram_api(config, "setWebhook", payload, timeout_seconds=5)
    return result is not None


def delete_telegram_webhook(config: TelegramConfig, drop_pending_updates: bool = True) -> bool:
    result = _post_telegram_api(
        config,
        "deleteWebhook",
        {"drop_pending_updates": drop_pending_updates},
        timeout_seconds=5,
    )
    return result is not None


def answer_telegram_callback_query(config: TelegramConfig, callback_query_id: str, text: str) -> bool:
    result = _post_telegram_api(
        config,
        "answerCallbackQuery",
        {"callback_query_id": callback_query_id, "text": text},
        timeout_seconds=5,
    )
    return result is not None


def edit_telegram_message_text(
    config: TelegramConfig,
    chat_id: str | int,
    message_id: int,
    text: str,
    parse_mode: str | None = None,
    reply_markup: dict | None = None,
) -> bool:
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    result = _post_telegram_api(
        config,
        "editMessageText",
        payload,
        timeout_seconds=5,
    )
    return result is not None


def set_telegram_commands(config: TelegramConfig, commands: list[dict[str, str]]) -> bool:
    result = _post_telegram_api(
        config,
        "setMyCommands",
        {"commands": commands},
        timeout_seconds=5,
    )
    return result is not None


def set_telegram_commands_menu(config: TelegramConfig) -> bool:
    result = _post_telegram_api(
        config,
        "setChatMenuButton",
        {"chat_id": config.chat_id, "menu_button": {"type": "commands"}},
        timeout_seconds=5,
    )
    return result is not None


def is_allowed_telegram_chat(config: TelegramConfig | None, chat_id: str | int | None) -> bool:
    return config is not None and str(chat_id) == config.chat_id


def _post_telegram_api(config: TelegramConfig, method: str, payload: dict, timeout_seconds: int) -> dict | None:
    url = f"https://api.telegram.org/bot{config.bot_token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response_body = response.read().decode("utf-8")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.error("Telegram notification failed: %s", exc)
        return None

    try:
        result = json.loads(response_body)
    except json.JSONDecodeError:
        logger.error("Telegram notification failed: invalid JSON response.")
        return None

    if not result.get("ok"):
        logger.error("Telegram notification failed: %s", result.get("description", "unknown error"))
        return None

    return result


def _parse_enabled(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"Invalid telegram_enabled value: {value!r}")
