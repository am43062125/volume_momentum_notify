import tempfile
import unittest
from pathlib import Path

import pandas as pd

from volume_momentum.data import (
    FetchResult,
    NotificationRecord,
    TickerRecord,
    connect_database,
    count_instruments,
    count_instruments_missing_market_cap,
    get_cached_date_range,
    has_notification_history,
    initialize_database,
    list_notified_event_keys,
    list_universe_tickers,
    parse_jpx_listed_issues,
    normalize_price_frame,
    record_fetch_history,
    record_notification_history,
    upsert_instruments,
    upsert_market_caps,
    upsert_price_bars,
)


class DataLayerTests(unittest.TestCase):
    def test_parse_jpx_listed_issues(self):
        frame = pd.DataFrame(
            [
                {"コード": 7203, "銘柄名": "トヨタ自動車", "市場・商品区分": "プライム（内国株式）"},
                {"コード": 1306, "銘柄名": "TOPIX ETF", "市場・商品区分": "ETF・ETN"},
            ]
        )

        records = parse_jpx_listed_issues(frame)

        self.assertEqual(records, [TickerRecord(ticker="7203.T", name="トヨタ自動車", market="プライム（内国株式）")])

    def test_initialize_database_and_upsert_price_bars(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            connection = connect_database(Path(tmpdir) / "test.sqlite3")
            try:
                initialize_database(connection)
                upsert_instruments(connection, [TickerRecord("7203.T", "Toyota", "TSE")])

                raw = pd.DataFrame(
                    {
                        "Open": [1000.0, 1010.0],
                        "High": [1020.0, 1030.0],
                        "Low": [990.0, 1005.0],
                        "Close": [1015.0, 1025.0],
                        "Adj Close": [1015.0, 1025.0],
                        "Volume": [1_000_000, 2_000_000],
                    },
                    index=pd.to_datetime(["2026-01-05", "2026-01-06"]),
                )

                bars = normalize_price_frame("7203.T", raw)
                rows = upsert_price_bars(connection, bars)
                start_date, end_date, row_count = get_cached_date_range(connection, "7203.T")
            finally:
                connection.close()

        self.assertEqual(rows, 2)
        self.assertEqual(start_date, "2026-01-05")
        self.assertEqual(end_date, "2026-01-06")
        self.assertEqual(row_count, 2)
        self.assertEqual(float(bars.loc[0, "trading_value"]), 1_015_000_000.0)

    def test_market_cap_universe_filter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            connection = connect_database(Path(tmpdir) / "test.sqlite3")
            try:
                initialize_database(connection)
                upsert_instruments(
                    connection,
                    [
                        TickerRecord("AAA.T", "Large", "TSE"),
                        TickerRecord("BBB.T", "Small", "TSE"),
                    ],
                )
                upsert_market_caps(connection, {"AAA.T": 50_000_000_000, "BBB.T": 10_000_000_000})

                tickers = list_universe_tickers(connection, min_current_market_cap=30_000_000_000)
            finally:
                connection.close()

        self.assertEqual(tickers, ["AAA.T"])

    def test_universe_filter_excludes_benchmarks_when_market_cap_filter_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            connection = connect_database(Path(tmpdir) / "test.sqlite3")
            try:
                initialize_database(connection)
                upsert_instruments(connection, [TickerRecord("AAA.T", "Large", "TSE")])
                upsert_instruments(connection, [TickerRecord("^N225", "nikkei225", "benchmark")], instrument_type="benchmark")

                tickers = list_universe_tickers(connection, min_current_market_cap=None)
            finally:
                connection.close()

        self.assertEqual(tickers, ["AAA.T"])

    def test_counts_missing_market_caps(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            connection = connect_database(Path(tmpdir) / "test.sqlite3")
            try:
                initialize_database(connection)
                upsert_instruments(
                    connection,
                    [
                        TickerRecord("AAA.T", "Large", "TSE"),
                        TickerRecord("BBB.T", "Unknown", "TSE"),
                    ],
                )
                upsert_market_caps(connection, {"AAA.T": 50_000_000_000})

                total = count_instruments(connection)
                missing = count_instruments_missing_market_cap(connection)
            finally:
                connection.close()

        self.assertEqual(total, 2)
        self.assertEqual(missing, 1)

    def test_records_fetch_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            connection = connect_database(Path(tmpdir) / "test.sqlite3")
            try:
                initialize_database(connection)
                upsert_instruments(connection, [TickerRecord("7203.T")])

                record_fetch_history(
                    connection,
                    FetchResult(
                        ticker="7203.T",
                        source="yfinance",
                        status="success",
                        start_date="2026-01-05",
                        end_date="2026-01-06",
                        rows_fetched=2,
                    ),
                    period="5y",
                    interval="1d",
                )
                row = connection.execute("SELECT ticker, status, rows_fetched FROM fetch_history").fetchone()
            finally:
                connection.close()

        self.assertEqual(dict(row), {"ticker": "7203.T", "status": "success", "rows_fetched": 2})

    def test_records_notification_history_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            connection = connect_database(Path(tmpdir) / "test.sqlite3")
            try:
                initialize_database(connection)

                inserted = record_notification_history(
                    connection,
                    [
                        NotificationRecord("7203.T", "2026-07-03", "signal-a", "new"),
                        NotificationRecord("7203.T", "2026-07-03", "signal-a", "new"),
                    ],
                    notified_at="2026-07-03T11:00:00+00:00",
                )
                keys = list_notified_event_keys(connection, "signal-a")
                has_history = has_notification_history(connection)
            finally:
                connection.close()

        self.assertEqual(inserted, 1)
        self.assertTrue(has_history)
        self.assertEqual(keys, {("7203.T", "2026-07-03")})


if __name__ == "__main__":
    unittest.main()
