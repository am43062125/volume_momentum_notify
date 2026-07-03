from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import smtplib
from email.message import EmailMessage
from typing import Iterable

import pandas as pd

from volume_momentum.config import AppConfig
from volume_momentum.data import (
    FetchResult,
    NotificationRecord,
    TickerRecord,
    configure_yfinance_cache,
    fetch_current_market_cap_with_yfinance,
    fetch_prices_with_yfinance,
    get_cached_date_range,
    get_latest_trade_date,
    has_notification_history,
    list_notified_event_keys,
    list_universe_tickers,
    load_price_bars,
    load_universe_from_jpx,
    record_fetch_history,
    record_notification_history,
    upsert_instruments,
    upsert_market_caps,
    upsert_price_bars,
)
from volume_momentum.signals import detect_events


@dataclass(frozen=True)
class DailyReportPaths:
    output_dir: Path
    events_csv: Path
    markdown: Path
    log: Path


@dataclass(frozen=True)
class DailyRunResult:
    latest_trade_date: str
    universe_count: int
    new_count: int
    monitoring_count: int
    paths: DailyReportPaths
    email_sent: bool


class DailyNotificationError(RuntimeError):
    """Raised when the daily notification workflow cannot continue."""


def update_daily_market_data(
    connection: object,
    config: AppConfig,
    download: bool,
    fetch_market_caps: bool,
    limit: int | None = None,
) -> tuple[int, int, int]:
    tickers = load_universe_from_jpx(config.data.jpx_listed_issues_url)
    if limit is not None:
        if limit <= 0:
            raise DailyNotificationError("--limit must be greater than 0")
        tickers = tickers[:limit]
    upsert_instruments(connection, tickers)
    benchmark_records = [
        TickerRecord(ticker=benchmark_ticker, name=benchmark_name, market="benchmark")
        for benchmark_name, benchmark_ticker in config.data.benchmark_tickers.items()
    ]
    upsert_instruments(connection, benchmark_records, instrument_type="benchmark")

    if download or fetch_market_caps:
        configure_yfinance_cache(config.data.yfinance_cache_dir)

    if fetch_market_caps:
        market_caps: dict[str, int | None] = {}
        for record in tickers:
            try:
                market_caps[record.ticker] = fetch_current_market_cap_with_yfinance(record.ticker)
            except Exception:
                market_caps[record.ticker] = None
        upsert_market_caps(connection, market_caps)

    updated = 0
    failed = 0
    if download:
        for record in [*tickers, *benchmark_records]:
            cached_start, cached_end, cached_rows = get_cached_date_range(connection, record.ticker)
            period = config.data.price_period if not cached_rows else f"{config.daily.price_refresh_lookback_days}d"
            requested_start = None
            if cached_rows and cached_end is not None:
                requested_start = (
                    pd.to_datetime(cached_end).date() - timedelta(days=config.daily.price_refresh_lookback_days)
                ).isoformat()
            try:
                bars, result = fetch_prices_with_yfinance(
                    record.ticker,
                    period=period,
                    interval=config.data.price_interval,
                    start=requested_start,
                )
            except Exception as exc:
                failed += 1
                result = FetchResult(
                    ticker=record.ticker,
                    source="yfinance",
                    status="error",
                    start_date=None,
                    end_date=None,
                    rows_fetched=0,
                    message=str(exc),
                )
                record_fetch_history(
                    connection,
                    result,
                    period=period,
                    interval=config.data.price_interval,
                    requested_start=requested_start,
                )
                continue
            upsert_price_bars(connection, bars)
            record_fetch_history(
                connection,
                result,
                period=period,
                interval=config.data.price_interval,
                requested_start=requested_start,
            )
            if not bars.empty:
                updated += 1
    return len(tickers), updated, failed


