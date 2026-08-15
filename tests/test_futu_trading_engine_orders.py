import unittest
import datetime as dt
import threading
from unittest import mock

import pandas as pd
from futu import RET_ERROR, RET_OK, TrdEnv, TrdSide

from trading.strategies.trading_strategy_base import TradingStrategyBase
from trading.trading_engine.futu_trading_engine import FutuTradingEngine


class FakeTradeContext:
    def __init__(self) -> None:
        self.place_response = (RET_OK, pd.DataFrame([{"order_id": "1"}]))
        self.modify_response = (RET_OK, pd.DataFrame([{"order_id": "1"}]))
        self.history_response = (RET_OK, pd.DataFrame([{"order_id": "history-1"}]))
        self.history_order_kwargs = None

    def place_order(self, **kwargs):
        return self.place_response

    def modify_order(self, *args, **kwargs):
        return self.modify_response

    def history_order_list_query(self, **kwargs):
        self.history_order_kwargs = kwargs
        return self.history_response


class FakeQuoteContext:
    def __init__(self) -> None:
        self.market_snapshot_response = (RET_OK, pd.DataFrame([{"code": "US.TEST", "last_price": 1.23}]))
        self.market_snapshot_code_list = None

    def get_market_snapshot(self, code_list):
        self.market_snapshot_code_list = code_list
        return self.market_snapshot_response


class FakeTelegram:
    def __init__(self, raise_on_start: bool = False, enabled: bool = True) -> None:
        self.raise_on_start = raise_on_start
        self.enabled = enabled
        self.started = False
        self.stopped = False
        self.messages = []

    def start(self, engine) -> None:
        self.started = True
        if self.raise_on_start:
            raise RuntimeError("telegram failed")

    def shutdown(self) -> None:
        self.stopped = True

    def send_message(self, text: str) -> bool:
        if not self.enabled:
            return False
        self.messages.append(text)
        return True


class FakeFlaskApp:
    def __init__(self, raise_on_start: bool = False) -> None:
        self.raise_on_start = raise_on_start
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True
        if self.raise_on_start:
            raise RuntimeError("app failed")

    def shutdown(self) -> None:
        self.stopped = True


class FakeStrategy(TradingStrategyBase):
    def __init__(
        self,
        strategy_id: str | None = None,
        raise_on_quote: bool = False,
        recovery_enabled: bool = False,
        maturing_result: bool = True,
        cut_loss_result: bool = True,
        raise_on_cut_loss: bool = False,
    ) -> None:
        super().__init__()
        self.strategy_id = strategy_id if strategy_id is not None else "fake"
        self.setup_called = False
        self.loaded_engine = None
        self.quote_calls = []
        self.time_triggers = []
        self.raise_on_quote = raise_on_quote
        self.recovery_enabled = recovery_enabled
        self.maturing_result = maturing_result
        self.cut_loss_result = cut_loss_result
        self.raise_on_cut_loss = raise_on_cut_loss
        self.recovery_calls = []

    def load_trading_engine(self, engine) -> None:
        super().load_trading_engine(engine)
        self.loaded_engine = engine

    def setup_time_triggers(self) -> None:
        self.setup_called = True

    def on_quote(self, data: pd.DataFrame) -> None:
        self.quote_calls.append(data)
        if self.raise_on_quote:
            raise RuntimeError("quote failed")

    def on_time_trigger(self, name: str) -> None:
        self.time_triggers.append(name)

    def get_restart_actions(self):
        if not self.recovery_enabled:
            return {}
        return {
            "update_maturing_put_strikes": self.update_maturing_put_strikes,
            "setup_cut_loss_monitor": self.setup_cut_loss_monitor,
        }

    def update_maturing_put_strikes(self, strategy=None) -> bool:
        self.recovery_calls.append("update_maturing_put_strikes")
        return self.maturing_result

    def setup_cut_loss_monitor(self, strategy=None) -> bool:
        self.recovery_calls.append("setup_cut_loss_monitor")
        if self.raise_on_cut_loss:
            raise RuntimeError("cut loss failed")
        return self.cut_loss_result


