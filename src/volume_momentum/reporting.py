from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from volume_momentum.config import AppConfig


@dataclass(frozen=True)
class ReportPaths:
    output_dir: Path
    events_csv: Path
    evaluations_csv: Path
    summary_csv: Path
    markdown: Path


def write_reports(
    config: AppConfig,
    events: pd.DataFrame,
    evaluations: pd.DataFrame,
    summary: pd.DataFrame,
    universe_count: int,
    price_row_count: int,
    analysis_start_date: str,
    analysis_end_date: str,
    output_dir: str | Path | None = None,
) -> ReportPaths:
    report_dir = Path(output_dir) if output_dir is not None else config.report.output_dir
    report_dir.mkdir(parents=True, exist_ok=True)

    paths = ReportPaths(
        output_dir=report_dir,
        events_csv=report_dir / "events.csv",
        evaluations_csv=report_dir / "evaluations.csv",
        summary_csv=report_dir / "summary.csv",
        markdown=report_dir / "report.md",
    )
    events.to_csv(paths.events_csv, index=False, encoding="utf-8-sig")
    evaluations.to_csv(paths.evaluations_csv, index=False, encoding="utf-8-sig")
    summary.to_csv(paths.summary_csv, index=False, encoding="utf-8-sig")
    paths.markdown.write_text(
        render_markdown_report(
            config=config,
            events=events,
            evaluations=evaluations,
            summary=summary,
            universe_count=universe_count,
            price_row_count=price_row_count,
            analysis_start_date=analysis_start_date,
            analysis_end_date=analysis_end_date,
        ),
        encoding="utf-8",
    )
    return paths


def render_markdown_report(
    config: AppConfig,
    events: pd.DataFrame,
    evaluations: pd.DataFrame,
    summary: pd.DataFrame,
    universe_count: int,
    price_row_count: int,
    analysis_start_date: str,
    analysis_end_date: str,
) -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines: list[str] = [
        "# 売買代金モメンタム検証レポート",
        "",
        f"作成日時: {generated_at}",
        "",
        "## 実行条件",
        "",
        "| 項目 | 値 |",
        "| --- | --- |",
        f"| バックテスト期間 | 過去{config.backtest.period_years}年 |",
        f"| 分析開始日 | {analysis_start_date} |",
        f"| 分析終了日 | {analysis_end_date} |",
        f"| 評価期間 | {', '.join(str(days) for days in config.backtest.evaluation_horizons)}営業日後 |",
        f"| 売買代金短期平均 | {config.signal.short_trading_value_window}日 |",
        f"| 売買代金長期平均 | {config.signal.long_trading_value_window}日 |",
        f"| 資金流入比率しきい値 | {config.signal.inflow_ratio_threshold} |",
        f"| 継続営業日数 | {config.signal.min_consecutive_days}日 |",
        f"| 最低売買代金長期平均 | {config.signal.min_long_trading_value:,.0f}円 |",
        f"| 移動平均期間 | {config.signal.moving_average_window}日 |",
        f"| 終値上昇条件 | {'有効' if config.signal.require_close_above_previous else '無効'} |",
        f"| 過去終値比較 | {config.signal.close_lookback_days}営業日前 |",
        f"| 再登録禁止期間 | {config.backtest.reentry_cooldown_days}営業日 |",
        f"| 現在時価総額フィルタ | {'有効' if config.universe.enable_current_market_cap_filter else '無効'} |",
        f"| 現在時価総額下限 | {_format_optional_number(config.universe.min_current_market_cap)}円 |",
        f"| 対象銘柄数 | {universe_count} |",
        f"| 価格データ行数 | {price_row_count} |",
        f"| 検出イベント数 | {len(events)} |",
        f"| 評価行数 | {len(evaluations)} |",
        "",
        "## 注意事項",
        "",
        "- 現在時価総額フィルタは、現在時点の情報を使った近似的なユニバースフィルタです。過去イベント日時点の厳密な時価総額条件ではありません。",
        "- 検証対象はJPX公式一覧から取得した現在上場中の銘柄です。過去に上場廃止した銘柄は本フェーズでは対象外であり、サバイバーシップバイアスが含まれる可能性があります。",
        "- TOPIX は Yahoo Finance で指数ティッカーを安定取得できなかったため、設定上は `1306.T` をTOPIX連動ETFによる代替ベンチマークとして扱います。",
        "- 本レポートは売買ルールの成績ではなく、資金流入イベント後の株価パフォーマンスを確認するイベントスタディです。",
        "",
        "## 評価サマリー",
        "",
        _markdown_table(_format_summary(summary)),
        "",
        "## イベント一覧",
        "",
        _markdown_table(_format_events(events.head(50))),
        "",
        "## 評価明細",
        "",
        _markdown_table(_format_evaluations(evaluations.head(100))),
        "",
    ]
    return "\n".join(lines)