def build_daily_notification(
    connection: object,
    config: AppConfig,
    limit: int | None = None,
    as_of: str | None = None,
) -> tuple[pd.DataFrame, str, int, str]:
    min_market_cap = config.universe.min_current_market_cap if config.universe.enable_current_market_cap_filter else None
    tickers = list_universe_tickers(connection, min_market_cap)
    if limit is not None:
        if limit <= 0:
            raise DailyNotificationError("--limit must be greater than 0")
        tickers = tickers[:limit]
    if not tickers:
        raise DailyNotificationError("日次通知の対象銘柄がありません。先に銘柄と時価総額を取得してください。")

    latest_trade_date = as_of or get_latest_trade_date(connection, tickers=tickers)
    if latest_trade_date is None:
        raise DailyNotificationError("価格データがありません。先に価格データを取得してください。")

    warmup_days = max(config.signal.long_trading_value_window, config.signal.moving_average_window) * 3
    load_days = config.daily.monitoring_lookback_days * 2 + warmup_days
    load_start = (pd.to_datetime(latest_trade_date).date() - timedelta(days=load_days)).isoformat()
    price_bars = load_price_bars(connection, tickers=tickers, start_date=load_start, end_date=latest_trade_date)
    if price_bars.empty:
        raise DailyNotificationError("日次通知に必要な価格データがありません。")

    events = detect_events(price_bars, config.signal, cooldown_days=config.backtest.reentry_cooldown_days)
    if events.empty:
        return _empty_daily_events(), latest_trade_date, len(tickers), signal_key(config)

    names = _load_instrument_names(connection)
    signal_key_value = signal_key(config)
    history_signal_key = signal_key_value if config.daily.notification_history_mode == "ticker_event_date_signal" else None
    daily_events = classify_daily_events(
        events=events,
        price_bars=price_bars,
        latest_trade_date=latest_trade_date,
        config=config,
        signal_key_value=signal_key_value,
        has_history=has_notification_history(connection),
        notified_keys=list_notified_event_keys(connection, history_signal_key),
        names=names,
    )
    return daily_events, latest_trade_date, len(tickers), signal_key_value


def classify_daily_events(
    events: pd.DataFrame,
    price_bars: pd.DataFrame,
    latest_trade_date: str,
    config: AppConfig,
    signal_key_value: str,
    has_history: bool,
    notified_keys: set[tuple[str, str]],
    names: dict[str, str | None] | None = None,
) -> pd.DataFrame:
    del has_history
    if events.empty:
        return _empty_daily_events()

    names = names or {}
    trading_dates = sorted(pd.to_datetime(price_bars["trade_date"]).dt.date.unique())
    latest_date = pd.to_datetime(latest_trade_date).date()
    price_lookup = _latest_price_lookup(price_bars, latest_trade_date)
    rows: list[dict[str, object]] = []
    for row in events.itertuples(index=False):
        event_date = pd.to_datetime(row.event_date).date()
        elapsed = sum(event_date < trade_date <= latest_date for trade_date in trading_dates)
        if elapsed < 0 or elapsed > config.daily.monitoring_lookback_days:
            continue
        ticker = str(row.ticker)
        is_latest_event = row.event_date == latest_trade_date
        is_unnotified = (ticker, str(row.event_date)) not in notified_keys
        group = _notification_group(is_latest_event, is_unnotified, elapsed, config)
        if group is None:
            continue
        current_close = price_lookup.get(ticker, {}).get("adj_close")
        event_return = None
        if current_close is not None and pd.notna(row.adj_close) and float(row.adj_close) != 0:
            event_return = (float(current_close) / float(row.adj_close)) - 1.0
        rows.append(
            {
                "notification_group": group,
                "ticker": ticker,
                "name": names.get(ticker),
                "event_date": str(row.event_date),
                "elapsed_trading_days": elapsed,
                "adj_close": current_close if current_close is not None else row.adj_close,
                "event_adj_close": row.adj_close,
                "event_return": event_return,
                "inflow_ratio": row.inflow_ratio,
                "long_trading_value_avg": row.long_trading_value_avg,
                "moving_average": row.moving_average,
                "signal_key": signal_key_value,
            }
        )
    if not rows:
        return _empty_daily_events()
    order = {"new": 0, "monitoring_early": 1, "monitoring_late": 2}
    frame = pd.DataFrame(rows)
    frame["_order"] = frame["notification_group"].map(order)
    return frame.sort_values(["_order", "elapsed_trading_days", "ticker"]).drop(columns=["_order"]).reset_index(drop=True)


