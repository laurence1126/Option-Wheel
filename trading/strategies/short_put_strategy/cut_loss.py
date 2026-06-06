from __future__ import annotations

from typing import TYPE_CHECKING, Any
from dataclasses import dataclass
from futu import SubType, TrdSide

from .utils.account_state import update_put_position
from .utils.option_parsing import OptionInfo, resolve_option_info

from trading.notification.telegram_summary import build_cut_loss_summary, build_execution_result_summary
from trading.trading_engine.execution_engine import LimitOrderRequest, OPEN_ORDER_STATUSES, build_price_ladder, round_up_to_tick
from app.utils.logging import configure_logger

if TYPE_CHECKING:
    from .strategy_main import ShortPutStrategy

logger = configure_logger(__name__)


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


def setup_cut_loss_monitor(strategy: ShortPutStrategy) -> bool:
    if strategy.config.stop_loss_multiple is None or strategy.config.stop_loss_multiple <= 0:
        logger.warning("Cut-loss monitor disabled because stop_loss_multiple=%s.", strategy.config.stop_loss_multiple)
        with strategy.lock:
            strategy._cut_loss_watchlist = {}
            strategy.trading_status["cut_loss_setup"] = False
        return True

    if not update_put_position(strategy):
        logger.warning("Cut-loss monitor setup failed: unable to refresh put positions.")
        with strategy.lock:
            strategy.trading_status["cut_loss_setup"] = False
        return False

    watchlist = _build_cut_loss_watchlist(strategy)
    if not watchlist:
        logger.info("Cut-loss monitor setup completed: no short put positions to monitor.")
        with strategy.lock:
            strategy._cut_loss_watchlist = {}
            strategy.trading_status["cut_loss_setup"] = True
        return True

    codes = list(watchlist)
    if not strategy.engine.subscribe(codes, [SubType.QUOTE, SubType.ORDER_BOOK], subscribe_push=True):
        logger.warning("Cut-loss monitor setup failed: unable to subscribe short put positions.")
        with strategy.lock:
            strategy.trading_status["cut_loss_setup"] = False
        return False

    _populate_cut_loss_ticks(strategy, watchlist)
    with strategy.lock:
        strategy._cut_loss_watchlist = watchlist
        strategy.trading_status["cut_loss_setup"] = True

    logger.info(
        "Cut-loss monitor setup completed: monitored_codes=%s, stop_prices=%s.",
        codes,
        {code: watch.stop_price for code, watch in watchlist.items()},
    )
    return True