class FakeContext:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FutuTradingEngineOrderWrapperTest(unittest.TestCase):
    def make_engine(self) -> tuple[FutuTradingEngine, FakeTradeContext]:
        engine = object.__new__(FutuTradingEngine)
        trade_context = FakeTradeContext()
        engine.trade_context = trade_context
        engine.quote_context = FakeQuoteContext()
        engine.trading_environment = TrdEnv.SIMULATE
        engine.trading_market = None
        return engine, trade_context

    def test_place_limit_order_returns_order_id_when_response_is_valid(self):
        engine, _ = self.make_engine()

        order_id = engine.place_limit_order(acc_id=1, code="US.TEST", side=TrdSide.SELL, qty=10, price=1.0)

        self.assertEqual(order_id, "1")

    def test_history_order_list_query_returns_dataframe_when_response_is_valid(self):
        engine, trade_context = self.make_engine()

        orders = engine.history_order_list_query(acc_id=1, code="US.TEST", start="2026-08-13 16:00:00", end="2026-08-14 16:00:00")

        self.assertEqual(orders.iloc[0]["order_id"], "history-1")
        self.assertEqual(trade_context.history_order_kwargs["acc_id"], 1)
        self.assertEqual(trade_context.history_order_kwargs["code"], "US.TEST")
        self.assertEqual(trade_context.history_order_kwargs["start"], "2026-08-13 16:00:00")
        self.assertEqual(trade_context.history_order_kwargs["end"], "2026-08-14 16:00:00")

    def test_get_market_snapshot_returns_dataframe_when_response_is_valid(self):
        engine, _ = self.make_engine()

        snapshot = engine.get_market_snapshot(["US.TEST"])

        self.assertEqual(snapshot.iloc[0]["last_price"], 1.23)
        self.assertEqual(engine.quote_context.market_snapshot_code_list, ["US.TEST"])

    def test_place_limit_order_returns_none_when_response_has_no_order_id(self):
        engine, trade_context = self.make_engine()
        trade_context.place_response = (RET_OK, pd.DataFrame([{"code": "US.TEST"}]))

        order_id = engine.place_limit_order(acc_id=1, code="US.TEST", side=TrdSide.SELL, qty=10, price=1.0)

        self.assertIsNone(order_id)

    def test_place_limit_order_returns_none_when_response_order_id_is_empty(self):
        engine, trade_context = self.make_engine()
        trade_context.place_response = (RET_OK, pd.DataFrame([{"order_id": None}]))

        order_id = engine.place_limit_order(acc_id=1, code="US.TEST", side=TrdSide.SELL, qty=10, price=1.0)

        self.assertIsNone(order_id)

    def test_place_limit_order_returns_none_on_futu_error(self):
        engine, trade_context = self.make_engine()
        trade_context.place_response = (RET_ERROR, "place failed")

        order_id = engine.place_limit_order(acc_id=1, code="US.TEST", side=TrdSide.SELL, qty=10, price=1.0)

        self.assertIsNone(order_id)

    def test_modify_limit_order_returns_true_when_response_order_id_matches(self):
        engine, _ = self.make_engine()

        modified = engine.modify_limit_order(acc_id=1, order_id="1", qty=10, price=0.9)

        self.assertTrue(modified)

    def test_modify_limit_order_returns_false_when_response_has_no_order_id(self):
        engine, trade_context = self.make_engine()
        trade_context.modify_response = (RET_OK, pd.DataFrame([{"code": "US.TEST"}]))

        modified = engine.modify_limit_order(acc_id=1, order_id="1", qty=10, price=0.9)

        self.assertFalse(modified)

    def test_modify_limit_order_returns_false_when_response_order_id_mismatches(self):
        engine, trade_context = self.make_engine()
        trade_context.modify_response = (RET_OK, pd.DataFrame([{"order_id": "2"}]))

        modified = engine.modify_limit_order(acc_id=1, order_id="1", qty=10, price=0.9)

        self.assertFalse(modified)

    def test_modify_limit_order_returns_false_on_futu_error(self):
        engine, trade_context = self.make_engine()
        trade_context.modify_response = (RET_ERROR, "modify failed")

        modified = engine.modify_limit_order(acc_id=1, order_id="1", qty=10, price=0.9)

        self.assertFalse(modified)

    def test_cancel_open_orders_returns_true_when_no_open_order_exists(self):
        engine, _ = self.make_engine()
        engine.order_list_query = lambda **kwargs: pd.DataFrame()

        cancelled = engine.cancel_open_orders(acc_id=1, order_id="1", code="US.TEST")

        self.assertTrue(cancelled)

    def make_runnable_engine(
        self,
        telegram: FakeTelegram,
        strategy: FakeStrategy | None = None,
        flask_app: FakeFlaskApp | None = None,
    ) -> tuple[FutuTradingEngine, FakeStrategy]:
        engine = object.__new__(FutuTradingEngine)
        strategy = strategy or FakeStrategy()
        engine._closed = False
        engine._running = False
        engine._time_triggers_configured = False
        engine._active_setup_strategy_id = None
        engine.telegram = telegram
        engine.flask_app = flask_app or FakeFlaskApp()
        engine.strategy = {"fake": strategy}
        engine.unlock_trade = lambda: True
        engine.set_order_handlers = lambda: None
        engine.set_trade_handlers = lambda: None
        engine.has_daily_time_trigger = lambda: False
        engine.start_time_trigger_scheduler = lambda: None
        return engine, strategy

    def test_run_starts_telegram_bot_service(self):
        telegram = FakeTelegram()
        engine, strategy = self.make_runnable_engine(telegram)

        engine.run()

        self.assertTrue(telegram.started)
        self.assertTrue(strategy.setup_called)
        self.assertTrue(engine._running)
        self.assertIsNotNone(engine._started_at)

    def test_run_starts_option_watcher_app_server(self):
        telegram = FakeTelegram()
        flask_app = FakeFlaskApp()
        engine, _ = self.make_runnable_engine(telegram, flask_app=flask_app)

        engine.run()

        self.assertTrue(flask_app.started)

    def test_run_continues_when_option_watcher_app_start_fails(self):
        telegram = FakeTelegram()
        flask_app = FakeFlaskApp(raise_on_start=True)
        engine, strategy = self.make_runnable_engine(telegram, flask_app=flask_app)

        engine.run()

        self.assertTrue(strategy.setup_called)
        self.assertTrue(engine._running)

    def test_run_continues_when_telegram_start_fails(self):
        telegram = FakeTelegram(raise_on_start=True)
        engine, strategy = self.make_runnable_engine(telegram)

        engine.run()

        self.assertTrue(telegram.started)
        self.assertTrue(strategy.setup_called)
        self.assertTrue(engine._running)

    def test_run_executes_startup_recovery_once_before_timer_scheduler(self):
        telegram = FakeTelegram()
        strategy = FakeStrategy(recovery_enabled=True)
        engine, strategy = self.make_runnable_engine(telegram, strategy)
        scheduler_calls = []
        engine.has_daily_time_trigger = lambda: True
        engine.start_time_trigger_scheduler = lambda: scheduler_calls.append(list(strategy.recovery_calls))

        engine.run()
        engine.run()

        self.assertEqual(strategy.recovery_calls, ["update_maturing_put_strikes", "setup_cut_loss_monitor"])
        self.assertEqual(scheduler_calls, [["update_maturing_put_strikes", "setup_cut_loss_monitor"]])

    def test_run_executes_generic_strategy_restart_action(self):
        telegram = FakeTelegram()
        engine, strategy = self.make_runnable_engine(telegram)
        calls = []
        strategy.get_restart_actions = lambda: {"restore_custom_state": lambda strategy: calls.append("restore_custom_state")}

        engine.run()

        self.assertEqual(calls, ["restore_custom_state"])

    def test_run_restores_monitoring_when_telegram_start_fails(self):
        telegram = FakeTelegram(raise_on_start=True)
        strategy = FakeStrategy(recovery_enabled=True)
        engine, strategy = self.make_runnable_engine(telegram, strategy)

        engine.run()

        self.assertEqual(strategy.recovery_calls, ["update_maturing_put_strikes", "setup_cut_loss_monitor"])
        self.assertTrue(engine._running)

    def test_run_restores_monitoring_when_telegram_is_disabled(self):
        telegram = FakeTelegram(enabled=False)
        strategy = FakeStrategy(recovery_enabled=True)
        engine, strategy = self.make_runnable_engine(telegram, strategy)

        engine.run()

        self.assertEqual(strategy.recovery_calls, ["update_maturing_put_strikes", "setup_cut_loss_monitor"])
        self.assertTrue(engine._running)

    def test_run_keeps_engine_alive_and_warns_when_startup_recovery_fails(self):
        telegram = FakeTelegram()
        strategy = FakeStrategy(recovery_enabled=True, maturing_result=False, raise_on_cut_loss=True)
        engine, strategy = self.make_runnable_engine(telegram, strategy)

        engine.run()

        self.assertEqual(strategy.recovery_calls, ["update_maturing_put_strikes", "setup_cut_loss_monitor"])
        self.assertEqual(telegram.messages, ["🚨 Startup recovery failed. Check logs."])
        self.assertTrue(engine._running)

    def test_run_warns_when_cut_loss_monitor_reports_setup_failure(self):
        telegram = FakeTelegram()
        strategy = FakeStrategy(recovery_enabled=True, cut_loss_result=False)
        engine, strategy = self.make_runnable_engine(telegram, strategy)

        engine.run()

        self.assertEqual(strategy.recovery_calls, ["update_maturing_put_strikes", "setup_cut_loss_monitor"])
        self.assertEqual(telegram.messages, ["🚨 Startup recovery failed. Check logs."])
        self.assertTrue(engine._running)

    def test_run_skips_startup_recovery_for_strategy_without_recovery_actions(self):
        telegram = FakeTelegram()
        engine, strategy = self.make_runnable_engine(telegram)

        engine.run()

        self.assertEqual(strategy.recovery_calls, [])
        self.assertEqual(telegram.messages, [])

    def test_close_stops_telegram_bot_service(self):
        engine = object.__new__(FutuTradingEngine)
        quote_context = FakeContext()
        trade_context = FakeContext()
        telegram = FakeTelegram()
        engine._closed = False
        engine._running = True
        engine._started_at = object()
        engine._stop_event = threading.Event()
        engine._schedule_changed_event = threading.Event()
        engine._timer_thread = None
        engine.quote_context = quote_context
        engine.trade_context = trade_context
        engine.telegram = telegram
        flask_app = FakeFlaskApp()
        engine.flask_app = flask_app

        engine.close()

        self.assertTrue(telegram.stopped)
        self.assertTrue(flask_app.stopped)
        self.assertTrue(quote_context.closed)
        self.assertTrue(trade_context.closed)
        self.assertIsNone(engine._started_at)

    def test_normalize_strategy_input_defaults_to_base_strategy(self):
        strategy = FutuTradingEngine._normalize_strategy_input(None)

        self.assertEqual(list(strategy), ["base_strategy"])
        self.assertIsInstance(strategy["base_strategy"], TradingStrategyBase)

    def test_normalize_strategy_input_registers_single_strategy_by_id(self):
        strategy = FakeStrategy("alpha")

        normalized = FutuTradingEngine._normalize_strategy_input(strategy)

        self.assertEqual(normalized, {"alpha": strategy})

    def test_normalize_strategy_input_registers_strategy_list_by_id(self):
        first = FakeStrategy("alpha")
        second = FakeStrategy("beta")

        normalized = FutuTradingEngine._normalize_strategy_input([first, second])

        self.assertEqual(normalized, {"alpha": first, "beta": second})

    def test_normalize_strategy_input_rejects_invalid_inputs(self):
        with self.assertRaises(TypeError):
            FutuTradingEngine._normalize_strategy_input("bad")
        with self.assertRaises(ValueError):
            FutuTradingEngine._normalize_strategy_input([])
        with self.assertRaises(ValueError):
            FutuTradingEngine._normalize_strategy_input([FakeStrategy("")])
        with self.assertRaises(ValueError):
            FutuTradingEngine._normalize_strategy_input([FakeStrategy("dup"), FakeStrategy("dup")])

    def test_constructor_loads_all_registered_strategies(self):
        first = FakeStrategy("alpha")
        second = FakeStrategy("beta")

        with (
            mock.patch("trading.trading_engine.futu_trading_engine.futu_utils.create_quote_context", return_value=FakeContext()),
            mock.patch("trading.trading_engine.futu_trading_engine.futu_utils.create_trade_context", return_value=FakeContext()),
            mock.patch("trading.trading_engine.futu_trading_engine.futu_utils.get_stock_account", return_value=1),
            mock.patch("trading.trading_engine.futu_trading_engine.futu_utils.get_option_account", return_value=2),
            mock.patch("trading.trading_engine.futu_trading_engine.futu_utils.get_margin_account", return_value=3),
        ):
            engine = FutuTradingEngine([first, second])

        self.assertEqual(engine.strategy, {"alpha": first, "beta": second})
        self.assertIs(first.loaded_engine, engine)
        self.assertIs(second.loaded_engine, engine)

    def test_dispatch_strategy_callback_fans_out_and_isolates_exceptions(self):
        first = FakeStrategy("alpha", raise_on_quote=True)
        second = FakeStrategy("beta")
        engine = object.__new__(FutuTradingEngine)
        engine.strategy = {"alpha": first, "beta": second}
        data = pd.DataFrame([{"code": "US.TEST"}])

        engine._dispatch_strategy_callback("on_quote", data)

        self.assertEqual(first.quote_calls, [data])
        self.assertEqual(second.quote_calls, [data])

    def test_strategy_owned_timers_allow_same_name_across_strategies(self):
        engine = object.__new__(FutuTradingEngine)
        engine.strategy = {"alpha": FakeStrategy("alpha"), "beta": FakeStrategy("beta")}
        engine._active_setup_strategy_id = None
        engine._timer_events = []
        engine._timer_last_fired = {}
        engine._timer_lock = threading.Lock()
        engine._schedule_changed_event = threading.Event()
        trigger_time = (dt.datetime.now() + dt.timedelta(hours=1)).time()

        engine.add_daily_time_trigger("open", trigger_time, strategy_id="alpha")
        engine.add_daily_time_trigger("open", trigger_time, strategy_id="beta")

        self.assertEqual([(event.strategy_id, event.name) for event in engine._timer_events], [("alpha", "open"), ("beta", "open")])
        with self.assertRaises(ValueError):
            engine.add_daily_time_trigger("open", trigger_time, strategy_id="alpha")

    def test_timer_strategy_id_is_inferred_only_during_strategy_setup(self):
        engine = object.__new__(FutuTradingEngine)
        engine.strategy = {"alpha": FakeStrategy("alpha")}
        engine._active_setup_strategy_id = "alpha"
        engine._timer_events = []
        engine._timer_last_fired = {}
        engine._timer_lock = threading.Lock()
        engine._schedule_changed_event = threading.Event()
        trigger_time = (dt.datetime.now() + dt.timedelta(hours=1)).time()

        engine.add_daily_time_trigger("open", trigger_time)
        self.assertEqual(engine._timer_events[0].strategy_id, "alpha")

        engine._active_setup_strategy_id = None
        with self.assertRaises(ValueError):
            engine.add_daily_time_trigger("close", trigger_time)

    def test_due_timer_dispatches_only_to_owner_strategy(self):
        alpha = FakeStrategy("alpha")
        beta = FakeStrategy("beta")
        engine = object.__new__(FutuTradingEngine)
        engine.strategy = {"alpha": alpha, "beta": beta}
        event = mock.Mock()
        event.strategy_id = "alpha"
        event.name = "open"

        strategy = engine.strategy.get(event.strategy_id)
        strategy.on_time_trigger(event.name)

        self.assertEqual(alpha.time_triggers, ["open"])
        self.assertEqual(beta.time_triggers, [])


if __name__ == "__main__":
    unittest.main()
