import unittest
from unittest.mock import patch

import pandas as pd

from app import create_app
from app.utils.telegram_utils import TelegramConfig


class FakeTelegramBot:
    def __init__(self) -> None:
        self.config = TelegramConfig(
            bot_token="token",
            chat_id="123",
            enabled=True,
            webhook_base_url="https://option-wheel.ubuntu-nuc.com:8443",
            webhook_path_secret="telegram-secret-path",
            webhook_secret_token="telegram-secret-token",
        )
        self.updates = []

    def handle_webhook_update(self, update: dict) -> None:
        self.updates.append(update)


class FlaskAppTest(unittest.TestCase):
    def test_index_page_is_available(self) -> None:
        app = create_app()

        with app.test_client() as client:
            response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Option Wheel", response.data)
        self.assertIn(b"The welcome page", response.data)

    def test_option_watcher_page_renders_loading_animation_without_loading_data(self) -> None:
        app = create_app()

        with patch("app.utils.option_watcher.get_watcher_data") as get_watcher_data, app.test_client() as client:
            response = client.get("/option-watcher")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Loading Option Watcher", response.data)
        self.assertIn(b'class="spinner"', response.data)
        self.assertIn(b'data-src="/option-watcher/content"', response.data)
        get_watcher_data.assert_not_called()

    def test_option_watcher_content_renders_interactive_plotly_chart(self) -> None:
        options = pd.DataFrame(
            [
                {
                    "TICKER": "SPY",
                    "QTY": -1,
                    "PREMIUM": 125.0,
                    "EXPIRATION": "2026-06-19",
                    "DTE": 19,
                    "STRIKE": 700.0,
                    "CLOSE": 710.0,
                    "PCT EXEC": 98.59,
                    "_PREV CLOSE": 705.0,
                    "_NOTIONAL": 70_000.0,
                }
            ]
        ).set_index("TICKER")
        app = create_app()

        with patch("app.utils.option_watcher.get_watcher_data", return_value=(options, 100_000.0)), app.test_client() as client:
            response = client.get("/option-watcher/content")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Option Price Watcher", response.data)
        self.assertIn(b"SPY", response.data)
        self.assertIn(b"Plotly.newPlot", response.data)
        self.assertNotIn(b"<img", response.data)
        self.assertIn(b"background: #1b1b1b", response.data)
        self.assertIn(b"background: #183a2f", response.data)
        self.assertIn(b"background: #3a1f2a", response.data)
        self.assertIn(b'class="sortable-table"', response.data)
        self.assertIn(b'data-sort-column="0"', response.data)
        self.assertIn(b'data-sort-value="98.59"', response.data)
        self.assertIn(b'aria-sort="descending"', response.data)

    def test_option_watcher_page_handles_empty_position_list(self) -> None:
        app = create_app()

        with patch("app.utils.option_watcher.get_watcher_data", return_value=(pd.DataFrame(), 100_000.0)), app.test_client() as client:
            response = client.get("/option-watcher/content")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No open short put positions were found.", response.data)

    def test_option_watcher_page_handles_data_source_error(self) -> None:
        def fail_to_load_watcher() -> tuple[pd.DataFrame, float | None]:
            raise RuntimeError("OpenD unavailable")

        app = create_app()

        with patch("app.utils.option_watcher.get_watcher_data", side_effect=fail_to_load_watcher), app.test_client() as client:
            response = client.get("/option-watcher/content")

        self.assertEqual(response.status_code, 503)
        self.assertIn(b"Confirm that Futu OpenD is running", response.data)

    def test_telegram_webhook_dispatches_valid_update(self) -> None:
        telegram = FakeTelegramBot()
        app = create_app(telegram_bot_service=telegram)
        update = {"update_id": 1, "message": {"chat": {"id": "123"}, "text": "/status"}}

        with app.test_client() as client:
            response = client.post(
                "/telegram/webhook/telegram-secret-path",
                json=update,
                headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(telegram.updates, [update])

    def test_telegram_webhook_rejects_invalid_secret_header(self) -> None:
        telegram = FakeTelegramBot()
        app = create_app(telegram_bot_service=telegram)

        with app.test_client() as client:
            response = client.post(
                "/telegram/webhook/telegram-secret-path",
                json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(telegram.updates, [])

    def test_telegram_webhook_rejects_invalid_path(self) -> None:
        telegram = FakeTelegramBot()
        app = create_app(telegram_bot_service=telegram)

        with app.test_client() as client:
            response = client.post(
                "/telegram/webhook/wrong-path",
                json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret-token"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(telegram.updates, [])

    def test_telegram_webhook_rejects_malformed_json(self) -> None:
        telegram = FakeTelegramBot()
        app = create_app(telegram_bot_service=telegram)

        with app.test_client() as client:
            response = client.post(
                "/telegram/webhook/telegram-secret-path",
                data="{bad json",
                content_type="application/json",
                headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret-token"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(telegram.updates, [])


if __name__ == "__main__":
    unittest.main()
