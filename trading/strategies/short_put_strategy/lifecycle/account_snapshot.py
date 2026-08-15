from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
from futu import TrdSide

from ..utils.account_state import get_net_asset_value, get_total_cash, update_put_position
from ..utils.option_parsing import resolve_option_info, resolve_option_name
from .daily_summary import _get_strategy_market_value, _get_strategy_position_rows

from app.utils.logging import configure_logger

if TYPE_CHECKING:
    from ..strategy_main import ShortPutStrategy

logger = configure_logger(__name__)


def capture_account_snapshot(strategy: ShortPutStrategy, snapshot_root: str | Path = "trading/snapshot") -> bool:
    now = dt.datetime.now(strategy.config.cut_loss_earliest_time.tzinfo)
    position = strategy.engine.get_open_position(strategy.acc_id)
    if position is None:
        logger.warning("Account snapshot failed: position query returned None.")
        return False

    if not update_put_position(strategy):
        logger.warning("Account snapshot failed: unable to refresh strategy put positions.")
        return False

    strategy_positions = _get_strategy_position_rows(strategy, position)
    strategy_market_value = _get_strategy_market_value(strategy_positions)
    total_nav = get_net_asset_value(strategy)
    total_cash = get_total_cash(strategy, capped=False)
    strategy_cash = _get_strategy_cash(strategy)
    market_data_by_code = _get_market_data_by_code(strategy, strategy_positions)
    order_history = _get_strategy_orders(strategy, now)

    snapshot = {
        "datetime": now.isoformat(),
        "strategy": strategy.strategy_id,
        "totalNav": total_nav,
        "totalCash": total_cash,
        "strategyCash": strategy_cash,
        "strategyMV": strategy_market_value,
        "position": _build_position_snapshots(strategy, strategy_positions, market_data_by_code),
        "orders": _build_order_snapshots(strategy, order_history),
        "params": _json_safe(asdict(strategy.config)),
    }

    snapshot_path = Path(snapshot_root) / strategy.strategy_id / f"{now.date().isoformat()}.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps(_json_safe(snapshot), indent=2), encoding="utf-8")
    logger.info("Account snapshot captured: strategy_id=%s, path=%s.", strategy.strategy_id, snapshot_path)
    return True


def _get_strategy_cash(strategy: ShortPutStrategy) -> float | None:
    premium_collected = sum(
        abs(option.qty) * option.price * 100 for option in strategy._put_option_position if option.qty and option.qty < 0 and option.price
    )
    return float(premium_collected)


def _get_market_data_by_code(strategy: ShortPutStrategy, position: pd.DataFrame) -> dict[str, pd.Series]:
    codes = [strategy.config.underlying]
    if not position.empty and "code" in position.columns:
        codes.extend(str(code) for code in position["code"].dropna())
    codes = list(dict.fromkeys(codes))

    quote = None
    snapshot_query = getattr(strategy.engine, "get_market_snapshot", None)
    if callable(snapshot_query):
        quote = snapshot_query(codes)
    if quote is None:
        quote = strategy.engine.get_stock_quote(codes)
    if quote is None or quote.empty or "code" not in quote.columns:
        return {}
    return {str(row["code"]): row for _, row in quote.iterrows()}


def _get_strategy_orders(strategy: ShortPutStrategy, now: dt.datetime) -> pd.DataFrame:
    start = now - dt.timedelta(hours=240)
    query = getattr(strategy.engine, "history_order_list_query", None)
    if not callable(query):
        logger.warning("Account snapshot skipped order history because engine has no history_order_list_query.")
        return pd.DataFrame()

    orders = query(
        acc_id=strategy.acc_id,
        start=start.strftime("%Y-%m-%d %H:%M:%S"),
        end=now.strftime("%Y-%m-%d %H:%M:%S"),
    )
    if orders is None:
        logger.warning("Account snapshot order history query returned None.")
        return pd.DataFrame()
    if orders.empty:
        return orders

    strategy_codes = {option.code for option in strategy._put_option_position if option.code}
    underlying_symbol = strategy.config.underlying.split(".")[-1]

    matched_rows = []
    for _, row in orders.iterrows():
        code = str(row.get("code", ""))
        name = _order_name(row)
        remark = str(row.get("remark", ""))
        option_info = resolve_option_name(name, strategy.engine.trading_environment, code=code)
        belongs_to_strategy = code in strategy_codes or remark in {"cut_loss", strategy.strategy_id}
        if option_info is not None and option_info.ticker == underlying_symbol and option_info.type == "put":
            belongs_to_strategy = True
        if belongs_to_strategy:
            matched_rows.append(row)

    return pd.DataFrame(matched_rows, columns=orders.columns)


