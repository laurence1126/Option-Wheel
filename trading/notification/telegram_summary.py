from __future__ import annotations

import html
from typing import Any


def build_sell_put_summary(
    code: str,
    name: str,
    snapshot: dict[str, Any],
    total_qty: int,
    child_qtys: list[int],
    prices: list[float],
    to_maturity: int | None = None,
    final_line: str = "🫡 Approve this trade?",
) -> str:
    child_qty_text = " + ".join(str(qty) for qty in child_qtys)
    price_ladder_text = " -> ".join(_format_price(price) for price in prices)
    top_book_text = _format_top_order_book(snapshot)
    maturity_text = "" if to_maturity is None else f"To Maturity: {html.escape(str(to_maturity))} Days\n"
    return (
        "<b>💸 SHORT PUT SUMMARY</b>\n"
        "\n"
        "<b>📜 Contract</b>\n"
        f"Code: {html.escape(str(code))}\n"
        f"Name: {html.escape(str(name))}\n"
        f"{maturity_text}"
        "\n"
        "<b>📷 Snapshot</b>\n"
        f"Delta: {_format_decimal(snapshot.get('delta'))}\n"
        f"Implied Vol: {_format_percent(snapshot.get('implied_volatility'))}\n"
        f"Daily Volume: {html.escape(_format_quantity(snapshot.get('volume')))}\n"
        f"TOB: {html.escape(top_book_text)}\n"
        "\n"
        "<b>🗓️ Order Plan</b>\n"
        f"Total Quantity: <b>{total_qty}</b>\n"
        f"Child Quantity: {html.escape(child_qty_text)}\n"
        f"Price Ladder: {html.escape(price_ladder_text)}\n"
        "\n"
        f"{html.escape(final_line)}"
    )


def build_cut_loss_summary(
    code: str,
    name: str,
    total_qty: int,
    child_qtys: list[int],
    order_book: dict[str, Any],
    average_price: float,
    stop_price: float,
    mid_signal_price: float,
    prices: list[float],
    final_line: str = "🫡 Approve this cut-loss order?",
) -> str:
    child_qty_text = " + ".join(str(qty) for qty in child_qtys)
    price_ladder_text = " -> ".join(_format_price(price) for price in prices)
    top_book_text = _format_top_order_book(order_book)
    return (
        "<b>🚨 CUT LOSS SUMMARY</b>\n"
        "\n"
        "<b>📜 Contract</b>\n"
        f"Code: {html.escape(str(code))}\n"
        f"Name: {html.escape(str(name))}\n"
        "\n"
        "<b>🔫 Trigger</b>\n"
        f"TOB: {html.escape(top_book_text)}\n"
        f"Average Price: {_format_price(average_price)}\n"
        f"Stop Price: {_format_price(stop_price)}\n"
        f"Mid Signal: {_format_price(mid_signal_price)}\n"
        "\n"
        "<b>🗓️ Order Plan</b>\n"
        f"Total Quantity: <b>{total_qty}</b>\n"
        f"Child Quantity: {html.escape(child_qty_text)}\n"
        f"Price Ladder: {html.escape(price_ladder_text)}\n"
        "\n"
        f"{html.escape(final_line)}"
    )


def build_assignment_summary(
    code: str,
    side: str,
    price: float,
    qty: int | float,
    matched_strike: float,
    market_state: Any,
    detected_at: Any,
    final_line: str = "🫡 Liquidate this position?",
) -> str:
    return (
        "<b>🚨 POTENTIAL PUT ASSIGNMENT</b>\n"
        "\n"
        "<b>📜 Underlying Order</b>\n"
        f"Code: {html.escape(str(code))}\n"
        f"Side: {html.escape(str(side))}\n"
        f"Price: {_format_price(price)}\n"
        f"Quantity: {html.escape(_format_quantity(qty))}\n"
        "\n"
        "<b>🧾 Assignment Signal</b>\n"
        f"Market State: {html.escape(str(market_state))}\n"
        f"Matched Strike: {_format_price(matched_strike)}\n"
        f"Detected At: {html.escape(str(detected_at))}\n"
        "\n"
        f"{html.escape(final_line)}"
    )


def replace_summary_prompt(summary: str, result_text: str) -> str:
    lines = str(summary).splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip():
            lines[index] = result_text
            return "\n".join(lines)
    return result_text


def build_execution_result_summary(
    title: str,
    code: str,
    name: str,
    requested_qty: int | float,
    filled_qty: int | float,
    child_results: list[Any],
    target_mid: float | None = None,
) -> str:
    result_icon = "✅" if float(filled_qty) >= float(requested_qty) else "🚨"
    suffix = "SUCCESS" if float(filled_qty) >= float(requested_qty) else "FAILURE"
    child_lines = "\n".join(_format_execution_child_result(index, result) for index, result in enumerate(child_results, start=1))
    if not child_lines:
        child_lines = "None"
    header_text = f"{result_icon} {title} - {suffix}"
    target_mid_text = f"Target Mid: {_format_price(target_mid)}\n" if target_mid is not None else ""

    return (
        f"<b>{html.escape(header_text)}</b>\n"
        "\n"
        "<b>📜 Contract</b>\n"
        f"Code: {html.escape(str(code))}\n"
        f"Name: {html.escape(str(name))}\n"
        "\n"
        "<b>📋 Result</b>\n"
        f"Requested Quantity: <b>{html.escape(_format_quantity(requested_qty))}</b>\n"
        f"{target_mid_text}"
        f"Filled Quantity: <b>{html.escape(_format_quantity(filled_qty))}</b>\n"
        "\n"
        "<b>🗓️ Child Orders</b>\n"
        f"{child_lines}"
    )


def _format_execution_child_result(index: int, result: Any) -> str:
    status = getattr(result, "execution_status", None)
    icon = "🟢" if status == "success" else "🔴"
    qty = getattr(result, "target_qty", "")
    filled_qty = getattr(result, "filled_qty", "")
    message = getattr(result, "message", "") if status != "success" else ""

    parts = [f"{icon} <b>Child {index}</b>: {_format_quantity(filled_qty)} / {_format_quantity(qty)} filled"]
    if message:
        parts.append(html.escape(str(message)))

    return " - ".join(parts)


def _format_price(price: float) -> str:
    return f"{float(price):.2f}"


def _format_percent(value: Any) -> str:
    return f"{float(value):.2f}%"


def _format_decimal(value: Any) -> str:
    return f"{float(value):.4f}"


def _format_quantity(quantity: Any) -> str:
    value = float(quantity)
    return str(int(value)) if value.is_integer() else str(value)


def _format_top_order_book(order_book: dict[str, Any]) -> str:
    return (
        f"{_format_quantity(order_book.get('bid_volume'))} @ {_format_price(order_book.get('bid_price'))} | "
        f"{_format_price(order_book.get('ask_price'))} @ {_format_quantity(order_book.get('ask_volume'))}"
    )
