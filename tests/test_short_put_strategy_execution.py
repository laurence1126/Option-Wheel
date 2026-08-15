import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
from futu import SubType, TrdEnv, TrdSide

from trading.config.trading_config import ShortPutLiveConfig
from trading.trading_engine.execution_engine import ExecutionResult, LimitOrderRequest, PriceLadderPlan, build_price_ladder
from trading.strategies.short_put_strategy.lifecycle.assignment import alert_assignment_at_close
from trading.strategies.short_put_strategy.lifecycle.cut_loss import is_cut_loss_execution_time, setup_cut_loss_monitor
from trading.strategies.short_put_strategy.lifecycle.daily_summary import send_daily_summary
from trading.strategies.short_put_strategy.lifecycle.account_snapshot import capture_account_snapshot
from trading.strategies.short_put_strategy.lifecycle.short_put import (
    _execution_checklist,
    _build_execution_requests,
    execute_short_put_strategy,
)
from trading.strategies.short_put_strategy.strategy_main import ShortPutStrategy
from trading.strategies.short_put_strategy.utils.account_state import update_put_position
from trading.strategies.short_put_strategy.utils.option_parsing import resolve_option_info, resolve_option_name


def make_sell_put_ladder_plan(prices: tuple[float, ...] = (2.33, 2.26)) -> PriceLadderPlan:
    return PriceLadderPlan(
        prices=prices,
        side="sell",
        ref_bid=2.26,
        ref_ask=2.4,
        price_tick=0.01,
    )


class FakeEngine:
    def __init__(self) -> None:
        self.trading_environment = TrdEnv.SIMULATE
        self.margin_account = 100
        self.option_account = 200
        self.execution_call = None
        self.execution_calls = []
        self.execution_results = []
        self.limit_order_execution_calls = []
        self.limit_order_execution_results = []
        self.order_list_result = pd.DataFrame()
        self.order_list_results = []
        self.history_order_result = pd.DataFrame()
        self.history_order_queries = []
        self.open_position_result = pd.DataFrame()
        self.market_state_result = pd.DataFrame([{"market_state": "AFTERNOON"}])
        self.account_info_result = pd.DataFrame([{"cash": 100000.0, "fund_assets": 100000.0, "total_assets": 100000.0}])
        self.stock_quote_result = pd.DataFrame()
        self.market_snapshot_result = None
        self.top_order_book_result = {"code": "US.SPY", "bid_price": 722.95, "bid_volume": 1000, "ask_price": 723.05, "ask_volume": 1200}
        self.subscriptions = []
        self.time_triggers = []
        self.telegram = FakeTelegram()

    def execute_limit_ladder(self, request: LimitOrderRequest, order_wait_seconds: int, cancel_wait_seconds: int, fill_outside_rth: bool = True):
        execution_call = {
            "request": request,
            "order_wait_seconds": order_wait_seconds,
            "cancel_wait_seconds": cancel_wait_seconds,
            "fill_outside_rth": fill_outside_rth,
        }
        self.execution_call = execution_call
        self.execution_calls.append(execution_call)
        if self.execution_results:
            return self.execution_results.pop(0)
        return ExecutionResult(
            code=request.code,
            target_qty=request.qty,
            filled_qty=request.qty,
            order_id="1",
            execution_status="success",
        )

    def execute_limit_order(
        self,
        request: LimitOrderRequest,
        order_wait_seconds: int,
        cancel_wait_seconds: int,
        fill_outside_rth: bool = False,
    ):
        execution_call = {
            "request": request,
            "order_wait_seconds": order_wait_seconds,
            "cancel_wait_seconds": cancel_wait_seconds,
            "fill_outside_rth": fill_outside_rth,
        }
        self.limit_order_execution_calls.append(execution_call)
        if self.limit_order_execution_results:
            return self.limit_order_execution_results.pop(0)
        return ExecutionResult(
            code=request.code,
            target_qty=request.qty,
            filled_qty=request.qty,
            order_id="underlying-sell-1",
            execution_status="success",
        )

    def order_list_query(self, acc_id, order_id="", code="", status_filter_list=None, refresh_cache=True):
        if self.order_list_results:
            return self.order_list_results.pop(0)
        return self.order_list_result

    def history_order_list_query(self, acc_id, code="", start="", end="", status_filter_list=None):
        self.history_order_queries.append(
            {
                "acc_id": acc_id,
                "code": code,
                "start": start,
                "end": end,
                "status_filter_list": status_filter_list,
            }
        )
        return self.history_order_result

    def get_open_position(self, acc_id, code="", refresh_cache=True):
        return self.open_position_result

    def subscribe(self, code_list, subtype_list, subscribe_push=True):
        self.subscriptions.append(
            {
                "code_list": code_list,
                "subtype_list": subtype_list,
                "subscribe_push": subscribe_push,
            }
        )
        return True

    def unsubscribe_all(self):
        return True

    def get_stock_quote(self, code_list):
        return self.stock_quote_result

    def get_market_snapshot(self, code_list):
        return self.market_snapshot_result

    def get_top_order_book(self, code):
        return self.top_order_book_result

    def get_market_state(self, code):
        return self.market_state_result

    def get_account_info(self, acc_id):
        return self.account_info_result

    def process_top_orderbook(self, data):
        if not isinstance(data, dict):
            return None

        code = data.get("code")
        bids = data.get("Bid")
        asks = data.get("Ask")
        if not code or not bids or not asks:
            return None

        bid_price = self._valid_positive_float(bids[0][0])
        ask_price = self._valid_positive_float(asks[0][0])
        bid_volume = self._valid_positive_float(bids[0][1])
        ask_volume = self._valid_positive_float(asks[0][1])
        if bid_price is None or ask_price is None or bid_volume is None or ask_volume is None or ask_price < bid_price:
            return None
        return {
            "code": str(code),
            "bid_price": bid_price,
            "bid_volume": bid_volume,
            "ask_price": ask_price,
            "ask_volume": ask_volume,
        }

    def add_daily_time_trigger(self, name, trigger_time, timezone="America/New_York"):
        self.time_triggers.append({"name": name, "trigger_time": trigger_time, "timezone": timezone})

    @staticmethod
    def _valid_positive_float(value):
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        if pd.isna(result) or result <= 0:
            return None
        return result

    @staticmethod
    def valid_positive_float(value):
        return FakeEngine._valid_positive_float(value)


