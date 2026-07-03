from __future__ import annotations

import argparse
import os
from pathlib import Path
from datetime import timedelta

import pandas as pd

from volume_momentum.config import ConfigError, load_config
from volume_momentum.data import (
    DataError,
    FetchResult,
    TickerRecord,
    configure_yfinance_cache,
    connect_database,
    count_instruments,
    count_instruments_missing_market_cap,
    fetch_current_market_cap_with_yfinance,
    fetch_prices_with_yfinance,
    get_cached_date_range,
    get_latest_trade_date,
    initialize_database,
    list_instrument_tickers,
    list_universe_tickers,
    load_price_bars,
    load_universe_from_jpx,
    record_fetch_history,
    record_notification_history,
    upsert_instruments,
    upsert_market_caps,
    upsert_price_bars,
)
from volume_momentum.daily import (
    DailyNotificationError,
    build_daily_notification,
    daily_email_subject,
    load_env_file,
    notification_records,
    render_email_body,
    send_gmail,
    should_send_email,
    update_daily_market_data,
    write_daily_report,
)
from volume_momentum.evaluation import EvaluationError, evaluate_events, summarize_evaluations
from volume_momentum.reporting import write_reports
from volume_momentum.signals import SignalError, detect_events


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="volume-momentum",
        description="売買代金モメンタム検証ツール",
    )
    parser.add_argument(
        "--config",
        default="config.example.json",
        help="設定ファイルへのパス。既定値: config.example.json",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("show-config", help="設定ファイルを読み込み、主要設定を表示します。")
    fetch_parser = subparsers.add_parser("fetch-data", help="価格データのSQLiteキャッシュを作成・更新します。")
    fetch_parser.add_argument(
        "--download",
        action="store_true",
        help="yfinance から実際に価格データを取得します。指定しない場合はDB初期化と対象確認のみ行います。",
    )
    fetch_parser.add_argument(
        "--market-caps",
        action="store_true",
        help="yfinance から現在時価総額を取得してSQLiteへ保存します。",
    )
    fetch_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="処理する銘柄数の上限。動作確認用です。",
    )
    fetch_parser.add_argument(
        "--refresh",
        action="store_true",
        help="保存済み価格データがある銘柄も再取得します。指定しない場合は取得済み銘柄をスキップします。",
    )
    backtest_parser = subparsers.add_parser("backtest", help="保存済みデータでイベント検出を実行します。")
    backtest_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="処理する銘柄数の上限。動作確認用です。",
    )
    backtest_parser.add_argument(
        "--write-reports",
        action="store_true",
        help="CSVと日本語Markdownレポートを出力します。",
    )
    backtest_parser.add_argument(
        "--output-dir",
        default=None,
        help="レポート出力先。指定しない場合は設定ファイルの report.output_dir を使用します。",
    )
    report_parser = subparsers.add_parser("report", help="保存済みデータからCSVと日本語Markdownレポートを生成します。")
    report_parser.add_argument("--limit", type=int, default=None, help="処理する銘柄数の上限。動作確認用です。")
    report_parser.add_argument("--output-dir", default=None, help="レポート出力先。")
    daily_parser = subparsers.add_parser("daily-notify", help="日次イベント検知とGmail通知を実行します。")
    daily_parser.add_argument("--download", action="store_true", help="日次価格データをyfinanceから差分更新します。")
    daily_parser.add_argument("--market-caps", action="store_true", help="現在時価総額を更新します。")
    daily_parser.add_argument("--send-email", action="store_true", help="Gmail SMTPでメールを送信します。")
    daily_parser.add_argument("--send-empty-email", action="store_true", help="通知対象が0件でもメールを送信します。疎通確認用です。")
    daily_parser.add_argument("--dry-run", action="store_true", help="メール送信と通知履歴登録を行わず、レポートのみ出力します。")
    daily_parser.add_argument("--limit", type=_limit_arg, default=None, help="処理する銘柄数の上限。動作確認用です。")
    daily_parser.add_argument("--as-of", default=None, help="対象取引日をYYYY-MM-DDで指定します。通常は最新取引日を使います。")
    daily_parser.add_argument("--output-dir", default=None, help="日次レポート出力先。")
    daily_parser.add_argument("--env-file", default=".env", help="メール認証情報を読み込む.envファイル。既定値: .env")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        parser.error(str(exc))

    if args.command == "show-config":
        _print_config_summary(config)
        return 0

    if args.command == "fetch-data":
        try:
            return _run_fetch_data(
                config,
                download=args.download,
                fetch_market_caps=args.market_caps,
                refresh=args.refresh,
                limit=args.limit,
            )
        except DataError as exc:
            parser.error(str(exc))

    if args.command == "backtest":
        try:
            return _run_backtest(
                config,
                limit=args.limit,
                write_report_files=args.write_reports,
                output_dir=args.output_dir,
            )
        except (DataError, SignalError, EvaluationError) as exc:
            parser.error(str(exc))

    if args.command == "report":
        try:
            return _run_backtest(
                config,
                limit=args.limit,
                write_report_files=True,
                output_dir=args.output_dir,
            )
        except (DataError, SignalError, EvaluationError) as exc:
            parser.error(str(exc))

    if args.command == "daily-notify":
        try:
            return _run_daily_notify(
                config,
                download=args.download,
                fetch_market_caps=args.market_caps,
                send_email=args.send_email,
                send_empty_email=args.send_empty_email,
                dry_run=args.dry_run,
                limit=args.limit,
                as_of=args.as_of,
                output_dir=args.output_dir,
                env_file=args.env_file,
            )
        except (DataError, SignalError, DailyNotificationError) as exc:
            parser.error(str(exc))

    parser.error(f"Unknown command: {args.command}")


