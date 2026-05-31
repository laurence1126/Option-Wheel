from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import quote

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import yfinance as yf

OPTION_PATTERN = re.compile(r"^(?P<symbol>[A-Z]+)\s+(?P<date>\d{6})\s+(?P<strike>\d+(?:\.\d+)?)(?P<type>[CP])$")


def get_watcher_data() -> tuple[pd.DataFrame, float | None]:
    from futu import Currency, RET_OK
    from trading.config import futu_config
    from trading.utils import futu_utils

    connection = futu_utils.create_trade_context(
        futu_config.FUTU_OPEND_ADDRESS,
        futu_config.FUTU_OPEND_PORT,
        futu_config.TRADING_MARKET,
    )
    try:
        ret, positions = connection.position_list_query()
        if ret != RET_OK:
            raise RuntimeError(positions)

        ret, account = connection.accinfo_query(currency=Currency.USD)
        current_bp = float(account.iloc[0]["fund_assets"] + account.iloc[0]["cash"]) if ret == RET_OK and not account.empty else None

        rows = []
        for _, position in positions.iterrows():
            match = OPTION_PATTERN.match(str(position["stock_name"]))
            qty = int(float(position["qty"]))
            if not match or qty == 0 or match.group("type") != "P":
                continue

            ticker = match.group("symbol")
            expiration = pd.to_datetime(match.group("date"), format="%y%m%d")
            strike = float(match.group("strike"))
            previous_close, close = get_close_prices(ticker)
            rows.append(
                {
                    "TICKER": ticker,
                    "QTY": qty,
                    "PREMIUM": -float(position["cost_price"]) * qty * 100,
                    "EXPIRATION": expiration.strftime("%Y-%m-%d"),
                    "DTE": max((expiration.date() - pd.Timestamp.now().date()).days, 0),
                    "STRIKE": strike,
                    "CLOSE": close,
                    "PCT EXEC": strike / close * 100,
                    "_PREV CLOSE": previous_close,
                    "_NOTIONAL": abs(qty * strike * 100),
                    "_PNL": float(position["unrealized_pl"]),
                }
            )
        if not rows:
            return pd.DataFrame(), current_bp
        return pd.DataFrame(rows).set_index("TICKER").sort_values("PCT EXEC", ascending=False), current_bp
    finally:
        connection.close()


def fmt(column: str, value: object) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, str):
        return value
    if column == "PCT EXEC":
        return f"{float(value):,.2f}%"
    if column in ("QTY", "DTE"):
        return f"{int(value):,}"
    return f"{float(value):,.2f}"


@lru_cache
def get_close_prices(ticker: str) -> tuple[float, float]:
    closes = yf.Ticker(ticker).history(period="10d", interval="1d")["Close"]
    if closes.empty:
        raise ValueError(f"No price data returned for {ticker}.")
    previous_close = closes.iloc[-2] if len(closes) >= 2 else np.nan
    return float(previous_close), float(closes.iloc[-1])


def build_option_watcher_context(df: pd.DataFrame | None = None, current_bp: float | None = None) -> dict[str, object]:
    options = pd.DataFrame() if df is None else df
    visible_columns = [column for column in options.columns if not column.startswith("_")]
    rows = [_build_watcher_row(index, ticker, row, visible_columns) for index, (ticker, row) in enumerate(options.iterrows())]
    total_bp = float(options["_NOTIONAL"].sum()) if "_NOTIONAL" in options else 0.0

    return {
        "title": "Option Price Watcher",
        "date": pd.Timestamp.now().strftime("%Y-%m-%d"),
        "columns": visible_columns,
        "rows": rows,
        "chart_html": _build_notional_chart_html(options) if not options.empty else None,
        "pnl_chart_html": _build_pnl_chart_html(options) if not options.empty and "_PNL" in options else None,
        "current_bp": fmt("", current_bp) if current_bp is not None else None,
        "total_bp": fmt("", total_bp),
        "bp_status": _get_bp_status(total_bp, current_bp),
        "leverage_ratio": fmt("", total_bp / current_bp) if current_bp else None,
        "error_message": None,
    }


