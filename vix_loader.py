from __future__ import annotations

from pathlib import Path

import pandas as pd


SUPPORTED_VIX_SERIES = ("VIXCLS", "VXVCLS", "VXNCLS", "RVXCLS")
FRED_GRAPH_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def load_vix(
    dates: pd.Index,
    data_root: str = "data",
    series: str = "VIXCLS",
    cache_path: str | None = None,
    refresh: bool = False,
) -> pd.Series:
    series = series.upper()
    if series not in SUPPORTED_VIX_SERIES:
        supported = ", ".join(SUPPORTED_VIX_SERIES)
        raise ValueError(f"Unsupported VIX series {series!r}. Choose one of: {supported}.")

    path = Path(cache_path) if cache_path is not None else Path(data_root) / "market" / f"{series}.csv"
    if refresh or not path.exists():
        frame = pd.read_csv(f"{FRED_GRAPH_URL}?id={series}")
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path, index=False)
    else:
        frame = pd.read_csv(path)

    vix = _normalize_vix_frame(frame, series)
    query_index = pd.DatetimeIndex(dates).normalize()
    aligned = vix.reindex(vix.index.union(query_index)).sort_index().ffill().bfill()
    return aligned.reindex(query_index).rename("vix")


def load_vixcls(
    dates: pd.Index,
    data_root: str = "data",
    cache_path: str | None = None,
    refresh: bool = False,
) -> pd.Series:
    return load_vix(dates, data_root=data_root, series="VIXCLS", cache_path=cache_path, refresh=refresh)


def _normalize_vix_frame(frame: pd.DataFrame, series: str) -> pd.Series:
    if "observation_date" in frame.columns:
        date_column = "observation_date"
    elif "date" in frame.columns:
        date_column = "date"
    else:
        raise ValueError(f"{series} data must include an observation_date or date column.")

    if series in frame.columns:
        vix_column = series
    elif "vix" in frame.columns:
        vix_column = "vix"
    else:
        raise ValueError(f"{series} data must include a {series} or vix column.")

    data = frame[[date_column, vix_column]].copy()
    data[date_column] = pd.to_datetime(data[date_column]).dt.normalize()
    data[vix_column] = pd.to_numeric(data[vix_column], errors="coerce")
    data = data.dropna().sort_values(date_column)
    return data.set_index(date_column)[vix_column]
