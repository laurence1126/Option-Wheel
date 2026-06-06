from __future__ import annotations

import pandas as pd
from typing import TYPE_CHECKING
from futu import TrdEnv

from .option_parsing import resolve_option_name

from app.utils.logging import configure_logger

if TYPE_CHECKING:
    from ..strategy_main import ShortPutStrategy

logger = configure_logger(__name__)


def get_underlying_market_state(strategy: ShortPutStrategy) -> object | None:
    market_state = strategy.engine.get_market_state(strategy.config.underlying)
    if market_state is None or market_state.empty or "market_state" not in market_state.columns:
        logger.warning("Market state is unavailable for %s.", strategy.config.underlying)
        return None
    return market_state.iloc[0]["market_state"]


def get_total_cash(strategy: ShortPutStrategy) -> float | None:
    account_info = strategy.engine.get_account_info(strategy.acc_id)
    if account_info is None or account_info.empty:
        return None

    res = account_info.iloc[0].copy()
    capital = res["fund_assets"] + res["cash"] if strategy.acc_id == strategy.engine.margin_account else res["cash"]
    if strategy.config.max_capital is not None:
        capital = min(capital, strategy.config.max_capital)

    return capital


def get_leverage_ratio(strategy: ShortPutStrategy) -> float | None:
    total_cash = get_total_cash(strategy)
    if not total_cash:
        return total_cash

    option_notional = sum(abs(x.qty) * x.strike * 100 for x in strategy._put_option_position if x.qty < 0)
    return option_notional / total_cash


def get_max_num_to_short(strategy: ShortPutStrategy, selected_option: pd.Series) -> int:
    total_cash = get_total_cash(strategy)
    if not total_cash or total_cash <= 0:
        logger.warning("Cannot calculate max short quantity because total cash is unavailable or non-positive: %s", total_cash)
        return 0

    required_fields = ["code", "name", "bid_price", "ask_price"]
    missing_fields = [field for field in required_fields if field not in selected_option.index]
    if missing_fields:
        logger.warning("Cannot calculate max short quantity because selected option is missing fields: %s", missing_fields)
        return 0

    option_info = resolve_option_name(selected_option["name"], TrdEnv.REAL)
    if option_info is None or option_info.strike is None or option_info.strike <= 0:
        logger.warning("Cannot calculate max short quantity because selected option strike is unavailable: %s", selected_option)
        return 0
    strike = option_info.strike

    option_notional = sum(abs(x.qty) * x.strike * 100 for x in strategy._put_option_position if x.qty < 0)
    max_allowed_notional = total_cash * strategy.config.leverage_ratio
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
    futu_max_short = strategy.engine.get_max_short_quantity(
        acc_id=strategy.acc_id,
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


def update_put_position(strategy: ShortPutStrategy) -> bool:
    position = strategy.engine.get_open_position(strategy.acc_id)
    if position is None:
        logger.error("Failed to update put positions: position query returned None.")
        return False
    if position.empty:
        strategy._put_option_position = []
        logger.info("Updated put positions: no open positions.")
        return True

    put_positions = []
    skipped_count = 0
    for _, row in position.iterrows():
        option_info = resolve_option_name(row["stock_name"], strategy.engine.trading_environment, row["code"], row["qty"], row["cost_price"])
        underlying_symbol = strategy.config.underlying.split(".")[-1]
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

    strategy._put_option_position = put_positions
    logger.info(
        "Updated put positions: put_count=%s, skipped_count=%s, total_position_rows=%s",
        len(strategy._put_option_position),
        skipped_count,
        len(position),
    )
    return True


def update_maturing_put_strikes(strategy: ShortPutStrategy) -> bool:
    if not update_put_position(strategy):
        with strategy.lock:
            strategy.trading_status["maturing_updated"] = False
        return False

    today = pd.Timestamp.today().date()
    maturing_strikes = []

    for option in strategy._put_option_position:
        if option.expiration is None or option.strike is None:
            continue

        expiration = pd.to_datetime(option.expiration).date()
        if expiration == today:
            maturing_strikes.append(option.strike)

    with strategy.lock:
        strategy._maturing_put_option_strike = maturing_strikes
        strategy.trading_status["maturing_updated"] = True

    return True
