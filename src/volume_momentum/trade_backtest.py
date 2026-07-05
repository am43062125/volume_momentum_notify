from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

import pandas as pd

from volume_momentum.config import TradeBacktestConfig


@dataclass(frozen=True)
class TradeBacktestInputs:
    events: pd.DataFrame
    price_bars: pd.DataFrame
    benchmark_bars: pd.DataFrame
    benchmark_ticker: str | None
    config: TradeBacktestConfig


@dataclass(frozen=True)
class TradeBacktestReportPaths:
    output_dir: Path
    trades_csv: Path
    summary_csv: Path
    strategy_comparison_csv: Path
    markdown: Path


class TradeBacktestError(ValueError):
    """Raised when trade backtest inputs are missing required data."""


def evaluate_trade_strategies(inputs: TradeBacktestInputs) -> pd.DataFrame:
    _validate_events(inputs.events)
    _validate_price_bars(inputs.price_bars, "price_bars")
    if inputs.benchmark_ticker and not inputs.benchmark_bars.empty:
        _validate_price_bars(inputs.benchmark_bars, "benchmark_bars")

    price_by_ticker = {
        str(ticker): _with_indicators(group, inputs.config)
        for ticker, group in inputs.price_bars.groupby("ticker", sort=False)
    }
    benchmark_prices = _benchmark_prices(inputs.benchmark_bars, inputs.benchmark_ticker)

    rows: list[dict[str, object]] = []
    for event in inputs.events.itertuples(index=False):
        ticker = str(event.ticker)
        if ticker not in price_by_ticker:
            continue
        prices = price_by_ticker[ticker]
        event_position = _position_for_date(prices, str(event.event_date))
        if event_position is None:
            continue

        for strategy in inputs.config.strategies:
            if strategy == "C" and not _passes_relative_return_filter(
                prices,
                benchmark_prices,
                event_position,
                str(event.event_date),
                inputs.config,
            ):
                continue
            trade = _evaluate_trade(
                strategy=strategy,
                ticker=ticker,
                event_date=str(event.event_date),
                prices=prices,
                event_position=event_position,
                config=inputs.config,
            )
            if trade is not None:
                rows.append(trade)

    return pd.DataFrame(rows)


