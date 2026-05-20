import re
import secrets
import pandas as pd
from typing import Any, Literal, Tuple, List
from time import sleep
from dataclasses import dataclass

from futu import *
from trading.config.trading_config import ShortPutLiveConfig
from trading.notification.telegram_summary import (
    build_assignment_summary,
    build_cut_loss_summary,
    build_execution_result_summary,
    build_sell_put_summary,
)
from trading.trading_engine.order_execution import LimitOrderRequest, OPEN_ORDER_STATUSES, build_price_ladder, round_up_to_tick
from trading.strategies.trading_strategy_base import TradingStrategyBase
from trading.utils.logging_utils import configure_logger

logger = configure_logger(__name__)


@dataclass
class OptionInfo:
    code: str = None
    ticker: str = None
    type: str = None
    strike: float = None
    expiration: str = None
    qty: float = None
    price: float = None


@dataclass
class CutLossWatch:
    option: OptionInfo
    qty: int
    average_price: float
    stop_price: float | None = None
    price_tick: float | None = None
    executing: bool = False

    @property
    def code(self) -> str:
        return self.option.code


OPTION_PATTERN = {
    TrdEnv.REAL: re.compile(r"^(?P<symbol>[A-Z]+)\s+(?P<date>\d{6})\s+(?P<strike>\d+(?:\.\d+)?)(?P<type>[CP])$"),
    TrdEnv.SIMULATE: re.compile(r"^(?P<symbol>[A-Z]+)(?P<date>\d{6})(?P<type>[CP])(?P<strike>\d+)$"),
}


