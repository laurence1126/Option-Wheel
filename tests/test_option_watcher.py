import unittest
from unittest import mock

import pandas as pd

from app.utils.option_watcher import _build_delta_chart_html
from app.utils.option_watcher import get_close_prices
from app.utils.option_watcher import get_watcher_data


class OptionWatcherTest(unittest.TestCase):
    def test_close_prices_are_cached_until_cleared(self) -> None:
        get_close_prices.cache_clear()
        self.addCleanup(get_close_prices.cache_clear)
        ticker = mock.Mock()
        ticker.history.return_value = pd.DataFrame({"Close": [100.0, 101.0]})

        with mock.patch("app.option_watcher.yf.Ticker", return_value=ticker) as ticker_factory:
            self.assertEqual(get_close_prices("SPY"), (100.0, 101.0))
            self.assertEqual(get_close_prices("SPY"), (100.0, 101.0))

        ticker_factory.assert_called_once_with("SPY")

    def test_watcher_data_clears_close_price_cache_after_refresh(self) -> None:
        trade_context = mock.Mock()
        trade_context.position_list_query.return_value = (0, pd.DataFrame())
        trade_context.accinfo_query.return_value = (0, pd.DataFrame())
        quote_context = mock.Mock()

        with (
            mock.patch("trading.utils.futu_utils.create_trade_context", return_value=trade_context),
            mock.patch("trading.utils.futu_utils.create_quote_context", return_value=quote_context),
            mock.patch("app.option_watcher.get_close_prices") as close_prices,
        ):
            get_watcher_data()

        close_prices.cache_clear.assert_called_once_with()

    def test_delta_chart_uses_contract_weighted_average(self) -> None:
        options = pd.DataFrame(
            [
                {"TICKER": "SPY", "EXPIRATION": "2026-06-19", "QTY": -1, "_DELTA": -0.1, "_IMPL_VOL": 20.0},
                {"TICKER": "SPY", "EXPIRATION": "2026-06-19", "QTY": -3, "_DELTA": -0.3, "_IMPL_VOL": 40.0},
                {"TICKER": "QQQ", "EXPIRATION": "2026-06-19", "QTY": -2, "_DELTA": -0.5, "_IMPL_VOL": 60.0},
            ]
        ).set_index("TICKER")
        figure = mock.Mock()
        figure.to_html.return_value = "<div>chart</div>"

        with mock.patch("app.option_watcher.go.Figure", return_value=figure):
            chart_html = _build_delta_chart_html(options)

        self.assertEqual(chart_html, "<div>chart</div>")
        expiration_trace = figure.add_trace.call_args_list[0].args[0]
        ticker_trace = figure.add_trace.call_args_list[1].args[0]
        expiration_impl_vol_trace = figure.add_trace.call_args_list[2].args[0]
        ticker_impl_vol_trace = figure.add_trace.call_args_list[3].args[0]
        self.assertAlmostEqual(expiration_trace.y.tolist()[0], 100 / 3)
        self.assertEqual(ticker_trace.y.tolist()[0], 50)
        self.assertAlmostEqual(ticker_trace.y.tolist()[1], 25)
        self.assertAlmostEqual(expiration_impl_vol_trace.y.tolist()[0], 130 / 3)
        self.assertEqual(ticker_impl_vol_trace.y.tolist()[0], 60)
        self.assertEqual(ticker_impl_vol_trace.y.tolist()[1], 35)
        self.assertFalse(expiration_trace.visible)
        self.assertIsNone(ticker_trace.visible)
        self.assertFalse(expiration_impl_vol_trace.visible)
        self.assertFalse(ticker_impl_vol_trace.visible)
        self.assertNotIn("# of contracts", expiration_trace.hovertemplate)
        self.assertNotIn("# of contracts", ticker_trace.hovertemplate)

    def test_delta_chart_is_omitted_when_quotes_have_no_delta(self) -> None:
        options = pd.DataFrame([{"TICKER": "SPY", "EXPIRATION": "2026-06-19", "QTY": -1, "_DELTA": None, "_IMPL_VOL": 20.0}]).set_index("TICKER")

        self.assertIsNone(_build_delta_chart_html(options))


if __name__ == "__main__":
    unittest.main()
