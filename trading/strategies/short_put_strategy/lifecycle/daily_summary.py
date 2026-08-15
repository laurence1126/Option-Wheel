from __future__ import annotations

from typing import TYPE_CHECKING
import pandas as pd

from ..utils.account_state import get_net_asset_value, get_total_cash, update_put_position
from ..utils.option_parsing import resolve_option_info

from app.utils.logging import configure_logger

if TYPE_CHECKING:
    from ..strategy_main import ShortPutStrategy

logger = configure_logger(__name__)


def send_daily_summary(strategy: ShortPutStrategy) -> bool:
    position = strategy.engine.get_open_position(strategy.acc_id)
    if position is None:
        logger.warning("Daily summary failed: position query returned None.")
        return False

    if not update_put_position(strategy):
        logger.warning("Daily summary failed: unable to refresh strategy put positions.")
        return False

    total_nav = get_net_asset_value(strategy)
    total_cash = get_total_cash(strategy, capped=False)
    strategy_cash = _get_strategy_cash(strategy)
    strategy_positions = _get_strategy_position_rows(strategy, position)
    strategy_market_value = _get_strategy_market_value(strategy_positions)

    message = _build_daily_summary_message(
        strategy_id=strategy.strategy_id,
        total_nav=total_nav,
        total_cash=total_cash,
        strategy_cash=strategy_cash,
        strategy_market_value=strategy_market_value,
        positions=strategy._put_option_position,
        position_rows=strategy_positions,
    )
    strategy.engine.telegram.send_message(message, parse_mode="HTML")
    logger.info(
        "Daily summary sent: strategy_id=%s, total_nav=%s, total_cash=%s, strategy_cash=%s, strategy_market_value=%s, position_count=%s.",
        strategy.strategy_id,
        total_nav,
        total_cash,
        strategy_cash,
        strategy_market_value,
        len(strategy._put_option_position),
    )
    return True


def _get_strategy_position_rows(strategy: ShortPutStrategy, position: pd.DataFrame) -> pd.DataFrame:
    if position.empty or not strategy._put_option_position:
        return pd.DataFrame(columns=position.columns)

    strategy_codes = {option.code for option in strategy._put_option_position if option.code}
    if not strategy_codes or "code" not in position.columns:
        return pd.DataFrame(columns=position.columns)

    return position.loc[position["code"].isin(strategy_codes)].copy()


def _get_strategy_market_value(position: pd.DataFrame) -> float:
    if position.empty:
        return 0.0
    if "market_val" in position.columns:
        return float(pd.to_numeric(position["market_val"], errors="coerce").fillna(0).sum())

    values = []
    for _, row in position.iterrows():
        qty = _to_float(row.get("qty"))
        price = _to_float(row.get("nominal_price"))
        if qty is None or price is None:
            continue
        values.append(qty * price * 100)
    return float(sum(values))


def _get_strategy_cash(strategy: ShortPutStrategy) -> float:
    premium_collected = sum(
        abs(option.qty) * option.price * 100 for option in strategy._put_option_position if option.qty and option.qty < 0 and option.price
    )
    return float(premium_collected)


def _build_daily_summary_message(
    strategy_id: str,
    total_nav: float | None,
    total_cash: float | None,
    strategy_cash: float,
    strategy_market_value: float,
    positions: list,
    position_rows: pd.DataFrame,
) -> str:
    lines = [
        "<b>📊 DAILY STRATEGY SUMMARY</b>",
        f"Strategy: <b>{strategy_id}</b>",
        "",
        f"Total NAV: <b>{_fmt_money(total_nav)}</b>",
        f"Total Cash: <b>{_fmt_money(total_cash)}</b>",
        f"Strategy Cash: <b>{_fmt_money(strategy_cash)}</b>",
        f"Strategy MV: <b>{_fmt_money(strategy_market_value)}</b>",
        "",
        "<b>Strategy Positions</b>",
    ]

    if not positions:
        lines.append(" • N/A")
        return "\n".join(lines)

    row_by_code = {}
    if not position_rows.empty and "code" in position_rows.columns:
        row_by_code = {str(row["code"]): row for _, row in position_rows.iterrows()}

    for option in positions:
        row = row_by_code.get(str(option.code))
        pnl = _to_float(row.get("pl_val")) if row is not None else None
        details = [
            f"Qty: {_fmt_qty(option.qty)}",
            f"Avg: {_fmt_price(option.price)}",
        ]
        if pnl is not None:
            details.append(f"PnL: {_fmt_money(pnl)}")
        lines.append(f" • {resolve_option_info(option)}")
        lines.append("   " + " | ".join(details))

    return "\n".join(lines)


def _to_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(result):
        return None
    return result


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"${value:,.2f}"


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):,.4f}"


def _fmt_qty(value: float | None) -> str:
    if value is None:
        return "N/A"
    numeric = float(value)
    return f"{int(numeric):,}" if numeric.is_integer() else f"{numeric:,.2f}"
