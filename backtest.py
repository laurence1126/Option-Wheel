from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from option_data_loader import OptionDataLoader
from rf_loader import load_rf_rates


@dataclass(slots=True)
class WheelConfig:
    symbol: str
    start_date: str | date
    end_date: str | date
    initial_cash: float = 100_000.0
    leverage: float = 1.0
    rf_series: str = "DGS3MO"
    rf_penalty_multiple: float = 0.85
    rf_path: str | None = None
    refresh_rf: bool = False
    target_delta: float = 0.15
    stop_loss_multiple: float | None = 3.0
    take_profit_multiple: float | None = None
    put_exp_days: int = 25
    call_exp_days: int = 25
    shares_per_contract: int = 100
    data_root: str = "data"


@dataclass(slots=True)
class OptionLeg:
    id: str
    type: str
    strike: float
    expiration: pd.Timestamp
    premium: float
    iv: float | None
    delta: float | None
    date: pd.Timestamp
    leverage_ratio: float | None


@dataclass(slots=True)
class BacktestResult:
    symbol: str
    start_date: pd.Timestamp
    end_date: pd.Timestamp
    initial_cash: float
    trades: pd.DataFrame
    events: pd.DataFrame
    daily_pnl: pd.DataFrame
    equity_curve: pd.Series
    ending_cash: float
    ending_shares: int
    ending_spot: float
    option_position: float
    ending_equity: float


