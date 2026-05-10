from __future__ import annotations

from dataclasses import dataclass

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from backtest import BacktestResult


@dataclass
class WheelPerformanceReport:
    result: BacktestResult

    def __post_init__(self) -> None:
        self.trades = self.result.trades.copy()
        self.events = self.result.events.copy()
        self.equity_curve = self.result.equity_curve.sort_index()
        self.underlying_curve = self._build_underlying_curve()
        self.returns = self.equity_curve.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
        self.underlying_returns = self.underlying_curve.pct_change().replace([np.inf, -np.inf], np.nan).dropna()

    def summary_stats(self) -> dict[str, float | int | str | None]:
        trades = self.trades
        short_trades = trades.loc[trades["side"] == "short"].copy() if not trades.empty and "side" in trades.columns else trades
        duration_days = max((self.result.end_date - self.result.start_date).days, 1)
        total_return = (self.result.ending_equity / self.result.initial_cash) - 1.0
        cagr = (self.result.ending_equity / self.result.initial_cash) ** (365.25 / duration_days) - 1.0
        sharpe = None
        if not self.returns.empty:
            volatility = float(self.returns.std())
            if volatility > 0:
                sharpe = float(self.returns.mean()) / volatility * np.sqrt(252.0)
        underlying_sharpe = None
        if not self.underlying_returns.empty:
            underlying_volatility = float(self.underlying_returns.std())
            if underlying_volatility > 0:
                underlying_sharpe = float(self.underlying_returns.mean()) / underlying_volatility * np.sqrt(252.0)
        drawdown = self._drawdown_series(self.equity_curve)
        puts = int((short_trades["type"] == "put").sum()) if not short_trades.empty else 0
        calls = int((short_trades["type"] == "call").sum()) if not short_trades.empty else 0
        assignments = int(short_trades["outcome"].isin(["assigned", "assigned_liquidated"]).sum()) if not short_trades.empty else 0
        call_aways = int((short_trades["outcome"] == "called_away").sum()) if not short_trades.empty else 0
        cut_losses = int((short_trades["outcome"] == "cut_loss").sum()) if not short_trades.empty else 0
        take_profits = int((short_trades["outcome"] == "take_profit").sum()) if not short_trades.empty else 0
        total_cash_interest = 0.0
        if not self.result.daily_pnl.empty and "cash_interest" in self.result.daily_pnl.columns:
            total_cash_interest = float(self.result.daily_pnl["cash_interest"].sum())
        avg_liquidation_cost = None
        if not short_trades.empty and "liquidation_cost" in short_trades.columns:
            liquidation_costs = short_trades["liquidation_cost"].dropna()
            if not liquidation_costs.empty:
                avg_liquidation_cost = float(liquidation_costs.mean())

        return {
            "symbol": self.result.symbol,
            "start_date": self.result.start_date.date().isoformat(),
            "end_date": self.result.end_date.date().isoformat(),
            "initial_cash": self.result.initial_cash,
            "ending_equity": self.result.ending_equity,
            "net_profit": self.result.ending_equity - self.result.initial_cash,
            "total_cash_interest": total_cash_interest,
            "total_return": total_return,
            "cagr": cagr,
            "sharpe": sharpe,
            "underlying_sharpe": underlying_sharpe,
            "max_drawdown": drawdown.min() if not drawdown.empty else 0.0,
            "completed_legs": int(len(trades)),
            "puts_sold": puts,
            "calls_sold": calls,
            "assignments": assignments,
            "call_aways": call_aways,
            "cut_losses": cut_losses,
            "take_profits": take_profits,
            "put_assignment_rate": assignments / puts if puts else None,
            "put_cut_loss_rate": cut_losses / puts if puts else None,
            "put_take_profit_rate": take_profits / puts if puts else None,
            "call_away_rate": call_aways / calls if calls else None,
            "total_premium_collected": float(short_trades["premium"].sum()) if not short_trades.empty else 0.0,
            "avg_premium": float(short_trades["premium"].mean()) if not short_trades.empty else None,
            "avg_liquidation_cost": avg_liquidation_cost,
            "avg_iv": float(trades["iv"].mean()) if not trades.empty else None,
            "avg_abs_delta": float(trades["delta"].abs().mean()) if not trades.empty else None,
            "avg_leverage_ratio": float(trades["leverage_ratio"].mean()) if not trades.empty and "leverage_ratio" in trades.columns else None,
            "max_leverage_ratio": float(trades["leverage_ratio"].max()) if not trades.empty and "leverage_ratio" in trades.columns else None,
        }

    def summary_table(self) -> pd.DataFrame:
        stats = self.summary_stats()
        rows = [
            ("Symbol", stats["symbol"]),
            ("Start Date", stats["start_date"]),
            ("End Date", stats["end_date"]),
            ("Initial Cash", self._fmt_currency(stats["initial_cash"])),
            ("Ending Equity", self._fmt_currency(stats["ending_equity"])),
            ("Net Profit", self._fmt_currency(stats["net_profit"])),
            ("Cash Interest Earned", self._fmt_currency(stats["total_cash_interest"])),
            ("Total Return", self._fmt_percent(stats["total_return"])),
            ("CAGR", self._fmt_percent(stats["cagr"])),
            ("Sharpe", self._fmt_decimal(stats["sharpe"])),
            ("Underlying Sharpe", self._fmt_decimal(stats["underlying_sharpe"])),
            ("Max Drawdown", self._fmt_percent(stats["max_drawdown"])),
            ("Completed Legs", int(stats["completed_legs"])),
            ("Puts Sold", int(stats["puts_sold"])),
            ("Assignments", int(stats["assignments"])),
            ("Cut Losses", int(stats["cut_losses"])),
            ("Take Profits", int(stats["take_profits"])),
            ("Put Assignment Rate", self._fmt_percent(stats["put_assignment_rate"])),
            ("Put Cut Loss Rate", self._fmt_percent(stats["put_cut_loss_rate"])),
            ("Put Take Profit Rate", self._fmt_percent(stats["put_take_profit_rate"])),
            ("Total Premium Collected", self._fmt_currency(stats["total_premium_collected"])),
            ("Avg Premium / Leg", self._fmt_currency(stats["avg_premium"])),
            ("Avg Liquidation Cost", self._fmt_currency(stats["avg_liquidation_cost"])),
            ("Avg IV", self._fmt_percent(stats["avg_iv"])),
            ("Avg |Delta|", self._fmt_decimal(stats["avg_abs_delta"])),
            ("Avg Leverage Ratio", self._fmt_decimal(stats["avg_leverage_ratio"])),
            ("Max Leverage Ratio", self._fmt_decimal(stats["max_leverage_ratio"])),
        ]
        if stats["calls_sold"]:
            rows[11:11] = [
                ("Calls Sold", int(stats["calls_sold"])),
                ("Call Aways", int(stats["call_aways"])),
                ("Call Away Rate", self._fmt_percent(stats["call_away_rate"])),
            ]
        return pd.DataFrame(rows, columns=["Metric", "Value"]).set_index("Metric")

    def trade_breakdown_table(self) -> pd.DataFrame:
        if self.trades.empty:
            return pd.DataFrame()

        grouped = (
            self.trades.groupby("type")
            .agg(
                legs=("type", "size"),
                total_premium=("premium", "sum"),
                avg_premium=("premium", "mean"),
                avg_roi=("ROI%", lambda s: s.mean() / 100),
                avg_iv=("iv", "mean"),
                avg_abs_delta=("delta", lambda s: s.abs().mean()),
                avg_moneyness=("moneyness", lambda s: s.mean() / 100),
                avg_days_held=("days_held", "mean"),
            )
            .sort_index()
        )
        for column in ("total_premium", "avg_premium"):
            grouped[column] = grouped[column].map(self._fmt_currency)
        for column in ("avg_iv", "avg_roi", "avg_moneyness"):
            grouped[column] = grouped[column].map(self._fmt_percent)
        for column in ("avg_abs_delta", "avg_days_held"):
            grouped[column] = grouped[column].map(self._fmt_decimal)
        return grouped

    def plot_equity_and_drawdown(self) -> plt.Figure:
        drawdown = self._drawdown_series(self.equity_curve)
        underlying_drawdown = self._drawdown_series(self.underlying_curve)
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 5), sharex=True)

        ax1.plot(self.equity_curve.index, self.equity_curve.values, label="Strategy", color="blue", linewidth=2)
        if not self.underlying_curve.empty:
            ax1.plot(
                self.underlying_curve.index,
                self.underlying_curve.values,
                label=f"{self.result.symbol} Buy & Hold",
                color="black",
                linestyle="--",
                linewidth=1.75,
                alpha=0.8,
            )
        ax1.set_title(f"Equity Curve for {self.result.symbol}", fontsize=14, fontweight="bold")
        ax1.set_ylabel("Equity ($)", fontsize=12)
        ax1.grid(True, alpha=0.3)
        ax1.legend(fontsize=11)
        ax1.set_xlabel("Date", fontsize=12)

        ax2.fill_between(drawdown.index, drawdown.values, 0, color="red", alpha=0.3, label="Strategy")
        ax2.plot(drawdown.index, drawdown.values, color="red", linewidth=2)
        if not underlying_drawdown.empty:
            ax2.plot(
                underlying_drawdown.index,
                underlying_drawdown.values,
                color="black",
                linestyle="--",
                linewidth=1.75,
                alpha=0.8,
                label=f"{self.result.symbol} Buy & Hold",
            )
        ax2.set_title(f"Drawdown for {self.result.symbol}", fontsize=14, fontweight="bold")
        ax2.set_ylabel("Drawdown (%)", fontsize=12)
        ax2.set_xlabel("Date", fontsize=12)
        ax2.grid(True, alpha=0.3)
        ax2.legend(fontsize=11)
        ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:.1%}"))

        plt.tight_layout()
        return fig

    def _build_underlying_curve(self) -> pd.Series:
        daily_pnl = self.result.daily_pnl
        if daily_pnl.empty or "spot" not in daily_pnl.columns:
            return pd.Series(dtype=float)

        spot_series = daily_pnl.set_index("date")["spot"].sort_index().dropna()
        if spot_series.empty:
            return pd.Series(dtype=float)

        starting_spot = float(spot_series.iloc[0])
        if starting_spot == 0:
            return pd.Series(dtype=float)
        return spot_series / starting_spot * self.result.initial_cash

    @staticmethod
    def _drawdown_series(curve: pd.Series) -> pd.Series:
        if curve.empty:
            return pd.Series(dtype=float)
        running_max = curve.cummax()
        return curve.div(running_max).sub(1.0)

    @staticmethod
    def _fmt_currency(value: float | None) -> str:
        if value is None or pd.isna(value):
            return "-"
        return f"${value:,.2f}"

    @staticmethod
    def _fmt_percent(value: float | None) -> str:
        if value is None or pd.isna(value):
            return "-"
        return f"{value:.2%}"

    @staticmethod
    def _fmt_decimal(value: float | None) -> str:
        if value is None or pd.isna(value):
            return "-"
        return f"{value:,.2f}"
