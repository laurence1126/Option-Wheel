from __future__ import annotations

import pandas as pd
from typing import TYPE_CHECKING
from futu import SubType

from ..utils.account_state import update_put_position

from app.utils.logging import configure_logger

if TYPE_CHECKING:
    from ..strategy_main import ShortPutStrategy

logger = configure_logger(__name__)


def alert_assignment_at_close(strategy: ShortPutStrategy) -> None:
    with strategy.lock:
        maturing_strikes = list(strategy._maturing_put_option_strike)
    if not maturing_strikes:
        logger.info("Assignment close alert skipped because no maturing put strikes are tracked.")
        return

    if not update_put_position(strategy):
        logger.warning("Assignment close alert failed: unable to refresh short put positions.")
        return

    today = pd.Timestamp.today().date()
    assigned_contracts_by_strike: dict[float, int] = {}
    for option in strategy._put_option_position:
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

    if not strategy.engine.subscribe([strategy.config.underlying], [SubType.QUOTE], subscribe_push=False):
        logger.warning("Assignment close alert failed: unable to subscribe underlying quote. code=%s", strategy.config.underlying)
        return

    quote = strategy.engine.get_stock_quote([strategy.config.underlying])
    if quote is None or quote.empty:
        logger.warning("Assignment close alert failed: underlying quote unavailable. code=%s", strategy.config.underlying)
        return
    if "code" in quote.columns:
        quote = quote[quote["code"] == strategy.config.underlying]
    if quote.empty or "last_price" not in quote.columns:
        logger.warning("Assignment close alert failed: underlying last_price unavailable. code=%s", strategy.config.underlying)
        return

    underlying_price = strategy.engine.valid_positive_float(quote.iloc[0]["last_price"])
    if underlying_price is None:
        logger.warning(
            "Assignment close alert failed: underlying last_price is invalid. code=%s, last_price=%s",
            strategy.config.underlying,
            quote.iloc[0]["last_price"],
        )
        return

    assigned_strikes = {strike: qty for strike, qty in assigned_contracts_by_strike.items() if underlying_price < strike}
    if not assigned_strikes:
        logger.info(
            "Assignment close alert skipped because underlying is not below tracked maturing strikes: code=%s, price=%s, strikes=%s.",
            strategy.config.underlying,
            underlying_price,
            sorted(assigned_contracts_by_strike),
        )
        return

    lines = [
        "<b>🚨 SHORT PUT ASSIGNMENT DETECTED</b>\n",
        f"Underlying: {strategy.config.underlying}",
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

    strategy.engine.telegram.send_message("\n".join(lines), parse_mode="HTML")
    logger.warning(
        "Assignment close alert sent: code=%s, underlying_price=%s, assigned_strikes=%s.",
        strategy.config.underlying,
        underlying_price,
        assigned_strikes,
    )
