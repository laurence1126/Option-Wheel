import configparser
import os
from pathlib import Path

import pandas as pd
import ivolatility as ivol
import requests


def load_ivolatility_api_key(config_path=".config"):
    env_key = os.getenv("IVOLATILITY_API_KEY")
    if env_key:
        return env_key

    parser = configparser.ConfigParser()
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}. Create it or set IVOLATILITY_API_KEY.")

    parser.read(path)
    try:
        api_key = parser["ivolatility"]["api_key"].strip()
    except KeyError as exc:
        raise KeyError(f"Missing [ivolatility] api_key in {path}") from exc

    if not api_key or api_key == "YOUR_API_KEY_HERE":
        raise ValueError(f"Set a real IVolatility API key in {path}")
    return api_key


def fetch_options_data(symbol, start_date, end_date, dteFrom=0, dteTo=365, moneynessFrom=-50, moneynessTo=50):
    api_key = load_ivolatility_api_key()
    ivol.setLoginParams(apiKey=api_key)
    fetch = ivol.setMethod("/equities/eod/stock-opts-by-param")

    start_date = pd.to_datetime(start_date).strftime("%Y-%m-%d")
    end_date = pd.to_datetime(end_date).strftime("%Y-%m-%d")
    dates = pd.date_range(start=start_date, end=end_date, freq="B")  # Business days
    all_data = []
    for date in dates:
        try:
            data_call = fetch(
                symbol=symbol,
                cp="C",
                dteFrom=dteFrom,
                dteTo=dteTo,
                moneynessFrom=moneynessFrom,
                moneynessTo=moneynessTo,
                tradeDate=date.strftime("%Y-%m-%d"),
                delayBetweenRequests=0.5,
            )
            data_put = fetch(
                symbol=symbol,
                cp="P",
                dteFrom=dteFrom,
                dteTo=dteTo,
                moneynessFrom=moneynessFrom,
                moneynessTo=moneynessTo,
                tradeDate=date.strftime("%Y-%m-%d"),
                delayBetweenRequests=0.5,
            )
            all_data.append(data_call)
            all_data.append(data_put)
            print(f"Fetched data for date {date.strftime('%Y-%m-%d')}")
        except requests.exceptions.RequestException as e:
            print(f"Request failed for date {date.strftime('%Y-%m-%d')}: {e}")
    return pd.concat(all_data, ignore_index=True)


def fetch_data_by_month(symbol, start_date, end_date, dteFrom=0, dteTo=365, moneynessFrom=-50, moneynessTo=50):
    dates_by_month = pd.date_range(start=start_date, end=end_date, freq="MS")
    for date in dates_by_month:
        month_end = date + pd.offsets.MonthEnd(0)
        if month_end > pd.to_datetime(end_date):
            month_end = pd.to_datetime(end_date)
        data = fetch_options_data(symbol, date.strftime("%Y-%m-%d"), month_end.strftime("%Y-%m-%d"), dteFrom, dteTo, moneynessFrom, moneynessTo)
        if not os.path.exists(f"data/{symbol}"):
            os.makedirs(f"data/{symbol}")
        data.to_csv(f"data/{symbol}/{date.strftime('%Y-%m')}.csv", index=False)
        print(f"Saved data for {date.strftime('%Y-%m')} to CSV.")