def _build_watcher_row(index: int, ticker: object, row: pd.Series, visible_columns: list[str]) -> dict[str, object]:
    pct_exec = float(row["PCT EXEC"])
    if pct_exec < 85:
        row_status = "safe"
    elif pct_exec > 95:
        row_status = "risk"
    else:
        row_status = "neutral-alt" if index % 2 else "neutral"

    cells = []
    for column in visible_columns:
        value = row[column]
        cell_status = None
        if column == "CLOSE":
            cell_status = "down" if value < row["_PREV CLOSE"] else "up"
        cells.append({"status": cell_status, "text": fmt(column, value), "sort_value": value})

    ticker_text = str(ticker)
    return {
        "ticker": ticker_text,
        "ticker_url": f"https://finance.yahoo.com/quote/{quote(ticker_text, safe='')}",
        "status": row_status,
        "cells": cells,
    }


def _build_notional_chart_html(df: pd.DataFrame) -> str:
    chart_data = (
        df.assign(_CONTRACTS=df["QTY"].abs()).groupby("EXPIRATION").agg(notional=("_NOTIONAL", "sum"), contracts=("_CONTRACTS", "sum")).sort_index()
    )
    ticker_chart_data = (
        df.assign(_CONTRACTS=df["QTY"].abs())
        .reset_index()
        .groupby(["TICKER", "EXPIRATION"], as_index=False)
        .agg(notional=("_NOTIONAL", "sum"), contracts=("_CONTRACTS", "sum"))
        .sort_values(["EXPIRATION", "TICKER"])
    )
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=[pd.Timestamp(expiration).strftime("%m/%d/%y") for expiration in chart_data.index],
            y=chart_data["notional"].values,
            visible=False,
            marker_color="#60a5fa",
            text=[f"${notional:,.0f}" for notional in chart_data["notional"].values],
            textposition="outside",
            customdata=[
                [pd.Timestamp(expiration).strftime("%Y-%m-%d"), int(contracts)]
                for expiration, contracts in zip(chart_data.index, chart_data["contracts"], strict=True)
            ],
            hovertemplate="%{customdata[0]}<br>Notional: $%{y:,.0f}<br># of contracts: %{customdata[1]:,.0f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            x=[
                f"{ticker}<br>{pd.Timestamp(expiration).strftime('%m/%d/%y')}"
                for ticker, expiration in zip(ticker_chart_data["TICKER"], ticker_chart_data["EXPIRATION"], strict=True)
            ],
            y=ticker_chart_data["notional"].values,
            marker_color="#60a5fa",
            text=[f"${notional:,.0f}" for notional in ticker_chart_data["notional"].values],
            textposition="outside",
            customdata=[
                [ticker, pd.Timestamp(expiration).strftime("%Y-%m-%d"), int(contracts)]
                for ticker, expiration, contracts in zip(
                    ticker_chart_data["TICKER"],
                    ticker_chart_data["EXPIRATION"],
                    ticker_chart_data["contracts"],
                    strict=True,
                )
            ],
            hovertemplate="%{customdata[0]}<br>%{customdata[1]}<br>Notional: $%{y:,.0f}<br># of contracts: %{customdata[2]:,.0f}<extra></extra>",
        )
    )
    fig.update_layout(
        autosize=True,
        dragmode=False,
        showlegend=False,
        height=380,
        hoverlabel={"bgcolor": "#111111", "bordercolor": "#60a5fa", "font": {"color": "#e5e7eb"}},
        margin={"l": 96, "r": 24, "t": 24, "b": 64, "autoexpand": False},
        paper_bgcolor="#1b1b1b",
        plot_bgcolor="#1b1b1b",
        font={"color": "#e5e7eb"},
        xaxis={"title": "Ticker / Expiration", "gridcolor": "#2f2f2f", "type": "category", "automargin": False},
        yaxis={"title": "Total Notional (USD)", "gridcolor": "#2f2f2f", "tickprefix": "$", "tickformat": ",.0f", "automargin": False},
    )
    return fig.to_html(
        config={
            "displaylogo": False,
            "modeBarButtonsToRemove": ["select2d", "lasso2d"],
            "responsive": True,
        },
        default_height="380px",
        full_html=False,
        include_plotlyjs="cdn",
    )


