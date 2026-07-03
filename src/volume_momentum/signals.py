from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from volume_momentum.config import SignalConfig


@dataclass(frozen=True)
class EventRecord:
    ticker: str
    event_date: str
    adj_close: float
    trading_value: float
    short_trading_value_avg: float
    long_trading_value_avg: float
    inflow_ratio: float
    moving_average: float


class SignalError(ValueError):
    """Raised when price data is insufficient or malformed for signal calculation."""


def calculate_indicators(price_bars: pd.DataFrame, config: SignalConfig) -> pd.DataFrame:
    _validate_price_bars(price_bars)
    frame = price_bars.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame.sort_values(["ticker", "trade_date"]).reset_index(drop=True)

    grouped = frame.groupby("ticker", group_keys=False)
    frame["short_trading_value_avg"] = grouped["trading_value"].transform(
        lambda series: series.rolling(config.short_trading_value_window, min_periods=config.short_trading_value_window).mean()
    )
    frame["long_trading_value_avg"] = grouped["trading_value"].transform(
        lambda series: series.rolling(config.long_trading_value_window, min_periods=config.long_trading_value_window).mean()
    )
    frame["inflow_ratio"] = frame["short_trading_value_avg"] / frame["long_trading_value_avg"]
    frame["moving_average"] = grouped["adj_close"].transform(
        lambda series: series.rolling(config.moving_average_window, min_periods=config.moving_average_window).mean()
    )
    frame["moving_average_rising"] = grouped["moving_average"].transform(lambda series: series.diff() > 0)
    frame["above_moving_average"] = frame["adj_close"] > frame["moving_average"]
    frame["previous_adj_close"] = grouped["adj_close"].shift(1)
    frame["lookback_adj_close"] = grouped["adj_close"].shift(config.close_lookback_days)
    frame["close_above_previous"] = frame["adj_close"] > frame["previous_adj_close"]
    frame["close_above_lookback"] = frame["adj_close"] > frame["lookback_adj_close"]
    return frame


def detect_events(price_bars: pd.DataFrame, config: SignalConfig, cooldown_days: int) -> pd.DataFrame:
    indicators = calculate_indicators(price_bars, config)
    base_condition = (
        (indicators["inflow_ratio"] >= config.inflow_ratio_threshold)
        & (indicators["long_trading_value_avg"] >= config.min_long_trading_value)
        & indicators["above_moving_average"]
        & indicators["moving_average_rising"]
    )
    price_condition = indicators["close_above_previous"] & indicators["close_above_lookback"]
    condition = base_condition
    if config.require_close_above_previous:
        condition = condition & price_condition
    indicators["base_signal_condition"] = base_condition.fillna(False)
    indicators["price_signal_condition"] = price_condition.fillna(False)
    indicators["signal_condition"] = condition.fillna(False)
    indicators["condition_streak"] = (
        indicators.groupby("ticker", group_keys=False)["base_signal_condition"].transform(_consecutive_true_counts)
    )
    indicators["eligible_condition"] = indicators["condition_streak"] >= config.min_consecutive_days

    records: list[EventRecord] = []
    for ticker, ticker_frame in indicators.groupby("ticker", sort=False):
        last_event_position: int | None = None
        was_eligible = False
        ordered = ticker_frame.sort_values("trade_date").reset_index(drop=True)
        for position, row in ordered.iterrows():
            base_eligible = bool(row["eligible_condition"])
            price_eligible = (not config.require_close_above_previous) or bool(row["price_signal_condition"])
            eligible = base_eligible and price_eligible
            if not base_eligible:
                was_eligible = False
                continue
            if was_eligible:
                continue
            if not eligible:
                continue
            if last_event_position is not None and position - last_event_position <= cooldown_days:
                was_eligible = True
                continue

            records.append(
                EventRecord(
                    ticker=str(ticker),
                    event_date=row["trade_date"].date().isoformat(),
                    adj_close=float(row["adj_close"]),
                    trading_value=float(row["trading_value"]),
                    short_trading_value_avg=float(row["short_trading_value_avg"]),
                    long_trading_value_avg=float(row["long_trading_value_avg"]),
                    inflow_ratio=float(row["inflow_ratio"]),
                    moving_average=float(row["moving_average"]),
                )
            )
            last_event_position = position
            was_eligible = True

    return pd.DataFrame([record.__dict__ for record in records])


def _consecutive_true_counts(series: pd.Series) -> pd.Series:
    counts: list[int] = []
    current = 0
    for value in series:
        current = current + 1 if bool(value) else 0
        counts.append(current)
    return pd.Series(counts, index=series.index)


def _validate_price_bars(price_bars: pd.DataFrame) -> None:
    required = {"ticker", "trade_date", "adj_close", "trading_value"}
    missing = required.difference(price_bars.columns)
    if missing:
        raise SignalError(f"price_bars is missing columns: {', '.join(sorted(missing))}")
    if price_bars.empty:
        raise SignalError("price_bars must not be empty")