def _print_config_summary(config: object) -> None:
    print("設定ファイルを読み込みました。")
    print(f"バックテスト期間: 過去{config.backtest.period_years}年")
    print(f"評価期間: {', '.join(str(days) for days in config.backtest.evaluation_horizons)}営業日後")
    print(f"SQLite DB: {config.data.database_path}")
    print(f"現在時価総額フィルタ: {'有効' if config.universe.enable_current_market_cap_filter else '無効'}")
    print(f"現在時価総額下限: {config.universe.min_current_market_cap}")
    print(f"Markdown レポート言語: {config.report.language}")


def _limit_arg(value: str) -> int:
    normalized = value.removeprefix("limit=")
    try:
        limit = int(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer") from exc
    if limit <= 0:
        raise argparse.ArgumentTypeError("limit must be greater than 0")
    return limit


def _run_fetch_data(config: object, download: bool, fetch_market_caps: bool, refresh: bool, limit: int | None) -> int:
    connection = connect_database(config.data.database_path)
    try:
        initialize_database(connection)

        tickers = load_universe_from_jpx(config.data.jpx_listed_issues_url)
        if limit is not None:
            if limit <= 0:
                raise DataError("--limit must be greater than 0")
            tickers = tickers[:limit]
        upsert_instruments(connection, tickers)
        benchmark_records = [
            TickerRecord(ticker=benchmark_ticker, name=benchmark_name, market="benchmark")
            for benchmark_name, benchmark_ticker in config.data.benchmark_tickers.items()
        ]
        upsert_instruments(connection, benchmark_records, instrument_type="benchmark")

        print("SQLite キャッシュを初期化しました。")
        print(f"対象銘柄数: {len(tickers)}")
        print(f"ベンチマーク数: {len(benchmark_records)}")
        print(f"DB: {config.data.database_path}")

        if download or fetch_market_caps:
            configure_yfinance_cache(config.data.yfinance_cache_dir)

        if fetch_market_caps:
            _fetch_and_store_market_caps(connection, tickers)

        _print_market_cap_summary(connection)

        if not download:
            print("価格データ取得は実行していません。実取得する場合は --download を指定してください。")
            _print_cache_summary(connection, [record.ticker for record in tickers + benchmark_records])
            return 0

        for record in tickers + benchmark_records:
            cached_start, cached_end, cached_rows = get_cached_date_range(connection, record.ticker)
            if cached_rows and not refresh:
                print(f"{record.ticker}: skip cached ({cached_rows} rows, {cached_start} - {cached_end})")
                continue
            try:
                bars, result = fetch_prices_with_yfinance(
                    record.ticker,
                    period=config.data.price_period,
                    interval=config.data.price_interval,
                )
            except Exception as exc:
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
                    period=config.data.price_period,
                    interval=config.data.price_interval,
                )
                print(f"{record.ticker}: error (0 rows) {exc}")
                continue
            upsert_price_bars(connection, bars)
            record_fetch_history(
                connection,
                result,
                period=config.data.price_period,
                interval=config.data.price_interval,
            )
            print(f"{record.ticker}: {result.status} ({result.rows_fetched} rows)")

        _print_cache_summary(connection, [record.ticker for record in tickers + benchmark_records])
        return 0
    finally:
        connection.close()


