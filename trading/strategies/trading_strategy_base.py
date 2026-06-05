from __future__ import annotations

import threading
import pandas as pd
from typing import TYPE_CHECKING
from typing import Callable, Dict, Any

from app.utils.logging import configure_logger

if TYPE_CHECKING:
    from trading.trading_engine.futu_trading_engine import FutuTradingEngine

logger = configure_logger(__name__)


class TradingStrategyBase:
    def __init__(self) -> None:
        self.strategy_id = "base_strategy"
        self.engine: FutuTradingEngine | None = None
        # RLock to synchronize access to shared resources across callbacks
        # Needed when using multiple time triggers and/or subscribing to multiple data types that may trigger concurrent callbacks
        # Usage:
        # with self.lock:
        #   - Access shared resources safely across callbacks
        self.lock = threading.RLock()

    def load_trading_engine(self, engine: FutuTradingEngine) -> None:
        """Load the trading engine into the strategy for use in callbacks."""
        self.engine = engine
        logger.info("Trading engine loaded into strategy")

    def setup_time_triggers(self) -> None:
        """Register strategy time triggers with the loaded engine.

        Example
        ---
            self.engine.add_daily_time_trigger("daily_trigger_example", datetime.time(9, 30))
        """
        if self.engine is None:
            raise ValueError("Trading engine must be loaded before setting up time triggers.")
        pass

    def on_time_trigger(self, name: str) -> None:
        """Scheduled time trigger event callback.

        Example
        ---
            if name == "daily_trigger_example":
                - Do something at 9:30am every day
        """
        pass

    def on_quote(self, data: pd.DataFrame) -> None:
        """StockQuoteHandlerBase callback data from SubType.QUOTE."""
        pass

    def on_orderbook(self, data: Dict[str, Any]) -> None:
        """OrderBookHandlerBase callback data from SubType.ORDER_BOOK."""
        pass

    def on_kline(self, data: pd.DataFrame) -> None:
        """CurKlineHandlerBase callback data from candlestick SubType.K_*."""
        pass

    def on_rt_data(self, data: pd.DataFrame) -> None:
        """RTDataHandlerBase callback data from SubType.RT_DATA."""
        pass

    def on_tick(self, data: pd.DataFrame) -> None:
        """TickerHandlerBase callback data from SubType.TICKER."""
        pass

    def on_order_status(self, data: pd.DataFrame) -> None:
        """TradeOrderHandlerBase callback data."""
        if data is None or data.empty:
            return

        required_columns = {"order_status", "code", "price", "trd_side", "qty"}
        missing_columns = required_columns - set(data.columns)
        if missing_columns:
            logger.warning("Order status callback skipped malformed data: missing_columns=%s", sorted(missing_columns))
            return

        row = data.iloc[0]
        order_status = row["order_status"]
        order_info = {
            "Code": row["code"],
            "Price": row["price"],
            "TradeSide": row["trd_side"],
            "Quantity": row["qty"],
        }
        logger.info("[OrderStatus] %s %s", order_status, order_info)

    def on_fill(self, data: pd.DataFrame) -> None:
        fill_status = data["status"][0]
        fill_info = {
            "Code": data["code"][0],
            "Price": data["price"][0],
            "TradeSide": data["trd_side"][0],
            "Quantity": data["qty"][0],
        }
        logger.info("[OrderFilled] %s %s", fill_status, fill_info)

    def on_broker(self, data: tuple[str, pd.DataFrame, pd.DataFrame]) -> None:
        """BrokerHandlerBase callback data from SubType.BROKER."""
        pass

    def on_price_reminder(self, data: Dict[str, Any]) -> None:
        """PriceReminderHandlerBase callback data."""
        pass

    def on_sys_notify(self, data: tuple[Any, Any, Dict[str, Any]]) -> None:
        """SysNotifyHandlerBase callback data."""
        pass

    def get_strategy_actions(self) -> dict[str, Callable[[], Any]]:
        """Return actions that external controls, such as Telegram callbacks, may invoke."""
        return {}

    def get_restart_actions(self) -> dict[str, Callable[[], Any]]:
        """Return ordered actions that restore strategy runtime state after engine startup."""
        return {}
