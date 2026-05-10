# Option Wheel Backtester

A Python backtesting project for running an options wheel strategy on historical option-chain data.

The project loads local monthly CSV files, selects short puts and covered calls by delta and days to expiration, tracks daily mark-to-market equity, and generates summary statistics, trade breakdowns, and plots.

## What It Does

- Runs a one-contract wheel strategy on a selected symbol.
- Sells cash-secured puts when no shares are held.
- Handles put assignment into 100 shares.
- Sells covered calls after assignment, unless `call_exp_days=0`.
- Supports immediate liquidation of assigned shares when `call_exp_days=0`.
- Supports put stop-loss exits.
- Supports put take-profit exits.
- Tracks cash, shares, option value, daily PnL, trades, events, and equity.
- Compares strategy equity against underlying buy-and-hold.
- Reports strategy Sharpe, underlying Sharpe, drawdown, assignment rate, stop-loss rate, take-profit rate, and leverage ratio.

## Project Structure

```text
.
├── backtest/
│   ├── backtest.py          # Wheel strategy simulation
│   ├── report.py            # Summary tables and plots
│   └── grid_search.py       # Multiprocessing parameter sweep helper
├── data_loader/
│   ├── option_loader.py     # Local option-chain CSV loader
│   ├── rf_loader.py         # FRED short-rate loader and local cache helper
│   ├── vix_loader.py        # FRED VIX loader and local cache helper
│   └── fetch_option_data.py # IVolatility data fetch helper
├── run_backtest.ipynb       # Notebook workflow
└── data/
    ├── risk_free/
    │   └── DGS3MO.csv       # Cached FRED rf series
    ├── market/
    │   └── VIXCLS.csv       # Cached FRED VIX series
    └── SYMBOL/
        └── YYYY-MM.csv      # Historical option-chain data
```

## Data Format

The backtester expects monthly CSV files under:

```text
data/{SYMBOL}/{YYYY-MM}.csv
```

Each CSV must include these columns:

```text
c_date, option_symbol, dte, expiration_date, call_put, price_strike,
price, Ask, Bid, iv, delta, underlying_price
```

The included data folders currently cover symbols such as:

```text
AAPL, AMZN, GOOGL, META, MSFT, MU, NVDA, ORCL, QQQ, QQQM, SOXL, SPY, SPYM, TSLA
```

## Installation

Create an environment and install the core dependencies:

```bash
pip install pandas numpy matplotlib requests ivolatility
```

If you only want to run backtests from existing CSV data, `ivolatility` and `requests` are only needed for `fetch_option_data.py`.

## Quick Start

```python
from backtest.backtest import run_wheel_backtest
from backtest.report import WheelPerformanceReport

result = run_wheel_backtest(
    symbol="QQQ",
    start_date="2016-03-01",
    end_date="2026-03-15",
    target_delta=0.15,
    put_exp_days=56,
    call_exp_days=14,
    initial_cash=20_000,
    leverage=2.0,
    rf_series="DGS3MO",
    rf_penalty_multiple=0.85,
    stop_loss_multiple=3.0,
    take_profit_multiple=None,
)

report = WheelPerformanceReport(result)

report.summary_table()
report.trade_breakdown_table()
fig = report.plot_equity_and_drawdown()
```

## Strategy Parameters

| Parameter              |    Default | Description                                                                                                                                                                                                                                                                  |
| ---------------------- | ---------: | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `symbol`               |   required | Ticker symbol matching a folder under `data/`.                                                                                                                                                                                                                               |
| `start_date`           |   required | Backtest start date.                                                                                                                                                                                                                                                         |
| `end_date`             |   required | Backtest end date.                                                                                                                                                                                                                                                           |
| `initial_cash`         |  `100_000` | Starting portfolio cash.                                                                                                                                                                                                                                                     |
| `leverage`             |      `1.0` | Cash multiplier used when checking put strike notional capacity.                                                                                                                                                                                                             |
| `rf_series`            | `"DGS3MO"` | FRED short-rate series used for cash interest. Supported values: `DGS1MO`, `DGS3MO`, `DGS6MO`, `DTB3`, `EFFR`, `SOFR`.                                                                                                                                                       |
| `rf_penalty_multiple`  |     `0.85` | Haircut applied to the selected rf series before cash interest accrues. Example: if `DGS3MO` is 5%, `0.85` makes cash earn 4.25%.                                                                                                                                            |
| `rf_path`              |     `None` | Optional local CSV path for historical rates. Defaults to `data/risk_free/{rf_series}.csv`.                                                                                                                                                                                  |
| `refresh_rf`           |    `False` | If `True`, re-download the selected rate series from FRED and update the local cache.                                                                                                                                                                                        |
| `target_delta`         |     `0.15` | Target absolute option delta for put/call selection.                                                                                                                                                                                                                         |
| `put_exp_days`         |       `25` | Minimum DTE for short put selection.                                                                                                                                                                                                                                         |
| `call_exp_days`        |       `25` | Minimum DTE for covered call selection. Set to `0` to skip calls and liquidate assigned shares immediately.                                                                                                                                                                  |
| `stop_loss_multiple`   |      `3.0` | Close a short put when the option high is above `original_premium * stop_loss_multiple`. If the option opens below the threshold, the buyback fills at the threshold; if it opens above the threshold, the buyback fills at the open price. Use `None` or `<= 0` to disable. |
| `take_profit_multiple` |     `None` | Close a short put when current put price is below `original_premium * take_profit_multiple`. Example: `0.2` closes at 20% of original premium.                                                                                                                               |
| `data_root`            |   `"data"` | Root directory containing symbol CSV folders.                                                                                                                                                                                                                                |