def _format_summary(summary: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "horizon_days",
        "sample_count",
        "average_return",
        "median_return",
        "max_return",
        "min_return",
        "std_return",
        "win_rate",
        "hit_rate_10pct",
        "hit_rate_20pct",
        "hit_rate_30pct",
        "hit_rate_50pct",
        "hit_rate_100pct",
        "average_mfe",
        "average_mae",
        "max_drawdown",
        "average_drawdown",
    ]
    for column in summary.columns:
        if column.startswith("average_") and (
            column.endswith("_return") or column.endswith("_excess_return")
        ) and column not in columns:
            columns.append(column)
    labels = {
        "horizon_days": "評価期間",
        "sample_count": "サンプル数",
        "average_return": "平均リターン",
        "median_return": "中央値リターン",
        "max_return": "最大リターン",
        "min_return": "最小リターン",
        "std_return": "標準偏差",
        "win_rate": "勝率",
        "hit_rate_10pct": "+10%以上",
        "hit_rate_20pct": "+20%以上",
        "hit_rate_30pct": "+30%以上",
        "hit_rate_50pct": "+50%以上",
        "hit_rate_100pct": "+100%以上",
        "average_mfe": "平均MFE",
        "average_mae": "平均MAE",
        "max_drawdown": "最大DD",
        "average_drawdown": "平均DD",
    }
    for column in columns:
        if column.startswith("average_") and column.endswith("_excess_return"):
            labels[column] = column.removeprefix("average_").removesuffix("_excess_return") + "超過平均"
        elif column.startswith("average_") and column.endswith("_return"):
            labels[column] = column.removeprefix("average_").removesuffix("_return") + "平均"
    return _select_and_format(summary, columns, labels)


def _format_events(events: pd.DataFrame) -> pd.DataFrame:
    columns = ["ticker", "event_date", "adj_close", "inflow_ratio", "long_trading_value_avg"]
    labels = {
        "ticker": "銘柄",
        "event_date": "イベント日",
        "adj_close": "調整後終値",
        "inflow_ratio": "資金流入比率",
        "long_trading_value_avg": "売買代金長期平均",
    }
    return _select_and_format(events, columns, labels)


def _format_evaluations(evaluations: pd.DataFrame) -> pd.DataFrame:
    columns = ["ticker", "event_date", "horizon_days", "target_date", "return", "mfe", "mae", "max_drawdown", "complete"]
    labels = {
        "ticker": "銘柄",
        "event_date": "イベント日",
        "horizon_days": "評価期間",
        "target_date": "評価日",
        "return": "リターン",
        "mfe": "MFE",
        "mae": "MAE",
        "max_drawdown": "最大DD",
        "complete": "完了",
    }
    return _select_and_format(evaluations, columns, labels)


def _select_and_format(frame: pd.DataFrame, columns: list[str], labels: dict[str, str]) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame({"結果": ["該当データなし"]})
    available = [column for column in columns if column in frame.columns]
    formatted = frame[available].copy()
    for column in formatted.columns:
        if (
            column.endswith("return")
            or column.startswith("hit_rate")
            or column in {"win_rate", "mfe", "mae", "max_drawdown", "average_mfe", "average_mae", "average_drawdown"}
        ):
            formatted[column] = formatted[column].map(_format_percent)
        elif pd.api.types.is_float_dtype(formatted[column]):
            formatted[column] = formatted[column].map(_format_number)
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
    if pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _format_number(value: object) -> str:
    if pd.isna(value):
        return ""
    return f"{float(value):,.2f}"


def _format_optional_number(value: int | None) -> str:
    return "なし" if value is None else f"{value:,.0f}"
