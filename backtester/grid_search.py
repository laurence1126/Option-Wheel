from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import product
import os
from typing import Any

import pandas as pd

from backtester.backtest import run_wheel_backtest
from backtester.report import WheelPerformanceReport


def _run_one_grid_case(params: dict[str, Any]) -> dict[str, Any]:
    case_label = _case_label(params)
    result = run_wheel_backtest(**params)
    stats = WheelPerformanceReport(result).summary_stats()
    stats.update(
        {
            "case": case_label,
            "target_delta": params["target_delta"],
            "stop_loss_multiple": params["stop_loss_multiple"],
            "put_exp_days": params["put_exp_days"],
        }
    )
    return stats


def _case_label(params: dict[str, Any]) -> str:
    return f"delta={params['target_delta']}, " f"stop={params['stop_loss_multiple']}, " f"put_dte={params['put_exp_days']}"


def run_grid_search(
    symbol: str,
    start_date: str,
    end_date: str,
    target_delta_values: list[float] | None = None,
    stop_loss_multiple_values: list[float | None] | None = None,
    put_exp_days_values: list[int] | None = None,
    call_exp_days: int = 0,
    initial_cash: float = 10_000.0,
    leverage: float = 2.0,
    rf_series: str = "DGS3MO",
    rf_penalty_multiple: float = 0.85,
    rf_path: str | None = None,
    refresh_rf: bool = False,
    take_profit_multiple: float | None = None,
    data_root: str = "data",
    max_workers: int | None = None,
    sort_by: str = "sharpe",
    ascending: bool = False,
    verbose: bool = True,
) -> pd.DataFrame:
    target_delta_values = target_delta_values or [0.10, 0.15, 0.20, 0.25, 0.30]
    stop_loss_multiple_values = stop_loss_multiple_values or [1, 1.5, 2.0, 2.5, 3.0, 4.0, None]
    put_exp_days_values = put_exp_days_values or [3, 5, 10, 12, 14, 20, 30]

    cases = [
        {
            "symbol": symbol,
            "start_date": start_date,
            "end_date": end_date,
            "target_delta": target_delta,
            "stop_loss_multiple": stop_loss_multiple,
            "take_profit_multiple": take_profit_multiple,
            "put_exp_days": put_exp_days,
            "call_exp_days": call_exp_days,
            "initial_cash": initial_cash,
            "leverage": leverage,
            "rf_series": rf_series,
            "rf_penalty_multiple": rf_penalty_multiple,
            "rf_path": rf_path,
            "refresh_rf": refresh_rf,
            "data_root": data_root,
        }
        for target_delta, stop_loss_multiple, put_exp_days in product(
            target_delta_values,
            stop_loss_multiple_values,
            put_exp_days_values,
        )
    ]

    worker_count = max_workers or os.cpu_count() or 1
    if verbose:
        print(f"Starting grid search: {len(cases)} cases, max_workers={worker_count}")
        print(f"Symbol={symbol}, period={start_date} to {end_date}")

    rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_run_one_grid_case, case): case for case in cases}
        completed = 0
        for future in as_completed(futures):
            case = futures[future]
            completed += 1
            label = _case_label(case)
            try:
                row = future.result()
            except Exception as exc:
                if verbose:
                    print(f"[{completed}/{len(cases)}] failed: {label} ({exc})")
                rows.append(
                    {
                        "case": label,
                        "target_delta": case["target_delta"],
                        "stop_loss_multiple": case["stop_loss_multiple"],
                        "put_exp_days": case["put_exp_days"],
                        "error": str(exc),
                    }
                )
                continue

            rows.append(row)
            if verbose:
                sharpe = row.get("sharpe")
                ending_equity = row.get("ending_equity")
                sharpe_text = "-" if pd.isna(sharpe) else f"{sharpe:.3f}"
                equity_text = "-" if pd.isna(ending_equity) else f"{ending_equity:,.2f}"
                print(f"[{completed}/{len(cases)}] done: {label}, sharpe={sharpe_text}, ending_equity={equity_text}")

    results = pd.DataFrame(rows)
    if sort_by in results.columns:
        results = results.sort_values(sort_by, ascending=ascending, na_position="last")
        if verbose:
            print(f"Sorted results by {sort_by}, ascending={ascending}")
    if verbose:
        failures = int(results["error"].notna().sum()) if "error" in results.columns else 0
        print(f"Grid search complete: {len(results) - failures} succeeded, {failures} failed")
    return results.reset_index(drop=True)


if __name__ == "__main__":
    grid_results = run_grid_search(
        symbol="QQQ",
        start_date="2016-03-15",
        end_date="2026-03-15",
        max_workers=None,
    )
    print(grid_results.head(20).to_string(index=False))
