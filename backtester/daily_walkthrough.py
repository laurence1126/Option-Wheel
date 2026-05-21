from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from backtester.backtest import OptionLeg, WheelBacktester, WheelConfig


@dataclass(slots=True)
class DailyWalkthroughResult:
    symbol: str
    start_date: pd.Timestamp
    end_date: pd.Timestamp
    pnl_series: pd.Series
    trades: pd.DataFrame


class DailyWheelWalkthrough:
    def __init__(self, config: WheelConfig) -> None:
        self.config = config
        self.engine = WheelBacktester(config)
        self.loader = self.engine.loader

    def run(self) -> DailyWalkthroughResult:
        price_history = self.loader.build_price_history()
        start_ts = pd.Timestamp(self.config.start_date)
        end_ts = pd.Timestamp(self.config.end_date)
        walkthrough_history = price_history.loc[(price_history.index >= start_ts) & (price_history.index <= end_ts)].copy()
        if walkthrough_history.empty:
            raise ValueError("No equity price history is available for the requested date range.")

        rows: list[dict[str, Any]] = []
        for entry_ts, entry_row in walkthrough_history.iterrows():
            spot = float(entry_row["close"])
            chain = self.loader.get_chain(entry_ts.date())
            selected = self.engine._select_put(chain, spot, self.config.initial_cash * self.config.leverage)
            if selected is None:
                rows.append(self._no_trial_row(entry_ts, spot, "no_candidate"))
                continue

            expiration = pd.Timestamp(selected["expiration_date"])
            if expiration <= entry_ts:
                rows.append(self._selected_no_trial_row(entry_ts, spot, selected, "invalid_expiration"))
                continue

            try:
                premium = self.engine._entry_price(selected)
            except ValueError:
                rows.append(self._selected_no_trial_row(entry_ts, spot, selected, "missing_entry_price"))
                continue

            leg = OptionLeg(
                id=str(selected["option_symbol"]),
                type=self.engine._option_type(str(selected["call_put"])),
                strike=float(selected["price_strike"]),
                expiration=expiration,
                premium=premium,
                iv=self.engine._optional_float(selected.get("iv")),
                delta=self.engine._optional_float(selected.get("delta")),
                date=entry_ts,
                leverage_ratio=None,
            )
            rows.append(self._simulate_trial(price_history, entry_ts, spot, leg))

        trades = pd.DataFrame(rows)
        if not trades.empty:
            for column in ("date", "expiration", "exit_date"):
                if column in trades.columns:
                    trades[column] = pd.to_datetime(trades[column])

        pnl_series = pd.to_numeric(trades.set_index("date")["pnl"], errors="coerce").sort_index()
        pnl_series = pnl_series.reindex(walkthrough_history.index)
        pnl_series.name = f"{self.config.symbol}_daily_walkthrough_pnl"

        return DailyWalkthroughResult(
            symbol=self.config.symbol,
            start_date=walkthrough_history.index[0],
            end_date=walkthrough_history.index[-1],
            pnl_series=pnl_series,
            trades=trades,
        )

    def _simulate_trial(self, price_history: pd.DataFrame, entry_ts: pd.Timestamp, entry_spot: float, leg: OptionLeg) -> dict[str, Any]:
        trial_history = price_history.loc[(price_history.index > entry_ts) & (price_history.index <= leg.expiration)]
        if trial_history.empty or leg.expiration not in trial_history.index:
            return self._leg_row(entry_ts, entry_spot, leg, "missing_expiration")

        for trade_ts, row in trial_history.iterrows():
            spot = float(row["close"])

            if trade_ts == leg.expiration:
                return self._expiration_row(trade_ts, spot, entry_spot, leg)

            stop_price = self.engine._put_stop_loss_price(leg)
            if stop_price is not None:
                stop_check_price = self.engine._option_price(leg, trade_ts.date(), spot, price_column="price_high")
                if stop_check_price >= stop_price:
                    stop_open_price = self.engine._option_price(leg, trade_ts.date(), spot, price_column="price_open")
                    buyback_price = stop_price if stop_open_price < stop_price else stop_open_price
                    cash_flow = self.engine._premium_cash_flow(leg) - buyback_price * self.config.shares_per_contract
                    return self._leg_row(
                        entry_ts,
                        entry_spot,
                        leg,
                        "cut_loss",
                        exit_date=trade_ts,
                        exit_spot=spot,
                        cash_flow=cash_flow,
                    )

            take_profit_price = self.engine._put_take_profit_price(leg)
            if take_profit_price is not None:
                current_option_price = self.engine._option_price(leg, trade_ts.date(), spot)
                if current_option_price < take_profit_price:
                    cash_flow = self.engine._premium_cash_flow(leg) - current_option_price * self.config.shares_per_contract
                    return self._leg_row(
                        entry_ts,
                        entry_spot,
                        leg,
                        "take_profit",
                        exit_date=trade_ts,
                        exit_spot=spot,
                        cash_flow=cash_flow,
                    )

        return self._leg_row(entry_ts, entry_spot, leg, "missing_expiration")

    def _expiration_row(self, exit_ts: pd.Timestamp, exit_spot: float, entry_spot: float, leg: OptionLeg) -> dict[str, Any]:
        premium_cash_flow = self.engine._premium_cash_flow(leg)
        stock_cash_flow = 0.0
        outcome = "expired"
        if leg.type == "put" and exit_spot < leg.strike:
            stock_cash_flow = -leg.strike * self.config.shares_per_contract
            stock_cash_flow += exit_spot * self.config.shares_per_contract
            outcome = "assigned_liquidated"
        return self._leg_row(
            leg.date,
            entry_spot,
            leg,
            outcome,
            exit_date=exit_ts,
            exit_spot=exit_spot,
            cash_flow=premium_cash_flow + stock_cash_flow,
        )

    def _leg_row(
        self,
        entry_ts: pd.Timestamp,
        entry_spot: float,
        leg: OptionLeg,
        outcome: str,
        *,
        exit_date: pd.Timestamp | None = None,
        exit_spot: float | None = None,
        cash_flow: float | None = None,
    ) -> dict[str, Any]:
        premium_cash_flow = self.engine._premium_cash_flow(leg)
        return {
            "date": entry_ts,
            "exit_date": exit_date,
            "side": "short",
            "type": leg.type,
            "strike": leg.strike,
            "expiration": leg.expiration,
            "premium": premium_cash_flow,
            "delta": leg.delta,
            "moneyness": (leg.strike - entry_spot) / entry_spot * 100 if entry_spot != 0 else None,
            "iv": leg.iv,
            "spot": entry_spot,
            "exit_spot": exit_spot,
            "outcome": outcome,
            "pnl": cash_flow,
        }

    def _no_trial_row(self, entry_ts: pd.Timestamp, spot: float, outcome: str) -> dict[str, Any]:
        return {
            "date": entry_ts,
            "exit_date": None,
            "side": None,
            "type": "put",
            "strike": None,
            "expiration": None,
            "premium": None,
            "delta": None,
            "moneyness": None,
            "iv": None,
            "spot": spot,
            "exit_spot": None,
            "outcome": outcome,
            "pnl": None,
        }

    def _selected_no_trial_row(self, entry_ts: pd.Timestamp, spot: float, selected: pd.Series, outcome: str) -> dict[str, Any]:
        expiration = pd.Timestamp(selected["expiration_date"])
        return {
            **self._no_trial_row(entry_ts, spot, outcome),
            "side": "short",
            "strike": float(selected["price_strike"]),
            "expiration": expiration,
            "delta": self.engine._optional_float(selected.get("delta")),
            "iv": self.engine._optional_float(selected.get("iv")),
            "moneyness": (float(selected["price_strike"]) - spot) / spot * 100 if spot != 0 else None,
        }


def run_daily_walkthrough(
    symbol: str,
    start_date: str | date,
    end_date: str | date,
    target_delta: float = 0.15,
    stop_loss_multiple: float | None = 3.0,
    take_profit_multiple: float | None = None,
    put_exp_days: int = 25,
) -> DailyWalkthroughResult:
    config = WheelConfig(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        initial_cash=1_000_000,
        leverage=100,
        rf_series="DGS3MO",
        rf_penalty_multiple=0.85,
        rf_path=None,
        refresh_rf=False,
        target_delta=target_delta,
        stop_loss_multiple=stop_loss_multiple,
        take_profit_multiple=take_profit_multiple,
        put_exp_days=put_exp_days,
        call_exp_days=0,
        data_root="data",
    )
    return DailyWheelWalkthrough(config).run()
