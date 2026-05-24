from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, log_loss, precision_score, recall_score, roc_auc_score

from backtester.daily_walkthrough import DailyWalkthroughResult, DailyWheelWalkthrough
from backtester.backtest import WheelConfig

INITIAL_CAPITAL_FOR_DRAWDOWN = 100_000.0


@dataclass(slots=True)
class MLModelConfig:
    symbol: str
    start_date: str | date
    end_date: str | date
    target_delta: float = 0.15
    stop_loss_multiple: float | None = 3.0
    take_profit_multiple: float | None = None
    put_exp_days: int = 25
    put_day_of_week: list[int] | None = None
    retrain_frequency: str = "monthly"
    min_train_samples: int = 252
    probability_threshold: float = 0.50
    market_source: str = "auto"
    yfinance_symbol: str | None = None
    xgboost_params: dict[str, Any] | None = None
    data_root: str = "data"


@dataclass(slots=True)
class MLModelResult:
    walkthrough: DailyWalkthroughResult
    features: pd.DataFrame
    predictions: pd.DataFrame
    filtered_pnl_series: pd.Series
    metrics: dict[str, Any]
    feature_importance: pd.DataFrame


class MLModelRunner:
    def __init__(self, config: MLModelConfig) -> None:
        self.config = config

    def run(self) -> MLModelResult:
        walkthrough = self._run_walkthrough()
        trades = walkthrough.trades.copy()
        if trades.empty:
            raise ValueError("Daily walkthrough returned no trades to model.")

        features = self._build_features(trades)
        predictions, last_model = self._walk_forward_predictions(trades, features)
        filtered_pnl_series = self._filtered_pnl_series(predictions)
        metrics = self._metrics(predictions, filtered_pnl_series)
        feature_importance = self._feature_importance(last_model, features.columns)

        return MLModelResult(
            walkthrough=walkthrough,
            features=features,
            predictions=predictions.reset_index(drop=True),
            filtered_pnl_series=filtered_pnl_series,
            metrics=metrics,
            feature_importance=feature_importance,
        )

    def _run_walkthrough(self) -> DailyWalkthroughResult:
        config = WheelConfig(
            symbol=self.config.symbol,
            start_date=self.config.start_date,
            end_date=self.config.end_date,
            initial_cash=1_000_000.0,
            leverage=100.0,
            target_delta=self.config.target_delta,
            stop_loss_multiple=self.config.stop_loss_multiple,
            take_profit_multiple=self.config.take_profit_multiple,
            put_exp_days=self.config.put_exp_days,
            put_day_of_week=self.config.put_day_of_week,
            call_exp_days=0,
            data_root=self.config.data_root,
        )
        return DailyWheelWalkthrough(config).run()

    def _build_features(self, trades: pd.DataFrame) -> pd.DataFrame:
        frame = trades.copy()
        frame["date"] = pd.to_datetime(frame["date"])
        frame["expiration"] = pd.to_datetime(frame["expiration"])
        frame = frame.sort_values("date").reset_index(drop=True)

        features = pd.DataFrame(index=frame["date"])
        for column in ("strike", "premium", "delta", "moneyness", "iv", "spot"):
            features[column] = pd.to_numeric(frame[column], errors="coerce").to_numpy()

        features["abs_delta"] = features["delta"].abs()
        features["premium_to_spot"] = features["premium"] / (features["spot"] * 100.0)
        features["strike_to_spot"] = features["strike"] / features["spot"]
        features["dte"] = (frame["expiration"] - frame["date"]).dt.days.to_numpy()
        features["entry_day_of_week"] = frame["date"].dt.dayofweek.to_numpy()
        features["entry_month"] = frame["date"].dt.month.to_numpy()

        market_features = self._market_features(frame)
        features = features.join(market_features, how="left")
        return features.sort_index()

    def _market_features(self, trades: pd.DataFrame) -> pd.DataFrame:
        dates = pd.DatetimeIndex(pd.to_datetime(trades["date"])).normalize()
        market = self._load_market_frame(trades, dates)
        market = market.loc[~market.index.duplicated(keep="last")].sort_index()
        market.index = pd.DatetimeIndex(market.index).normalize()

        all_dates = market.index.union(dates).sort_values()
        market = market.reindex(all_dates).ffill()
        close = pd.to_numeric(market["close"], errors="coerce")
        returns = close.pct_change()

        aligned = pd.DataFrame(index=dates)
        aligned["ret_1d"] = close.pct_change(1).reindex(dates).to_numpy()
        aligned["ret_5d"] = close.pct_change(5).reindex(dates).to_numpy()
        aligned["ret_20d"] = close.pct_change(20).reindex(dates).to_numpy()
        aligned["realized_vol_20d"] = (returns.rolling(20, min_periods=2).std() * np.sqrt(252.0)).reindex(dates).to_numpy()
        aligned["ma20_dist"] = (close / close.rolling(20, min_periods=2).mean() - 1.0).reindex(dates).to_numpy()
        aligned["ma50_dist"] = (close / close.rolling(50, min_periods=2).mean() - 1.0).reindex(dates).to_numpy()
        if "volume" in market.columns and market["volume"].notna().any():
            volume = pd.to_numeric(market["volume"], errors="coerce")
            volume_mean = volume.rolling(20, min_periods=2).mean()
            volume_std = volume.rolling(20, min_periods=2).std()
            aligned["volume_z20"] = ((volume - volume_mean) / volume_std.replace(0.0, np.nan)).reindex(dates).to_numpy()
        else:
            aligned["volume_z20"] = np.nan
        return aligned

    def _load_market_frame(self, trades: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DataFrame:
        source = self.config.market_source.lower()
        if source not in {"auto", "yfinance", "local"}:
            raise ValueError("market_source must be one of: 'auto', 'yfinance', 'local'.")

        if source in {"auto", "yfinance"}:
            try:
                return self._load_yfinance_market(dates)
            except Exception:
                if source == "yfinance":
                    raise

        return self._load_local_market(trades)

    def _load_yfinance_market(self, dates: pd.DatetimeIndex) -> pd.DataFrame:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise ImportError("yfinance is required for market_source='yfinance'.") from exc

        start = (dates.min() - pd.Timedelta(days=400)).date().isoformat()
        end = (dates.max() + pd.Timedelta(days=5)).date().isoformat()
        symbol = self.config.yfinance_symbol or self.config.symbol
        data = yf.download(symbol, start=start, end=end, progress=False, auto_adjust=False)
        if data.empty:
            raise ValueError(f"yfinance returned no market data for {symbol}.")

        if isinstance(data.columns, pd.MultiIndex):
            if symbol in data.columns.get_level_values(-1):
                data = data.xs(symbol, axis=1, level=-1)
            else:
                data.columns = data.columns.get_level_values(0)

        close_column = "Adj Close" if "Adj Close" in data.columns else "Close"
        required = {close_column, "Open", "High", "Low"}
        missing = required - set(data.columns)
        if missing:
            raise ValueError(f"yfinance market data is missing columns: {sorted(missing)}")

        columns = {
            "Open": "open",
            "High": "high",
            "Low": "low",
            close_column: "close",
        }
        if "Volume" in data.columns:
            columns["Volume"] = "volume"
        market = data.loc[:, list(columns)].rename(columns=columns)
        market.index = pd.to_datetime(market.index).normalize()
        return market.sort_index()

    @staticmethod
    def _load_local_market(trades: pd.DataFrame) -> pd.DataFrame:
        market = trades[["date", "spot"]].dropna(subset=["date", "spot"]).copy()
        market["date"] = pd.to_datetime(market["date"]).dt.normalize()
        market = market.drop_duplicates("date", keep="last").set_index("date").sort_index()
        market = market.rename(columns={"spot": "close"})
        market["open"] = market["close"]
        market["high"] = market["close"]
        market["low"] = market["close"]
        market["volume"] = np.nan
        return market[["open", "high", "low", "close", "volume"]]

    def _walk_forward_predictions(self, trades: pd.DataFrame, features: pd.DataFrame) -> tuple[pd.DataFrame, Any | None]:
        predictions = self._prediction_base(trades)
        classifier_factory = _load_xgboost_classifier()
        last_model: Any | None = None

        for period_dates in self._prediction_periods(predictions["date"]):
            retrain_date = period_dates.min()
            train_mask = self._eligible_training_mask(predictions, retrain_date)
            train_features = features.loc[predictions.loc[train_mask, "date"]]
            train_labels = predictions.loc[train_mask, "label"].astype(int)

            if len(train_labels) < self.config.min_train_samples or train_labels.nunique() < 2:
                model = None
            else:
                params = self._xgboost_params(train_labels)
                model = classifier_factory(**params)
                train_features = self._prepare_model_features(train_features)
                model.fit(train_features, train_labels)
                last_model = model

            score_mask = predictions["date"].isin(period_dates) & predictions["pnl"].notna()
            if model is None:
                predictions.loc[score_mask, "reason"] = "ml_not_ready"
                continue

            score_dates = predictions.loc[score_mask, "date"]
            score_features = self._prepare_model_features(features.loc[score_dates])
            probabilities = model.predict_proba(score_features)[:, 1]
            predictions.loc[score_mask, "probability"] = probabilities
            predictions.loc[score_mask, "decision"] = probabilities >= self.config.probability_threshold
            predictions.loc[score_mask & predictions["decision"], "reason"] = "selected"
            predictions.loc[score_mask & ~predictions["decision"], "reason"] = "below_threshold"

        return predictions, last_model

    @staticmethod
    def _prediction_base(trades: pd.DataFrame) -> pd.DataFrame:
        predictions = trades[["date", "exit_date", "pnl", "outcome"]].copy()
        predictions["date"] = pd.to_datetime(predictions["date"])
        predictions["exit_date"] = pd.to_datetime(predictions["exit_date"])
        predictions["pnl"] = pd.to_numeric(predictions["pnl"], errors="coerce")
        predictions = predictions.sort_values("date").reset_index(drop=True)
        predictions["label"] = np.where(predictions["pnl"].notna(), predictions["pnl"] > 0.0, np.nan)
        predictions["probability"] = np.nan
        predictions["decision"] = False
        predictions["reason"] = None
        missing_pnl = predictions["pnl"].isna()
        predictions.loc[missing_pnl, "reason"] = predictions.loc[missing_pnl, "outcome"]
        return predictions.drop(columns=["outcome"])

    def _prediction_periods(self, dates: pd.Series) -> list[pd.DatetimeIndex]:
        date_index = pd.DatetimeIndex(pd.to_datetime(dates)).sort_values()
        frequency = self.config.retrain_frequency.lower()
        if frequency == "daily":
            return [pd.DatetimeIndex([dt]) for dt in date_index]
        if frequency == "monthly":
            periods = date_index.to_period("M")
        elif frequency == "quarterly":
            periods = date_index.to_period("Q")
        else:
            raise ValueError("retrain_frequency must be one of: 'daily', 'monthly', 'quarterly'.")

        return [pd.DatetimeIndex(group) for _, group in pd.Series(date_index, index=date_index).groupby(periods)]

    @staticmethod
    def _eligible_training_mask(predictions: pd.DataFrame, retrain_date: pd.Timestamp) -> pd.Series:
        return (
            predictions["pnl"].notna() & predictions["label"].notna() & predictions["exit_date"].notna() & (predictions["exit_date"] < retrain_date)
        )

    def _xgboost_params(self, train_labels: pd.Series) -> dict[str, Any]:
        positives = int((train_labels == 1).sum())
        negatives = int((train_labels == 0).sum())
        params: dict[str, Any] = {
            "objective": "binary:logistic",
            "eval_metric": "logloss",
            "n_estimators": 500,
            "learning_rate": 0.03,
            "max_depth": 3,
            "min_child_weight": 10,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "reg_lambda": 1.0,
            "tree_method": "hist",
            "n_jobs": -1,
            "random_state": 42,
        }
        if positives > 0 and "scale_pos_weight" not in params:
            params["scale_pos_weight"] = negatives / positives
        if self.config.xgboost_params:
            params.update(self.config.xgboost_params)
        return params

    @staticmethod
    def _prepare_model_features(features: pd.DataFrame) -> pd.DataFrame:
        return features.replace([np.inf, -np.inf], np.nan)

    @staticmethod
    def _filtered_pnl_series(predictions: pd.DataFrame) -> pd.Series:
        pnl = predictions["pnl"].where(predictions["decision"], 0.0).fillna(0.0)
        series = pd.Series(pnl.to_numpy(), index=pd.DatetimeIndex(predictions["date"]), name="filtered_pnl")
        return series.sort_index()

    @staticmethod
    def _metrics(predictions: pd.DataFrame, filtered_pnl_series: pd.Series) -> dict[str, Any]:
        valid = predictions["probability"].notna() & predictions["label"].notna()
        selected = predictions["decision"] & predictions["pnl"].notna()
        metrics: dict[str, Any] = {
            "prediction_count": int(valid.sum()),
            "selected_count": int(selected.sum()),
            "selected_rate": float(selected.sum() / valid.sum()) if valid.any() else None,
            "auc": None,
            "log_loss": None,
            "accuracy": None,
            "precision": None,
            "recall": None,
            "total_raw_pnl": float(predictions["pnl"].fillna(0.0).sum()),
            "total_selected_pnl": float(filtered_pnl_series.sum()),
            "selected_hit_rate": float((predictions.loc[selected, "pnl"] > 0.0).mean()) if selected.any() else None,
            "avg_selected_trade_pnl": float(predictions.loc[selected, "pnl"].mean()) if selected.any() else None,
        }

        if valid.any():
            y_true = predictions.loc[valid, "label"].astype(int)
            y_prob = predictions.loc[valid, "probability"].astype(float)
            y_pred = predictions.loc[valid, "decision"].astype(bool)
            if y_true.nunique() == 2:
                metrics["auc"] = float(roc_auc_score(y_true, y_prob))
            metrics["log_loss"] = float(log_loss(y_true, y_prob, labels=[0, 1]))
            metrics["accuracy"] = float(accuracy_score(y_true, y_pred))
            metrics["precision"] = float(precision_score(y_true, y_pred, zero_division=0))
            metrics["recall"] = float(recall_score(y_true, y_pred, zero_division=0))

        equity = INITIAL_CAPITAL_FOR_DRAWDOWN + filtered_pnl_series.cumsum()
        running_peak = equity.cummax()
        drawdown = equity - running_peak
        drawdown_pct = equity / running_peak - 1.0
        metrics["max_dollar_drawdown"] = float(drawdown.min()) if not drawdown.empty else 0.0
        metrics["max_percent_drawdown"] = float(drawdown_pct.min()) if not drawdown_pct.empty else 0.0
        return metrics

    @staticmethod
    def _feature_importance(model: Any | None, feature_names: pd.Index) -> pd.DataFrame:
        if model is None or not hasattr(model, "feature_importances_"):
            return pd.DataFrame(columns=["feature", "importance"])
        importance = pd.DataFrame(
            {
                "feature": list(feature_names),
                "importance": list(model.feature_importances_),
            }
        )
        return importance.sort_values("importance", ascending=False).reset_index(drop=True)


def _load_xgboost_classifier() -> Callable[..., Any]:
    try:
        from xgboost import XGBClassifier
    except ImportError as exc:
        raise ImportError("xgboost is required to run MLModelRunner. Install it with `pip install xgboost`.") from exc
    return XGBClassifier


def run_ml_model(
    symbol: str,
    start_date: str | date,
    end_date: str | date,
    target_delta: float = 0.15,
    stop_loss_multiple: float | None = 3.0,
    take_profit_multiple: float | None = None,
    put_exp_days: int = 25,
    put_day_of_week: list[int] | None = None,
    retrain_frequency: str = "quarterly",
    min_train_samples: int = 60,
    probability_threshold: float = 0.50,
    market_source: str = "auto",
    yfinance_symbol: str | None = None,
    xgboost_params: dict[str, Any] | None = None,
    data_root: str = "data",
) -> MLModelResult:
    config = MLModelConfig(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        target_delta=target_delta,
        stop_loss_multiple=stop_loss_multiple,
        take_profit_multiple=take_profit_multiple,
        put_exp_days=put_exp_days,
        put_day_of_week=put_day_of_week,
        retrain_frequency=retrain_frequency,
        min_train_samples=min_train_samples,
        probability_threshold=probability_threshold,
        market_source=market_source,
        yfinance_symbol=yfinance_symbol,
        xgboost_params=xgboost_params,
        data_root=data_root,
    )
    return MLModelRunner(config).run()
