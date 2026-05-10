from __future__ import annotations

from pathlib import Path

import pandas as pd

SUPPORTED_RISK_FREE_SERIES = ("DGS1MO", "DGS3MO", "DGS6MO", "DTB3", "EFFR", "SOFR")
FRED_GRAPH_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def load_rf_rates(
    dates: pd.Index,
    data_root: str = "data",
    series: str = "DGS3MO",
    cache_path: str | None = None,
    refresh: bool = False,
) -> pd.Series:
    series = series.upper()
    if series not in SUPPORTED_RISK_FREE_SERIES:
        supported = ", ".join(SUPPORTED_RISK_FREE_SERIES)
        raise ValueError(f"Unsupported rf series {series!r}. Choose one of: {supported}.")

    path = Path(cache_path) if cache_path is not None else Path(data_root) / "risk_free" / f"{series}.csv"
    if refresh or not path.exists():
        frame = pd.read_csv(f"{FRED_GRAPH_URL}?id={series}")
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path, index=False)
    else:
        frame = pd.read_csv(path)

    rates = _normalize_rate_frame(frame, series)
    query_index = pd.DatetimeIndex(dates).normalize()
    aligned = rates.reindex(rates.index.union(query_index)).sort_index().ffill().bfill()
    return aligned.reindex(query_index).rename("rf")


def _normalize_rate_frame(frame: pd.DataFrame, series: str) -> pd.Series:
    if "observation_date" in frame.columns:
        date_column = "observation_date"
    elif "date" in frame.columns:
        date_column = "date"
    else:
        raise ValueError(f"{series} data must include an observation_date or date column.")

    if series in frame.columns:
        rate_column = series
    elif "rf" in frame.columns:
        rate_column = "rf"
    else:
        raise ValueError(f"{series} data must include a {series} or rf column.")

    data = frame[[date_column, rate_column]].copy()
    data[date_column] = pd.to_datetime(data[date_column]).dt.normalize()
    data[rate_column] = pd.to_numeric(data[rate_column], errors="coerce")
    data = data.dropna().sort_values(date_column)

    rates = data.set_index(date_column)[rate_column]
    if not rates.empty and rates.abs().max() > 1.0:
        rates = rates / 100.0
    return rates
