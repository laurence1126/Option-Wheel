import unittest

import pandas as pd

from app import create_app


class FlaskAppTest(unittest.TestCase):
    def test_index_page_is_available(self) -> None:
        app = create_app()

        with app.test_client() as client:
            response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Option Wheel", response.data)
        self.assertIn(b"The welcome page", response.data)

    def test_option_watcher_page_renders_loading_animation_without_loading_data(self) -> None:
        load_calls = []
        app = create_app(watcher_data_loader=lambda: load_calls.append(True))

        with app.test_client() as client:
            response = client.get("/option-watcher")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Loading Option Watcher", response.data)
        self.assertIn(b'class="spinner"', response.data)
        self.assertIn(b'data-src="/option-watcher/content"', response.data)
        self.assertEqual(load_calls, [])

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
        app = create_app(watcher_data_loader=lambda: (options, 100_000.0))

        with app.test_client() as client:
            response = client.get("/option-watcher/content")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Option Price Watcher", response.data)
        self.assertIn(b"SPY", response.data)
        self.assertIn(b"Plotly.newPlot", response.data)
        self.assertNotIn(b"<img", response.data)
        self.assertIn(b"background: #1b1b1b", response.data)
        self.assertIn(b"background: #183a2f", response.data)
        self.assertIn(b"background: #3a1f2a", response.data)

    def test_option_watcher_page_handles_empty_position_list(self) -> None:
        app = create_app(watcher_data_loader=lambda: (pd.DataFrame(), 100_000.0))

        with app.test_client() as client:
            response = client.get("/option-watcher/content")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No open short put positions were found.", response.data)

    def test_option_watcher_page_handles_data_source_error(self) -> None:
        def fail_to_load_watcher() -> tuple[pd.DataFrame, float | None]:
            raise RuntimeError("OpenD unavailable")

        app = create_app(watcher_data_loader=fail_to_load_watcher)

        with app.test_client() as client:
            response = client.get("/option-watcher/content")

        self.assertEqual(response.status_code, 503)
        self.assertIn(b"Confirm that Futu OpenD is running", response.data)


if __name__ == "__main__":
    unittest.main()