def summarize_trades(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()

    rows: list[dict[str, object]] = []
    for strategy, group in trades.groupby("strategy", sort=True):
        sort_columns = [column for column in ["exit_date", "entry_date", "ticker"] if column in group.columns]
        ordered_group = group.sort_values(sort_columns) if sort_columns else group
        returns = ordered_group["return"].dropna().astype(float)
        wins = returns[returns > 0]
        losses = returns[returns < 0]
        row = {
            "strategy": strategy,
            "trade_count": int(returns.count()),
            "win_rate": _safe_stat((returns > 0).mean()),
            "average_return": _safe_stat(returns.mean()),
            "median_return": _safe_stat(returns.median()),
            "average_win": _safe_stat(wins.mean()),
            "average_loss": _safe_stat(losses.mean()),
            "expectancy": _safe_stat(returns.mean()),
            "take_profit_rate": _safe_stat((ordered_group["exit_reason"] == "take_profit").mean()),
            "stop_loss_rate": _safe_stat((ordered_group["exit_reason"] == "stop_loss").mean()),
            "time_exit_rate": _safe_stat((ordered_group["exit_reason"] == "time_exit").mean()),
            "average_holding_days": _safe_stat(ordered_group["holding_days"].dropna().mean()),
            "max_drawdown": _safe_stat(_equity_drawdown(returns).min()),
        }
        rows.append(row)
    return pd.DataFrame(rows)


def write_trade_backtest_reports(
    trades: pd.DataFrame,
    summary: pd.DataFrame,
    output_dir: str | Path,
    analysis_start_date: str,
    analysis_end_date: str,
    event_count: int,
    universe_count: int,
) -> TradeBacktestReportPaths:
    report_dir = Path(output_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    paths = TradeBacktestReportPaths(
        output_dir=report_dir,
        trades_csv=report_dir / "trades.csv",
        summary_csv=report_dir / "summary.csv",
        strategy_comparison_csv=report_dir / "strategy_comparison.csv",
        markdown=report_dir / "report.md",
    )
    comparison = strategy_comparison(summary)
    trades.to_csv(paths.trades_csv, index=False, encoding="utf-8-sig")
    summary.to_csv(paths.summary_csv, index=False, encoding="utf-8-sig")
    comparison.to_csv(paths.strategy_comparison_csv, index=False, encoding="utf-8-sig")
    paths.markdown.write_text(
        render_trade_backtest_markdown(
            trades=trades,
            summary=summary,
            comparison=comparison,
            analysis_start_date=analysis_start_date,
            analysis_end_date=analysis_end_date,
            event_count=event_count,
            universe_count=universe_count,
        ),
        encoding="utf-8",
    )
    return paths


def strategy_comparison(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame()
    columns = [
        "strategy",
        "trade_count",
        "win_rate",
        "average_return",
        "median_return",
        "expectancy",
        "take_profit_rate",
        "stop_loss_rate",
        "time_exit_rate",
        "average_holding_days",
        "max_drawdown",
    ]
    return summary[[column for column in columns if column in summary.columns]].copy()


def render_trade_backtest_markdown(
    trades: pd.DataFrame,
    summary: pd.DataFrame,
    comparison: pd.DataFrame,
    analysis_start_date: str,
    analysis_end_date: str,
    event_count: int,
    universe_count: int,
) -> str:
    lines = [
        "# 売買代金モメンタム 仮想売買バックテストレポート",
        "",
        "## 実行条件",
        "",
        "| 項目 | 値 |",
        "| --- | --- |",
        f"| 分析開始日 | {analysis_start_date} |",
        f"| 分析終了日 | {analysis_end_date} |",
        f"| 対象銘柄数 | {universe_count} |",
        f"| 検出イベント数 | {event_count} |",
        f"| 仮想トレード数 | {len(trades)} |",
        "",
        "## 戦略定義",
        "",
        "| 戦略 | 内容 |",
        "| --- | --- |",
        "| A | 翌営業日始値でエントリーし、固定保有日数後の終値で決済 |",
        "| B | 翌営業日始値でエントリーし、ATR損切・2R利確・最大保有日数で決済 |",
        "| C | Bに相対20日リターンフィルタを追加 |",
        "",
        "## 戦略比較",
        "",
        _markdown_table(_format_summary(comparison)),
        "",
        "## 集計詳細",
        "",
        _markdown_table(_format_summary(summary)),
        "",
        "## トレード明細",
        "",
        _markdown_table(_format_trades(trades.head(100))),
        "",
    ]
    return "\n".join(lines)


def _evaluate_trade(
    strategy: str,
    ticker: str,
    event_date: str,
    prices: pd.DataFrame,
    event_position: int,
    config: TradeBacktestConfig,
) -> dict[str, object] | None:
    entry_position = event_position + 1
    if entry_position >= len(prices):
        return None
    entry_row = prices.iloc[entry_position]
    entry_price = _positive_float(entry_row["open"])
    if entry_price is None:
        return None

    if strategy == "A":
        return _evaluate_fixed_hold_trade(
            strategy=strategy,
            ticker=ticker,
            event_date=event_date,
            prices=prices,
            entry_position=entry_position,
            entry_price=entry_price,
            hold_days=config.fixed_hold_days,
        )

    atr = _positive_float(prices.iloc[event_position]["atr"])
    if atr is None:
        return None
    risk_width = atr * config.stop_atr_multiple
    if not math.isfinite(risk_width) or risk_width <= 0:
        return None
    stop_price = entry_price - risk_width
    take_profit_price = entry_price + risk_width * config.take_profit_r_multiple
    if stop_price <= 0:
        return None

    return _evaluate_risk_trade(
        strategy=strategy,
        ticker=ticker,
        event_date=event_date,
        prices=prices,
        entry_position=entry_position,
        entry_price=entry_price,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        max_hold_days=config.max_hold_days,
    )


def _evaluate_fixed_hold_trade(
    strategy: str,
    ticker: str,
    event_date: str,
    prices: pd.DataFrame,
    entry_position: int,
    entry_price: float,
    hold_days: int,
) -> dict[str, object] | None:
    exit_position = entry_position + hold_days - 1
    if exit_position >= len(prices):
        return None
    exit_row = prices.iloc[exit_position]
    exit_price = _positive_float(exit_row["close"])
    if exit_price is None:
        return None
    return _trade_row(
        strategy=strategy,
        ticker=ticker,
        event_date=event_date,
        entry_row=prices.iloc[entry_position],
        entry_price=entry_price,
        exit_row=exit_row,
        exit_price=exit_price,
        exit_reason="time_exit",
        holding_days=hold_days,
        stop_price=None,
        take_profit_price=None,
    )


def _evaluate_risk_trade(
    strategy: str,
    ticker: str,
    event_date: str,
    prices: pd.DataFrame,
    entry_position: int,
    entry_price: float,
    stop_price: float,
    take_profit_price: float,
    max_hold_days: int,
) -> dict[str, object] | None:
    max_exit_position = min(entry_position + max_hold_days - 1, len(prices) - 1)
    for position in range(entry_position, max_exit_position + 1):
        row = prices.iloc[position]
        low = _positive_float(row["low"])
        high = _positive_float(row["high"])
        if low is not None and low <= stop_price:
            return _trade_row(
                strategy=strategy,
                ticker=ticker,
                event_date=event_date,
                entry_row=prices.iloc[entry_position],
                entry_price=entry_price,
                exit_row=row,
                exit_price=stop_price,
                exit_reason="stop_loss",
                holding_days=position - entry_position + 1,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
            )
        if high is not None and high >= take_profit_price:
            return _trade_row(
                strategy=strategy,
                ticker=ticker,
                event_date=event_date,
                entry_row=prices.iloc[entry_position],
                entry_price=entry_price,
                exit_row=row,
                exit_price=take_profit_price,
                exit_reason="take_profit",
                holding_days=position - entry_position + 1,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
            )

    exit_row = prices.iloc[max_exit_position]
    exit_price = _positive_float(exit_row["close"])
    if exit_price is None:
        return None
    return _trade_row(
        strategy=strategy,
        ticker=ticker,
        event_date=event_date,
        entry_row=prices.iloc[entry_position],
        entry_price=entry_price,
        exit_row=exit_row,
        exit_price=exit_price,
        exit_reason="time_exit",
        holding_days=max_exit_position - entry_position + 1,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
    )


def _trade_row(
    strategy: str,
    ticker: str,
    event_date: str,
    entry_row: pd.Series,
    entry_price: float,
    exit_row: pd.Series,
    exit_price: float,
    exit_reason: str,
    holding_days: int,
    stop_price: float | None,
    take_profit_price: float | None,
) -> dict[str, object]:
    return {
        "strategy": strategy,
        "ticker": ticker,
        "event_date": event_date,
        "entry_date": entry_row["trade_date"],
        "entry_price": entry_price,
        "exit_date": exit_row["trade_date"],
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "holding_days": holding_days,
        "return": exit_price / entry_price - 1.0,
        "stop_price": stop_price,
        "take_profit_price": take_profit_price,
    }


def _passes_relative_return_filter(
    prices: pd.DataFrame,
    benchmark_prices: pd.DataFrame,
    event_position: int,
    event_date: str,
    config: TradeBacktestConfig,
) -> bool:
    stock_return = _past_return(prices, event_position, config.relative_return_window)
    benchmark_position = _position_for_date(benchmark_prices, event_date)
    benchmark_return = None if benchmark_position is None else _past_return(
        benchmark_prices,
        benchmark_position,
        config.relative_return_window,
    )
    if stock_return is None or benchmark_return is None:
        return False
    return stock_return - benchmark_return >= config.min_relative_return


def _past_return(prices: pd.DataFrame, position: int, window: int) -> float | None:
    start_position = position - window
    if start_position < 0:
        return None
    start_price = _positive_float(prices.iloc[start_position]["adj_close"])
    end_price = _positive_float(prices.iloc[position]["adj_close"])
    if start_price is None or end_price is None:
        return None
    return end_price / start_price - 1.0


def _with_indicators(frame: pd.DataFrame, config: TradeBacktestConfig) -> pd.DataFrame:
    prepared = _prepare_price_frame(frame)
    previous_close = prepared["close"].shift(1)
    true_range = pd.concat(
        [
            prepared["high"] - prepared["low"],
            (prepared["high"] - previous_close).abs(),
            (prepared["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    prepared["atr"] = true_range.rolling(config.atr_window, min_periods=config.atr_window).mean()
    return prepared


def _benchmark_prices(benchmark_bars: pd.DataFrame, benchmark_ticker: str | None) -> pd.DataFrame:
    if not benchmark_ticker or benchmark_bars.empty:
        return pd.DataFrame()
    return _prepare_price_frame(benchmark_bars[benchmark_bars["ticker"] == benchmark_ticker])


def _prepare_price_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    prepared = frame.copy()
    prepared["trade_date"] = pd.to_datetime(prepared["trade_date"]).dt.date.astype(str)
    prepared = prepared.sort_values("trade_date").reset_index(drop=True)
    return prepared


def _position_for_date(prices: pd.DataFrame, trade_date: str) -> int | None:
    if prices.empty or "trade_date" not in prices.columns:
        return None
    matches = prices.index[prices["trade_date"] == trade_date].tolist()
    return None if not matches else int(matches[0])


def _positive_float(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _equity_drawdown(returns: pd.Series) -> pd.Series:
    equity = (1.0 + returns.fillna(0.0)).cumprod()
    running_max = equity.cummax()
    return equity / running_max - 1.0


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
        raise TradeBacktestError(f"events is missing columns: {', '.join(sorted(missing))}")


def _validate_price_bars(price_bars: pd.DataFrame, name: str) -> None:
    required = {"ticker", "trade_date", "open", "high", "low", "close", "adj_close"}
    missing = required.difference(price_bars.columns)
    if missing:
        raise TradeBacktestError(f"{name} is missing columns: {', '.join(sorted(missing))}")


def _format_summary(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame({"結果": ["該当データなし"]})
    labels = {
        "strategy": "戦略",
        "trade_count": "トレード数",
        "win_rate": "勝率",
        "average_return": "平均損益率",
        "median_return": "中央値損益率",
        "average_win": "平均利益",
        "average_loss": "平均損失",
        "expectancy": "期待値",
        "take_profit_rate": "利確率",
        "stop_loss_rate": "損切率",
        "time_exit_rate": "期限決済率",
        "average_holding_days": "平均保有日数",
        "max_drawdown": "最大DD",
    }
    formatted = summary.copy()
    percent_columns = {
        "win_rate",
        "average_return",
        "median_return",
        "average_win",
        "average_loss",
        "expectancy",
        "take_profit_rate",
        "stop_loss_rate",
        "time_exit_rate",
        "max_drawdown",
    }
    for column in formatted.columns:
        if column in percent_columns:
            formatted[column] = formatted[column].map(_format_percent)
        elif pd.api.types.is_float_dtype(formatted[column]):
            formatted[column] = formatted[column].map(_format_number)
    return formatted.rename(columns=labels)


def _format_trades(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame({"結果": ["該当データなし"]})
    columns = [
        "strategy",
        "ticker",
        "event_date",
        "entry_date",
        "entry_price",
        "exit_date",
        "exit_price",
        "exit_reason",
        "holding_days",
        "return",
    ]
    labels = {
        "strategy": "戦略",
        "ticker": "銘柄",
        "event_date": "イベント日",
        "entry_date": "エントリー日",
        "entry_price": "エントリー価格",
        "exit_date": "決済日",
        "exit_price": "決済価格",
        "exit_reason": "決済理由",
        "holding_days": "保有日数",
        "return": "損益率",
    }
    available = [column for column in columns if column in trades.columns]
    formatted = trades[available].copy()
    for column in ["entry_price", "exit_price"]:
        if column in formatted.columns:
            formatted[column] = formatted[column].map(_format_number)
    if "return" in formatted.columns:
        formatted["return"] = formatted["return"].map(_format_percent)
    return formatted.rename(columns=labels)


def _markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "| 結果 |\n| --- |\n| 該当データなし |"
    headers = [str(column) for column in frame.columns]
    rows = [[_escape_cell(value) for value in row] for row in frame.astype(object).itertuples(index=False, name=None)]
    header_line = "| " + " | ".join(_escape_cell(header) for header in headers) + " |"
    separator_line = "| " + " | ".join("---" for _ in headers) + " |"
    row_lines = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join([header_line, separator_line, *row_lines])


def _escape_cell(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _format_percent(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _format_number(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):,.2f}"
