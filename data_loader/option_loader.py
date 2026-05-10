from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd


class OptionDataLoader:
    def __init__(self, symbol: str, data_root: str = "data") -> None:
        self.symbol = symbol
        self.data_root = Path(data_root)
        self._data: pd.DataFrame | None = None
        self._chain_cache: dict[date, pd.DataFrame] = {}

    def load_data(self) -> pd.DataFrame:
        if self._data is not None:
            return self._data

        symbol_dir = self.data_root / self.symbol
        files = sorted(symbol_dir.glob("*.csv"))
        if not files:
            raise FileNotFoundError(f"No CSV files found under {symbol_dir}")

        frame = pd.concat((pd.read_csv(path) for path in files), ignore_index=True)
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

        self._data = frame.sort_values(["c_date", "expiration_date", "price_strike", "call_put"]).reset_index(drop=True)
        return self._data

    def build_price_history(self) -> pd.DataFrame:
        data = self.load_data()
        price_history = data.groupby("c_date", as_index=True)["underlying_price"].first().to_frame(name="close").sort_index()
        price_history["open"] = price_history["close"]
        price_history["high"] = price_history["close"]
        price_history["low"] = price_history["close"]
        price_history["volume"] = 0.0
        return price_history[["open", "high", "low", "close", "volume"]]

    def get_chain(self, query_date: date) -> pd.DataFrame:
        if query_date not in self._chain_cache:
            query_ts = pd.Timestamp(query_date).normalize()
            data = self.load_data()
            self._chain_cache[query_date] = data.loc[data["c_date"] == query_ts].copy()
        return self._chain_cache[query_date]
