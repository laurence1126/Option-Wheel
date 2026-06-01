import unittest
from unittest import mock

import pandas as pd

from app.option_watcher import _build_delta_chart_html


class OptionWatcherTest(unittest.TestCase):
    def test_delta_chart_uses_contract_weighted_average(self) -> None:
        options = pd.DataFrame(
            [
                {"TICKER": "SPY", "EXPIRATION": "2026-06-19", "QTY": -1, "_DELTA": -0.1},
                {"TICKER": "SPY", "EXPIRATION": "2026-06-19", "QTY": -3, "_DELTA": -0.3},
                {"TICKER": "QQQ", "EXPIRATION": "2026-06-19", "QTY": -2, "_DELTA": -0.5},
            ]
        ).set_index("TICKER")
        figure = mock.Mock()
        figure.to_html.return_value = "<div>chart</div>"

        with mock.patch("app.option_watcher.go.Figure", return_value=figure):
            chart_html = _build_delta_chart_html(options)

        self.assertEqual(chart_html, "<div>chart</div>")
        expiration_trace = figure.add_trace.call_args_list[0].args[0]
        ticker_trace = figure.add_trace.call_args_list[1].args[0]
        self.assertAlmostEqual(expiration_trace.y.tolist()[0], 100 / 3)
        self.assertEqual(ticker_trace.y.tolist()[0], 50)
        self.assertAlmostEqual(ticker_trace.y.tolist()[1], 25)
        self.assertNotIn("# of contracts", expiration_trace.hovertemplate)
        self.assertNotIn("# of contracts", ticker_trace.hovertemplate)

    def test_delta_chart_is_omitted_when_quotes_have_no_delta(self) -> None:
        options = pd.DataFrame([{"TICKER": "SPY", "EXPIRATION": "2026-06-19", "QTY": -1, "_DELTA": None}]).set_index("TICKER")

        self.assertIsNone(_build_delta_chart_html(options))


if __name__ == "__main__":
    unittest.main()
