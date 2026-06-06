from __future__ import annotations

import pandas as pd
from typing import TYPE_CHECKING
from futu import TrdEnv, TrdSide, MarketState

from .utils.account_state import get_leverage_ratio, get_max_num_to_short, get_total_cash, get_underlying_market_state, update_put_position
from .utils.put_selection import select_short_put
from .utils.option_parsing import resolve_option_info, resolve_option_name

from trading.notification.telegram_summary import build_execution_result_summary, build_sell_put_summary
from trading.trading_engine.execution_engine import LimitOrderRequest, OPEN_ORDER_STATUSES, build_price_ladder
from app.utils.logging import configure_logger

if TYPE_CHECKING:
    from .strategy_main import ShortPutStrategy

logger = configure_logger(__name__)


def execute_short_put_strategy(strategy: ShortPutStrategy):
    with strategy.lock:
        if strategy._short_put_execution_active:
            logger.warning("Short put execution skipped because another run is already active.")
            return
        strategy._short_put_execution_active = True

    try:
        if not _execution_checklist(strategy):
            return

        selected_option = select_short_put(strategy)
        if selected_option.empty:
            logger.warning("No suitable short put candidate found.")
            return

        max_short = get_max_num_to_short(strategy, selected_option)
        if max_short <= 0:
            logger.warning("No capacity to short selected put.")
            return

        logger.info("Executing short put strategy...")
        requests = _build_execution_requests(strategy, selected_option, max_short)
        if requests is None:
            logger.warning("Short put execution checklist rejected. Skipping execution.")
            return

        requested_qty = sum(request.qty for request in requests)
        requires_approval = strategy.config.telegram_approval.get("short_put", True)
        price_ladder = list(requests[0].price_ladder_plan.prices) if requests else []
        option_info = resolve_option_name(selected_option["name"], TrdEnv.REAL)
        option_name = resolve_option_info(option_info) if option_info is not None else selected_option["name"]
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
            approved = strategy.engine.telegram.request_trade_approval(
                approval_summary,
                timeout_seconds=strategy.config.telegram_approval_timeout,
            )
            if not approved:
                logger.warning("Short put execution skipped because Telegram trade approval was not granted.")
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
                        {"text": "🔄 Retry", "callback_data": f"strategy:{strategy.strategy_id}:retry:execute_short_put_strategy"},
                        {"text": "❌ Cancel", "callback_data": f"strategy:{strategy.strategy_id}:cancel"},
                    ]
                ]
            }
        strategy.engine.telegram.send_message(
            result_summary,
            reply_markup=retry_markup,
            parse_mode="HTML",
        )
        logger.info("Short put execution completed: requested_qty=%s, filled_qty=%s, child_orders=%s.", requested_qty, filled_qty, len(requests))
    finally:
        with strategy.lock:
            strategy._short_put_execution_active = False


def _execution_checklist(strategy: ShortPutStrategy) -> bool:
    position_updated = update_put_position(strategy)
    if not position_updated:
        logger.warning("Strategy checklist failed: unable to refresh put positions.")
        return False

    market_state = get_underlying_market_state(strategy)
    if market_state not in [MarketState.AFTERNOON, MarketState.AFTER_HOURS_BEGIN]:
        logger.warning(
            "Strategy checklist failed: market status is %s, expected one of %s.",
            market_state,
            [MarketState.AFTERNOON, MarketState.AFTER_HOURS_BEGIN],
        )
        return False

    total_cash = get_total_cash(strategy)
    if total_cash is None or total_cash <= 0:
        logger.warning("Strategy checklist failed: total cash is unavailable or non-positive: %s.", total_cash)
        return False

    leverage = get_leverage_ratio(strategy)
    if leverage is None or leverage >= strategy.config.leverage_ratio:
        logger.warning(
            "Strategy checklist failed: leverage ratio is %s, limit is %s.",
            leverage,
            strategy.config.leverage_ratio,
        )
        return False

    open_orders = strategy.engine.order_list_query(acc_id=strategy.acc_id, status_filter_list=OPEN_ORDER_STATUSES)
    if open_orders is None:
        logger.warning("Strategy checklist failed: unable to query open orders.")
        return False
    if not open_orders.empty:
        logger.warning("Strategy checklist failed: open orders exist, count=%s.", len(open_orders))
        return False

    return True


