from __future__ import annotations

import secrets
from typing import TYPE_CHECKING
from flask import Flask, abort, render_template, request

from app.telegram_bot import TelegramBotService
from app.utils.logging_utils import configure_logger

logger = configure_logger(__name__)


def create_app(
    telegram_bot_service: TelegramBotService | None = None,
) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/option-watcher")
    def option_watcher() -> str:
        return render_template(
            "loading.html",
            page_title="Option Price Watcher",
            loading_title="Loading Option Watcher",
            loading_paragraph="Fetching the latest positions and market data...",
            frame_title="Option Price Watcher",
            content_url="/option-watcher/content",
        )

    @app.get("/option-watcher/content")
    def option_watcher_content() -> tuple[str, int] | str:
        from app.utils.option_watcher import build_option_watcher_context
        from app.utils.option_watcher import get_watcher_data

        try:
            options, current_bp = get_watcher_data()
        except Exception:
            logger.exception("Unable to load option watcher data.")
            context = build_option_watcher_context()
            context["error_message"] = "Unable to load option data. Confirm that Futu OpenD is running and try again."
            return render_template("option_watcher.html", **context), 503

        return render_template("option_watcher.html", **build_option_watcher_context(options, current_bp))

    @app.post("/telegram/webhook/<path:webhook_path_secret>")
    def telegram_webhook(webhook_path_secret: str) -> tuple[str, int]:
        config = telegram_bot_service.config if telegram_bot_service is not None else None
        if config is None or not secrets.compare_digest(webhook_path_secret.strip("/"), config.webhook_path_secret):
            abort(404)

        secret_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secrets.compare_digest(secret_token, config.webhook_secret_token):
            abort(403)

        update = request.get_json(silent=True)
        if not isinstance(update, dict):
            abort(400)

        telegram_bot_service.handle_webhook_update(update)
        return "", 200

    return app