class FakeTelegram:
    def __init__(self) -> None:
        self.approval_calls = []
        self.approval_result = True
        self.messages = []

    def request_trade_approval(self, summary: str, timeout_seconds: int) -> bool:
        self.approval_calls.append({"summary": summary, "timeout_seconds": timeout_seconds})
        return self.approval_result

    def send_message(self, text: str, reply_markup: dict | None = None, parse_mode: str | None = None) -> bool:
        self.messages.append({"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode})
        return True


class ShortPutStrategyExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cut_loss_time_gate_patcher = patch("trading.strategies.short_put_strategy.strategy_main.is_cut_loss_execution_time", return_value=True)
        self.cut_loss_time_gate = self.cut_loss_time_gate_patcher.start()
        self.addCleanup(self.cut_loss_time_gate_patcher.stop)

    def make_strategy(self) -> tuple[ShortPutStrategy, FakeEngine]:
        config = ShortPutLiveConfig(
            max_contracts_per_trade=30,
            max_order_book_participation=0.5,
            price_ladder_steps=(0.0, 0.5, 1.0),
            order_wait_seconds=7,
            cancel_wait_seconds=9,
        )
        strategy = ShortPutStrategy(config)
        engine = FakeEngine()
        strategy.load_trading_engine(engine)
        return strategy, engine

    def selected_option(self) -> pd.Series:
        return pd.Series(
            {
                "code": "US.SPY260527P723000",
                "name": "SPY 260527 723P",
                "volume": 1000,
                "implied_volatility": 31.234,
                "delta": -0.12345,
                "bid_price": 2.26,
                "ask_price": 2.40,
                "bid_volume": 100,
                "ask_volume": 120,
                "price_spread": 0.01,
            }
        )

    def short_put_position(self, qty=-12, average_cost=1.0) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "code": "US.SPY260527P723000",
                    "stock_name": "SPY260527P723000",
                    "qty": qty,
                    "cost_price": average_cost,
                }
            ]
        )

    def mixed_position_list_for_daily_summary(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "code": "US.SPY260527P723000",
                    "stock_name": "SPY260527P723000",
                    "qty": -2,
                    "cost_price": 1.25,
                    "market_val": -260.0,
                    "pl_val": -10.0,
                },
                {
                    "code": "US.SPY260527C723000",
                    "stock_name": "SPY260527C723000",
                    "qty": -1,
                    "cost_price": 1.75,
                    "market_val": -180.0,
                    "pl_val": -5.0,
                },
                {
                    "code": "US.QQQ260527P500000",
                    "stock_name": "QQQ260527P500000",
                    "qty": -3,
                    "cost_price": 2.0,
                    "market_val": -660.0,
                    "pl_val": -60.0,
                },
                {
                    "code": "US.SPY260527P700000",
                    "stock_name": "SPY260527P700000",
                    "qty": 1,
                    "cost_price": 0.75,
                    "market_val": 80.0,
                    "pl_val": 5.0,
                },
            ]
        )

    def strategy_position_list_for_snapshot(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "code": "US.SPY260527P723000",
                    "stock_name": "SPY260527P723000",
                    "qty": -2,
                    "cost_price": 1.25,
                    "nominal_price": 1.30,
                    "market_val": -260.0,
                    "pl_val": -10.0,
                },
                {
                    "code": "US.SPY260527C723000",
                    "stock_name": "SPY260527C723000",
                    "qty": -1,
                    "cost_price": 1.75,
                    "nominal_price": 1.80,
                    "market_val": -180.0,
                    "pl_val": -5.0,
                },
            ]
        )

    def market_snapshot_for_snapshot(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"code": "US.SPY", "last_price": 722.5},
                {
                    "code": "US.SPY260527P723000",
                    "last_price": 1.30,
                    "option_implied_volatility": 0.31,
                    "option_delta": -0.2,
                    "option_gamma": 0.01,
                    "option_theta": -0.03,
                },
            ]
        )

    def order_history_for_snapshot(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "stock_name": "SPY260527P723000",
                    "code": "US.SPY260527P723000",
                    "order_status": "FILLED_ALL",
                    "trd_side": TrdSide.SELL,
                    "qty": 2,
                    "price": 1.25,
                    "dealt_qty": 2,
                    "dealt_avg_price": 1.24,
                    "updated_time": "2026-08-14 15:59:00",
                    "remark": "",
                },
                {
                    "stock_name": "QQQ260527P500000",
                    "code": "US.QQQ260527P500000",
                    "order_status": "FILLED_ALL",
                    "trd_side": TrdSide.SELL,
                    "qty": 1,
                    "price": 2.0,
                    "dealt_qty": 1,
                    "dealt_avg_price": 1.9,
                    "updated_time": "2026-08-14 15:58:00",
                    "remark": "",
                },
            ]
        )

    def maturing_short_put_position(self, qty=-2, average_cost=1.0, strike=723.0) -> pd.DataFrame:
        expiration = pd.Timestamp.today().strftime("%y%m%d")
        strike_text = f"{int(strike * 1000):06d}"
        return pd.DataFrame(
            [
                {
                    "code": f"US.SPY{expiration}P{strike_text}",
                    "stock_name": f"SPY{expiration}P{strike_text}",
                    "qty": qty,
                    "cost_price": average_cost,
                }
            ]
        )

    def quote_for_short_put(self, price_spread=0.01) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "code": "US.SPY260527P723000",
                    "price_spread": price_spread,
                }
            ]
        )

    def orderbook(self, bid_price: float, ask_price: float, bid_volume: float = 10, ask_volume: float = 10) -> dict:
        return {
            "code": "US.SPY260527P723000",
            "Bid": [(bid_price, bid_volume, 1)],
            "Ask": [(ask_price, ask_volume, 1)],
        }

    def prepare_cut_loss_watch(self, strategy: ShortPutStrategy, engine: FakeEngine, average_cost=1.0) -> None:
        strategy.config.telegram_approval["cut_loss"] = False
        engine.open_position_result = self.short_put_position(average_cost=average_cost)
        engine.stock_quote_result = self.quote_for_short_put()
        setup_cut_loss_monitor(strategy)

    def test_short_put_execution_checklist_returns_limit_order_request(self):
        strategy, _ = self.make_strategy()

        requests = _build_execution_requests(strategy, self.selected_option(), max_short=20)

        self.assertIsNotNone(requests)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.acc_id, 200)
        self.assertEqual(request.code, "US.SPY260527P723000")
        self.assertEqual(request.side, TrdSide.SELL)
        self.assertEqual(request.qty, 20)
        self.assertIsInstance(request.price_ladder_plan, PriceLadderPlan)
        self.assertEqual(request.price_ladder_plan.prices, (2.33, 2.26))
        self.assertEqual(request.price_ladder_plan.side, "sell")
        self.assertEqual(request.price_ladder_plan.ref_bid, 2.26)
        self.assertEqual(request.price_ladder_plan.ref_ask, 2.4)
        self.assertEqual(request.price_ladder_plan.ref_mid, 2.33)
        self.assertIsNone(request.remark)

    def test_setup_time_triggers_adds_cut_loss_prepare_at_920(self):
        strategy, engine = self.make_strategy()

        strategy.setup_time_triggers()

        trigger = next(item for item in engine.time_triggers if item["name"] == "setup_cut_loss_monitor")
        self.assertEqual(str(trigger["trigger_time"]), "09:20:00")

    def test_setup_time_triggers_adds_daily_summary_after_close(self):
        strategy, engine = self.make_strategy()

        strategy.setup_time_triggers()

        trigger = next(item for item in engine.time_triggers if item["name"] == "send_daily_summary")
        self.assertEqual(str(trigger["trigger_time"]), "16:30:00")

    def test_setup_time_triggers_adds_account_snapshot_at_market_close(self):
        strategy, engine = self.make_strategy()

        strategy.setup_time_triggers()

        trigger = next(item for item in engine.time_triggers if item["name"] == "capture_account_snapshot")
        self.assertEqual(str(trigger["trigger_time"]), "16:00:00")

    def test_strategy_actions_include_account_snapshot(self):
        strategy, _ = self.make_strategy()

        actions = strategy.get_strategy_actions()

        self.assertIs(actions["capture_account_snapshot"], capture_account_snapshot)

    def test_restart_actions_refresh_maturing_strikes_before_cut_loss_monitor(self):
        strategy, _ = self.make_strategy()

        restart_actions = strategy.get_restart_actions()

        self.assertEqual(list(restart_actions), ["update_maturing_put_strikes", "setup_cut_loss_monitor"])

    def test_resolve_option_name_handles_simulate_compact_name(self):
        strategy, _ = self.make_strategy()

        option_info = resolve_option_name("SPY260527P723000", TrdEnv.SIMULATE)

        self.assertIsNotNone(option_info)
        self.assertEqual(option_info.ticker, "SPY")
        self.assertEqual(option_info.type, "put")
        self.assertEqual(option_info.strike, 723.0)
        self.assertEqual(option_info.expiration, "2026-05-27")

    def test_resolve_option_name_handles_real_spaced_name(self):
        strategy, engine = self.make_strategy()
        engine.trading_environment = TrdEnv.REAL

        option_info = resolve_option_name("SPY 260527 723P")

        self.assertIsNotNone(option_info)
        self.assertEqual(option_info.ticker, "SPY")
        self.assertEqual(option_info.type, "put")
        self.assertEqual(option_info.strike, 723.0)
        self.assertEqual(option_info.expiration, "2026-05-27")

    def test_short_put_execution_checklist_limits_order_book_participation(self):
        strategy, _ = self.make_strategy()
        selected_option = self.selected_option()
        selected_option["bid_volume"] = 10

        requests = _build_execution_requests(strategy, selected_option, max_short=20)

        self.assertIsNotNone(requests)
        self.assertEqual([request.qty for request in requests], [5, 5, 5, 5])

    def test_short_put_execution_checklist_splits_by_max_contracts_per_trade(self):
        strategy, _ = self.make_strategy()

        requests = _build_execution_requests(strategy, self.selected_option(), max_short=80)

        self.assertIsNotNone(requests)
        self.assertEqual([request.qty for request in requests], [30, 30, 20])
        self.assertTrue(all(request.price_ladder_plan.prices == (2.33, 2.26) for request in requests))

    def test_short_put_execution_checklist_splits_by_participation_cap(self):
        strategy, _ = self.make_strategy()
        strategy.config.max_contracts_per_trade = 100
        selected_option = self.selected_option()
        selected_option["bid_volume"] = 50

        requests = _build_execution_requests(strategy, selected_option, max_short=80)

        self.assertIsNotNone(requests)
        self.assertEqual([request.qty for request in requests], [25, 25, 25, 5])

    def test_short_put_execution_checklist_ignores_missing_max_contracts_cap(self):
        strategy, _ = self.make_strategy()
        strategy.config.max_contracts_per_trade = None

        requests = _build_execution_requests(strategy, self.selected_option(), max_short=80)

        self.assertIsNotNone(requests)
        self.assertEqual([request.qty for request in requests], [50, 30])

    def test_short_put_execution_checklist_uses_smaller_child_cap(self):
        strategy, _ = self.make_strategy()
        strategy.config.max_contracts_per_trade = 30
        selected_option = self.selected_option()
        selected_option["bid_volume"] = 120

        requests = _build_execution_requests(strategy, selected_option, max_short=85)

        self.assertIsNotNone(requests)
        self.assertEqual([request.qty for request in requests], [30, 30, 25])

    def test_short_put_execution_checklist_rejects_zero_participation_qty(self):
        strategy, _ = self.make_strategy()
        selected_option = self.selected_option()
        selected_option["bid_volume"] = 1
        strategy.config.max_order_book_participation = 0.4

        requests = _build_execution_requests(strategy, selected_option, max_short=20)

        self.assertIsNone(requests)

    def test_short_put_execution_checklist_rejects_non_positive_max_short(self):
        strategy, _ = self.make_strategy()

        requests = _build_execution_requests(strategy, self.selected_option(), max_short=0)

        self.assertIsNone(requests)

    def test_buy_price_ladder_moves_from_mid_to_ask(self):
        strategy, _ = self.make_strategy()

        plan = build_price_ladder(
            side="buy",
            code="US.SPY260527P723000",
            bid_price=2.26,
            ask_price=2.40,
            price_tick=0.01,
            steps=strategy.config.price_ladder_steps,
        )

        self.assertEqual(list(plan.prices), [2.33, 2.4])
        self.assertEqual(plan.side, "buy")
        self.assertEqual(plan.ref_bid, 2.26)
        self.assertEqual(plan.ref_ask, 2.4)

    def test_price_ladders_round_directionally_to_tick(self):
        strategy, _ = self.make_strategy()

        sell_plan = build_price_ladder(
            side="sell",
            code="US.TEST",
            bid_price=1.01,
            ask_price=1.04,
            price_tick=0.02,
            steps=strategy.config.price_ladder_steps,
        )
        buy_plan = build_price_ladder(
            side="buy",
            code="US.TEST",
            bid_price=1.01,
            ask_price=1.04,
            price_tick=0.02,
            steps=strategy.config.price_ladder_steps,
        )

        self.assertEqual(list(sell_plan.prices), [1.02, 1.01])
        self.assertEqual(list(buy_plan.prices), [1.04])

    def test_execute_short_put_strategy_delegates_execution_to_engine(self):
        strategy, engine = self.make_strategy()
        strategy.config.telegram_approval["short_put"] = True

        with (
            patch("trading.strategies.short_put_strategy.lifecycle.short_put._execution_checklist", return_value=True),
            patch(
                "trading.strategies.short_put_strategy.lifecycle.short_put.select_short_put", side_effect=lambda strategy_arg: self.selected_option()
            ),
            patch("trading.strategies.short_put_strategy.lifecycle.short_put.get_max_num_to_short", return_value=80),
        ):
            execute_short_put_strategy(strategy)

        self.assertEqual(len(engine.execution_calls), 3)
        requests = [execution_call["request"] for execution_call in engine.execution_calls]
        self.assertEqual([request.qty for request in requests], [30, 30, 20])
        self.assertTrue(all(request.code == "US.SPY260527P723000" for request in requests))
        self.assertTrue(all(isinstance(request.price_ladder_plan, PriceLadderPlan) for request in requests))
        self.assertTrue(all(request.price_ladder_plan.prices == (2.33, 2.26) for request in requests))
        self.assertTrue(all(execution_call["order_wait_seconds"] == 7 for execution_call in engine.execution_calls))
        self.assertTrue(all(execution_call["cancel_wait_seconds"] == 9 for execution_call in engine.execution_calls))
        self.assertEqual(len(engine.telegram.approval_calls), 1)
        self.assertEqual(engine.telegram.approval_calls[0]["timeout_seconds"], strategy.config.telegram_approval_timeout)
        self.assertIn("SHORT PUT SUMMARY", engine.telegram.approval_calls[0]["summary"])
        self.assertIn("Name: SPY 723.00 Put (2026-05-27)", engine.telegram.approval_calls[0]["summary"])
        self.assertIn("Implied Vol: 31.23%", engine.telegram.approval_calls[0]["summary"])
        self.assertIn("Delta: -0.1235", engine.telegram.approval_calls[0]["summary"])
        self.assertIn("Volume: 1000", engine.telegram.approval_calls[0]["summary"])
        self.assertIn("TOB: 100 @ 2.26 | 2.40 @ 120", engine.telegram.approval_calls[0]["summary"])
        self.assertIn("Total Quantity: <b>80</b>", engine.telegram.approval_calls[0]["summary"])

    def test_prepare_cut_loss_monitor_subscribes_short_put_positions_with_push(self):
        strategy, engine = self.make_strategy()
        engine.open_position_result = self.short_put_position(qty=-12, average_cost=1.001)
        engine.stock_quote_result = self.quote_for_short_put(price_spread=0.01)

        setup_cut_loss_monitor(strategy)

        self.assertEqual(len(engine.subscriptions), 1)
        self.assertEqual(engine.subscriptions[0]["code_list"], ["US.SPY260527P723000"])
        self.assertEqual(engine.subscriptions[0]["subtype_list"], [SubType.QUOTE, SubType.ORDER_BOOK])
        self.assertTrue(engine.subscriptions[0]["subscribe_push"])
        watch = strategy._cut_loss_watchlist["US.SPY260527P723000"]
        self.assertEqual(watch.qty, 12)
        self.assertEqual(watch.average_price, 1.001)
        self.assertEqual(watch.stop_price, 1.51)

    def test_resolve_option_code_formats_existing_option_position(self):
        strategy, engine = self.make_strategy()
        engine.open_position_result = self.short_put_position(qty=-12, average_cost=1.0)
        update_put_position(strategy)

        option_name = resolve_option_info(strategy._put_option_position[0])

        self.assertEqual(option_name, "SPY 723.00 Put (2026-05-27)")

    def test_daily_summary_sends_strategy_owned_short_put_positions_only(self):
        strategy, engine = self.make_strategy()
        engine.account_info_result = pd.DataFrame([{"cash": 100000.0, "fund_assets": 100000.0, "total_assets": 99740.0}])
        engine.open_position_result = self.mixed_position_list_for_daily_summary()

        result = send_daily_summary(strategy)

        self.assertTrue(result)
        self.assertEqual(len(engine.telegram.messages), 1)
        message = engine.telegram.messages[0]
        self.assertEqual(message["parse_mode"], "HTML")
        self.assertIn("DAILY STRATEGY SUMMARY", message["text"])
        self.assertIn("Strategy: <b>short_put_spy</b>", message["text"])
        self.assertIn("Total NAV: <b>$99,740.00</b>", message["text"])
        self.assertIn("Total Cash: <b>$100,000.00</b>", message["text"])
        self.assertIn("Strategy Cash: <b>$250.00</b>", message["text"])
        self.assertIn("Strategy MV: <b>$-260.00</b>", message["text"])
        self.assertIn("<b>Strategy Positions</b>", message["text"])
        self.assertIn("SPY 723.00 Put (2026-05-27)", message["text"])
        self.assertIn(" • SPY 723.00 Put (2026-05-27)\n   Qty: -2 | Avg: 1.2500 | PnL: $-10.00", message["text"])
        self.assertIn("PnL: $-10.00", message["text"])
        self.assertNotIn("SPY260527C723000", message["text"])
        self.assertNotIn("QQQ", message["text"])
        self.assertNotIn("700.00 Put", message["text"])

    def test_daily_summary_reports_no_positions_when_strategy_has_none(self):
        strategy, engine = self.make_strategy()
        engine.open_position_result = pd.DataFrame(
            [
                {
                    "code": "US.QQQ260527P500000",
                    "stock_name": "QQQ260527P500000",
                    "qty": -3,
                    "cost_price": 2.0,
                    "market_val": -660.0,
                }
            ]
        )

        result = send_daily_summary(strategy)

        self.assertTrue(result)
        self.assertIn("Strategy Cash: <b>$0.00</b>", engine.telegram.messages[0]["text"])
        self.assertIn("Strategy MV: <b>$0.00</b>", engine.telegram.messages[0]["text"])
        self.assertIn("<b>Strategy Positions</b>\n • N/A", engine.telegram.messages[0]["text"])

    def test_account_snapshot_writes_strategy_json_file(self):
        strategy, engine = self.make_strategy()
        engine.open_position_result = self.strategy_position_list_for_snapshot()
        engine.market_snapshot_result = self.market_snapshot_for_snapshot()
        engine.history_order_result = self.order_history_for_snapshot()

        with tempfile.TemporaryDirectory() as tmpdir:
            result = capture_account_snapshot(strategy, snapshot_root=tmpdir)
            snapshot_date = dt.datetime.now(strategy.config.cut_loss_earliest_time.tzinfo).date().isoformat()
            snapshot_path = Path(tmpdir) / strategy.strategy_id / f"{snapshot_date}.json"
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))

        self.assertTrue(result)
        self.assertEqual(
            list(payload),
            ["datetime", "strategy", "totalNav", "totalCash", "strategyCash", "strategyMV", "position", "orders", "params"],
        )
        self.assertEqual(payload["strategy"], "short_put_spy")
        self.assertEqual(payload["totalNav"], 100000.0)
        self.assertEqual(payload["totalCash"], 100000.0)
        self.assertEqual(payload["strategyCash"], 250.0)
        self.assertEqual(payload["strategyMV"], -260.0)
        self.assertEqual(payload["params"]["underlying"], "US.SPY")
        self.assertEqual(payload["params"]["price_ladder_steps"], [0.0, 0.5, 1.0])
        self.assertEqual(len(payload["position"]), 1)
        self.assertEqual(
            list(payload["position"][0]),
            ["name", "code", "qty", "avgPrice", "mktPrice", "underlyingPrice", "pnl", "iv", "delta", "gamma", "theta"],
        )
        self.assertEqual(payload["position"][0]["name"], "SPY 723.00 Put (2026-05-27)")
        self.assertEqual(payload["position"][0]["code"], "US.SPY260527P723000")
        self.assertEqual(payload["position"][0]["qty"], -2.0)
        self.assertEqual(payload["position"][0]["avgPrice"], 1.25)
        self.assertEqual(payload["position"][0]["mktPrice"], 1.3)
        self.assertEqual(payload["position"][0]["underlyingPrice"], 722.5)
        self.assertEqual(payload["position"][0]["pnl"], -10.0)
        self.assertEqual(payload["position"][0]["iv"], 0.0031)
        self.assertEqual(payload["position"][0]["delta"], -0.2)
        self.assertEqual(payload["position"][0]["gamma"], 0.01)
        self.assertEqual(payload["position"][0]["theta"], -0.03)
        self.assertEqual(len(payload["orders"]), 1)
        self.assertEqual(
            list(payload["orders"][0]),
            ["name", "code", "status", "qty", "limitPrice", "filledQty", "avgPrice", "updateTime"],
        )
        self.assertEqual(payload["orders"][0]["code"], "US.SPY260527P723000")
        self.assertEqual(payload["orders"][0]["qty"], -2.0)
        self.assertEqual(payload["orders"][0]["limitPrice"], 1.25)
        self.assertEqual(payload["orders"][0]["filledQty"], -2.0)
        self.assertEqual(payload["orders"][0]["avgPrice"], 1.24)
        self.assertEqual(payload["orders"][0]["updateTime"], "2026-08-14 15:59:00")
        self.assertEqual(len(engine.history_order_queries), 1)
        self.assertEqual(engine.history_order_queries[0]["acc_id"], strategy.acc_id)

    def test_prepare_cut_loss_monitor_ignores_when_stop_loss_disabled(self):
        strategy, engine = self.make_strategy()
        strategy.config.stop_loss_multiple = None
        engine.open_position_result = self.short_put_position()

        setup_cut_loss_monitor(strategy)

        self.assertEqual(engine.subscriptions, [])
        self.assertEqual(strategy._cut_loss_watchlist, {})

    def test_cut_loss_orderbook_mid_signal_rounds_up_to_trigger(self):
        strategy, engine = self.make_strategy()
        self.prepare_cut_loss_watch(strategy, engine, average_cost=1.0)

        strategy.on_orderbook(self.orderbook(bid_price=1.48, ask_price=1.51))

        self.assertEqual(len(engine.execution_calls), 1)
        request = engine.execution_calls[0]["request"]
        self.assertEqual(request.side, TrdSide.BUY)
        self.assertEqual(request.qty, 12)
        self.assertIsInstance(request.price_ladder_plan, PriceLadderPlan)
        self.assertEqual(request.price_ladder_plan.prices, (1.5, 1.51))
        self.assertEqual(request.price_ladder_plan.side, "buy")
        self.assertEqual(request.remark, "cut_loss")
        self.assertEqual(engine.telegram.approval_calls, [])
        self.assertEqual(len(engine.telegram.messages), 2)
        self.assertIn("CUT LOSS SUMMARY", engine.telegram.messages[0]["text"])
        self.assertTrue(engine.telegram.messages[0]["text"].endswith("Executing the above order..."))
        self.assertEqual(engine.telegram.messages[0]["parse_mode"], "HTML")
        self.assertIn("CUT LOSS RESULT", engine.telegram.messages[1]["text"])
        self.assertIn("Target Mid: 1.50", engine.telegram.messages[1]["text"])
        self.assertIn("Filled Quantity: <b>12</b>", engine.telegram.messages[1]["text"])
        self.assertIsNone(engine.telegram.messages[1]["reply_markup"])
        self.assertEqual(engine.telegram.messages[1]["parse_mode"], "HTML")

    def test_cut_loss_orderbook_skips_before_10_am_et(self):
        strategy, engine = self.make_strategy()
        self.prepare_cut_loss_watch(strategy, engine, average_cost=1.0)
        self.cut_loss_time_gate.return_value = False

        strategy.on_orderbook(self.orderbook(bid_price=1.48, ask_price=1.51))

        self.assertEqual(engine.execution_calls, [])
        watch = strategy._cut_loss_watchlist["US.SPY260527P723000"]
        self.assertFalse(watch.executing)

    def test_cut_loss_time_gate_starts_at_10_am(self):
        cut_loss_earliest_time = dt.time(10, 0)

        self.assertFalse(is_cut_loss_execution_time(cut_loss_earliest_time, dt.time(9, 59, 59)))
        self.assertTrue(is_cut_loss_execution_time(cut_loss_earliest_time, dt.time(10, 0)))

    def test_cut_loss_config_default_time_is_new_york(self):
        strategy, _ = self.make_strategy()

        self.assertEqual(strategy.config.cut_loss_earliest_time.tzinfo, ZoneInfo("America/New_York"))

    def test_cut_loss_orderbook_does_not_trigger_below_rounded_mid_signal(self):
        strategy, engine = self.make_strategy()
        self.prepare_cut_loss_watch(strategy, engine, average_cost=1.0)

        strategy.on_orderbook(self.orderbook(bid_price=1.48, ask_price=1.50))

        self.assertEqual(engine.execution_calls, [])

    def test_cut_loss_uses_telegram_approval_setting(self):
        strategy, engine = self.make_strategy()
        self.prepare_cut_loss_watch(strategy, engine, average_cost=1.0)
        strategy.config.telegram_approval["cut_loss"] = True
        engine.telegram.approval_result = False

        strategy.on_orderbook(self.orderbook(bid_price=1.48, ask_price=1.51))

        self.assertEqual(len(engine.telegram.approval_calls), 1)
        self.assertIn("CUT LOSS SUMMARY", engine.telegram.approval_calls[0]["summary"])
        self.assertIn("TOB: 10 @ 1.48 | 1.51 @ 10", engine.telegram.approval_calls[0]["summary"])
        self.assertEqual(engine.execution_calls, [])

    def test_cut_loss_skips_when_open_order_exists_for_code(self):
        strategy, engine = self.make_strategy()
        self.prepare_cut_loss_watch(strategy, engine, average_cost=1.0)
        engine.order_list_result = pd.DataFrame([{"order_id": "1"}])

        strategy.on_orderbook(self.orderbook(bid_price=1.48, ask_price=1.51))

        self.assertEqual(engine.execution_calls, [])

    def test_cut_loss_ignores_duplicate_orderbook_while_executing(self):
        strategy, engine = self.make_strategy()
        self.prepare_cut_loss_watch(strategy, engine, average_cost=1.0)
        calls = []

        def fake_execute_cut_loss(strategy_arg, watch, order_book, mid_signal_price):
            self.assertIs(strategy_arg, strategy)
            calls.append((watch.code, order_book["bid_price"], order_book["ask_price"], mid_signal_price))
            strategy.on_orderbook(self.orderbook(bid_price=1.48, ask_price=1.51))

        with patch("trading.strategies.short_put_strategy.strategy_main.execute_cut_loss", side_effect=fake_execute_cut_loss):
            strategy.on_orderbook(self.orderbook(bid_price=1.48, ask_price=1.51))

        self.assertEqual(len(calls), 1)

    def test_cut_loss_failure_does_not_send_retry_keyboard(self):
        strategy, engine = self.make_strategy()
        self.prepare_cut_loss_watch(strategy, engine, average_cost=1.0)
        engine.execution_results.append(
            ExecutionResult(
                code="US.SPY260527P723000",
                target_qty=12,
                filled_qty=0,
                order_id="1",
                execution_status="fail",
                message="Price ladder exhausted without fill.",
            )
        )

        strategy.on_orderbook(self.orderbook(bid_price=1.48, ask_price=1.51))

        self.assertEqual(len(engine.telegram.messages), 2)
        self.assertIn("CUT LOSS RESULT - FAILURE", engine.telegram.messages[1]["text"])
        self.assertIsNone(engine.telegram.messages[1]["reply_markup"])

    def test_execute_short_put_strategy_stops_after_failed_child_order(self):
        strategy, engine = self.make_strategy()
        engine.execution_results.extend(
            [
                ExecutionResult(
                    code="US.SPY260527P723000",
                    target_qty=30,
                    filled_qty=30,
                    order_id="1",
                    execution_status="success",
                ),
                ExecutionResult(
                    code="US.SPY260527P723000",
                    target_qty=30,
                    filled_qty=5,
                    order_id="2",
                    execution_status="fail",
                ),
            ]
        )

        with (
            patch("trading.strategies.short_put_strategy.lifecycle.short_put._execution_checklist", return_value=True),
            patch(
                "trading.strategies.short_put_strategy.lifecycle.short_put.select_short_put", side_effect=lambda strategy_arg: self.selected_option()
            ),
            patch("trading.strategies.short_put_strategy.lifecycle.short_put.get_max_num_to_short", return_value=80),
        ):
            execute_short_put_strategy(strategy)

        self.assertEqual(len(engine.execution_calls), 2)
        self.assertEqual([execution_call["request"].qty for execution_call in engine.execution_calls], [30, 30])
        self.assertEqual(len(engine.telegram.messages), 2)
        self.assertIn("SHORT PUT RESULT - FAILURE", engine.telegram.messages[1]["text"])
        reply_markup = engine.telegram.messages[1]["reply_markup"]
        self.assertEqual(reply_markup["inline_keyboard"][0][0]["callback_data"], "strategy:short_put_spy:retry:execute_short_put_strategy")
        self.assertEqual(reply_markup["inline_keyboard"][0][1]["callback_data"], "strategy:short_put_spy:cancel")

    def test_execute_short_put_strategy_skips_execution_when_telegram_rejects(self):
        strategy, engine = self.make_strategy()
        strategy.config.telegram_approval["short_put"] = True
        execution_requests = [
            LimitOrderRequest(
                acc_id=200,
                code="US.SPY260527P723000",
                side=TrdSide.SELL,
                qty=30,
                price_ladder_plan=make_sell_put_ladder_plan(),
            ),
            LimitOrderRequest(
                acc_id=200,
                code="US.SPY260527P723000",
                side=TrdSide.SELL,
                qty=30,
                price_ladder_plan=make_sell_put_ladder_plan(),
            ),
            LimitOrderRequest(
                acc_id=200,
                code="US.SPY260527P723000",
                side=TrdSide.SELL,
                qty=20,
                price_ladder_plan=make_sell_put_ladder_plan(),
            ),
        ]
        engine.telegram.approval_result = False

        with (
            patch("trading.strategies.short_put_strategy.lifecycle.short_put._execution_checklist", return_value=True),
            patch(
                "trading.strategies.short_put_strategy.lifecycle.short_put.select_short_put", side_effect=lambda strategy_arg: self.selected_option()
            ),
            patch("trading.strategies.short_put_strategy.lifecycle.short_put.get_max_num_to_short", return_value=80),
            patch("trading.strategies.short_put_strategy.lifecycle.short_put._build_execution_requests", return_value=execution_requests),
        ):
            execute_short_put_strategy(strategy)

        self.assertEqual(len(engine.telegram.approval_calls), 1)
        self.assertEqual(engine.execution_calls, [])

    def test_execute_short_put_strategy_skips_approval_when_disabled(self):
        strategy, engine = self.make_strategy()
        strategy.config.telegram_approval["short_put"] = False
        execution_requests = [
            LimitOrderRequest(
                acc_id=200,
                code="US.SPY260527P723000",
                side=TrdSide.SELL,
                qty=30,
                price_ladder_plan=make_sell_put_ladder_plan(),
            ),
            LimitOrderRequest(
                acc_id=200,
                code="US.SPY260527P723000",
                side=TrdSide.SELL,
                qty=30,
                price_ladder_plan=make_sell_put_ladder_plan(),
            ),
            LimitOrderRequest(
                acc_id=200,
                code="US.SPY260527P723000",
                side=TrdSide.SELL,
                qty=20,
                price_ladder_plan=make_sell_put_ladder_plan(),
            ),
        ]

        with (
            patch("trading.strategies.short_put_strategy.lifecycle.short_put._execution_checklist", return_value=True),
            patch(
                "trading.strategies.short_put_strategy.lifecycle.short_put.select_short_put", side_effect=lambda strategy_arg: self.selected_option()
            ),
            patch("trading.strategies.short_put_strategy.lifecycle.short_put.get_max_num_to_short", return_value=80),
            patch("trading.strategies.short_put_strategy.lifecycle.short_put._build_execution_requests", return_value=execution_requests),
        ):
            execute_short_put_strategy(strategy)

        self.assertEqual(engine.telegram.approval_calls, [])
        self.assertEqual(len(engine.telegram.messages), 2)
        self.assertIn("SHORT PUT SUMMARY", engine.telegram.messages[0]["text"])
        self.assertTrue(engine.telegram.messages[0]["text"].endswith("Executing the above order..."))
        self.assertEqual(engine.telegram.messages[0]["parse_mode"], "HTML")
        self.assertIn("SHORT PUT RESULT", engine.telegram.messages[1]["text"])
        self.assertIn("Target Mid: 2.33", engine.telegram.messages[1]["text"])
        self.assertIn("Filled Quantity: <b>80</b>", engine.telegram.messages[1]["text"])
        self.assertIsNone(engine.telegram.messages[1]["reply_markup"])
        self.assertEqual(engine.telegram.messages[1]["parse_mode"], "HTML")
        self.assertEqual(len(engine.execution_calls), 3)

    def test_strategy_checklist_fails_when_open_order_query_errors(self):
        strategy, engine = self.make_strategy()
        engine.order_list_result = None

        self.assertFalse(_execution_checklist(strategy))

    def test_strategy_checklist_fails_when_position_query_errors(self):
        strategy, engine = self.make_strategy()
        engine.open_position_result = None

        self.assertFalse(_execution_checklist(strategy))

    def test_alert_assignment_at_close_sends_alert_for_maturing_short_put_below_strike(self):
        strategy, engine = self.make_strategy()
        strategy._maturing_put_option_strike = [723.0]
        engine.open_position_result = self.maturing_short_put_position(qty=-2, strike=723.0)
        engine.stock_quote_result = pd.DataFrame([{"code": "US.SPY", "last_price": 722.5}])

        alert_assignment_at_close(strategy)

        self.assertEqual(engine.subscriptions[-1]["code_list"], ["US.SPY"])
        self.assertEqual(engine.subscriptions[-1]["subtype_list"], [SubType.QUOTE])
        self.assertFalse(engine.subscriptions[-1]["subscribe_push"])
        self.assertEqual(len(engine.telegram.messages), 1)
        telegram_message = engine.telegram.messages[0]
        self.assertIn("SHORT PUT ASSIGNMENT DETECTED", telegram_message["text"])
        self.assertIn("Underlying: US.SPY", telegram_message["text"])
        self.assertIn("Underlying Price: 722.50", telegram_message["text"])
        self.assertIn("Strike: 723.00", telegram_message["text"])
        self.assertIn("Contracts: 2", telegram_message["text"])
        self.assertIsNone(telegram_message["reply_markup"])
        self.assertEqual(telegram_message["parse_mode"], "HTML")

    def test_alert_assignment_at_close_skips_when_underlying_is_not_below_strike(self):
        strategy, engine = self.make_strategy()
        strategy._maturing_put_option_strike = [723.0]
        engine.open_position_result = self.maturing_short_put_position(qty=-2, strike=723.0)
        engine.stock_quote_result = pd.DataFrame([{"code": "US.SPY", "last_price": 723.0}])

        alert_assignment_at_close(strategy)

        self.assertEqual(engine.telegram.messages, [])


if __name__ == "__main__":
    unittest.main()
