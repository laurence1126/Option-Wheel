import pandas as pd
from pathlib import Path
from backtester.grid_search import run_grid_search


def main() -> None:
    data_dir = Path("data/grid_search")
    data_dir.mkdir(parents=True, exist_ok=True)

    grid_results_spy = run_grid_search(
        symbol="SPY",
        start_date="2016-01-01",
        end_date="2026-05-22",
        # put_day_of_week=[2, 4],
        initial_cash=7_500,
        leverage=10,
        max_workers=2,
    )

    grid_results_spy.to_csv(data_dir / "grid_results_spy.csv", index=False)

    grid_results_qqq = run_grid_search(
        symbol="QQQ",
        start_date="2016-01-01",
        end_date="2026-05-22",
        # put_day_of_week=[2, 4],
        initial_cash=5_000,
        leverage=10,
        max_workers=2,
    )

    grid_results_qqq.to_csv(data_dir / "grid_results_qqq.csv", index=False)


if __name__ == "__main__":
    main()
