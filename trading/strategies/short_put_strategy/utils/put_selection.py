from __future__ import annotations

import pandas as pd
from time import sleep
from typing import TYPE_CHECKING, List, Tuple, Literal
from futu import RET_OK, OptionDataFilter, SubType

from app.utils.logging import configure_logger

if TYPE_CHECKING:
    from ..strategy_main import ShortPutStrategy

logger = configure_logger(__name__)


def select_short_put(strategy: ShortPutStrategy) -> pd.Series:
    target_exp_days = strategy.config.target_exp_days
    attempted_expirations = set()

    while True:
        expiration = _get_option_target_expiration(
            strategy,
            strategy.config.underlying,
            target_exp_days,
            direction=strategy.config.expiration_direction,
        )
        if expiration is None:
            logger.warning("No suitable expiration found.")
            return pd.Series(dtype="object")

        expiration_date, days_to_mature = expiration
        if expiration_date in attempted_expirations:
            logger.warning("No later suitable expiration found after trying: %s", sorted(attempted_expirations))
            return pd.Series(dtype="object")
        attempted_expirations.add(expiration_date)

        delta_min = max(strategy.config.target_delta * 0.8, 0.01)
        delta_max = strategy.config.target_delta * 1.2
        option_codes = _get_put_option_codes_by_delta(
            strategy,
            strategy.config.underlying,
            expiration_date,
            abs_delta_min=delta_min,
            abs_delta_max=delta_max,
        )
        if option_codes is None:
            logger.warning("Unable to query option chain for %s.", expiration_date)
            return pd.Series(dtype="object")
        selected = _get_target_option_quote(strategy, option_codes, strategy.config.target_delta)
        if not selected.empty:
            return selected

        target_exp_days = days_to_mature + 1
        logger.info("No target option found for %s. Retrying with target_exp_days=%s.", expiration_date, target_exp_days)


def _get_option_expirations(strategy: ShortPutStrategy, code: str) -> pd.DataFrame | None:
    ret, data = strategy.engine.quote_context.get_option_expiration_date(code=code)
    if ret != RET_OK:
        logger.error("Get option expirations failed: %s", data)
        return None

    today = pd.to_datetime("today").normalize()
    result = data[["strike_time"]].copy()
    result["date_distance"] = result["strike_time"].apply(lambda x: (pd.to_datetime(x) - today).days)
    return result


def _get_option_target_expiration(
    strategy: ShortPutStrategy,
    code: str,
    target_distance: int,
    direction: Literal["closest", "smaller", "larger"] = "larger",
) -> Tuple[str, int] | None:
    expiration_df = _get_option_expirations(strategy, code)
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
    strategy: ShortPutStrategy,
    code: str,
    expiration_date: str,
    abs_delta_min: float = 0.1,
    abs_delta_max: float = 0.3,
) -> List[str] | None:
    data_filter = OptionDataFilter(delta_min=-abs_delta_max, delta_max=-abs_delta_min)
    ret, data = strategy.engine.quote_context.get_option_chain(code=code, start=expiration_date, end=expiration_date, data_filter=data_filter)
    if ret != RET_OK:
        logger.error("Get option chain failed: %s", data)
        return None

    option_codes = data["code"].tolist()
    if not option_codes:
        logger.warning("No option contracts matched the delta filter.")
        return []

    if not strategy.engine.subscribe(option_codes, [SubType.QUOTE, SubType.ORDER_BOOK], subscribe_push=False):
        return []

    return option_codes


def _get_target_option_quote(strategy: ShortPutStrategy, codes: list[str], target_delta: float) -> pd.Series:
    if not codes:
        return pd.Series(dtype="object")

    data = strategy.engine.get_stock_quote(codes)
    if data is None or data.empty:
        return pd.Series(dtype="object")

    result = data[["code", "name", "volume", "implied_volatility", "delta", "price_spread"]].copy()
    result = result[result["volume"] > strategy.config.min_volume].reset_index(drop=True)
    if result.empty:
        logger.warning("No option contracts passed the liquidity filter.")
        return pd.Series(dtype="object")
    selected = result.loc[(result["delta"].abs() - target_delta).abs().argsort().iloc[0]].copy()

    for _ in range(5):
        order_book = strategy.engine.get_top_order_book(selected["code"])
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
