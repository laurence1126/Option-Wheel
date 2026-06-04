import datetime as dt
import threading
from dataclasses import dataclass
from typing import Dict, Any
from zoneinfo import ZoneInfo

import pandas as pd
from futu import *
from app.flask_app import FlaskAppService
from trading.config import futu_config
from trading.notification.telegram_bot import TelegramBotService
from trading.trading_engine.order_execution import ExecutionResult, LimitOrderRequest, OPEN_ORDER_STATUSES, OrderExecutionService
from trading.utils import futu_utils
from trading.utils.logging_utils import configure_logger
from trading.strategies.trading_strategy_base import TradingStrategyBase

logger = configure_logger(__name__)


@dataclass(frozen=True)
class DailyTimerEvent:
    strategy_id: str
    name: str
    trigger_time: dt.time
    timezone: str = "America/New_York"


class FutuTradingEngine:
    def __init__(self, strategy: TradingStrategyBase | list[TradingStrategyBase] | None = None) -> None:
        # Internal state
        self._closed = False
        self._running = False
        self._started_at: dt.datetime | None = None
        self._timer_events: list[DailyTimerEvent] = []
        self._timer_last_fired: dict[DailyTimerEvent, dt.date] = {}
        self._timer_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._schedule_changed_event = threading.Event()
        self._timer_thread: threading.Thread | None = None
        self._time_triggers_configured = False
        self._active_setup_strategy_id: str | None = None

        # Futu trading configuration
        self.trading_environment = futu_config.TRADING_ENVIRONMENT
        self.trading_market = futu_config.TRADING_MARKET
        self.trading_pwd = futu_config.TRADING_PWD

        # Futu contexts
        self.quote_context = futu_utils.create_quote_context(futu_config.FUTU_OPEND_ADDRESS, futu_config.FUTU_OPEND_PORT)
        self.trade_context = futu_utils.create_trade_context(futu_config.FUTU_OPEND_ADDRESS, futu_config.FUTU_OPEND_PORT, self.trading_market)

        # Futu accounts
        self.stock_account = futu_utils.get_stock_account(self.trade_context)
        self.option_account = futu_utils.get_option_account(self.trade_context)
        self.margin_account = futu_utils.get_margin_account(self.trade_context)

        # Additional services initialization
        self.execution = OrderExecutionService(self)
        self.telegram = TelegramBotService()
        self.flask_app = FlaskAppService(telegram_bot_service=self.telegram)

        # Load trading strategies
        self.strategy = self._normalize_strategy_input(strategy)
        for loaded_strategy in self.strategy.values():
            loaded_strategy.load_trading_engine(self)

    def run(self) -> None:
        if self._closed:
            raise RuntimeError("Cannot run a closed trading engine.")
        if self._running:
            return

        self._running = True
        self._started_at = dt.datetime.now(dt.timezone.utc)
        try:
            if not self.unlock_trade():
                raise RuntimeError("Unlock trade failed.")
            self.set_order_handlers()
            self.set_trade_handlers()
            try:
                self.telegram.start(self)
            except Exception as exc:
                logger.error("Telegram bot service failed to start; trading engine will continue: %s", exc)

            if not self._time_triggers_configured:
                self._setup_strategy_time_triggers()
                self._time_triggers_configured = True

            self._run_startup_recovery_actions()

            if self.has_daily_time_trigger():
                self.start_time_trigger_scheduler()

            try:
                self.flask_app.start()
            except Exception as exc:
                logger.error("Flask app service failed to start; trading engine will continue: %s", exc)
        except Exception:
            self._running = False
            self._started_at = None
            raise

    def __enter__(self) -> "FutuTradingEngine":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._stop_event.set()
        self._schedule_changed_event.set()
        try:
            self.telegram.shutdown()
        except Exception as exc:
            logger.error("Telegram bot service failed to stop cleanly: %s", exc)
        try:
            self.flask_app.shutdown()
        except Exception as exc:
            logger.error("Flask app service failed to stop cleanly: %s", exc)
        if self._timer_thread and self._timer_thread.is_alive() and threading.current_thread() is not self._timer_thread:
            self._timer_thread.join(timeout=5)
        for context in (self.quote_context, self.trade_context):
            try:
                context.close()
            except Exception:
                pass
        self._closed = True
        self._running = False
        self._started_at = None
        logger.info("Trading engine closed.")

    @staticmethod
    def _normalize_strategy_input(strategy: TradingStrategyBase | list[TradingStrategyBase] | None) -> dict[str, TradingStrategyBase]:
        if strategy is None:
            strategy_list = [TradingStrategyBase()]
        elif isinstance(strategy, TradingStrategyBase):
            strategy_list = [strategy]
        elif isinstance(strategy, list):
            if not strategy:
                raise ValueError("strategy list cannot be empty.")
            strategy_list = strategy
        else:
            raise TypeError("strategy must be None, a TradingStrategyBase instance, or a list of TradingStrategyBase instances.")

        normalized = {}
        for strategy_item in strategy_list:
            if not isinstance(strategy_item, TradingStrategyBase):
                raise TypeError("All strategies must be TradingStrategyBase instances.")
            strategy_id = getattr(strategy_item, "strategy_id", None)
            if not isinstance(strategy_id, str) or not strategy_id.strip():
                raise ValueError("Each strategy must define a non-empty strategy_id.")
            if strategy_id in normalized:
                raise ValueError(f"Duplicate strategy_id: {strategy_id}")
            normalized[strategy_id] = strategy_item
        return normalized

    def _setup_strategy_time_triggers(self) -> None:
        for strategy_id, strategy in self.strategy.items():
            self._active_setup_strategy_id = strategy_id
            try:
                strategy.setup_time_triggers()
            except Exception as exc:
                logger.error("Strategy time trigger setup failed: strategy_id=%s, error=%s", strategy_id, exc)
            finally:
                self._active_setup_strategy_id = None

    def _run_startup_recovery_actions(self) -> None:
        matched_strategy_count = 0
        recovery_failed = False
        for strategy_id, strategy in self.strategy.items():
            get_actions = getattr(strategy, "get_restart_actions", None)
            if not callable(get_actions):
                continue
            try:
                restart_actions = get_actions()
            except Exception:
                recovery_failed = True
                logger.exception("Startup recovery failed because restart actions are unavailable: strategy_id=%s.", strategy_id)
                continue
            if not isinstance(restart_actions, dict):
                recovery_failed = True
                logger.error("Startup recovery skipped strategy because get_restart_actions did not return a dict: strategy_id=%s.", strategy_id)
                continue
            if not restart_actions:
                continue

            matched_strategy_count += 1
            for action_name, action in restart_actions.items():
                if not callable(action):
                    recovery_failed = True
                    logger.error("Startup recovery skipped non-callable action: strategy_id=%s, action=%s.", strategy_id, action_name)
                    continue

                logger.info("Startup recovery starting: strategy_id=%s, action=%s.", strategy_id, action_name)
                try:
                    action_succeeded = action()
                except Exception:
                    recovery_failed = True
                    logger.exception("Startup recovery failed: strategy_id=%s, action=%s.", strategy_id, action_name)
                else:
                    if action_succeeded is False:
                        recovery_failed = True
                        logger.error("Startup recovery failed: strategy_id=%s, action=%s returned False.", strategy_id, action_name)
                    else:
                        logger.info("Startup recovery completed: strategy_id=%s, action=%s.", strategy_id, action_name)

        if matched_strategy_count == 0:
            logger.info("Startup recovery skipped because no strategy registered restart actions.")

        if recovery_failed:
            try:
                self.telegram.send_message("🚨 Startup recovery failed. Check logs.")
            except Exception:
                logger.exception("Startup recovery Telegram warning failed.")

    def _dispatch_strategy_callback(self, callback_name: str, *args) -> None:
        for strategy_id, strategy in self.strategy.items():
            callback = getattr(strategy, callback_name)
            try:
                callback(*args)
            except Exception as exc:
                logger.error("Strategy callback failed: strategy_id=%s, callback=%s, error=%s", strategy_id, callback_name, exc)

    def add_daily_time_trigger(self, name: str, trigger_time: dt.time, timezone: str = "America/New_York", strategy_id: str | None = None) -> None:
        owner_strategy_id = strategy_id or self._active_setup_strategy_id
        if owner_strategy_id is None:
            raise ValueError("strategy_id is required when adding a timer outside strategy setup.")
        if owner_strategy_id not in self.strategy:
            raise ValueError(f"Unknown strategy_id for daily time trigger: {owner_strategy_id}")

        tz = ZoneInfo(timezone)
        event = DailyTimerEvent(strategy_id=owner_strategy_id, name=name, trigger_time=trigger_time, timezone=timezone)
        now = dt.datetime.now(tz)
        run_at = dt.datetime.combine(now.date(), trigger_time, tzinfo=tz)
        with self._timer_lock:
            if any(existing.strategy_id == owner_strategy_id and existing.name == name for existing in self._timer_events):
                raise ValueError(f"Duplicate daily time trigger name for strategy {owner_strategy_id}: {name}")
            self._timer_events.append(event)
            if run_at <= now:
                self._timer_last_fired[event] = now.date()
        self._schedule_changed_event.set()
        logger.info("Daily timer added: strategy_id=%s, name=%s, trigger_time=%s, timezone=%s", owner_strategy_id, name, trigger_time, timezone)

    def has_daily_time_trigger(self) -> bool:
        with self._timer_lock:
            return bool(self._timer_events)

    def start_time_trigger_scheduler(self) -> None:
        if self._timer_thread and self._timer_thread.is_alive():
            return
        self._stop_event.clear()
        self._schedule_changed_event.set()
        self._timer_thread = threading.Thread(target=self._timer_loop, daemon=True)
        self._timer_thread.start()

    def _timer_loop(self) -> None:
        while not self._stop_event.is_set():
            event, next_run = self._get_next_timer_event()
            if event is None or next_run is None:
                self._schedule_changed_event.wait()
                self._schedule_changed_event.clear()
                continue

            now = dt.datetime.now(next_run.tzinfo)
            wait_seconds = max(0.0, (next_run - now).total_seconds())
            self._schedule_changed_event.clear()
            if self._schedule_changed_event.wait(wait_seconds):
                continue
            if self._stop_event.is_set():
                break

            for due_event in self._get_due_timer_events():
                strategy = self.strategy.get(due_event.strategy_id)
                if strategy is None:
                    logger.error("Timer callback skipped for unknown strategy: strategy_id=%s, name=%s", due_event.strategy_id, due_event.name)
                    continue
                try:
                    strategy.on_time_trigger(due_event.name)
                except Exception as exc:
                    logger.error("Timer callback failed: strategy_id=%s, name=%s, error=%s", due_event.strategy_id, due_event.name, exc)

    def _get_next_timer_event(self) -> tuple[DailyTimerEvent | None, dt.datetime | None]:
        with self._timer_lock:
            events = list(self._timer_events)
            last_fired = dict(self._timer_last_fired)
        if not events:
            return None, None

        next_event = None
        next_run = None
        for event in events:
            tz = ZoneInfo(event.timezone)
            now = dt.datetime.now(tz)
            run_at = dt.datetime.combine(now.date(), event.trigger_time, tzinfo=tz)
            if run_at <= now and last_fired.get(event) != now.date():
                run_at = now
            elif run_at <= now:
                run_at += dt.timedelta(days=1)
            if next_run is None or run_at < next_run:
                next_event = event
                next_run = run_at
        return next_event, next_run

    def _get_due_timer_events(self) -> list[DailyTimerEvent]:
        due_events = []
        with self._timer_lock:
            for event in self._timer_events:
                tz = ZoneInfo(event.timezone)
                now = dt.datetime.now(tz)
                run_at = dt.datetime.combine(now.date(), event.trigger_time, tzinfo=tz)
                if run_at <= now and self._timer_last_fired.get(event) != now.date():
                    self._timer_last_fired[event] = now.date()
                    due_events.append(event)
        return due_events

    def unlock_trade(self) -> bool:
        if self.trading_environment == TrdEnv.REAL:
            ret, data = self.trade_context.unlock_trade(self.trading_pwd)
            if ret != RET_OK:
                logger.error("Unlock trade failed: %s", data)
                return False
            logger.info("Unlock Trade success!")
        return True

    def get_market_state(self, code: str | list) -> pd.DataFrame | None:
        ret, data = self.quote_context.get_market_state(code_list=code)
        if ret != RET_OK:
            logger.error("Get market status failed: %s", data)
            return None
        return data

    def subscribe(self, code_list: list[str], subtype_list: list[SubType], subscribe_push: bool = True) -> bool:
        ret, err_message = self.quote_context.subscribe(
            code_list, subtype_list, subscribe_push=subscribe_push, extended_time=True, session=Session.ALL
        )
        if ret != RET_OK:
            logger.error("Subscribe quote data failed: %s", err_message)
            return False
        logger.info("Subscribed %s securities for quote data.", len(code_list))
        return True

    def unsubscribe_all(self) -> bool:
        ret, err = self.quote_context.unsubscribe_all()
        if ret != RET_OK:
            logger.error("Unsubscribe all quote subscriptions failed: %s", err)
            return False
        logger.info("Unsubscribed all quote subscriptions")
        return True

    def get_stock_quote(self, code_list: list[str]) -> pd.DataFrame | None:
        ret, data = self.quote_context.get_stock_quote(code_list)
        if ret != RET_OK:
            logger.error("Get stock quote failed: %s", data)
            return None
        return data

    def order_list_query(
        self,
        acc_id: str | int,
        order_id: str = "",
        code: str = "",
        status_filter_list: list[OrderStatus] | None = None,
        refresh_cache: bool = True,
    ) -> pd.DataFrame | None:
        ret, data = self.trade_context.order_list_query(
            order_id=order_id,
            status_filter_list=status_filter_list or [],
            code=code,
            trd_env=self.trading_environment,
            acc_id=acc_id,
            order_market=self.trading_market,
            refresh_cache=refresh_cache,
        )
        if ret != RET_OK:
            logger.error("Order list query failed: order_id=%s, code=%s, error=%s", order_id, code, data)
            return None
        return data

    def get_open_position(self, acc_id: str | int, code: str = "", refresh_cache: bool = True) -> pd.DataFrame | None:
        ret, data = self.trade_context.position_list_query(
            code=code,
            trd_env=self.trading_environment,
            acc_id=acc_id,
            refresh_cache=refresh_cache,
            position_market=self.trading_market,
        )
        if ret != RET_OK:
            logger.error("Get open position failed: %s", data)
            return None
        return data[data["qty"] != 0]

    def get_account_info(self, acc_id: str | int, currency: Currency = Currency.USD, refresh_cache: bool = True) -> pd.DataFrame | None:
        ret, data = self.trade_context.accinfo_query(
            trd_env=self.trading_environment,
            acc_id=acc_id,
            currency=currency,
            refresh_cache=refresh_cache,
        )
        if ret != RET_OK:
            logger.error(f"Get account info failed: {data}")
            return None

        return data

    def get_max_short_quantity(self, acc_id: str | int, code: str, price: float) -> float | None:
        ret, data = self.trade_context.acctradinginfo_query(
            order_type=OrderType.NORMAL,
            code=code,
            price=price,
            trd_env=self.trading_environment,
            acc_id=acc_id,
        )
        if ret != RET_OK:
            logger.error("Get max short quantity failed: code=%s, price=%s, error=%s", code, price, data)
            return None
        if data.empty or "max_sell_short" not in data.columns:
            logger.error("Get max short quantity failed: missing max_sell_short. code=%s, price=%s, data=%s", code, price, data)
            return None
        max_short_qty = float(data.iloc[0]["max_sell_short"])
        logger.info("Max short quantity from Futu: code=%s, price=%s, max_sell_short=%s", code, price, max_short_qty)
        return max_short_qty

    def get_top_order_book(self, code: str) -> Dict[str, Any] | None:
        ret, order_book = self.quote_context.get_order_book(code, num=1)
        if ret != RET_OK:
            logger.error("Get order book failed: %s", order_book)
            return None
        return self.process_top_orderbook(order_book)

    def process_top_orderbook(self, data: dict[str, Any]) -> dict[str, float | str] | None:
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
            "bid_time": data.get("svr_recv_time_bid"),
            "bid_price": bid_price,
            "bid_volume": bid_volume,
            "ask_time": data.get("svr_recv_time_ask"),
            "ask_price": ask_price,
            "ask_volume": ask_volume,
        }

    def place_limit_order(
        self,
        acc_id: str | int,
        code: str,
        side: TrdSide,
        price: float,
        qty: float,
        order_type: OrderType = OrderType.NORMAL,
        fill_outside_rth: bool = None,
        session: Session = None,
        remark: str | None = None,
    ) -> str | None:
        if fill_outside_rth is None:
            fill_outside_rth = self.trading_environment == TrdEnv.REAL
        if session == None:
            session = Session.ALL if self.trading_environment == TrdEnv.REAL else Session.RTH
        ret, data = self.trade_context.place_order(
            price=price,
            qty=qty,
            code=code,
            trd_side=side,
            order_type=order_type,
            adjust_limit=0.01,
            trd_env=self.trading_environment,
            acc_id=acc_id,
            remark=remark,
            fill_outside_rth=fill_outside_rth,
            session=session,
        )
        if ret != RET_OK:
            logger.error("Place order failed: code=%s, side=%s, qty=%s, price=%s, error=%s", code, side, qty, price, data)
            return None

        if not isinstance(data, pd.DataFrame) or data.empty or "order_id" not in data.columns:
            logger.error("Place order returned no order_id: code=%s, side=%s, qty=%s, price=%s, data=%s", code, side, qty, price, data)
            return None

        raw_order_id = data.iloc[0]["order_id"]
        if pd.isna(raw_order_id):
            logger.error("Place order returned invalid order_id: code=%s, side=%s, qty=%s, price=%s, data=%s", code, side, qty, price, data)
            return None
        order_id = str(raw_order_id)
        if not order_id:
            logger.error("Place order returned invalid order_id: code=%s, side=%s, qty=%s, price=%s, data=%s", code, side, qty, price, data)
            return None

        logger.info("Order submitted: code=%s, side=%s, qty=%s, price=%s, remark=%s, order_id=%s", code, side, qty, price, remark, order_id)
        return order_id

    def modify_limit_order(self, acc_id: str | int, order_id: str, qty: float, price: float) -> bool:
        ret, data = self.trade_context.modify_order(
            ModifyOrderOp.NORMAL,
            order_id=order_id,
            qty=qty,
            price=price,
            trd_env=self.trading_environment,
            acc_id=acc_id,
        )
        if ret != RET_OK:
            logger.error("Failed to modify order %s: qty=%s, price=%s, error=%s", order_id, qty, price, data)
            return False

        if not isinstance(data, pd.DataFrame) or data.empty or "order_id" not in data.columns:
            logger.error("Modify order %s returned invalid response: qty=%s, price=%s, data=%s", order_id, qty, price, data)
            return False

        raw_returned_order_id = data.iloc[0]["order_id"]
        if pd.isna(raw_returned_order_id):
            logger.error("Modify order %s returned empty order_id: qty=%s, price=%s, data=%s", order_id, qty, price, data)
            return False

        returned_order_id = str(raw_returned_order_id)
        if returned_order_id != str(order_id):
            logger.error(
                "Modify order returned mismatched order_id: requested=%s, returned=%s, qty=%s, price=%s, data=%s",
                order_id,
                returned_order_id,
                qty,
                price,
                data,
            )
            return False

        logger.info("Modified order %s successfully: qty=%s, price=%s", order_id, qty, price)
        return True

    def cancel_open_orders(self, acc_id: str | int, order_id: str = "", code: str = "") -> bool:
        orders = self.order_list_query(acc_id=acc_id, order_id=order_id, code=code, status_filter_list=OPEN_ORDER_STATUSES)
        if orders is None:
            logger.error("Cancel skipped because open order query failed: order_id=%s, code=%s.", order_id, code)
            return False
        if orders.empty:
            logger.info("No open orders to cancel: order_id=%s, code=%s.", order_id, code)
            return True

        success = True
        for _, order in orders.iterrows():
            ret, data = self.trade_context.modify_order(
                ModifyOrderOp.CANCEL, order_id=order["order_id"], qty=0, price=0, trd_env=self.trading_environment, acc_id=acc_id
            )
            if ret != RET_OK:
                logger.error("Failed to cancel order %s: %s", order["order_id"], data)
                success = False
            else:
                logger.info("Cancelled order %s successfully.", order["order_id"])
        return success

    def execute_limit_ladder(
        self,
        request: LimitOrderRequest,
        order_wait_seconds: int,
        cancel_wait_seconds: int,
        fill_outside_rth: bool = None,
    ) -> ExecutionResult:
        return self.execution.execute_limit_ladder(
            request=request,
            order_wait_seconds=order_wait_seconds,
            cancel_wait_seconds=cancel_wait_seconds,
            fill_outside_rth=fill_outside_rth,
        )

    def execute_limit_order(
        self,
        request: LimitOrderRequest,
        order_wait_seconds: int,
        cancel_wait_seconds: int,
        fill_outside_rth: bool = None,
    ) -> ExecutionResult:
        return self.execution.execute_limit_order(
            request=request,
            order_wait_seconds=order_wait_seconds,
            cancel_wait_seconds=cancel_wait_seconds,
            fill_outside_rth=fill_outside_rth,
        )

    def set_order_handlers(self) -> None:
        engine = self

        class OnOrderClass(TradeOrderHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super(OnOrderClass, self).on_recv_rsp(rsp_pb)
                if ret == RET_OK:
                    engine.execution.on_order_status(data)
                    engine._dispatch_strategy_callback("on_order_status", data)
                else:
                    logger.error("Order status callback failed: %s", data)
                return ret, data

        class OnFillClass(TradeDealHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super(OnFillClass, self).on_recv_rsp(rsp_pb)
                if ret == RET_OK:
                    engine._dispatch_strategy_callback("on_fill", data)
                else:
                    logger.error("Fill callback failed: %s", data)
                return ret, data

        self.trade_context.set_handler(OnOrderClass())
        self.trade_context.set_handler(OnFillClass())

    def set_trade_handlers(self) -> None:
        engine = self

        class OnQuoteClass(StockQuoteHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super(OnQuoteClass, self).on_recv_rsp(rsp_pb)
                if ret == RET_OK:
                    engine._dispatch_strategy_callback("on_quote", data)
                else:
                    logger.error("Quote callback failed: %s", data)
                return ret, data

        class OnOrderBookClass(OrderBookHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super(OnOrderBookClass, self).on_recv_rsp(rsp_pb)
                if ret == RET_OK:
                    engine._dispatch_strategy_callback("on_orderbook", data)
                else:
                    logger.error("Order book callback failed: %s", data)
                return ret, data

        class OnKlineClass(CurKlineHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super(OnKlineClass, self).on_recv_rsp(rsp_pb)
                if ret == RET_OK:
                    engine._dispatch_strategy_callback("on_kline", data)
                else:
                    logger.error("Kline callback failed: %s", data)
                return ret, data

        class OnRTDataClass(RTDataHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super(OnRTDataClass, self).on_recv_rsp(rsp_pb)
                if ret == RET_OK:
                    engine._dispatch_strategy_callback("on_rt_data", data)
                else:
                    logger.error("RT data callback failed: %s", data)
                return ret, data

        class OnTickClass(TickerHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super(OnTickClass, self).on_recv_rsp(rsp_pb)
                if ret == RET_OK:
                    engine._dispatch_strategy_callback("on_tick", data)
                else:
                    logger.error("Tick callback failed: %s", data)
                return ret, data

        self.quote_context.set_handler(OnQuoteClass())
        self.quote_context.set_handler(OnOrderBookClass())
        self.quote_context.set_handler(OnKlineClass())
        self.quote_context.set_handler(OnRTDataClass())
        self.quote_context.set_handler(OnTickClass())

    @staticmethod
    def _valid_positive_float(value: object) -> float | None:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        if pd.isna(result) or result <= 0:
            return None
        return result
