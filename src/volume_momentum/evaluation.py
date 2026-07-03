from __future__ import annotations

import math

import pandas as pd


class EvaluationError(ValueError):
    """Raised when event or price data is not usable for evaluation."""


def evaluate_events(
    events: pd.DataFrame,
    price_bars: pd.DataFrame,
    benchmark_bars: pd.DataFrame,
    horizons: list[int] | tuple[int, ...],
    benchmark_tickers: dict[str, str],
) -> pd.DataFrame:
    _validate_events(events)
    _validate_price_bars(price_bars, "price_bars")
    if benchmark_bars.empty:
        benchmark_bars = pd.DataFrame(columns=price_bars.columns)
    else:
        _validate_price_bars(benchmark_bars, "benchmark_bars")

    price_by_ticker = _frames_by_ticker(price_bars)
    benchmark_by_name = {
        name: _prepare_price_frame(benchmark_bars[benchmark_bars["ticker"] == ticker])
        for name, ticker in benchmark_tickers.items()
    }

    rows: list[dict[str, object]] = []
    for event in events.itertuples(index=False):
        ticker = str(event.ticker)
        if ticker not in price_by_ticker:
            continue
        ticker_prices = price_by_ticker[ticker]
        event_position = _position_for_date(ticker_prices, str(event.event_date))
        if event_position is None:
            continue
        event_price = float(ticker_prices.iloc[event_position]["adj_close"])
        if not math.isfinite(event_price) or event_price <= 0:
            continue

        for horizon in horizons:
            horizon_row = _evaluate_horizon(
                ticker=ticker,
                event_date=str(event.event_date),
                event_position=event_position,
                event_price=event_price,
                prices=ticker_prices,
                horizon=int(horizon),
            )
            for benchmark_name, benchmark_prices in benchmark_by_name.items():
                benchmark_return = _benchmark_return(
                    benchmark_prices,
                    event_date=str(event.event_date),
                    horizon=int(horizon),
                )
                horizon_row[f"{benchmark_name}_return"] = benchmark_return
                event_return = horizon_row["return"]
                horizon_row[f"{benchmark_name}_excess_return"] = (
                    None
                    if benchmark_return is None or event_return is None
                    else float(event_return) - benchmark_return
                )
            rows.append(horizon_row)

    return pd.DataFrame(rows)


def summarize_evaluations(evaluations: pd.DataFrame) -> pd.DataFrame:
    if evaluations.empty:
        return pd.DataFrame()

    rows: list[dict[str, object]] = []
    for horizon, group in evaluations.groupby("horizon_days", sort=True):
        returns = group["return"].dropna()
        row = {
            "horizon_days": int(horizon),
            "sample_count": int(returns.count()),
            "average_return": _safe_stat(returns.mean()),
            "median_return": _safe_stat(returns.median()),
            "max_return": _safe_stat(returns.max()),
            "min_return": _safe_stat(returns.min()),
            "std_return": _safe_stat(returns.std(ddof=1)),
            "win_rate": _safe_stat((returns > 0).mean()),
            "hit_rate_10pct": _safe_stat((returns >= 0.10).mean()),
            "hit_rate_20pct": _safe_stat((returns >= 0.20).mean()),
            "hit_rate_30pct": _safe_stat((returns >= 0.30).mean()),
            "hit_rate_50pct": _safe_stat((returns >= 0.50).mean()),
            "hit_rate_100pct": _safe_stat((returns >= 1.00).mean()),
            "average_mfe": _safe_stat(group["mfe"].dropna().mean()),
            "average_mae": _safe_stat(group["mae"].dropna().mean()),
            "max_drawdown": _safe_stat(group["max_drawdown"].dropna().min()),
            "average_drawdown": _safe_stat(group["average_drawdown"].dropna().mean()),
        }
        for column in group.columns:
            if column.endswith("_return") and column != "return":
                row[f"average_{column}"] = _safe_stat(group[column].dropna().mean())
            if column.endswith("_excess_return"):
                row[f"average_{column}"] = _safe_stat(group[column].dropna().mean())
        rows.append(row)

    return pd.DataFrame(rows)


def _evaluate_horizon(
    ticker: str,
    event_date: str,
    event_position: int,
    event_price: float,
    prices: pd.DataFrame,
    horizon: int,
) -> dict[str, object]:
    if not math.isfinite(event_price) or event_price <= 0:
        raise EvaluationError("event_price must be positive and finite")
    target_position = event_position + horizon
    if target_position >= len(prices):
        return {
            "ticker": ticker,
            "event_date": event_date,
            "horizon_days": horizon,
            "target_date": None,
            "event_price": event_price,
            "target_price": None,
            "return": None,
            "mfe": None,
            "mae": None,
            "max_drawdown": None,
            "average_drawdown": None,
            "complete": False,
        }

    window = prices.iloc[event_position : target_position + 1].copy()
    target = prices.iloc[target_position]
    relative = window["adj_close"].astype(float) / event_price - 1.0
    drawdown = _drawdown(window["adj_close"].astype(float))
    return {
        "ticker": ticker,
        "event_date": event_date,
        "horizon_days": horizon,
        "target_date": target["trade_date"],
        "event_price": event_price,
        "target_price": float(target["adj_close"]),
        "return": float(target["adj_close"]) / event_price - 1.0,
        "mfe": float(relative.max()),
        "mae": float(relative.min()),
        "max_drawdown": float(drawdown.min()),
        "average_drawdown": float(drawdown.mean()),
        "complete": True,
    }


def _benchmark_return(benchmark_prices: pd.DataFrame, event_date: str, horizon: int) -> float | None:
    if benchmark_prices.empty:
        return None
    event_position = _position_for_date(benchmark_prices, event_date)
    if event_position is None:
        return None
    target_position = event_position + horizon
    if target_position >= len(benchmark_prices):
        return None
    event_price = float(benchmark_prices.iloc[event_position]["adj_close"])
    if not math.isfinite(event_price) or event_price <= 0:
        return None
    target_price = float(benchmark_prices.iloc[target_position]["adj_close"])
    if not math.isfinite(target_price):
        return None
    return target_price / event_price - 1.0


def _frames_by_ticker(price_bars: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {
        str(ticker): _prepare_price_frame(group)
        for ticker, group in price_bars.groupby("ticker", sort=False)
    }


def _prepare_price_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    prepared = frame.copy()
    prepared["trade_date"] = pd.to_datetime(prepared["trade_date"]).dt.date.astype(str)
    prepared = prepared.sort_values("trade_date").reset_index(drop=True)
    return prepared


def _position_for_date(prices: pd.DataFrame, event_date: str) -> int | None:
    matches = prices.index[prices["trade_date"] == event_date].tolist()
    return None if not matches else int(matches[0])


def _drawdown(prices: pd.Series) -> pd.Series:
    running_max = prices.cummax()
    return prices / running_max - 1.0


def _safe_stat(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return float(value)


def _validate_events(events: pd.DataFrame) -> None:
    required = {"ticker", "event_date"}
    missing = required.difference(events.columns)
    if missing:
        raise EvaluationError(f"events is missing columns: {', '.join(sorted(missing))}")


def _validate_price_bars(price_bars: pd.DataFrame, name: str) -> None:
    required = {"ticker", "trade_date", "adj_close"}
    missing = required.difference(price_bars.columns)
    if missing:
        raise EvaluationError(f"{name} is missing columns: {', '.join(sorted(missing))}")
