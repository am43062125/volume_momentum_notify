import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from volume_momentum.cli import _analysis_start_date, _fetch_and_store_market_caps, _run_fetch_data
from volume_momentum.config import load_config
from volume_momentum.data import (
    FetchResult,
    TickerRecord,
    connect_database,
    initialize_database,
    list_universe_tickers,
    normalize_price_frame,
    upsert_instruments,
)
import pandas as pd


class CliTests(unittest.TestCase):
    def test_analysis_start_date_uses_period_years(self):
        self.assertEqual(_analysis_start_date("2026-07-02", 5), "2021-07-02")

    def test_fetch_and_store_market_caps_uses_yfinance_adapter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            connection = connect_database(Path(tmpdir) / "test.sqlite3")
            try:
                initialize_database(connection)
                records = [
                    TickerRecord("AAA.T", "Large", "TSE"),
                    TickerRecord("BBB.T", "Small", "TSE"),
                ]
                upsert_instruments(connection, records)

                with patch(
                    "volume_momentum.cli.fetch_current_market_cap_with_yfinance",
                    side_effect=[50_000_000_000, 10_000_000_000],
                ):
                    with redirect_stdout(StringIO()):
                        _fetch_and_store_market_caps(connection, records)

                tickers = list_universe_tickers(connection, min_current_market_cap=30_000_000_000)
            finally:
                connection.close()

        self.assertEqual(tickers, ["AAA.T"])

    def test_fetch_data_continues_when_one_price_download_fails(self):
        config = load_config("config.example.json")
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _replace_config_paths(config, tmpdir)
            universe_records = [TickerRecord("AAA.T", "AAA", "プライム（内国株式）"), TickerRecord("BBB.T", "BBB", "プライム（内国株式）")]

            raw = pd.DataFrame(
                {
                    "Open": [100.0],
                    "High": [101.0],
                    "Low": [99.0],
                    "Close": [100.0],
                    "Adj Close": [100.0],
                    "Volume": [1000],
                },
                index=pd.to_datetime(["2026-01-05"]),
            )
            good_bars = normalize_price_frame("AAA.T", raw)
            good_result = FetchResult("AAA.T", "yfinance", "success", "2026-01-05", "2026-01-05", 1)

            def fake_fetch(ticker, period, interval):
                if ticker == "BBB.T":
                    raise RuntimeError("download failed")
                return good_bars.assign(ticker=ticker), FetchResult(ticker, "yfinance", "success", "2026-01-05", "2026-01-05", 1)

            with patch("volume_momentum.cli.load_universe_from_jpx", return_value=universe_records), patch(
                "volume_momentum.cli.configure_yfinance_cache"
            ), patch(
                "volume_momentum.cli.fetch_prices_with_yfinance",
                side_effect=fake_fetch,
            ), redirect_stdout(StringIO()):
                result_code = _run_fetch_data(config, download=True, fetch_market_caps=False, refresh=False, limit=None)

            connection = connect_database(config.data.database_path)
            try:
                rows = connection.execute("SELECT ticker, status FROM fetch_history ORDER BY ticker").fetchall()
            finally:
                connection.close()

        self.assertEqual(result_code, 0)
        self.assertIn(("BBB.T", "error"), [(row["ticker"], row["status"]) for row in rows])

    def test_fetch_data_skips_cached_prices_by_default(self):
        config = load_config("config.example.json")
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _replace_config_paths(config, tmpdir)
            universe_records = [TickerRecord("AAA.T", "AAA", "プライム（内国株式）")]

            raw = pd.DataFrame(
                {
                    "Open": [100.0],
                    "High": [101.0],
                    "Low": [99.0],
                    "Close": [100.0],
                    "Adj Close": [100.0],
                    "Volume": [1000],
                },
                index=pd.to_datetime(["2026-01-05"]),
            )

            def fake_fetch(ticker, period, interval):
                return normalize_price_frame(ticker, raw), FetchResult(ticker, "yfinance", "success", "2026-01-05", "2026-01-05", 1)

            with patch("volume_momentum.cli.load_universe_from_jpx", return_value=universe_records), patch(
                "volume_momentum.cli.configure_yfinance_cache"
            ), patch(
                "volume_momentum.cli.fetch_prices_with_yfinance",
                side_effect=fake_fetch,
            ) as fetch_mock, redirect_stdout(StringIO()):
                _run_fetch_data(config, download=True, fetch_market_caps=False, refresh=False, limit=1)
                _run_fetch_data(config, download=True, fetch_market_caps=False, refresh=False, limit=1)

        self.assertEqual(fetch_mock.call_count, 3)


def _replace_config_paths(config, tmpdir):
    from dataclasses import replace

    return replace(
        config,
        data=replace(
            config.data,
            database_path=Path(tmpdir) / "test.sqlite3",
            yfinance_cache_dir=Path(tmpdir) / "yf-cache",
        ),
    )


if __name__ == "__main__":
    unittest.main()