def _build_pnl_chart_html(df: pd.DataFrame) -> str:
    expiration_chart_data = df.groupby("EXPIRATION", as_index=False).agg(pnl=("_PNL", "sum"), premium=("PREMIUM", "sum")).sort_values("EXPIRATION")
    expiration_chart_data["pnl_pct"] = expiration_chart_data["pnl"].div(expiration_chart_data["premium"].replace(0, np.nan)).mul(100)
    chart_data = (
        df.reset_index()
        .groupby(["TICKER", "EXPIRATION"], as_index=False)
        .agg(pnl=("_PNL", "sum"), premium=("PREMIUM", "sum"))
        .sort_values(["EXPIRATION", "TICKER"])
    )
    chart_data["pnl_pct"] = chart_data["pnl"].div(chart_data["premium"].replace(0, np.nan)).mul(100)
    expiration_x_values = [pd.Timestamp(expiration).strftime("%m/%d/%y") for expiration in expiration_chart_data["EXPIRATION"]]
    x_values = [
        f"{ticker}<br>{pd.Timestamp(expiration).strftime('%m/%d/%y')}"
        for ticker, expiration in zip(chart_data["TICKER"], chart_data["EXPIRATION"], strict=True)
    ]
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=expiration_x_values,
            y=expiration_chart_data["pnl"].values,
            visible=False,
            marker_color=["#22c55e" if pnl >= 0 else "#ef4444" for pnl in expiration_chart_data["pnl"].values],
            text=[f"${pnl:,.0f}" for pnl in expiration_chart_data["pnl"].values],
            textposition="outside",
            customdata=[[pd.Timestamp(expiration).strftime("%Y-%m-%d")] for expiration in expiration_chart_data["EXPIRATION"]],
            hovertemplate="%{customdata[0]}<br>PnL: $%{y:,.2f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            x=expiration_x_values,
            y=expiration_chart_data["pnl_pct"].values,
            visible=False,
            marker_color=["#22c55e" if pnl_pct >= 0 else "#ef4444" for pnl_pct in expiration_chart_data["pnl_pct"].values],
            text=[f"{pnl_pct:,.1f}%" if pd.notna(pnl_pct) else "" for pnl_pct in expiration_chart_data["pnl_pct"].values],
            textposition="outside",
            customdata=[[pd.Timestamp(expiration).strftime("%Y-%m-%d")] for expiration in expiration_chart_data["EXPIRATION"]],
            hovertemplate="%{customdata[0]}<br>PnL: %{y:,.2f}%<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            x=x_values,
            y=chart_data["pnl"].values,
            visible=False,
            marker_color=["#22c55e" if pnl >= 0 else "#ef4444" for pnl in chart_data["pnl"].values],
            text=[f"${pnl:,.0f}" for pnl in chart_data["pnl"].values],
            textposition="outside",
            customdata=[
                [ticker, pd.Timestamp(expiration).strftime("%Y-%m-%d")]
                for ticker, expiration in zip(chart_data["TICKER"], chart_data["EXPIRATION"], strict=True)
            ],
            hovertemplate="%{customdata[0]}<br>%{customdata[1]}<br>PnL: $%{y:,.2f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            x=x_values,
            y=chart_data["pnl_pct"].values,
            marker_color=["#22c55e" if pnl_pct >= 0 else "#ef4444" for pnl_pct in chart_data["pnl_pct"].values],
            text=[f"{pnl_pct:,.1f}%" if pd.notna(pnl_pct) else "" for pnl_pct in chart_data["pnl_pct"].values],
            textposition="outside",
            customdata=[
                [ticker, pd.Timestamp(expiration).strftime("%Y-%m-%d")]
                for ticker, expiration in zip(chart_data["TICKER"], chart_data["EXPIRATION"], strict=True)
            ],
            hovertemplate="%{customdata[0]}<br>%{customdata[1]}<br>PnL: %{y:,.2f}%<extra></extra>",
        )
    )
    fig.update_layout(
        autosize=True,
        dragmode=False,
        showlegend=False,
        height=380,
        hoverlabel={"bgcolor": "#111111", "bordercolor": "#60a5fa", "font": {"color": "#e5e7eb"}},
        margin={"l": 96, "r": 24, "t": 24, "b": 64, "autoexpand": False},
        paper_bgcolor="#1b1b1b",
        plot_bgcolor="#1b1b1b",
        font={"color": "#e5e7eb"},
        xaxis={"title": "Ticker / Expiration", "gridcolor": "#2f2f2f", "type": "category", "automargin": False},
        yaxis={"title": "Option PnL (%)", "gridcolor": "#2f2f2f", "ticksuffix": "%", "tickformat": ",.1f", "automargin": False},
    )
    return fig.to_html(
        config={
            "displaylogo": False,
            "modeBarButtonsToRemove": ["select2d", "lasso2d"],
            "responsive": True,
        },
        default_height="380px",
        full_html=False,
        include_plotlyjs=False,
    )


def _get_bp_status(total_bp: float, current_bp: float | None) -> str:
    if current_bp is None:
        return "neutral"
    return "positive" if total_bp <= current_bp else "negative"