def write_daily_report(
    config: AppConfig,
    daily_events: pd.DataFrame,
    latest_trade_date: str,
    universe_count: int,
    output_dir: str | Path | None = None,
) -> DailyReportPaths:
    report_root = Path(output_dir) if output_dir is not None else config.daily.output_dir
    report_dir = report_root / latest_trade_date
    report_dir.mkdir(parents=True, exist_ok=True)
    paths = DailyReportPaths(
        output_dir=report_dir,
        events_csv=report_dir / "events.csv",
        markdown=report_dir / "report.md",
        log=report_dir / "run.log",
    )
    daily_events.to_csv(paths.events_csv, index=False, encoding="utf-8-sig")
    paths.markdown.write_text(render_daily_markdown(daily_events, latest_trade_date, universe_count), encoding="utf-8")
    paths.log.write_text(
        "\n".join(
            [
                f"run_at={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"latest_trade_date={latest_trade_date}",
                f"universe_count={universe_count}",
                f"new_count={_count_group(daily_events, 'new')}",
                f"monitoring_count={_count_monitoring(daily_events)}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return paths


def render_daily_markdown(daily_events: pd.DataFrame, latest_trade_date: str, universe_count: int) -> str:
    lines = [
        "# 売買代金モメンタム日次レポート",
        "",
        f"作成日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"対象取引日: {latest_trade_date}",
        f"検証対象銘柄数: {universe_count:,}",
        f"新規ヒット: {_count_group(daily_events, 'new')}件",
        f"監視中: {_count_monitoring(daily_events)}件",
        "",
    ]
    for group, title in [
        ("new", "本日新規ヒット"),
        ("monitoring_early", "監視中: 1〜21営業日"),
        ("monitoring_late", "監視中: 22〜63営業日"),
    ]:
        lines.extend([f"## {title}", "", _markdown_table(_group_frame(daily_events, group)), ""])
    return "\n".join(lines)


def render_email_body(daily_events: pd.DataFrame, latest_trade_date: str, universe_count: int) -> str:
    lines = [
        "売買代金モメンタムの日次通知です。",
        "",
        f"実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"対象取引日: {latest_trade_date}",
        f"検証対象: {universe_count:,}銘柄",
        f"新規ヒット: {_count_group(daily_events, 'new')}件",
        f"監視中: {_count_monitoring(daily_events)}件",
        "",
    ]
    for group, title in [
        ("new", "本日新規ヒット"),
        ("monitoring_early", "監視中: 1〜21営業日"),
        ("monitoring_late", "監視中: 22〜63営業日"),
    ]:
        lines.extend(["━━━━━━━━━━━━━━━━━━━━", f"■ {title}", "━━━━━━━━━━━━━━━━━━━━", ""])
        group_frame = _group_frame(daily_events, group)
        if group_frame.empty:
            lines.extend(["該当なし", ""])
            continue
        for index, row in enumerate(group_frame.itertuples(index=False), start=1):
            name = f" {row.name}" if getattr(row, "name", None) else ""
            lines.append(f"{index}. {row.ticker}{name}")
            lines.append(f"   イベント日: {row.event_date}")
            lines.append(f"   経過営業日: {row.elapsed_trading_days}")
            lines.append(f"   終値: {_format_number(row.adj_close)}")
            if group == "new":
                lines.append(f"   資金流入比率: {_format_number(row.inflow_ratio)}")
                lines.append(f"   売買代金20日平均: {_format_hundred_million(row.long_trading_value_avg)}億円")
                lines.append(f"   75MA: {_format_number(row.moving_average)}")
            else:
                lines.append(f"   イベント日比: {_format_percent(row.event_return)}")
                lines.append(f"   資金流入比率: {_format_number(row.inflow_ratio)}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def send_gmail(config: AppConfig, subject: str, body: str, env: dict[str, str] | None = None) -> None:
    env = env or os.environ
    username = _required_env(env, config.email.username_env)
    password = _required_env(env, config.email.password_env)
    from_addr = _optional_env(env, config.email.from_env) or username
    to_addrs = _split_addresses(_optional_env(env, config.email.to_env) or username)
    cc_addrs = _split_addresses(env.get(config.email.cc_env or "", ""))
    bcc_addrs = _split_addresses(env.get(config.email.bcc_env or "", ""))

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_addr
    message["To"] = ", ".join(to_addrs)
    if cc_addrs:
        message["Cc"] = ", ".join(cc_addrs)
    message.set_content(body)

    recipients = [*to_addrs, *cc_addrs, *bcc_addrs]
    if config.email.use_ssl:
        with smtplib.SMTP_SSL(config.email.smtp_host, config.email.smtp_port) as smtp:
            smtp.login(username, password)
            smtp.send_message(message, to_addrs=recipients)
        return
    with smtplib.SMTP(config.email.smtp_host, config.email.smtp_port) as smtp:
        if config.email.use_starttls:
            smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(message, to_addrs=recipients)


def daily_email_subject(latest_trade_date: str, daily_events: pd.DataFrame) -> str:
    return (
        f"[売買代金モメンタム] {latest_trade_date} "
        f"新規{_count_group(daily_events, 'new')}件 / 監視中{_count_monitoring(daily_events)}件"
    )


def load_env_file(path: str | Path) -> dict[str, str]:
    env_path = Path(path)
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def should_send_email(config: AppConfig, daily_events: pd.DataFrame) -> bool:
    return config.daily.send_empty_email or not daily_events.empty


def notification_records(daily_events: pd.DataFrame) -> list[NotificationRecord]:
    if daily_events.empty:
        return []
    return [
        NotificationRecord(
            ticker=str(row.ticker),
            event_date=str(row.event_date),
            signal_key=str(row.signal_key),
            notification_group=str(row.notification_group),
        )
        for row in daily_events.itertuples(index=False)
    ]


def signal_key(config: AppConfig) -> str:
    payload = {
        "signal": config.signal.__dict__,
        "reentry_cooldown_days": config.backtest.reentry_cooldown_days,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _notification_group(
    is_latest_event: bool,
    is_unnotified: bool,
    elapsed: int,
    config: AppConfig,
) -> str | None:
    if is_latest_event and elapsed == 0 and is_unnotified:
        return "new"
    if elapsed <= 0:
        return None
    if elapsed <= config.daily.early_monitoring_days:
        return "monitoring_early"
    if elapsed <= config.daily.monitoring_lookback_days:
        return "monitoring_late"
    return None


def _load_instrument_names(connection: object) -> dict[str, str | None]:
    rows = connection.execute("SELECT ticker, name FROM instruments").fetchall()
    return {str(row["ticker"]): row["name"] for row in rows}


def _latest_price_lookup(price_bars: pd.DataFrame, latest_trade_date: str) -> dict[str, dict[str, object]]:
    frame = price_bars[pd.to_datetime(price_bars["trade_date"]) <= pd.to_datetime(latest_trade_date)].copy()
    if frame.empty:
        return {}
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    latest_rows = frame.sort_values(["ticker", "trade_date"]).groupby("ticker").tail(1)
    return {
        str(row.ticker): {"adj_close": row.adj_close, "trade_date": row.trade_date}
        for row in latest_rows.itertuples(index=False)
    }


def _group_frame(daily_events: pd.DataFrame, group: str) -> pd.DataFrame:
    if daily_events.empty:
        return daily_events
    return daily_events[daily_events["notification_group"] == group].reset_index(drop=True)


def _count_group(daily_events: pd.DataFrame, group: str) -> int:
    return len(_group_frame(daily_events, group))


def _count_monitoring(daily_events: pd.DataFrame) -> int:
    if daily_events.empty:
        return 0
    return int(daily_events["notification_group"].isin(["monitoring_early", "monitoring_late"]).sum())


def _markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "| 結果 |\n| --- |\n| 該当なし |"
    columns = [
        "ticker",
        "name",
        "event_date",
        "elapsed_trading_days",
        "adj_close",
        "event_return",
        "inflow_ratio",
        "long_trading_value_avg",
        "moving_average",
    ]
    labels = {
        "ticker": "銘柄",
        "name": "銘柄名",
        "event_date": "イベント日",
        "elapsed_trading_days": "経過営業日",
        "adj_close": "終値",
        "event_return": "イベント日比",
        "inflow_ratio": "資金流入比率",
        "long_trading_value_avg": "売買代金20日平均",
        "moving_average": "75MA",
    }
    available = [column for column in columns if column in frame.columns]
    formatted = frame[available].copy()
    for column in ["adj_close", "inflow_ratio", "moving_average"]:
        if column in formatted.columns:
            formatted[column] = formatted[column].map(_format_number)
    if "event_return" in formatted.columns:
        formatted["event_return"] = formatted["event_return"].map(_format_percent)
    if "long_trading_value_avg" in formatted.columns:
        formatted["long_trading_value_avg"] = formatted["long_trading_value_avg"].map(_format_hundred_million)
    formatted = formatted.rename(columns=labels)
    header = "| " + " | ".join(formatted.columns) + " |"
    separator = "| " + " | ".join("---" for _ in formatted.columns) + " |"
    rows = ["| " + " | ".join(_escape_cell(value) for value in row) + " |" for row in formatted.itertuples(index=False, name=None)]
    return "\n".join([header, separator, *rows])


def _empty_daily_events() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "notification_group",
            "ticker",
            "name",
            "event_date",
            "elapsed_trading_days",
            "adj_close",
            "event_adj_close",
            "event_return",
            "inflow_ratio",
            "long_trading_value_avg",
            "moving_average",
            "signal_key",
        ]
    )


def _required_env(env: dict[str, str], key: str) -> str:
    value = env.get(key)
    if not value:
        raise DailyNotificationError(f"Environment variable is required: {key}")
    return value


def _optional_env(env: dict[str, str], key: str) -> str | None:
    value = env.get(key)
    return value or None


def _split_addresses(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _escape_cell(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _format_number(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):,.2f}"


def _format_percent(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:+.1f}%"


def _format_hundred_million(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) / 100_000_000:,.2f}"