def _print_cache_summary(connection: object, tickers: list[str]) -> None:
    cached = 0
    empty = 0
    for ticker in tickers:
        start_date, end_date, row_count = get_cached_date_range(connection, ticker)
        if row_count:
            cached += 1
            print(f"{ticker}: {row_count} rows ({start_date} - {end_date})")
        else:
            empty += 1
    print(f"キャッシュ済み銘柄数: {cached}")
    print(f"未取得銘柄数: {empty}")


def _fetch_and_store_market_caps(connection: object, tickers: list[TickerRecord]) -> None:
    market_caps: dict[str, int | None] = {}
    for record in tickers:
        try:
            market_cap = fetch_current_market_cap_with_yfinance(record.ticker)
        except DataError as exc:
            raise exc
        except Exception as exc:
            market_cap = None
            print(f"{record.ticker}: 現在時価総額の取得に失敗しました ({exc})")
        market_caps[record.ticker] = market_cap
        if market_cap is not None:
            print(f"{record.ticker}: 現在時価総額 {market_cap}")

    upsert_market_caps(connection, market_caps)
    print(f"現在時価総額 取得成功: {sum(value is not None for value in market_caps.values())}")
    print(f"現在時価総額 取得失敗: {sum(value is None for value in market_caps.values())}")


def _print_market_cap_summary(connection: object) -> None:
    total = count_instruments(connection)
    missing = count_instruments_missing_market_cap(connection)
    available = total - missing
    print(f"現在時価総額 登録済み銘柄数: {available}")
    print(f"現在時価総額 未取得銘柄数: {missing}")


def _run_backtest(config: object, limit: int | None, write_report_files: bool, output_dir: str | None) -> int:
    connection = connect_database(config.data.database_path)
    try:
        initialize_database(connection)
        min_market_cap = (
            config.universe.min_current_market_cap
            if config.universe.enable_current_market_cap_filter
            else None
        )
        tickers = list_universe_tickers(connection, min_market_cap)
        if limit is not None:
            if limit <= 0:
                raise DataError("--limit must be greater than 0")
            tickers = tickers[:limit]
        if not tickers:
            if config.universe.enable_current_market_cap_filter:
                raise DataError(
                    "分析対象銘柄がありません。現在時価総額フィルタが有効ですが、"
                    "時価総額が未取得か条件を満たす銘柄がありません。"
                    "設定ファイルでフィルタを無効にするか、時価総額を取得してください。"
                )
            raise DataError("分析対象銘柄がありません。先に fetch-data を実行して銘柄を登録してください。")

        latest_trade_date = get_latest_trade_date(connection, tickers=tickers)
        if latest_trade_date is None:
            raise DataError("価格データがありません。先に fetch-data --download を実行してください。")
        analysis_start = _analysis_start_date(latest_trade_date, config.backtest.period_years)
        warmup_days = max(
            config.signal.long_trading_value_window,
            config.signal.moving_average_window,
        ) * 3
        load_start = _date_offset(analysis_start, -warmup_days)
        price_bars = load_price_bars(connection, tickers=tickers, start_date=load_start)
        if price_bars.empty:
            raise DataError("価格データがありません。先に fetch-data --download を実行してください。")

        events = detect_events(
            price_bars,
            config.signal,
            cooldown_days=config.backtest.reentry_cooldown_days,
        )
        if not events.empty:
            events = events[events["event_date"] >= analysis_start].reset_index(drop=True)
        benchmark_tickers = list_instrument_tickers(connection, "benchmark")
        benchmark_bars = load_price_bars(connection, tickers=benchmark_tickers, start_date=load_start)
        evaluations = pd.DataFrame()
        summary = pd.DataFrame()
        if not events.empty:
            evaluations = evaluate_events(
                events,
                price_bars,
                benchmark_bars,
                horizons=config.backtest.evaluation_horizons,
                benchmark_tickers=config.data.benchmark_tickers,
            )
            summary = summarize_evaluations(evaluations)

        print("イベント検出を実行しました。")
        print(f"対象銘柄数: {len(tickers)}")
        print(f"分析開始日: {analysis_start}")
        print(f"価格行数: {len(price_bars)}")
        print(f"イベント数: {len(events)}")
        print(f"評価行数: {len(evaluations)}")
        if not events.empty:
            print(events.head(10).to_string(index=False))
        if not summary.empty:
            print("評価サマリー:")
            print(summary.to_string(index=False))
        if write_report_files:
            paths = write_reports(
                config=config,
                events=events,
                evaluations=evaluations,
                summary=summary,
                universe_count=len(tickers),
                price_row_count=len(price_bars),
                analysis_start_date=analysis_start,
                analysis_end_date=latest_trade_date,
                output_dir=output_dir,
            )
            print(f"CSV/Markdownレポートを出力しました: {paths.output_dir}")
        return 0
    finally:
        connection.close()


