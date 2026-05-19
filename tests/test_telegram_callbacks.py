import unittest

from trading.notification.telegram_callbacks import (
    AssignmentCallback,
    StrategyCallback,
    parse_assignment_callback,
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

    def test_parse_assignment_liquidate_callback(self):
        callback = parse_assignment_callback("assignment:short_put_spy:liquidate:abc123")

        self.assertEqual(
            callback,
            AssignmentCallback(
                strategy_id="short_put_spy",
                action_type="liquidate",
                assignment_token="abc123",
            ),
        )

    def test_parse_assignment_cancel_callback(self):
        callback = parse_assignment_callback("assignment:short_put_spy:cancel:abc123")

        self.assertEqual(
            callback,
            AssignmentCallback(
                strategy_id="short_put_spy",
                action_type="cancel",
                assignment_token="abc123",
            ),
        )

    def test_parse_assignment_liquidation_method_callbacks(self):
        market_order_callback = parse_assignment_callback("assignment:short_put_spy:market_order:abc123")
        price_ladder_callback = parse_assignment_callback("assignment:short_put_spy:price_ladder:abc123")

        self.assertEqual(
            market_order_callback,
            AssignmentCallback(
                strategy_id="short_put_spy",
                action_type="market_order",
                assignment_token="abc123",
            ),
        )
        self.assertEqual(
            price_ladder_callback,
            AssignmentCallback(
                strategy_id="short_put_spy",
                action_type="price_ladder",
                assignment_token="abc123",
            ),
        )

    def test_parse_assignment_callback_rejects_invalid_data(self):
        invalid_callbacks = [
            "",
            "strategy:short_put_spy:cancel",
            "assignment::cancel:abc123",
            "assignment:short_put_spy:cancel",
            "assignment:short_put_spy:liquidate:",
            "assignment:short_put_spy:unknown:abc123",
            "assignment:short_put_spy:cancel:abc123:extra",
        ]

        for data in invalid_callbacks:
            with self.subTest(data=data):
                self.assertIsNone(parse_assignment_callback(data))


if __name__ == "__main__":
    unittest.main()
