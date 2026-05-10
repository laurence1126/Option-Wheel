import os, sys
import pandas as pd
import ivolatility as ivol
import requests


def fetch_options_data(symbol, start_date, end_date, dteFrom=0, dteTo=365, moneynessFrom=-50, moneynessTo=50):
    api_key = "Z5xfNV1MfCPK35Z5"
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
