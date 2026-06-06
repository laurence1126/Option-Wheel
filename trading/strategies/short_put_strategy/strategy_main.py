import pandas as pd
from typing import Any, List

from trading.config.trading_config import ShortPutLiveConfig
from trading.strategies.trading_strategy_base import TradingStrategyBase
from trading.trading_engine.execution_engine import round_up_to_tick
from app.utils.logging import configure_logger

logger = configure_logger(__name__)


from .utils.option_parsing import *
from .utils.account_state import *
from .utils.put_selection import *
from .lifecycle.short_put import *
from .lifecycle.cut_loss import *
from .lifecycle.assignment import *


class ShortPutStrategy(TradingStrategyBase):
    def __init__(self, config: ShortPutLiveConfig = ShortPutLiveConfig()):
        super().__init__()
        self.strategy_id = "short_put" + "_" + config.underlying.split(".")[-1].lower()
        self.config: ShortPutLiveConfig = config

        self._short_put_execution_active = False
        self._put_option_position: List[OptionInfo] = []
        self._cut_loss_watchlist: dict[str, CutLossWatch] = {}

        self._maturing_put_option_strike: List[float] = []

        self.trading_status: dict[str, bool] = {
            "maturing_updated": False,
            "cut_loss_setup": False,
        }

    @property
    def acc_id(self):
        return self.engine.margin_account if self.engine.trading_environment == TrdEnv.REAL else self.engine.option_account

    ####################################################################################################
    # Lifecycle / Timer Entry Points
    ####################################################################################################

    def setup_time_triggers(self):
        super().setup_time_triggers()

        self.engine.add_daily_time_trigger("update_maturing_put_strikes", pd.to_datetime("09:00").time())
        self.engine.add_daily_time_trigger("setup_cut_loss_monitor", pd.to_datetime("09:20").time())
        self.engine.add_daily_time_trigger("execute_short_put_strategy", pd.to_datetime("15:50").time())
        self.engine.add_daily_time_trigger("alert_assignment_at_close", pd.to_datetime("16:00").time())
        self.engine.add_daily_time_trigger("clear_all_subscriptions", pd.to_datetime("20:00").time())

    def on_time_trigger(self, name: str):
        super().on_time_trigger(name)

        if name == "update_maturing_put_strikes":
            update_maturing_put_strikes(self)

        elif name == "setup_cut_loss_monitor":
            setup_cut_loss_monitor(self)

        elif name == "execute_short_put_strategy":
            execute_short_put_strategy(self)

        elif name == "alert_assignment_at_close":
            alert_assignment_at_close(self)

        elif name == "clear_all_subscriptions":
            logger.info("Clearing all subscriptions to free up resources.")
            self.engine.cancel_open_orders(self.acc_id)
            self.engine.unsubscribe_all()

    def get_strategy_actions(self):
        return {
            "update_maturing_put_strikes": update_maturing_put_strikes,
            "setup_cut_loss_monitor": setup_cut_loss_monitor,
            "execute_short_put_strategy": execute_short_put_strategy,
            "alert_assignment_at_close": alert_assignment_at_close,
        }

    def get_restart_actions(self):
        return {
            "update_maturing_put_strikes": update_maturing_put_strikes,
            "setup_cut_loss_monitor": setup_cut_loss_monitor,
        }

    ####################################################################################################
    # Futu Push Callback Entry Points
    ####################################################################################################

    def on_quote(self, data: pd.DataFrame) -> None:
        if data is None or data.empty:
            return
        super().on_quote(data)

    def on_orderbook(self, data: dict[str, Any]) -> None:
        if not data:
            return
        super().on_orderbook(data)

        self.on_orderbook_cut_loss(data)

    def on_orderbook_cut_loss(self, data: dict[str, Any]) -> None:
        top_book = self.engine.process_top_orderbook(data)
        if top_book is None:
            return

        code = top_book["code"]
        bid_price = top_book["bid_price"]
        ask_price = top_book["ask_price"]
        with self.lock:
            watch = self._cut_loss_watchlist.get(code)
            if watch is None or watch.executing:
                return
            if watch.price_tick is None or watch.stop_price is None:
                logger.warning("Cut-loss monitor skipped order book update because price tick is unavailable: code=%s.", code)
                return

            mid_price = (bid_price + ask_price) / 2
            mid_signal_price = round_up_to_tick(mid_price, watch.price_tick)
            if mid_signal_price < watch.stop_price:
                return
            watch.executing = True

        try:
            execute_cut_loss(self, watch, top_book, mid_signal_price)
        finally:
            with self.lock:
                current_watch = self._cut_loss_watchlist.get(code)
                if current_watch is not None:
                    current_watch.executing = False

    def on_order_status(self, data: pd.DataFrame) -> None:
        if data is None or data.empty:
            return
        super().on_order_status(data)