def _run_daily_notify(
    config: object,
    download: bool,
    fetch_market_caps: bool,
    send_email: bool,
    send_empty_email: bool,
    dry_run: bool,
    limit: int | None,
    as_of: str | None,
    output_dir: str | None,
    env_file: str,
) -> int:
    if not config.daily.enabled:
        raise DailyNotificationError("日次通知は設定で無効化されています。")
    connection = connect_database(config.data.database_path)
    try:
        initialize_database(connection)
        updated = 0
        failed = 0
        if download or fetch_market_caps:
            _, updated, failed = update_daily_market_data(
                connection,
                config,
                download=download,
                fetch_market_caps=fetch_market_caps,
                limit=limit,
            )

        daily_events, latest_trade_date, universe_count, _ = build_daily_notification(
            connection,
            config,
            limit=limit,
            as_of=as_of,
        )
        paths = write_daily_report(
            config,
            daily_events,
            latest_trade_date=latest_trade_date,
            universe_count=universe_count,
            output_dir=output_dir,
        )

        email_sent = False
        email_requested = should_send_email(config, daily_events) or send_empty_email
        if send_email and not dry_run and email_requested:
            env = dict(load_env_file(env_file))
            env.update({key: value for key, value in os.environ.items() if key not in env})
            send_gmail(
                config,
                subject=daily_email_subject(latest_trade_date, daily_events),
                body=render_email_body(daily_events, latest_trade_date, universe_count),
                env=env,
            )
            email_sent = True

        if email_sent:
            record_count = record_notification_history(connection, notification_records(daily_events))
        else:
            record_count = 0

        print("日次イベント検知を実行しました。")
        print(f"対象取引日: {latest_trade_date}")
        print(f"対象銘柄数: {universe_count}")
        print(f"価格更新成功銘柄数: {updated}")
        print(f"価格更新失敗銘柄数: {failed}")
        print(f"新規ヒット: {len(daily_events[daily_events['notification_group'] == 'new']) if not daily_events.empty else 0}")
        print(
            "監視中: "
            f"{int(daily_events['notification_group'].isin(['monitoring_early', 'monitoring_late']).sum()) if not daily_events.empty else 0}"
        )
        print(f"通知履歴登録件数: {record_count}")
        print(f"メール送信: {'実行' if email_sent else '未実行'}")
        print(f"日次レポート: {paths.output_dir}")
        return 0
    finally:
        connection.close()


def _analysis_start_date(latest_trade_date: str, period_years: int) -> str:
    latest = pd.to_datetime(latest_trade_date).date()
    try:
        start = latest.replace(year=latest.year - period_years)
    except ValueError:
        start = latest - timedelta(days=365 * period_years)
    return start.isoformat()


def _date_offset(date_text: str, days: int) -> str:
    date_value = pd.to_datetime(date_text).date()
    return (date_value + timedelta(days=days)).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
