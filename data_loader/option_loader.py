from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf


class OptionDataLoader:
    def __init__(self, symbol: str, data_root: str = "data") -> None:
        self.symbol = symbol
        self.data_root = Path(data_root)
        self._data: pd.DataFrame | None = None
        self._data_month: str | None = None

    def load_data(self, query_date: date) -> pd.DataFrame:
        query_month = query_date.strftime("%Y-%m")
        if self._data_month == query_month and self._data is not None:
            return self._data

        symbol_dir = self.data_root / self.symbol
        file = symbol_dir / f"{query_month}.csv"
        if not file.exists():
            raise FileNotFoundError(f"File not found: {file}")

        frame = pd.read_csv(file)
        required = {
            "c_date",
            "option_symbol",
            "dte",
            "expiration_date",
            "call_put",
            "price_strike",
            "price_open",
            "price_high",
            "price_low",
            "price",
            "Ask",
            "Bid",
            "iv",
            "delta",
            "underlying_price",
        }
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"Missing required columns in local data: {sorted(missing)}")

        frame["c_date"] = pd.to_datetime(frame["c_date"]).dt.normalize()
        frame["expiration_date"] = pd.to_datetime(frame["expiration_date"]).dt.normalize()
        numeric_columns = [
            "dte",
            "price_strike",
            "price_open",
            "price_high",
            "price_low",
            "price",
            "Ask",
            "Bid",
            "volume",
            "openinterest",
            "iv",
            "delta",
            "gamma",
            "theta",
            "vega",
            "rho",
            "underlying_price",
        ]
        for column in numeric_columns:
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")

        saturday_expiration = frame["expiration_date"].dt.weekday == 5
        if saturday_expiration.any():
            frame.loc[saturday_expiration, "expiration_date"] = frame.loc[saturday_expiration, "expiration_date"] - pd.Timedelta(days=1)
            frame.loc[saturday_expiration, "dte"] = frame.loc[saturday_expiration, "dte"] - 1

        self._data = frame.sort_values(["c_date", "expiration_date", "price_strike", "call_put"]).reset_index(drop=True)
        self._data_month = query_month
        return self._data

    def build_price_history(self) -> pd.DataFrame:
        start_ts, end_ts = self._option_data_date_bounds()
        price_history = self._download_price_history(start_ts, end_ts)
        price_history = price_history.loc[(price_history.index >= start_ts) & (price_history.index <= end_ts)].copy()
        if price_history.empty:
            raise ValueError(f"yfinance returned no market data for {self.symbol} between {start_ts.date()} and {end_ts.date()}.")

        price_history["symbol"] = self.symbol
        return price_history[["open", "high", "low", "close", "volume", "symbol"]]

    def get_chain(self, query_date: date) -> pd.DataFrame:
        data = self.load_data(query_date)
        query_ts = pd.Timestamp(query_date).normalize()
        return data.loc[data["c_date"] == query_ts].copy()

    def _option_data_date_bounds(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        symbol_dir = self.data_root / self.symbol
        files = sorted(symbol_dir.glob("*.csv"))
        if not files:
            raise FileNotFoundError(f"No local option data files found in: {symbol_dir}")

        min_dates: list[pd.Timestamp] = []
        max_dates: list[pd.Timestamp] = []
        for file in (files[0], files[-1]):
            try:
                frame = pd.read_csv(file, usecols=["c_date"])
            except ValueError as exc:
                raise ValueError(f"Missing required column c_date in local data: {file}") from exc

            c_dates = pd.to_datetime(frame["c_date"], errors="coerce").dropna()
            if c_dates.empty:
                continue
            min_dates.append(c_dates.min().normalize())
            max_dates.append(c_dates.max().normalize())

        if not min_dates or not max_dates:
            raise ValueError(f"No valid c_date values found in local option data: {symbol_dir}")
        return min(min_dates), max(max_dates)

    def _download_price_history(self, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
        data = yf.download(
            self.symbol,
            start=start_ts.date().isoformat(),
            end=(end_ts + pd.Timedelta(days=1)).date().isoformat(),
            progress=False,
            auto_adjust=False,
        )
        if data.empty:
            raise ValueError(f"yfinance returned no market data for {self.symbol}.")

        if isinstance(data.columns, pd.MultiIndex):
            if self.symbol in data.columns.get_level_values(-1):
                data = data.xs(self.symbol, axis=1, level=-1)
            elif self.symbol in data.columns.get_level_values(0):
                data = data.xs(self.symbol, axis=1, level=0)
            else:
                data.columns = data.columns.get_level_values(0)

        required = {"Open", "High", "Low", "Close", "Volume"}
        missing = required - set(data.columns)
        if missing:
            raise ValueError(f"yfinance market data is missing columns: {sorted(missing)}")

        price_history = data.loc[:, ["Open", "High", "Low", "Close", "Volume"]].rename(
            columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )
        price_history.index = pd.to_datetime(price_history.index).normalize()
        for column in ("open", "high", "low", "close", "volume"):
            price_history[column] = pd.to_numeric(price_history[column], errors="coerce")
        return price_history.sort_index()