## Outputs

`run_wheel_backtest()` returns a `BacktestResult` with:

- `trades`: completed option legs with outcome, premium, cash flow, days held, leverage ratio, and buyback price where applicable.
- `events`: start, sell, expiration, stop-loss, take-profit, liquidation, and final state events.
- `daily_pnl`: daily portfolio accounting, including `raw_rf`, haircut-adjusted `rf`, and `cash_interest`.
- `equity_curve`: strategy equity indexed by date.
- `ending_cash`, `ending_shares`, `ending_spot`, `option_position`, `ending_equity`.

Common trade outcomes:

```text
expired
assigned
assigned_liquidated
called_away
cut_loss
take_profit
```

## Reports

`WheelPerformanceReport.summary_table()` includes:

- Total return and CAGR
- Cash interest earned
- Strategy Sharpe
- Underlying Sharpe
- Max drawdown
- Hit rate, where completed short option legs with positive `cash_flow` count as hits
- Puts sold
- Assignments
- Cut losses and put cut-loss rate
- Take profits and put take-profit rate
- Call stats when calls are sold
- Total premium collected
- Average IV and delta
- Average and maximum leverage ratio

## RF Cash Yield

Cash interest uses a historical short-rate series from FRED. The default is the 3-month Treasury constant maturity rate, `DGS3MO`. You can choose:

```text
DGS1MO, DGS3MO, DGS6MO, DTB3, EFFR, SOFR
```

On first use, the loader downloads and caches the selected series at:

```text
data/risk_free/{series}.csv
```

The daily cash yield calculation is:

```python
rf = raw_rf * rf_penalty_multiple
cash_interest = max(cash, 0) * rf / 252
```

`raw_rf` and `rf` are annualized decimal rates in `daily_pnl`. For example, 5% is stored as `0.05`.

To refresh the local copy:

```python
result = run_wheel_backtest(
    "QQQ",
    "2016-03-01",
    "2026-03-15",
    rf_series="DGS3MO",
    rf_penalty_multiple=0.85,
    refresh_rf=True,
)
```

## VIX Data

`vix_loader.py` provides the same local-cache pattern for FRED VIX series. The default is `VIXCLS`, the Cboe VIX close.

Supported series:

```text
VIXCLS, VXVCLS, VXNCLS, RVXCLS
```

Example:

```python
from data_loader.option_loader import OptionDataLoader
from data_loader.vix_loader import load_vix

price_history = OptionDataLoader("QQQ").build_price_history()
vix = load_vix(price_history.index, series="VIXCLS")
```

On first use, the loader downloads and caches the selected series at:

```text
data/market/{series}.csv
```

`plot_equity_and_drawdown()` shows:

- Strategy equity curve
- Underlying buy-and-hold curve
- Strategy drawdown
- Underlying buy-and-hold drawdown

## Multiprocessing Grid Search

`grid_search.py` provides a multiprocessing helper for sweeping strategy parameters:

```python
from backtest.grid_search import run_grid_search

grid_results = run_grid_search(
    symbol="QQQ",
    start_date="2016-03-01",
    end_date="2026-03-15",
    target_delta_values=[0.10, 0.15, 0.20, 0.25, 0.30],
    stop_loss_multiple_values=[2.0, 2.5, 3.0, 4.0, None],
    put_exp_days_values=[14, 21, 30, 45, 60],
    call_exp_days=0,
    initial_cash=20_000,
    leverage=2.0,
    rf_series="DGS3MO",
    rf_penalty_multiple=0.85,
    max_workers=None,
    sort_by="sharpe",
)

grid_results.head(20)
```

You can also run it directly:

```bash
python -m backtest.grid_search
```

## Fetching Data

`fetch_option_data.py` contains helpers for downloading option-chain data from IVolatility and saving it into the required monthly CSV layout:

Create a local `.config` file first:

```ini
[ivolatility]
api_key = YOUR_API_KEY_HERE
```

You can also set the key with an environment variable:

```bash
export IVOLATILITY_API_KEY="YOUR_API_KEY_HERE"
```

```python
from data_loader.fetch_option_data import fetch_data_by_month

fetch_data_by_month("QQQ", "2016-01-01", "2026-03-15")
```

The real `.config` file is ignored by Git.

## Notes And Assumptions

- The simulation supports one open option leg at a time.
- Each option contract controls 100 shares.
- Option entries and marks use the `price` column when available, otherwise bid/ask midpoint.
- If an option is missing from a later chain, intrinsic value is used as a fallback mark.
- No commissions, fees, bid/ask slippage model, taxes, margin interest, or early assignment model is included.
- Price history is inferred from `underlying_price` in the option-chain data.

## Example Notebook

Open `run_backtest.ipynb` for an interactive workflow that:

1. Runs a backtest.
2. Builds a `WheelPerformanceReport`.
3. Displays the summary table.
4. Displays trade breakdowns.
5. Plots equity and drawdown.
6. Shows the full trade log.
