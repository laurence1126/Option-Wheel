import unittest

from trading.notification.telegram_callbacks import (
    StrategyCallback,
    parse_strategy_callback,
)


class TelegramCallbackParsingTest(unittest.TestCase):
    def test_parse_strategy_retry_callback(self):
        callback = parse_strategy_callback("strategy:short_put_spy:retry:execute_short_put_strategy")

        self.assertEqual(
            callback,
            StrategyCallback(
                strategy_id="short_put_spy",
                action_type="retry",
                action_id="execute_short_put_strategy",
            ),
        )

    def test_parse_strategy_cancel_callback(self):
        callback = parse_strategy_callback("strategy:short_put_spy:cancel")

        self.assertEqual(
            callback,
            StrategyCallback(
                strategy_id="short_put_spy",
                action_type="cancel",
            ),
        )

    def test_parse_strategy_callback_rejects_invalid_data(self):
        invalid_callbacks = [
            "",
            "assignment:short_put_spy:liquidate:token",
            "strategy::cancel",
            "strategy:short_put_spy:retry",
            "strategy:short_put_spy:cancel:extra",
            "strategy:short_put_spy:unknown",
        ]

        for data in invalid_callbacks:
            with self.subTest(data=data):
                self.assertIsNone(parse_strategy_callback(data))


if __name__ == "__main__":
    unittest.main()