class ShortPutStrategy(TradingStrategyBase):
    def __init__(self, config: ShortPutLiveConfig = ShortPutLiveConfig()):
        super().__init__()
        self.strategy_id = "short_put" + "_" + config.underlying.split(".")[-1].lower()
        self.config: ShortPutLiveConfig = config

        self._short_put_execution_active = False
        self._put_option_position: List[OptionInfo] = []
        self._cut_loss_watchlist: dict[str, CutLossWatch] = {}

        self._maturing_put_option_strike: List[float] = []
        self._underlying_alerted_order_ids: set[str] = set()
        self._pending_assignment_actions: dict[str, dict[str, Any]] = {}

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
            self.update_maturing_put_strikes()

        elif name == "setup_cut_loss_monitor":
            self.setup_cut_loss_monitor()

        elif name == "execute_short_put_strategy":
            self.execute_short_put_strategy()

        elif name == "alert_assignment_at_close":
            self.alert_assignment_at_close()

        elif name == "clear_all_subscriptions":
            logger.info("Clearing all subscriptions to free up resources.")
            self.engine.cancel_open_orders(self.acc_id)
            self.engine.unsubscribe_all()

    def get_strategy_actions(self):
        return {
            "update_maturing_put_strikes": self.update_maturing_put_strikes,
            "setup_cut_loss_monitor": self.setup_cut_loss_monitor,
            "execute_short_put_strategy": self.execute_short_put_strategy,
            "alert_assignment_at_close": self.alert_assignment_at_close,
            "execute_underlying_assignment": self.execute_underlying_assignment,
        }

    ####################################################################################################
    # Futu Push Callback Entry Points
    ####################################################################################################

    def on_quote(self, data: pd.DataFrame) -> None:
        if data is None or data.empty:
            return
        super().on_quote(data)

        self.on_quote_cut_loss(data)

    def on_quote_cut_loss(self, data: pd.DataFrame) -> None:
        with self.lock:
            for _, row in data.iterrows():
                code = row["code"] if "code" in row.index else None
                if code not in self._cut_loss_watchlist or "price_spread" not in row.index:
                    continue
                price_tick = self.engine._valid_positive_float(row["price_spread"])
                if price_tick is None:
                    continue
                watch = self._cut_loss_watchlist[code]
                watch.price_tick = price_tick
                watch.stop_price = round_up_to_tick(watch.average_price * float(self.config.stop_loss_multiple), price_tick)

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
            self.execute_cut_loss(watch, top_book, mid_signal_price)
        finally:
            with self.lock:
                current_watch = self._cut_loss_watchlist.get(code)
                if current_watch is not None:
                    current_watch.executing = False

    def on_order_status(self, data: pd.DataFrame) -> None:
        if data is None or data.empty:
            return
        super().on_order_status(data)

        self.on_order_status_underlying(data)

    ####################################################################################################
    # Main Strategy Workflows
    ####################################################################################################

    def pre_strategy_checklist(self):
        position_updated = self.update_put_position()
        if not position_updated:
            logger.warning("Strategy checklist failed: unable to refresh put positions.")
            return False

        market_state = self.get_underlying_market_state()
        if market_state not in [MarketState.AFTERNOON, MarketState.AFTER_HOURS_BEGIN]:
            logger.warning(
                "Strategy checklist failed: market status is %s, expected one of %s.",
                market_state,
                [MarketState.AFTERNOON, MarketState.AFTER_HOURS_BEGIN],
            )
            return False

        total_cash = self.get_total_cash()
        if total_cash is None or total_cash <= 0:
            logger.warning("Strategy checklist failed: total cash is unavailable or non-positive: %s.", total_cash)
            return False

        leverage = self.get_leverage_ratio()
        if leverage is None or leverage >= self.config.leverage_ratio:
            logger.warning(
                "Strategy checklist failed: leverage ratio is %s, limit is %s.",
                leverage,
                self.config.leverage_ratio,
            )
            return False

        open_orders = self.engine.order_list_query(acc_id=self.acc_id, status_filter_list=OPEN_ORDER_STATUSES)
        if open_orders is None:
            logger.warning("Strategy checklist failed: unable to query open orders.")
            return False
        if not open_orders.empty:
            logger.warning("Strategy checklist failed: open orders exist, count=%s.", len(open_orders))
            return False

        return True

    def execute_short_put_strategy(self):
        with self.lock:
            if self._short_put_execution_active:
                logger.warning("Short put execution skipped because another run is already active.")
                return
            self._short_put_execution_active = True

        try:
            if not self.pre_strategy_checklist():
                return

            selected_option = self.select_short_put()
            if selected_option.empty:
                logger.warning("No suitable short put candidate found.")
                return

            max_short = self.get_max_num_to_short(selected_option)
            if max_short <= 0:
                logger.warning("No capacity to short selected put.")
                return

            logger.info("Executing short put strategy...")
            requests = self.short_put_execution_checklist(selected_option, max_short)
            if requests is None:
                logger.warning("Short put execution checklist rejected. Skipping execution.")
                return

            requested_qty = sum(request.qty for request in requests)
            requires_approval = self.config.telegram_approval.get("short_put", True)
            prices = requests[0].price if requests else []
            price_ladder = prices if isinstance(prices, list) else [prices]
            option_info = self.resolve_option_name(selected_option["name"], TrdEnv.REAL)
            option_name = self.resolve_option_info(option_info) if option_info is not None else selected_option["name"]
            to_maturity = None
            if option_info is not None and option_info.expiration is not None:
                to_maturity = (pd.to_datetime(option_info.expiration).date() - pd.Timestamp.today().date()).days
            approval_summary = build_sell_put_summary(
                code=selected_option["code"],
                name=option_name,
                snapshot=selected_option,
                total_qty=requested_qty,
                child_qtys=[request.qty for request in requests],
                prices=price_ladder,
                to_maturity=to_maturity,
                final_line="🫡 Approve this trade?" if requires_approval else "🛎️ Executing the above order...",
            )
            if requires_approval:
                approved = self.engine.telegram.request_trade_approval(
                    approval_summary,
                    timeout_seconds=self.config.telegram_approval_timeout,
                )
                if not approved:
                    logger.warning("Short put execution skipped because Telegram trade approval was not granted.")
                    return
            else:
                self.engine.telegram.send_message(approval_summary, parse_mode="HTML")

            filled_qty = 0.0
            child_results = []
            for index, request in enumerate(requests, start=1):
                execution_result = self.engine.execute_limit_ladder(
                    request=request,
                    order_wait_seconds=self.config.order_wait_seconds,
                    cancel_wait_seconds=self.config.cancel_wait_seconds,
                )
                child_results.append(execution_result)
                filled_qty += execution_result.filled_qty
                logger.info("Short put child execution result: child=%s/%s, result=%s", index, len(requests), execution_result)
                if execution_result.execution_status != "success":
                    logger.warning(
                        "Short put execution stopped after failed child order: child=%s/%s, requested_qty=%s, filled_qty=%s.",
                        index,
                        len(requests),
                        requested_qty,
                        filled_qty,
                    )
                    break

            result_summary = build_execution_result_summary(
                title="SHORT PUT RESULT",
                code=selected_option["code"],
                name=option_name,
                requested_qty=requested_qty,
                filled_qty=filled_qty,
                child_results=child_results,
                target_mid=price_ladder[0] if price_ladder else None,
            )
            retry_markup = None
            if filled_qty < requested_qty:
                retry_markup = {
                    "inline_keyboard": [
                        [
                            {"text": "🔄 Retry", "callback_data": f"strategy:{self.strategy_id}:retry:execute_short_put_strategy"},
                            {"text": "❌ Cancel", "callback_data": f"strategy:{self.strategy_id}:cancel"},
                        ]
                    ]
                }
            self.engine.telegram.send_message(
                result_summary,
                reply_markup=retry_markup,
                parse_mode="HTML",
            )
            logger.info("Short put execution completed: requested_qty=%s, filled_qty=%s, child_orders=%s.", requested_qty, filled_qty, len(requests))
        finally:
            with self.lock:
                self._short_put_execution_active = False

    ####################################################################################################
    # Cut-Loss Workflow
    ####################################################################################################

    def setup_cut_loss_monitor(self) -> None:
        if self.config.stop_loss_multiple is None or self.config.stop_loss_multiple <= 0:
            logger.warning("Cut-loss monitor disabled because stop_loss_multiple=%s.", self.config.stop_loss_multiple)
            with self.lock:
                self._cut_loss_watchlist = {}
                self.trading_status["cut_loss_setup"] = False
            return

        if not self.update_put_position():
            logger.warning("Cut-loss monitor setup failed: unable to refresh put positions.")
            with self.lock:
                self.trading_status["cut_loss_setup"] = False
            return

        watchlist = self._build_cut_loss_watchlist()
        if not watchlist:
            logger.info("Cut-loss monitor setup completed: no short put positions to monitor.")
            with self.lock:
                self._cut_loss_watchlist = {}
                self.trading_status["cut_loss_setup"] = True
            return

        codes = list(watchlist)
        if not self.engine.subscribe(codes, [SubType.QUOTE, SubType.ORDER_BOOK], subscribe_push=True):
            logger.warning("Cut-loss monitor setup failed: unable to subscribe short put positions.")
            with self.lock:
                self.trading_status["cut_loss_setup"] = False
            return

        self._populate_cut_loss_ticks(watchlist)
        with self.lock:
            self._cut_loss_watchlist = watchlist
            self.trading_status["cut_loss_setup"] = True

        logger.info(
            "Cut-loss monitor setup completed: monitored_codes=%s, stop_prices=%s.",
            codes,
            {code: watch.stop_price for code, watch in watchlist.items()},
        )

    def execute_cut_loss(self, watch: CutLossWatch, order_book: dict[str, Any], mid_signal_price: float) -> None:
        if watch.price_tick is None or watch.stop_price is None:
            logger.warning("Cut-loss execution skipped because price tick is unavailable: code=%s.", watch.code)
            return

        bid_price = float(order_book["bid_price"])
        ask_price = float(order_book["ask_price"])

        open_orders = self.engine.order_list_query(acc_id=self.acc_id, code=watch.code, status_filter_list=OPEN_ORDER_STATUSES)
        if open_orders is None:
            logger.warning("Cut-loss execution skipped because open order query failed: code=%s.", watch.code)
            return
        if not open_orders.empty:
            logger.warning("Cut-loss execution skipped because open orders already exist: code=%s, count=%s.", watch.code, len(open_orders))
            return

        requests = self.cut_loss_execution_checklist(watch, bid_price, ask_price)
        if not requests:
            logger.warning("Cut-loss execution checklist rejected: code=%s.", watch.code)
            return

        requested_qty = sum(request.qty for request in requests)
        price_ladder = requests[0].price if isinstance(requests[0].price, list) else [requests[0].price]
        requires_approval = self.config.telegram_approval.get("cut_loss", True)
        approval_summary = build_cut_loss_summary(
            code=watch.code,
            name=self.resolve_option_info(watch.option),
            total_qty=requested_qty,
            child_qtys=[request.qty for request in requests],
            order_book=order_book,
            average_price=watch.average_price,
            stop_price=watch.stop_price,
            mid_signal_price=mid_signal_price,
            prices=price_ladder,
            final_line="🫡 Approve this cut-loss order?" if requires_approval else "🛎️ Executing the above order...",
        )
        if requires_approval:
            approved = self.engine.telegram.request_trade_approval(
                approval_summary,
                timeout_seconds=self.config.telegram_approval_timeout,
            )
            if not approved:
                logger.warning("Cut-loss execution skipped because Telegram trade approval was not granted: code=%s.", watch.code)
                return
        else:
            self.engine.telegram.send_message(approval_summary, parse_mode="HTML")

        filled_qty = 0.0
        child_results = []
        for index, request in enumerate(requests, start=1):
            execution_result = self.engine.execute_limit_ladder(
                request=request,
                order_wait_seconds=self.config.order_wait_seconds,
                cancel_wait_seconds=self.config.cancel_wait_seconds,
            )
            child_results.append(execution_result)
            filled_qty += execution_result.filled_qty
            logger.info("Cut-loss child execution result: child=%s/%s, result=%s", index, len(requests), execution_result)
            if execution_result.execution_status != "success":
                logger.warning(
                    "Cut-loss execution stopped after failed child order: code=%s, child=%s/%s, requested_qty=%s, filled_qty=%s.",
                    watch.code,
                    index,
                    len(requests),
                    requested_qty,
                    filled_qty,
                )
                break

        self.engine.telegram.send_message(
            build_execution_result_summary(
                title="CUT LOSS RESULT",
                code=watch.code,
                name=self.resolve_option_info(watch.option),
                requested_qty=requested_qty,
                filled_qty=filled_qty,
                child_results=child_results,
                target_mid=price_ladder[0] if price_ladder else None,
            ),
            parse_mode="HTML",
        )
        logger.info("Cut-loss execution completed: requested_qty=%s, filled_qty=%s, child_orders=%s.", requested_qty, filled_qty, len(requests))
        self._refresh_cut_loss_watchlist(watch.code)

    ####################################################################################################
    # Underlying Assignment Detection / Execution
    ####################################################################################################

    def on_order_status_underlying(self, data: pd.DataFrame) -> None:
        required_columns = {"code", "trd_side", "qty", "price", "order_status", "order_id"}
        missing_columns = required_columns - set(data.columns)
        if missing_columns:
            logger.warning("Underlying assignment detector skipped malformed order status data: missing_columns=%s", sorted(missing_columns))
            return

        with self.lock:
            maturing_strikes = list(self._maturing_put_option_strike)
        if not maturing_strikes:
            return

        market_state = self.get_underlying_market_state()
        if market_state not in [MarketState.AFTER_HOURS_BEGIN, MarketState.AFTER_HOURS_END]:
            return

        for _, row in data.iterrows():
            try:
                self._check_underlying_assignment_order(row, maturing_strikes, market_state)
            except Exception as exc:
                logger.warning("Underlying assignment detector skipped malformed order status row: error=%s, row=%s", exc, row.to_dict())

    def alert_assignment_at_close(self) -> None:
        with self.lock:
            maturing_strikes = list(self._maturing_put_option_strike)
        if not maturing_strikes:
            logger.info("Assignment close alert skipped because no maturing put strikes are tracked.")
            return

        if not self.update_put_position():
            logger.warning("Assignment close alert failed: unable to refresh short put positions.")
            return

        today = pd.Timestamp.today().date()
        assigned_contracts_by_strike: dict[float, int] = {}
        for option in self._put_option_position:
            if option.type != "put" or option.qty is None or option.qty >= 0:
                continue
            if option.expiration is None or option.strike is None:
                continue
            if pd.to_datetime(option.expiration).date() != today:
                continue
            if option.strike not in maturing_strikes:
                continue
            assigned_contracts_by_strike.setdefault(float(option.strike), 0)
            assigned_contracts_by_strike[float(option.strike)] += int(abs(float(option.qty)))

        if not assigned_contracts_by_strike:
            logger.info("Assignment close alert skipped because no tracked maturing short puts are open.")
            return

        if not self.engine.subscribe([self.config.underlying], [SubType.QUOTE], subscribe_push=False):
            logger.warning("Assignment close alert failed: unable to subscribe underlying quote. code=%s", self.config.underlying)
            return

        quote = self.engine.get_stock_quote([self.config.underlying])
        if quote is None or quote.empty:
            logger.warning("Assignment close alert failed: underlying quote unavailable. code=%s", self.config.underlying)
            return
        if "code" in quote.columns:
            quote = quote[quote["code"] == self.config.underlying]
        if quote.empty or "last_price" not in quote.columns:
            logger.warning("Assignment close alert failed: underlying last_price unavailable. code=%s", self.config.underlying)
            return

        underlying_price = self.engine._valid_positive_float(quote.iloc[0]["last_price"])
        if underlying_price is None:
            logger.warning(
                "Assignment close alert failed: underlying last_price is invalid. code=%s, last_price=%s",
                self.config.underlying,
                quote.iloc[0]["last_price"],
            )
            return

        assigned_strikes = {strike: qty for strike, qty in assigned_contracts_by_strike.items() if underlying_price < strike}
        if not assigned_strikes:
            logger.info(
                "Assignment close alert skipped because underlying is not below tracked maturing strikes: code=%s, price=%s, strikes=%s.",
                self.config.underlying,
                underlying_price,
                sorted(assigned_contracts_by_strike),
            )
            return

        lines = [
            "<b>🚨 SHORT PUT ASSIGNMENT DETECTED</b>\n",
            f"Underlying: {self.config.underlying}",
            f"Underlying Price: {underlying_price:.2f}",
        ]
        for strike, contracts in sorted(assigned_strikes.items()):
            lines.extend(
                [
                    "",
                    f"Strike: {strike:.2f}",
                    f"Contracts: {contracts}",
                ]
            )

        self.engine.telegram.send_message("\n".join(lines), parse_mode="HTML")
        logger.warning(
            "Assignment close alert sent: code=%s, underlying_price=%s, assigned_strikes=%s.",
            self.config.underlying,
            underlying_price,
            assigned_strikes,
        )

    def execute_underlying_assignment(self, method: Literal["market_order", "price_ladder"], assignment_token: str) -> None:
        if method not in {"market_order", "price_ladder"}:
            logger.warning("Unknown underlying assignment liquidation method: method=%s, token=%s.", method, assignment_token)
            return

        with self.lock:
            pending_action = self._pending_assignment_actions.get(assignment_token)
            if pending_action is None:
                logger.warning("Underlying assignment liquidation skipped because action token expired: token=%s.", assignment_token)
                return
            expires_at = pending_action.get("expires_at")
            if expires_at is not None and pd.Timestamp.now() >= pd.Timestamp(expires_at):
                self._pending_assignment_actions.pop(assignment_token, None)
                logger.warning("Underlying assignment liquidation skipped because action token expired: token=%s.", assignment_token)
                return

        with self.lock:
            pending_action = self._pending_assignment_actions.get(assignment_token)
            if pending_action is None:
                logger.warning("Underlying assignment liquidation skipped because action token expired: token=%s.", assignment_token)
                return
            if pending_action.get("action_status") in {
                "market_order_executing",
                "market_order_completed",
                "price_ladder_executing",
                "price_ladder_completed",
            }:
                logger.warning(
                    "Underlying assignment liquidation skipped because action is already %s: token=%s.",
                    pending_action.get("action_status"),
                    assignment_token,
                )
                return
            pending_action["action_status"] = f"{method}_executing"

        code = str(pending_action.get("code", self.config.underlying))
        assignment_qty = int(float(pending_action.get("qty", 0)))
        if code != self.config.underlying or assignment_qty <= 0:
            self._fail_underlying_assignment(assignment_token, code, "Invalid assignment action data.")
            return

        position = self.engine.get_open_position(self.acc_id, code=self.config.underlying, refresh_cache=True)
        if position is None:
            self._fail_underlying_assignment(assignment_token, code, "Unable to query current underlying position.")
            return
        if position.empty:
            self._fail_underlying_assignment(assignment_token, code, "No current underlying position found.")
            return
        if "can_sell_qty" not in position.columns:
            self._fail_underlying_assignment(assignment_token, code, "Current underlying sellable quantity is unavailable.")
            return

        sellable_qty = int(sum(float(qty) for qty in position["can_sell_qty"] if float(qty) > 0))
        sell_qty = min(assignment_qty, sellable_qty)
        if sell_qty <= 0:
            self._fail_underlying_assignment(assignment_token, code, "No sellable underlying shares available to liquidate.")
            return
        if sell_qty < assignment_qty:
            logger.warning(
                "Underlying assignment liquidation quantity capped by sellable position: code=%s, assignment_qty=%s, sellable_qty=%s, sell_qty=%s.",
                code,
                assignment_qty,
                sellable_qty,
                sell_qty,
            )

        open_orders = self.engine.order_list_query(acc_id=self.acc_id, code=self.config.underlying, status_filter_list=OPEN_ORDER_STATUSES)
        if open_orders is None:
            self._fail_underlying_assignment(assignment_token, code, "Unable to query open underlying orders.")
            return
        if not open_orders.empty:
            self._fail_underlying_assignment(assignment_token, code, f"Open underlying orders already exist: count={len(open_orders)}.")
            return

        if not self.engine.subscribe([self.config.underlying], [SubType.QUOTE, SubType.ORDER_BOOK], subscribe_push=False):
            self._fail_underlying_assignment(assignment_token, code, "Unable to subscribe underlying order book.")
            return

        order_book = self.engine.get_top_order_book(self.config.underlying)
        if order_book is None:
            self._fail_underlying_assignment(assignment_token, code, "Underlying top order book is unavailable.")
            return

        bid_price = self.engine._valid_positive_float(order_book.get("bid_price"))
        if bid_price is None:
            self._fail_underlying_assignment(assignment_token, code, "Underlying bid price is unavailable.")
            return

        if method == "price_ladder":
            ask_price = self.engine._valid_positive_float(order_book.get("ask_price"))
            if ask_price is None or ask_price < bid_price:
                self._fail_underlying_assignment(assignment_token, code, "Underlying ask price is unavailable or invalid.")
                return

            quote = self.engine.get_stock_quote([self.config.underlying])
            if quote is None or quote.empty:
                self._fail_underlying_assignment(assignment_token, code, "Unable to query underlying quote for price tick.")
                return
            if "code" in quote.columns:
                quote = quote[quote["code"] == self.config.underlying]
            if quote.empty or "price_spread" not in quote.columns:
                self._fail_underlying_assignment(assignment_token, code, "Underlying price tick is unavailable.")
                return

            price_tick = self.engine._valid_positive_float(quote.iloc[0]["price_spread"])
            if price_tick is None:
                self._fail_underlying_assignment(assignment_token, code, "Underlying price tick is invalid.")
                return

            prices = build_price_ladder(
                side="sell",
                code=self.config.underlying,
                bid_price=bid_price,
                ask_price=ask_price,
                price_tick=price_tick,
                steps=self.config.price_ladder_steps,
            )
            if not prices:
                self._fail_underlying_assignment(assignment_token, code, "Underlying price ladder is empty.")
                return

            request = LimitOrderRequest(
                acc_id=self.acc_id,
                code=self.config.underlying,
                side=TrdSide.SELL,
                qty=sell_qty,
                price=prices,
                remark="assignment_price_ladder",
            )
            logger.warning(
                "Submitting underlying assignment price ladder sell: code=%s, qty=%s, prices=%s, token=%s.",
                request.code,
                request.qty,
                request.price,
                assignment_token,
            )
            result = self.engine.execute_limit_ladder(
                request=request,
                order_wait_seconds=self.config.order_wait_seconds,
                cancel_wait_seconds=self.config.cancel_wait_seconds,
            )
            target_mid = prices[0]
        else:
            request = LimitOrderRequest(
                acc_id=self.acc_id,
                code=self.config.underlying,
                side=TrdSide.SELL,
                qty=sell_qty,
                price=bid_price,
                remark="assignment_market_order",
            )
            logger.warning(
                "Submitting underlying assignment marketable limit sell: code=%s, qty=%s, price=%s, token=%s.",
                request.code,
                request.qty,
                request.price,
                assignment_token,
            )
            result = self.engine.execute_limit_order(
                request=request,
                order_wait_seconds=self.config.order_wait_seconds,
                cancel_wait_seconds=self.config.cancel_wait_seconds,
            )
            target_mid = bid_price

        with self.lock:
            pending_action = self._pending_assignment_actions.get(assignment_token)
            if pending_action is not None:
                pending_action["action_status"] = f"{method}_completed" if result.execution_status == "success" else f"{method}_failed"
                pending_action["liquidation_order_id"] = result.order_id
                pending_action["liquidation_requested_qty"] = sell_qty
                pending_action["liquidation_filled_qty"] = result.filled_qty
                pending_action["liquidation_status"] = result.order_status

        retry_markup = None
        if result.filled_qty < sell_qty:
            retry_markup = {
                "inline_keyboard": [
                    [
                        {"text": "🔄 Retry", "callback_data": f"assignment:{self.strategy_id}:{method}:{assignment_token}"},
                        {"text": "❌ Cancel", "callback_data": f"assignment:{self.strategy_id}:cancel:{assignment_token}"},
                    ]
                ]
            }
        self.engine.telegram.send_message(
            build_execution_result_summary(
                title="ASSIGNMENT LIQUIDATION RESULT",
                code=self.config.underlying,
                name=self.config.underlying,
                requested_qty=sell_qty,
                filled_qty=result.filled_qty,
                child_results=[result],
                target_mid=target_mid,
            ),
            reply_markup=retry_markup,
            parse_mode="HTML",
        )
        logger.warning(
            "Underlying assignment liquidation finished: method=%s, code=%s, requested_qty=%s, filled_qty=%s, result=%s.",
            method,
            self.config.underlying,
            sell_qty,
            result.filled_qty,
            result,
        )

    def _check_underlying_assignment_order(self, row: pd.Series, maturing_strikes: list[float], market_state: object) -> None:
        code = row["code"]
        trd_side = row["trd_side"]
        order_status = row["order_status"]
        order_id = str(row["order_id"])

        if code != self.config.underlying:
            return
        if trd_side != TrdSide.BUY:
            return
        if order_status != OrderStatus.FILLED_ALL:
            return

        qty = float(row["qty"])
        price = float(row["price"])
        if qty <= 0 or qty % 100 != 0:
            return

        matched_strike = self._match_maturing_put_strike(price, maturing_strikes)
        if matched_strike is None:
            return

        detected_at = pd.Timestamp.now()
        expires_at = detected_at + pd.Timedelta(seconds=self.config.assignment_action_timeout)
        assignment_token = secrets.token_urlsafe(8)
        with self.lock:
            if order_id in self._underlying_alerted_order_ids:
                return
            self._underlying_alerted_order_ids.add(order_id)
            self._pending_assignment_actions[assignment_token] = {
                "order_id": order_id,
                "code": code,
                "qty": qty,
                "price": price,
                "matched_strike": matched_strike,
                "market_state": market_state,
                "detected_at": detected_at,
                "expires_at": expires_at,
            }

        message = build_assignment_summary(
            code=code,
            side=trd_side,
            price=price,
            qty=qty,
            matched_strike=matched_strike,
            market_state=market_state,
            detected_at=detected_at,
            final_line="🫡 Liquidate this position?",
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "🚬 Liquidate", "callback_data": f"assignment:{self.strategy_id}:liquidate:{assignment_token}"},
                    {"text": "❌ Cancel", "callback_data": f"assignment:{self.strategy_id}:cancel:{assignment_token}"},
                ]
            ]
        }
        logger.warning("Sent potential put assignment alert via Telegram.")
        self.engine.telegram.send_message(message, reply_markup=reply_markup, parse_mode="HTML")

    def _match_maturing_put_strike(self, price: float, maturing_strikes: list[float]) -> float | None:
        for strike in maturing_strikes:
            if abs(float(price) - float(strike)) <= 0.05:
                return float(strike)
        return None

    def _fail_underlying_assignment(self, assignment_token: str, code: str, reason: str) -> None:
        with self.lock:
            pending_action = self._pending_assignment_actions.get(assignment_token)
            if pending_action is not None:
                action_status = str(pending_action.get("action_status", ""))
                pending_action["action_status"] = "price_ladder_failed" if action_status.startswith("price_ladder") else "market_order_failed"
                pending_action["failure_reason"] = reason

        logger.warning("Underlying assignment liquidation skipped: code=%s, reason=%s, token=%s.", code, reason, assignment_token)
        message = f"<b>🚨 Assignment liquidation skipped.</b>\nCode: {code}\nReason: {reason}"
        self.engine.telegram.send_message(message, parse_mode="HTML")

    ####################################################################################################
    # Execution Checklists / Order Request Builders
    ####################################################################################################

    def cut_loss_execution_checklist(self, watch: CutLossWatch, bid_price: float, ask_price: float) -> list[LimitOrderRequest] | None:
        if watch.qty <= 0:
            logger.warning("Cut-loss execution checklist failed: qty is non-positive. code=%s, qty=%s.", watch.code, watch.qty)
            return None
        if watch.price_tick is None or watch.price_tick <= 0:
            logger.warning("Cut-loss execution checklist failed: invalid price tick. code=%s, price_tick=%s.", watch.code, watch.price_tick)
            return None
        if bid_price <= 0 or ask_price <= 0 or ask_price < bid_price:
            logger.warning(
                "Cut-loss execution checklist failed: invalid bid/ask. code=%s, bid=%s, ask=%s.",
                watch.code,
                bid_price,
                ask_price,
            )
            return None

        price_ladder = build_price_ladder(
            side="buy",
            code=watch.code,
            bid_price=bid_price,
            ask_price=ask_price,
            price_tick=watch.price_tick,
            steps=self.config.price_ladder_steps,
        )
        if not price_ladder:
            logger.warning("Cut-loss execution checklist failed: empty price ladder. code=%s.", watch.code)
            return None

        child_qty_cap = watch.qty
        if self.config.max_contracts_per_trade is not None:
            child_qty_cap = min(child_qty_cap, int(self.config.max_contracts_per_trade))
        if child_qty_cap <= 0:
            logger.warning("Cut-loss execution checklist failed: child qty cap is non-positive. code=%s.", watch.code)
            return None

        requests = []
        remaining_qty = int(watch.qty)
        while remaining_qty > 0:
            child_qty = min(child_qty_cap, remaining_qty)
            requests.append(
                LimitOrderRequest(
                    acc_id=self.acc_id,
                    code=watch.code,
                    side=TrdSide.BUY,
                    qty=child_qty,
                    price=price_ladder,
                    remark="cut_loss",
                )
            )
            remaining_qty -= child_qty

        logger.info(
            "Cut-loss execution checklist passed: code=%s, total_qty=%s, child_qtys=%s, stop_price=%s, bid=%.2f, ask=%.2f, prices=%s",
            watch.code,
            watch.qty,
            [request.qty for request in requests],
            watch.stop_price,
            bid_price,
            ask_price,
            price_ladder,
        )
        return requests

    def short_put_execution_checklist(self, selected_option: pd.Series, max_short: int) -> list[LimitOrderRequest] | None:
        if max_short <= 0:
            logger.warning("Short put execution checklist failed: max_short is non-positive. max_short=%s.", max_short)
            return None

        required_fields = [
            "code",
            "name",
            "bid_price",
            "ask_price",
            "bid_volume",
            "price_spread",
        ]
        missing_fields = [field for field in required_fields if field not in selected_option.index]
        if missing_fields:
            logger.warning("Short put execution checklist failed: missing fields=%s", missing_fields)
            return None

        option_info = self.resolve_option_name(selected_option["name"], TrdEnv.REAL)
        if option_info is None or option_info.type != "put" or option_info.strike is None:
            logger.warning("Short put execution checklist failed: invalid option name=%s.", selected_option["name"])
            return None

        bid_price = float(selected_option["bid_price"])
        ask_price = float(selected_option["ask_price"])
        bid_volume = float(selected_option["bid_volume"])
        price_tick = float(selected_option["price_spread"])

        if bid_price <= 0 or ask_price <= 0 or ask_price < bid_price:
            logger.warning(
                "Short put execution checklist failed: invalid bid/ask. bid=%s, ask=%s, option=%s.",
                bid_price,
                ask_price,
                selected_option["name"],
            )
            return None
        if price_tick <= 0:
            logger.warning(
                "Short put execution checklist failed: invalid price tick. price_tick=%s, option=%s.",
                price_tick,
                selected_option["name"],
            )
            return None

        mid_price = (bid_price + ask_price) / 2
        spread = ask_price - bid_price
        spread_pct = spread / mid_price if mid_price > 0 else float("inf")
        if spread_pct > self.config.max_spread_pct:
            logger.warning(
                "Short put execution checklist failed: spread too wide. spread_pct=%.4f, max_spread_pct=%.4f, bid=%s, ask=%s, option=%s.",
                spread_pct,
                self.config.max_spread_pct,
                bid_price,
                ask_price,
                selected_option["name"],
            )
            return None

        if bid_price < self.config.min_credit:
            logger.warning(
                "Short put execution checklist failed: bid below minimum credit. bid=%s, min_credit=%s, option=%s.",
                bid_price,
                self.config.min_credit,
                selected_option["name"],
            )
            return None

        participation_qty = int(bid_volume * self.config.max_order_book_participation)
        if participation_qty <= 0:
            logger.warning(
                "Short put execution checklist failed: participation qty is non-positive. bid_volume=%s, participation_rate=%s, participation_qty=%s.",
                bid_volume,
                self.config.max_order_book_participation,
                participation_qty,
            )
            return None

        child_qty_caps = [participation_qty]
        if self.config.max_contracts_per_trade is not None:
            child_qty_caps.append(self.config.max_contracts_per_trade)

        child_qty_cap = int(min(child_qty_caps))
        if child_qty_cap <= 0:
            logger.warning(
                "Short put execution checklist failed: child qty cap is non-positive. max_short=%s, max_contracts_per_trade=%s, participation_qty=%s.",
                max_short,
                self.config.max_contracts_per_trade,
                participation_qty,
            )
            return None

        price_ladder = build_price_ladder(
            side="sell",
            code=selected_option["code"],
            bid_price=bid_price,
            ask_price=ask_price,
            price_tick=price_tick,
            steps=self.config.price_ladder_steps,
        )
        requests = []
        remaining_qty = int(max_short)
        while remaining_qty > 0:
            child_qty = min(child_qty_cap, remaining_qty)
            requests.append(
                LimitOrderRequest(
                    acc_id=self.acc_id,
                    code=selected_option["code"],
                    side=TrdSide.SELL,
                    qty=child_qty,
                    price=price_ladder,
                )
            )
            remaining_qty -= child_qty

        logger.info(
            "Short put execution checklist passed: code=%s, total_qty=%s, child_qtys=%s, price_tick=%s, bid=%.2f, ask=%.2f, spread_pct=%.4f, prices=%s",
            selected_option["code"],
            max_short,
            [request.qty for request in requests],
            price_tick,
            bid_price,
            ask_price,
            spread_pct,
            price_ladder,
        )
        return requests

    ####################################################################################################
    # Option Selection / Quote Discovery Helpers
    ####################################################################################################

    def select_short_put(self) -> pd.Series:
        target_exp_days = self.config.target_exp_days
        attempted_expirations = set()

        while True:
            expiration = self._get_option_target_expiration(
                self.config.underlying,
                target_exp_days,
                direction=self.config.expiration_direction,
            )
            if expiration is None:
                logger.warning("No suitable expiration found.")
                return pd.Series(dtype="object")

            expiration_date, days_to_mature = expiration
            if expiration_date in attempted_expirations:
                logger.warning("No later suitable expiration found after trying: %s", sorted(attempted_expirations))
                return pd.Series(dtype="object")
            attempted_expirations.add(expiration_date)

            delta_min = max(self.config.target_delta * 0.8, 0.01)
            delta_max = self.config.target_delta * 1.2
            option_codes = self._get_put_option_codes_by_delta(
                self.config.underlying,
                expiration_date,
                abs_delta_min=delta_min,
                abs_delta_max=delta_max,
            )
            if option_codes is None:
                logger.warning("Unable to query option chain for %s.", expiration_date)
                return pd.Series(dtype="object")
            selected = self._get_target_option_quote(option_codes, self.config.target_delta)
            if not selected.empty:
                return selected

            target_exp_days = days_to_mature + 1
            logger.info("No target option found for %s. Retrying with target_exp_days=%s.", expiration_date, target_exp_days)

    def _get_option_expirations(self, code: str) -> pd.DataFrame | None:
        ret, data = self.engine.quote_context.get_option_expiration_date(code=code)
        if ret != RET_OK:
            logger.error("Get option expirations failed: %s", data)
            return None

        today = pd.to_datetime("today").normalize()
        result = data[["strike_time"]].copy()
        result["date_distance"] = result["strike_time"].apply(lambda x: (pd.to_datetime(x) - today).days)
        return result

    def _get_option_target_expiration(
        self,
        code: str,
        target_distance: int,
        direction: Literal["closest", "smaller", "larger"] = "larger",
    ) -> Tuple[str, int] | None:
        expiration_df = self._get_option_expirations(code)
        if expiration_df is None or expiration_df.empty:
            return None

        if direction == "smaller":
            filtered_df = expiration_df[expiration_df["date_distance"] <= target_distance]
            if filtered_df.empty:
                return None
            target_expiration = filtered_df.loc[filtered_df["date_distance"].idxmax()]
        elif direction == "larger":
            filtered_df = expiration_df[expiration_df["date_distance"] >= target_distance]
            if filtered_df.empty:
                return None
            target_expiration = filtered_df.loc[filtered_df["date_distance"].idxmin()]
        else:
            target_expiration = expiration_df.loc[(expiration_df["date_distance"] - target_distance).abs().argsort().iloc[0]]

        logger.info(f"Target expiration: {target_expiration['strike_time']} (DTE: {target_expiration['date_distance']} days)")
        return (target_expiration["strike_time"], int(target_expiration["date_distance"]))

    def _get_put_option_codes_by_delta(
        self,
        code: str,
        expiration_date: str,
        abs_delta_min: float = 0.1,
        abs_delta_max: float = 0.3,
    ) -> List[str] | None:
        data_filter = OptionDataFilter(delta_min=-abs_delta_max, delta_max=-abs_delta_min)
        ret, data = self.engine.quote_context.get_option_chain(code=code, start=expiration_date, end=expiration_date, data_filter=data_filter)
        if ret != RET_OK:
            logger.error("Get option chain failed: %s", data)
            return None

        option_codes = data["code"].tolist()
        if not option_codes:
            logger.warning("No option contracts matched the delta filter.")
            return []

        if not self.engine.subscribe(option_codes, [SubType.QUOTE, SubType.ORDER_BOOK], subscribe_push=False):
            return []

        return option_codes

    def _get_target_option_quote(self, codes: list[str], target_delta: float) -> pd.Series:
        if not codes:
            return pd.Series(dtype="object")

        data = self.engine.get_stock_quote(codes)
        if data is None or data.empty:
            return pd.Series(dtype="object")

        result = data[["code", "name", "volume", "implied_volatility", "delta", "price_spread"]].copy()
        result = result[result["volume"] > self.config.min_volume].reset_index(drop=True)
        if result.empty:
            logger.warning("No option contracts passed the liquidity filter.")
            return pd.Series(dtype="object")
        selected = result.loc[(result["delta"].abs() - target_delta).abs().argsort().iloc[0]].copy()

        for _ in range(5):
            order_book = self.engine.get_top_order_book(selected["code"])
            if order_book:
                break
            sleep(0.5)
        if not order_book:
            return pd.Series(dtype="object")

        for key, value in order_book.items():
            selected[key] = value

        selected.name = selected["name"]
        logger.info("Selected option: %s", selected["name"])
        logger.info(f"Volume: {selected['volume']}, Delta: {selected['delta']:.4f}, Implied Volatility: {selected['implied_volatility'] / 100:.2%}")
        logger.info(f"Top Order Book: {selected['bid_volume']} @ {selected['bid_price']} | {selected['ask_price']} @ {selected['ask_volume']}")
        return selected

    ####################################################################################################
    # Cut-Loss State Helpers
    ####################################################################################################

    def _build_cut_loss_watchlist(self) -> dict[str, CutLossWatch]:
        watchlist = {}
        for position in self._put_option_position:
            if not position.code or not position.qty or position.qty >= 0:
                continue
            average_price = self.engine._valid_positive_float(position.price)
            if average_price is None:
                logger.warning("Cut-loss monitor skipped position with invalid average price: code=%s, price=%s.", position.code, position.price)
                continue
            watchlist[position.code] = CutLossWatch(
                option=position,
                qty=int(abs(position.qty)),
                average_price=average_price,
            )
        return watchlist

    def _populate_cut_loss_ticks(self, watchlist: dict[str, CutLossWatch]) -> None:
        quotes = self.engine.get_stock_quote(list(watchlist))
        if quotes is None or quotes.empty:
            logger.warning("Cut-loss monitor setup could not load price ticks from quotes.")
            return

        for _, row in quotes.iterrows():
            code = row["code"] if "code" in row.index else None
            if code not in watchlist or "price_spread" not in row.index:
                continue
            price_tick = self.engine._valid_positive_float(row["price_spread"])
            if price_tick is None:
                continue
            watch = watchlist[code]
            watch.price_tick = price_tick
            watch.stop_price = round_up_to_tick(watch.average_price * float(self.config.stop_loss_multiple), price_tick)

    def _refresh_cut_loss_watchlist(self, code: str) -> None:
        if not self.update_put_position():
            logger.warning("Cut-loss watch refresh failed: code=%s.", code)
            return

        refreshed = self._build_cut_loss_watchlist()
        self._populate_cut_loss_ticks(refreshed)
        with self.lock:
            if code not in refreshed:
                self._cut_loss_watchlist.pop(code, None)
                logger.info("Cut-loss watch removed after execution: code=%s.", code)
                return
            self._cut_loss_watchlist[code] = refreshed[code]

    ####################################################################################################
    # Account / Risk / Position State Helpers
    ####################################################################################################

    def get_underlying_market_state(self) -> object | None:
        market_state = self.engine.get_market_state(self.config.underlying)
        if market_state is None or market_state.empty or "market_state" not in market_state.columns:
            logger.warning("Market state is unavailable for %s.", self.config.underlying)
            return None
        return market_state.iloc[0]["market_state"]

    def get_total_cash(self) -> float | None:
        account_info = self.engine.get_account_info(self.acc_id)
        if account_info is None or account_info.empty:
            return None

        res = account_info.iloc[0].copy()
        capital = res["fund_assets"] + res["cash"] if self.acc_id == self.engine.margin_account else res["cash"]
        if self.config.max_capital is not None:
            capital = min(capital, self.config.max_capital)

        return capital

    def get_leverage_ratio(self) -> float | None:
        total_cash = self.get_total_cash()
        if not total_cash:
            return total_cash

        option_notional = sum(abs(x.qty) * x.strike * 100 for x in self._put_option_position if x.qty < 0)
        return option_notional / total_cash

    def get_max_num_to_short(self, selected_option: pd.Series) -> int:
        total_cash = self.get_total_cash()
        if not total_cash or total_cash <= 0:
            logger.warning("Cannot calculate max short quantity because total cash is unavailable or non-positive: %s", total_cash)
            return 0

        required_fields = ["code", "name", "bid_price", "ask_price"]
        missing_fields = [field for field in required_fields if field not in selected_option.index]
        if missing_fields:
            logger.warning("Cannot calculate max short quantity because selected option is missing fields: %s", missing_fields)
            return 0

        option_info = self.resolve_option_name(selected_option["name"], TrdEnv.REAL)
        if option_info is None or option_info.strike is None or option_info.strike <= 0:
            logger.warning("Cannot calculate max short quantity because selected option strike is unavailable: %s", selected_option)
            return 0
        strike = option_info.strike

        option_notional = sum(abs(x.qty) * x.strike * 100 for x in self._put_option_position if x.qty < 0)
        max_allowed_notional = total_cash * self.config.leverage_ratio
        remaining_notional = max_allowed_notional - option_notional
        if remaining_notional <= 0:
            logger.info(
                "Current short put notional %.2f already reaches leverage cap %.2f.",
                option_notional,
                max_allowed_notional,
            )
            return 0

        bid_price = float(selected_option["bid_price"])
        ask_price = float(selected_option["ask_price"])
        if bid_price <= 0 or ask_price <= 0 or ask_price < bid_price:
            logger.warning(
                "Cannot calculate max short quantity because selected option bid/ask is invalid: code=%s, bid=%s, ask=%s",
                selected_option["code"],
                bid_price,
                ask_price,
            )
            return 0

        mid_price = (bid_price + ask_price) / 2
        futu_max_short = self.engine.get_max_short_quantity(
            acc_id=self.acc_id,
            code=selected_option["code"],
            price=mid_price,
        )
        if futu_max_short is None:
            logger.warning("Cannot calculate max short quantity because Futu max short quantity is unavailable.")
            return 0

        contract_notional = strike * 100
        strategy_max_short = int(remaining_notional // contract_notional)
        max_num_to_short = min(strategy_max_short, int(futu_max_short))
        logger.info(
            "Max contracts to short: %s. strategy_max_short=%s, futu_max_short=%s, total_cash=%.2f, current_notional=%.2f, selected_strike=%.2f, mid_price=%.2f",
            max_num_to_short,
            strategy_max_short,
            futu_max_short,
            total_cash,
            option_notional,
            strike,
            mid_price,
        )
        return max(0, max_num_to_short)

    def update_put_position(self) -> bool:
        position = self.engine.get_open_position(self.acc_id)
        if position is None:
            logger.error("Failed to update put positions: position query returned None.")
            return False
        if position.empty:
            self._put_option_position = []
            logger.info("Updated put positions: no open positions.")
            return True

        put_positions = []
        skipped_count = 0
        for _, row in position.iterrows():
            option_info = self.resolve_option_name(row["stock_name"], self.engine.trading_environment, row["code"], row["qty"], row["cost_price"])
            underlying_symbol = self.config.underlying.split(".")[-1]
            if option_info is None:
                skipped_count += 1
                continue
            if option_info.ticker != underlying_symbol:
                skipped_count += 1
                continue
            if option_info.type != "put":
                skipped_count += 1
                continue
            if option_info.qty >= 0:
                skipped_count += 1
                continue
            put_positions.append(option_info)

        self._put_option_position = put_positions
        logger.info(
            "Updated put positions: put_count=%s, skipped_count=%s, total_position_rows=%s",
            len(self._put_option_position),
            skipped_count,
            len(position),
        )
        return True

    def update_maturing_put_strikes(self) -> bool:
        if not self.update_put_position():
            with self.lock:
                self.trading_status["maturing_updated"] = False
            return False

        today = pd.Timestamp.today().date()
        maturing_strikes = []

        for option in self._put_option_position:
            if option.expiration is None or option.strike is None:
                continue

            expiration = pd.to_datetime(option.expiration).date()
            if expiration == today:
                maturing_strikes.append(option.strike)

        with self.lock:
            self._maturing_put_option_strike = maturing_strikes
            self.trading_status["maturing_updated"] = True

        return True

    ####################################################################################################
    # Option Parsing / Display Helpers
    ####################################################################################################

    def resolve_option_info(self, option_info: OptionInfo) -> str:
        if not option_info.ticker or not option_info.expiration or option_info.strike is None or not option_info.type:
            return option_info.code

        option_type = "Put" if option_info.type == "put" else "Call"
        return f"{option_info.ticker} {option_info.strike:.2f} {option_type} ({option_info.expiration})"

    def resolve_option_name(
        self, option_name: str, trading_environment: TrdEnv = TrdEnv.REAL, code: str = None, qty: float = None, price: float = None
    ) -> OptionInfo | None:
        pattern = OPTION_PATTERN.get(trading_environment, OPTION_PATTERN[TrdEnv.REAL])
        match = pattern.match(option_name)
        if not match:
            return None

        expiration = pd.to_datetime(match.group("date"), format="%y%m%d").date().isoformat()
        strike = float(match.group("strike"))
        if trading_environment == TrdEnv.SIMULATE:
            strike = strike / 1000

        return OptionInfo(
            code=code,
            ticker=match.group("symbol"),
            type="put" if match.group("type") == "P" else "call",
            strike=strike,
            expiration=expiration,
            qty=qty,
            price=price,
        )