def _build_position_snapshots(strategy: ShortPutStrategy, position: pd.DataFrame, market_data_by_code: dict[str, pd.Series]) -> list[dict[str, Any]]:
    if position.empty:
        return []

    underlying_quote = market_data_by_code.get(strategy.config.underlying)
    underlying_price = _row_float(underlying_quote, "last_price")
    snapshots = []
    for _, row in position.iterrows():
        code = str(row.get("code", ""))
        quote = market_data_by_code.get(code)
        option_info = resolve_option_name(
            str(row.get("stock_name", row.get("name", ""))),
            strategy.engine.trading_environment,
            code=code,
            qty=_row_float(row, "qty"),
            price=_row_float(row, "cost_price"),
        )
        snapshots.append(
            {
                "name": resolve_option_info(option_info) if option_info is not None else str(row.get("stock_name", row.get("name", code))),
                "code": code,
                "qty": _row_float(row, "qty"),
                "avgPrice": _row_float(row, "cost_price"),
                "mktPrice": _first_float(quote, row, ["nominal_price", "market_price", "last_price", "price"]),
                "underlyingPrice": underlying_price,
                "pnl": _first_float(row, None, ["pl_val", "unrealized_pl", "unrealizedPL"]),
                "iv": _scale(_first_float(quote, row, ["option_implied_volatility", "implied_volatility", "iv"]), 0.01),
                "delta": _first_float(quote, row, ["option_delta", "delta"]),
                "gamma": _first_float(quote, row, ["option_gamma", "gamma"]),
                "theta": _first_float(quote, row, ["option_theta", "theta"]),
            }
        )
    return snapshots


def _build_order_snapshots(strategy: ShortPutStrategy, orders: pd.DataFrame) -> list[dict[str, Any]]:
    if orders.empty:
        return []

    snapshots = []
    for _, row in orders.iterrows():
        snapshots.append(
            {
                "name": _order_name(row),
                "code": str(row.get("code", "")),
                "status": _json_safe(row.get("order_status")),
                "qty": _signed_order_quantity(row, "qty"),
                "limitPrice": _row_float(row, "price"),
                "filledQty": _signed_order_quantity(row, "dealt_qty"),
                "avgPrice": _row_float(row, "dealt_avg_price"),
                "updateTime": _first_value(row, ["updated_time", "update_time"]),
            }
        )
    return snapshots


def _order_name(row: pd.Series) -> str:
    return str(row.get("stock_name", row.get("name", "")))


def _signed_order_quantity(row: pd.Series, column: str) -> float | None:
    qty = _row_float(row, column)
    if qty is None:
        return None

    side = row.get("trd_side")
    side_text = getattr(side, "name", str(side)).lower()
    sell_sides = {TrdSide.SELL}
    sell_short = getattr(TrdSide, "SELL_SHORT", None)
    if sell_short is not None:
        sell_sides.add(sell_short)
    buy_sides = {TrdSide.BUY}
    buy_back = getattr(TrdSide, "BUY_BACK", None)
    if buy_back is not None:
        buy_sides.add(buy_back)

    if side in sell_sides or "sell" in side_text:
        return -abs(qty)
    if side in buy_sides or "buy" in side_text:
        return abs(qty)
    return qty


def _scale(value: float | None, factor: float) -> float | None:
    if value is None:
        return None
    return value * factor


def _first_float(primary: pd.Series | None, secondary: pd.Series | None, columns: list[str]) -> float | None:
    for row in (primary, secondary):
        for column in columns:
            value = _row_float(row, column)
            if value is not None:
                return value
    return None


def _row_float(row: pd.Series | None, column: str) -> float | None:
    if row is None or column not in row.index:
        return None
    try:
        value = float(row[column])
    except (TypeError, ValueError):
        return None
    if pd.isna(value):
        return None
    return value


def _first_value(row: pd.Series, columns: list[str]) -> Any:
    for column in columns:
        if column in row.index and not pd.isna(row[column]):
            return _json_safe(row[column])
    return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if hasattr(value, "name") and value.__class__.__module__.startswith("futu"):
        return value.name
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value