def execute_cut_loss(strategy: ShortPutStrategy, watch: CutLossWatch, order_book: dict[str, Any], mid_signal_price: float) -> None:
    if watch.price_tick is None or watch.stop_price is None:
        logger.warning("Cut-loss execution skipped because price tick is unavailable: code=%s.", watch.code)
        return

    bid_price = strategy.engine.valid_positive_float(order_book["bid_price"])
    ask_price = strategy.engine.valid_positive_float(order_book["ask_price"])

    open_orders = strategy.engine.order_list_query(acc_id=strategy.acc_id, code=watch.code, status_filter_list=OPEN_ORDER_STATUSES)
    if open_orders is None:
        logger.warning("Cut-loss execution skipped because open order query failed: code=%s.", watch.code)
        return
    if not open_orders.empty:
        logger.warning("Cut-loss execution skipped because open orders already exist: code=%s, count=%s.", watch.code, len(open_orders))
        return

    requests = _build_execution_requests(strategy, watch, bid_price, ask_price)
    if not requests:
        logger.warning("Build execution requests failed: code=%s.", watch.code)
        return

    requested_qty = sum(request.qty for request in requests)
    price_ladder = requests[0].price if isinstance(requests[0].price, list) else [requests[0].price]
    requires_approval = strategy.config.telegram_approval.get("cut_loss", True)
    approval_summary = build_cut_loss_summary(
        code=watch.code,
        name=resolve_option_info(watch.option),
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
        approved = strategy.engine.telegram.request_trade_approval(
            approval_summary,
            timeout_seconds=strategy.config.telegram_approval_timeout,
        )
        if not approved:
            logger.warning("Cut-loss execution skipped because Telegram trade approval was not granted: code=%s.", watch.code)
            return
    else:
        strategy.engine.telegram.send_message(approval_summary, parse_mode="HTML")

    filled_qty = 0.0
    child_results = []
    for index, request in enumerate(requests, start=1):
        execution_result = strategy.engine.execute_limit_ladder(
            request=request,
            order_wait_seconds=strategy.config.order_wait_seconds,
            cancel_wait_seconds=strategy.config.cancel_wait_seconds,
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

    strategy.engine.telegram.send_message(
        build_execution_result_summary(
            title="CUT LOSS RESULT",
            code=watch.code,
            name=resolve_option_info(watch.option),
            requested_qty=requested_qty,
            filled_qty=filled_qty,
            child_results=child_results,
            target_mid=price_ladder[0] if price_ladder else None,
        ),
        parse_mode="HTML",
    )
    logger.info("Cut-loss execution completed: requested_qty=%s, filled_qty=%s, child_orders=%s.", requested_qty, filled_qty, len(requests))
    _refresh_cut_loss_watchlist(strategy, watch.code)


def _build_execution_requests(strategy: ShortPutStrategy, watch: CutLossWatch, bid_price: float, ask_price: float) -> list[LimitOrderRequest] | None:
    if watch.qty <= 0:
        logger.warning("Build execution requests failed: qty is non-positive. code=%s, qty=%s.", watch.code, watch.qty)
        return None
    if watch.price_tick is None or watch.price_tick <= 0:
        logger.warning("Build execution requests failed: invalid price tick. code=%s, price_tick=%s.", watch.code, watch.price_tick)
        return None
    if bid_price is None or ask_price is None or ask_price < bid_price:
        logger.warning(
            "Build execution requests failed: invalid bid/ask. code=%s, bid=%s, ask=%s.",
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
        steps=strategy.config.price_ladder_steps,
    )
    if not price_ladder:
        logger.warning("Build execution requests failed: empty price ladder. code=%s.", watch.code)
        return None

    child_qty_cap = watch.qty
    if strategy.config.max_contracts_per_trade is not None:
        child_qty_cap = min(child_qty_cap, int(strategy.config.max_contracts_per_trade))
    if child_qty_cap <= 0:
        logger.warning("Build execution requests failed: child qty cap is non-positive. code=%s.", watch.code)
        return None

    requests = []
    remaining_qty = int(watch.qty)
    while remaining_qty > 0:
        child_qty = min(child_qty_cap, remaining_qty)
        requests.append(
            LimitOrderRequest(
                acc_id=strategy.acc_id,
                code=watch.code,
                side=TrdSide.BUY,
                qty=child_qty,
                price=price_ladder,
                remark="cut_loss",
            )
        )
        remaining_qty -= child_qty

    logger.info(
        "Build execution requests passed: code=%s, total_qty=%s, child_qtys=%s, stop_price=%s, bid=%.2f, ask=%.2f, prices=%s",
        watch.code,
        watch.qty,
        [request.qty for request in requests],
        watch.stop_price,
        bid_price,
        ask_price,
        price_ladder,
    )
    return requests


def _build_cut_loss_watchlist(strategy: ShortPutStrategy) -> dict[str, CutLossWatch]:
    watchlist = {}
    for position in strategy._put_option_position:
        if not position.code or not position.qty or position.qty >= 0:
            continue
        average_price = strategy.engine.valid_positive_float(position.price)
        if average_price is None:
            logger.warning("Cut-loss monitor skipped position with invalid average price: code=%s, price=%s.", position.code, position.price)
            continue
        watchlist[position.code] = CutLossWatch(
            option=position,
            qty=int(abs(position.qty)),
            average_price=average_price,
        )
    return watchlist


def _populate_cut_loss_ticks(strategy: ShortPutStrategy, watchlist: dict[str, CutLossWatch]) -> None:
    quotes = strategy.engine.get_stock_quote(list(watchlist))
    if quotes is None or quotes.empty:
        logger.warning("Cut-loss monitor setup could not load price ticks from quotes.")
        return

    for _, row in quotes.iterrows():
        code = row["code"] if "code" in row.index else None
        if code not in watchlist or "price_spread" not in row.index:
            continue
        price_tick = strategy.engine.valid_positive_float(row["price_spread"])
        if price_tick is None:
            continue
        watch = watchlist[code]
        watch.price_tick = price_tick
        watch.stop_price = round_up_to_tick(watch.average_price * float(strategy.config.stop_loss_multiple), price_tick)


def _refresh_cut_loss_watchlist(strategy: ShortPutStrategy, code: str) -> None:
    if not update_put_position(strategy):
        logger.warning("Cut-loss watch refresh failed: code=%s.", code)
        return

    refreshed = _build_cut_loss_watchlist(strategy)
    _populate_cut_loss_ticks(strategy, refreshed)
    with strategy.lock:
        if code not in refreshed:
            strategy._cut_loss_watchlist.pop(code, None)
            logger.info("Cut-loss watch removed after execution: code=%s.", code)
            return
        strategy._cut_loss_watchlist[code] = refreshed[code]