class WheelBacktester:
    def __init__(self, config: WheelConfig) -> None:
        self.config = config
        self.loader = OptionDataLoader(symbol=config.symbol, data_root=config.data_root)

    def run(self) -> BacktestResult:
        price_history = self.loader.build_price_history()
        start_ts = pd.Timestamp(self.config.start_date)
        end_ts = pd.Timestamp(self.config.end_date)
        price_history = price_history.loc[(price_history.index >= start_ts) & (price_history.index <= end_ts)].copy()
        if price_history.empty:
            raise ValueError("No equity price history is available for the requested date range.")
        rf_rates = load_rf_rates(
            price_history.index,
            data_root=self.config.data_root,
            series=self.config.rf_series,
            cache_path=self.config.rf_path,
            refresh=self.config.refresh_rf,
        )

        cash = float(self.config.initial_cash)
        shares = 0
        assigned_cost: float | None = None
        open_leg: OptionLeg | None = None
        exec_spot: float | None = None

        trades: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = [
            self._event_row(
                when=price_history.index[0],
                event="start",
                spot=float(price_history.iloc[0]["close"]),
                cash=cash,
                shares=shares,
                cost=assigned_cost,
                open_contract=None,
                option_position=0.0,
            )
        ]
        daily_rows: list[dict[str, Any]] = []
        previous_cash = cash
        previous_spot = float(price_history.iloc[0]["close"])
        previous_shares = 0
        previous_option_price = 0.0
        previous_option_contracts = 0
        previous_stock_value = 0.0
        previous_option_position = 0.0

        for trade_ts, row in price_history.iterrows():
            spot = float(row["close"])
            stock_cash_flow_today = 0.0
            option_trade_cash_flow_today = 0.0
            option_expiry_cash_flow_today = 0.0
            option_stop_loss_cash_flow_today = 0.0
            option_take_profit_cash_flow_today = 0.0
            cash_interest_today = 0.0
            settled_today = False

            raw_rf_today = float(rf_rates.loc[trade_ts])
            rf_today = raw_rf_today * self.config.rf_penalty_multiple
            if daily_rows and rf_today > 0:
                cash_interest_today = max(cash, 0.0) * rf_today / 252.0
                cash += cash_interest_today

            if open_leg is not None and trade_ts == open_leg.expiration:
                outcome = "expired"

                if open_leg.type == "put" and spot < open_leg.strike:
                    stock_cash_flow_today = -open_leg.strike * self.config.shares_per_contract
                    cash += stock_cash_flow_today
                    shares = self.config.shares_per_contract
                    assigned_cost = open_leg.strike
                    outcome = "assigned"
                    liquidation_cost = None
                    if self.config.call_exp_days == 0:
                        liquidation_cash_flow = spot * self.config.shares_per_contract
                        stock_cash_flow_today += liquidation_cash_flow
                        cash += liquidation_cash_flow
                        shares = 0
                        assigned_cost = None
                        liquidation_cost = (open_leg.strike - spot) * self.config.shares_per_contract
                        outcome = "assigned_liquidated"
                elif open_leg.type == "call" and spot > open_leg.strike:
                    stock_cash_flow_today = open_leg.strike * self.config.shares_per_contract
                    cash += stock_cash_flow_today
                    shares = 0
                    assigned_cost = None
                    outcome = "called_away"
                    liquidation_cost = None
                else:
                    liquidation_cost = None

                premium_cash_flow = self._premium_cash_flow(open_leg)
                trades.append(
                    {
                        "date": open_leg.date,
                        "side": "short",
                        "type": open_leg.type,
                        "strike": open_leg.strike,
                        "expiration": open_leg.expiration,
                        "premium_per_share": open_leg.premium,
                        "premium": premium_cash_flow,
                        "delta": open_leg.delta,
                        "leverage_ratio": open_leg.leverage_ratio,
                        "moneyness": (open_leg.strike - exec_spot) / exec_spot * 100 if exec_spot not in (None, 0) else None,
                        "iv": open_leg.iv,
                        "outcome": outcome,
                        "days_held": int((open_leg.expiration - open_leg.date).days),
                        "ROI%": premium_cash_flow / (open_leg.strike * self.config.shares_per_contract) * 100 if open_leg.strike != 0 else None,
                        "cash_flow": premium_cash_flow + option_expiry_cash_flow_today + stock_cash_flow_today,
                        "liquidation_cost": liquidation_cost,
                        "nav": cash + shares * spot,
                        "exec_spot": exec_spot,
                        "spot": spot,
                    }
                )
                events.append(
                    self._event_row(
                        when=trade_ts,
                        event=f"short_{open_leg.type}_expiry",
                        spot=spot,
                        cash=cash,
                        shares=shares,
                        cost=assigned_cost,
                        open_contract=None,
                        option_position=0.0,
                    )
                )
                open_leg = None
                settled_today = True

            if open_leg is not None and not settled_today:
                stop_check_price = self._option_price(open_leg, trade_ts.date(), spot, price_column="price_high")
                stop_price = self._put_stop_loss_price(open_leg)
                if open_leg.type == "put" and stop_price is not None and stop_check_price > stop_price:
                    stop_open_price = self._option_price(open_leg, trade_ts.date(), spot, price_column="price_open")
                    buyback_price = stop_price if stop_open_price < stop_price else stop_open_price
                    option_stop_loss_cash_flow_today = -buyback_price * self.config.shares_per_contract
                    cash += option_stop_loss_cash_flow_today
                    premium_cash_flow = self._premium_cash_flow(open_leg)
                    trades.append(
                        {
                            "date": open_leg.date,
                            "side": "short",
                            "type": open_leg.type,
                            "strike": open_leg.strike,
                            "expiration": open_leg.expiration,
                            "premium_per_share": open_leg.premium,
                            "premium": premium_cash_flow,
                            "delta": open_leg.delta,
                            "leverage_ratio": open_leg.leverage_ratio,
                            "moneyness": (open_leg.strike - exec_spot) / exec_spot * 100 if exec_spot not in (None, 0) else None,
                            "iv": open_leg.iv,
                            "outcome": "cut_loss",
                            "days_held": int((trade_ts - open_leg.date).days),
                            "ROI%": (
                                (premium_cash_flow + option_stop_loss_cash_flow_today) / (open_leg.strike * self.config.shares_per_contract) * 100
                                if open_leg.strike != 0
                                else None
                            ),
                            "cash_flow": premium_cash_flow + option_stop_loss_cash_flow_today,
                            "nav": cash + shares * spot,
                            "exec_spot": exec_spot,
                            "spot": spot,
                            "buyback_price_per_share": buyback_price,
                            "stop_trigger_price_per_share": stop_check_price,
                            "stop_open_price_per_share": stop_open_price,
                        }
                    )
                    events.append(
                        self._event_row(
                            when=trade_ts,
                            event="short_put_cut_loss",
                            spot=spot,
                            cash=cash,
                            shares=shares,
                            cost=assigned_cost,
                            open_contract=None,
                            option_position=0.0,
                        )
                    )
                    open_leg = None
                    settled_today = True

            if open_leg is not None and not settled_today:
                current_option_price = self._option_price(open_leg, trade_ts.date(), spot)
                take_profit_price = self._put_take_profit_price(open_leg)
                if open_leg.type == "put" and take_profit_price is not None and current_option_price < take_profit_price:
                    buyback_price = current_option_price
                    option_take_profit_cash_flow_today = -buyback_price * self.config.shares_per_contract
                    cash += option_take_profit_cash_flow_today
                    premium_cash_flow = self._premium_cash_flow(open_leg)
                    trades.append(
                        {
                            "date": open_leg.date,
                            "side": "short",
                            "type": open_leg.type,
                            "strike": open_leg.strike,
                            "expiration": open_leg.expiration,
                            "premium_per_share": open_leg.premium,
                            "premium": premium_cash_flow,
                            "delta": open_leg.delta,
                            "leverage_ratio": open_leg.leverage_ratio,
                            "moneyness": (open_leg.strike - exec_spot) / exec_spot * 100 if exec_spot not in (None, 0) else None,
                            "iv": open_leg.iv,
                            "outcome": "take_profit",
                            "days_held": int((trade_ts - open_leg.date).days),
                            "ROI%": (
                                (premium_cash_flow + option_take_profit_cash_flow_today) / (open_leg.strike * self.config.shares_per_contract) * 100
                                if open_leg.strike != 0
                                else None
                            ),
                            "cash_flow": premium_cash_flow + option_take_profit_cash_flow_today,
                            "nav": cash + shares * spot,
                            "exec_spot": exec_spot,
                            "spot": spot,
                            "buyback_price_per_share": buyback_price,
                        }
                    )
                    events.append(
                        self._event_row(
                            when=trade_ts,
                            event="short_put_take_profit",
                            spot=spot,
                            cash=cash,
                            shares=shares,
                            cost=assigned_cost,
                            open_contract=None,
                            option_position=0.0,
                        )
                    )
                    open_leg = None
                    settled_today = True

            if open_leg is None and not settled_today:
                chain = self.loader.get_chain(trade_ts.date())
                exec_spot = spot
                selected = None

                if shares == 0:
                    selected = self._select_put(chain, spot, cash * self.config.leverage)
                elif shares == self.config.shares_per_contract:
                    if self.config.call_exp_days == 0:
                        stock_cash_flow_today = spot * self.config.shares_per_contract
                        cash += stock_cash_flow_today
                        shares = 0
                        assigned_cost = None
                        settled_today = True
                        events.append(
                            self._event_row(
                                when=trade_ts,
                                event="liquidate_underlying",
                                spot=spot,
                                cash=cash,
                                shares=shares,
                                cost=assigned_cost,
                                open_contract=None,
                                option_position=0.0,
                            )
                        )
                    else:
                        selected = self._select_call(chain, assigned_cost or 0.0)
                else:
                    raise ValueError(f"Unsupported share quantity in wheel state: {shares}")

                if selected is not None:
                    premium = self._entry_price(selected)
                    expiration = pd.Timestamp(selected["expiration_date"])
                    if expiration > trade_ts:
                        option_trade_cash_flow_today = premium * self.config.shares_per_contract
                        cash += option_trade_cash_flow_today
                        entry_option_position = self._option_position_from_price(premium)
                        open_leg = OptionLeg(
                            id=str(selected["option_symbol"]),
                            type=self._option_type(str(selected["call_put"])),
                            strike=float(selected["price_strike"]),
                            expiration=expiration,
                            premium=premium,
                            iv=self._optional_float(selected.get("iv")),
                            delta=self._optional_float(selected.get("delta")),
                            date=trade_ts,
                            leverage_ratio=self._leverage_ratio(
                                strike=float(selected["price_strike"]),
                                cash=cash,
                                shares=shares,
                                spot=spot,
                                option_position=entry_option_position,
                            ),
                        )
                        events.append(
                            self._event_row(
                                when=trade_ts,
                                event=f"sell_{open_leg.type}",
                                spot=spot,
                                cash=cash,
                                shares=shares,
                                cost=assigned_cost,
                                open_contract=open_leg.id,
                                option_position=entry_option_position,
                            )
                        )

            option_price = 0.0
            option_contracts = 0
            option_position = 0.0
            if open_leg is not None:
                option_price = self._option_price(open_leg, trade_ts.date(), spot)
                option_contracts = -1
                option_position = self._option_position_from_price(option_price)

            stock_value = shares * spot
            cash_change = cash - previous_cash
            spot_change = 0.0 if not daily_rows else spot - previous_spot
            share_change = shares - previous_shares
            stock_value_change = stock_value - previous_stock_value
            option_price_change = option_price - previous_option_price
            option_contract_change = option_contracts - previous_option_contracts
            option_position_change = option_position - previous_option_position
            stock_pnl = stock_value - previous_stock_value + stock_cash_flow_today
            option_pnl = (
                option_position
                - previous_option_position
                + option_trade_cash_flow_today
                + option_expiry_cash_flow_today
                + option_stop_loss_cash_flow_today
                + option_take_profit_cash_flow_today
            )
            total_pnl = stock_pnl + option_pnl + cash_interest_today
            equity = cash + stock_value + option_position
            nav_change = 0.0 if not daily_rows else total_pnl

            daily_rows.append(
                {
                    "date": trade_ts,
                    "spot": spot,
                    "spot_change": spot_change,
                    "cash": cash,
                    "cash_change": cash_change,
                    "shares": shares,
                    "share_change": share_change,
                    "stock_value": stock_value,
                    "stock_value_change": stock_value_change,
                    "option_price": option_price,
                    "option_price_change": option_price_change,
                    "option_contracts": option_contracts,
                    "option_contract_change": option_contract_change,
                    "option_position": option_position,
                    "option_position_change": option_position_change,
                    "equity": equity,
                    "nav_change": nav_change,
                    "stock_cash_flow": stock_cash_flow_today,
                    "option_trade_cash_flow": option_trade_cash_flow_today,
                    "option_expiry_cash_flow": option_expiry_cash_flow_today,
                    "option_stop_loss_cash_flow": option_stop_loss_cash_flow_today,
                    "option_take_profit_cash_flow": option_take_profit_cash_flow_today,
                    "raw_rf": raw_rf_today,
                    "rf": rf_today,
                    "cash_interest": cash_interest_today,
                    "stock_pnl": stock_pnl,
                    "option_pnl": option_pnl,
                    "cash_pnl": cash_interest_today,
                    "total_pnl": total_pnl,
                    "open_contract": open_leg.id if open_leg is not None else None,
                    "option_side": "short" if open_leg is not None else None,
                    "option_type": open_leg.type if open_leg is not None else None,
                    "option_strike": open_leg.strike if open_leg is not None else None,
                }
            )

            previous_cash = cash
            previous_spot = spot
            previous_shares = shares
            previous_option_price = option_price
            previous_option_contracts = option_contracts
            previous_stock_value = stock_value
            previous_option_position = option_position

        final_spot = float(price_history.iloc[-1]["close"])
        ending_cash = cash
        ending_shares = shares
        ending_option_position = daily_rows[-1]["option_position"] if daily_rows else 0.0
        ending_equity = daily_rows[-1]["equity"] if daily_rows else cash
        events.append(
            self._event_row(
                when=price_history.index[-1],
                event="final_state",
                spot=final_spot,
                cash=ending_cash,
                shares=ending_shares,
                cost=assigned_cost,
                open_contract=open_leg.id if open_leg is not None else None,
                option_position=ending_option_position,
            )
        )

        trades_frame = pd.DataFrame(trades)
        if not trades_frame.empty:
            trades_frame["date"] = pd.to_datetime(trades_frame["date"])
            trades_frame["expiration"] = pd.to_datetime(trades_frame["expiration"])

        events_frame = pd.DataFrame(events)
        events_frame["date"] = pd.to_datetime(events_frame["date"])

        daily_frame = pd.DataFrame(daily_rows)
        daily_frame["date"] = pd.to_datetime(daily_frame["date"])
        equity_curve = daily_frame.set_index("date")["equity"].sort_index()

        return BacktestResult(
            symbol=self.config.symbol,
            start_date=price_history.index[0],
            end_date=price_history.index[-1],
            initial_cash=self.config.initial_cash,
            trades=trades_frame,
            events=events_frame,
            daily_pnl=daily_frame,
            equity_curve=equity_curve,
            ending_cash=ending_cash,
            ending_shares=ending_shares,
            ending_spot=final_spot,
            option_position=ending_option_position,
            ending_equity=ending_equity,
        )

    def _select_put(self, chain: pd.DataFrame, spot: float, available_cash: float) -> pd.Series | None:
        candidates = chain.loc[
            (chain["call_put"] == "P")
            & (chain["dte"] >= self.config.put_exp_days)
            & ((chain["price_strike"] * self.config.shares_per_contract) <= available_cash)
        ].copy()
        if candidates.empty:
            return None

        nearest_dte = candidates["dte"].min()
        candidates = candidates.loc[candidates["dte"] == nearest_dte].copy()

        if candidates["delta"].notna().any():
            candidates["delta_distance"] = (candidates["delta"].abs() - self.config.target_delta).abs()
            candidates["delta_distance"] = candidates["delta_distance"].fillna(999.0)
            candidates = candidates.sort_values(["delta_distance", "price_strike"], ascending=[True, False])
            return candidates.iloc[0]

        below_spot = candidates.loc[candidates["price_strike"] <= spot]
        if not below_spot.empty:
            return below_spot.sort_values("price_strike", ascending=False).iloc[0]
        return candidates.sort_values("price_strike").iloc[0]

    def _select_call(self, chain: pd.DataFrame, minimum_strike: float) -> pd.Series | None:
        candidates = chain.loc[
            (chain["call_put"] == "C") & (chain["dte"] >= self.config.call_exp_days) & (chain["price_strike"] > minimum_strike)
        ].copy()
        if candidates.empty:
            return None

        nearest_dte = candidates["dte"].min()
        candidates = candidates.loc[candidates["dte"] == nearest_dte].copy()
        candidates["delta_distance"] = (candidates["delta"].abs() - self.config.target_delta).abs()
        candidates["delta_distance"] = candidates["delta_distance"].fillna(999.0)
        candidates = candidates.sort_values(["price_strike", "delta_distance"], ascending=[True, True])
        return candidates.iloc[0]

    @staticmethod
    def _entry_price(contract: pd.Series, price_column: str = "price") -> float:
        value = contract.get(price_column)
        if pd.notna(value):
            return float(value)

        value = contract.get("price")
        if pd.notna(value):
            return float(value)

        bid = contract.get("Bid")
        ask = contract.get("Ask")
        if pd.notna(bid) and pd.notna(ask):
            return float((float(bid) + float(ask)) / 2.0)
        raise ValueError(f"Cannot determine entry price for contract {contract.get('option_symbol')}")

    def _option_price(self, leg: OptionLeg, query_date: date, spot: float, price_column: str = "price") -> float:
        chain = self.loader.get_chain(query_date)
        matched = chain.loc[
            (chain["option_symbol"].astype(str) == leg.id)
            | (
                (chain["call_put"] == self._option_code(leg.type))
                & (chain["expiration_date"] == leg.expiration)
                & (chain["price_strike"] == leg.strike)
            )
        ]
        if not matched.empty:
            return self._entry_price(matched.iloc[0], price_column=price_column)

        if leg.type == "put":
            return max(0.0, leg.strike - spot)
        return max(0.0, spot - leg.strike)

    def _option_position_from_price(self, option_price: float) -> float:
        return -option_price * self.config.shares_per_contract

    def _premium_cash_flow(self, leg: OptionLeg) -> float:
        return leg.premium * self.config.shares_per_contract

    def _put_stop_loss_price(self, leg: OptionLeg) -> float | None:
        multiple = self.config.stop_loss_multiple
        if multiple is None or multiple <= 0:
            return None
        return leg.premium * multiple

    def _put_take_profit_price(self, leg: OptionLeg) -> float | None:
        multiple = self.config.take_profit_multiple
        if multiple is None or multiple <= 0:
            return None
        return leg.premium * multiple

    def _leverage_ratio(self, strike: float, cash: float, shares: int, spot: float, option_position: float) -> float | None:
        equity = cash + shares * spot + option_position
        if equity <= 0:
            return None
        return strike * self.config.shares_per_contract / equity

    @staticmethod
    def _option_type(call_put: str) -> str:
        return "put" if call_put == "P" else "call"

    @staticmethod
    def _option_code(option_type: str) -> str:
        return "P" if option_type == "put" else "C"

    @staticmethod
    def _event_row(
        when: pd.Timestamp,
        event: str,
        spot: float,
        cash: float,
        shares: int,
        cost: float | None,
        open_contract: str | None,
        option_position: float,
    ) -> dict[str, Any]:
        return {
            "date": pd.Timestamp(when),
            "event": event,
            "spot": spot,
            "cash": cash,
            "shares": shares,
            "cost": cost,
            "open_contract": open_contract,
            "option_position": option_position,
            "equity": cash + shares * spot + option_position,
        }

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if value is None or pd.isna(value):
            return None
        return float(value)


def run_wheel_backtest(
    symbol: str,
    start_date: str | date,
    end_date: str | date,
    target_delta: float = 0.15,
    stop_loss_multiple: float | None = 3.0,
    take_profit_multiple: float | None = None,
    put_exp_days: int = 25,
    call_exp_days: int = 25,
    initial_cash: float = 100_000.0,
    leverage: float = 1.0,
    rf_series: str = "DGS3MO",
    rf_penalty_multiple: float = 0.85,
    rf_path: str | None = None,
    refresh_rf: bool = False,
    data_root: str = "data",
) -> BacktestResult:
    config = WheelConfig(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        initial_cash=initial_cash,
        leverage=leverage,
        rf_series=rf_series,
        rf_penalty_multiple=rf_penalty_multiple,
        rf_path=rf_path,
        refresh_rf=refresh_rf,
        target_delta=target_delta,
        stop_loss_multiple=stop_loss_multiple,
        take_profit_multiple=take_profit_multiple,
        put_exp_days=put_exp_days,
        call_exp_days=call_exp_days,
        data_root=data_root,
    )
    return WheelBacktester(config=config).run()