def _build_execution_requests(strategy: ShortPutStrategy, selected_option: pd.Series, max_short: int) -> list[LimitOrderRequest] | None:
    if max_short <= 0:
        logger.warning("Build execution requests failed: max_short is non-positive. max_short=%s.", max_short)
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
        logger.warning("Build execution requests failed: missing fields=%s", missing_fields)
        return None

    option_info = resolve_option_name(selected_option["name"], TrdEnv.REAL)
    if option_info is None or option_info.type != "put" or option_info.strike is None:
        logger.warning("Build execution requests failed: invalid option name=%s.", selected_option["name"])
        return None

    bid_price = strategy.engine.valid_positive_float(selected_option["bid_price"])
    ask_price = strategy.engine.valid_positive_float(selected_option["ask_price"])
    bid_volume = strategy.engine.valid_positive_float(selected_option["bid_volume"])
    price_tick = strategy.engine.valid_positive_float(selected_option["price_spread"])

    if bid_price is None or ask_price is None or ask_price < bid_price:
        logger.warning(
            "Build execution requests failed: invalid bid/ask. bid=%s, ask=%s, option=%s.",
            bid_price,
            ask_price,
            selected_option["name"],
        )
        return None
    if price_tick is None:
        logger.warning(
            "Build execution requests failed: invalid price tick. price_tick=%s, option=%s.",
            price_tick,
            selected_option["name"],
        )
        return None

    mid_price = (bid_price + ask_price) / 2
    spread = ask_price - bid_price
    spread_pct = spread / mid_price if mid_price > 0 else float("inf")
    if spread_pct > strategy.config.max_spread_pct:
        logger.warning(
            "Build execution requests failed: spread too wide. spread_pct=%.4f, max_spread_pct=%.4f, bid=%s, ask=%s, option=%s.",
            spread_pct,
            strategy.config.max_spread_pct,
            bid_price,
            ask_price,
            selected_option["name"],
        )
        return None

    if bid_price < strategy.config.min_credit:
        logger.warning(
            "Build execution requests failed: bid below minimum credit. bid=%s, min_credit=%s, option=%s.",
            bid_price,
            strategy.config.min_credit,
            selected_option["name"],
        )
        return None

    participation_qty = int(bid_volume * strategy.config.max_order_book_participation)
    if participation_qty <= 0:
        logger.warning(
            "Build execution requests failed: participation qty is non-positive. bid_volume=%s, participation_rate=%s, participation_qty=%s.",
            bid_volume,
            strategy.config.max_order_book_participation,
            participation_qty,
        )
        return None

    child_qty_caps = [participation_qty]
    if strategy.config.max_contracts_per_trade is not None:
        child_qty_caps.append(strategy.config.max_contracts_per_trade)

    child_qty_cap = int(min(child_qty_caps))
    if child_qty_cap <= 0:
        logger.warning(
            "Build execution requests failed: child qty cap is non-positive. max_short=%s, max_contracts_per_trade=%s, participation_qty=%s.",
            max_short,
            strategy.config.max_contracts_per_trade,
            participation_qty,
        )
        return None

    price_ladder_plan = build_price_ladder(
        side="sell",
        code=selected_option["code"],
        bid_price=bid_price,
        ask_price=ask_price,
        price_tick=price_tick,
        steps=strategy.config.price_ladder_steps,
    )
    requests = []
    remaining_qty = int(max_short)
    while remaining_qty > 0:
        child_qty = min(child_qty_cap, remaining_qty)
        requests.append(
            LimitOrderRequest(
                acc_id=strategy.acc_id,
                code=selected_option["code"],
                side=TrdSide.SELL,
                qty=child_qty,
                price_ladder_plan=price_ladder_plan,
            )
        )
        remaining_qty -= child_qty

    logger.info(
        "Build execution requests passed: code=%s, total_qty=%s, child_qtys=%s, price_tick=%s, bid=%.2f, ask=%.2f, spread_pct=%.4f, prices=%s",
        selected_option["code"],
        max_short,
        [request.qty for request in requests],
        price_tick,
        bid_price,
        ask_price,
        spread_pct,
        price_ladder_plan.prices,
    )
    return requests
