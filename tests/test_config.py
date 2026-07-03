import json
import tempfile
import unittest
from pathlib import Path

from volume_momentum.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def test_loads_example_config(self):
        config = load_config("config.example.json")

        self.assertEqual(config.backtest.period_years, 5)
        self.assertEqual(config.backtest.evaluation_horizons, (21, 63, 126, 252))
        self.assertEqual(config.data.yfinance_cache_dir, Path("data") / "yfinance_cache")
        self.assertIn("jpx.co.jp", config.data.jpx_listed_issues_url)
        self.assertTrue(config.universe.enable_current_market_cap_filter)
        self.assertEqual(config.universe.min_current_market_cap, 30_000_000_000)
        self.assertTrue(config.signal.require_close_above_previous)
        self.assertEqual(config.signal.close_lookback_days, 5)
        self.assertEqual(config.report.language, "ja")
        self.assertEqual(config.daily.monitoring_lookback_days, 63)
        self.assertEqual(config.daily.early_monitoring_days, 21)
        self.assertEqual(config.daily.price_refresh_lookback_days, 10)
        self.assertEqual(config.email.smtp_host, "smtp.gmail.com")
        self.assertEqual(config.email.username_env, "GMAIL_SMTP_USER")

    def test_daily_and_email_config_have_defaults_for_existing_configs(self):
        raw = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        raw.pop("daily")
        raw.pop("email")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")

            config = load_config(path)

        self.assertTrue(config.daily.enabled)
        self.assertEqual(config.daily.output_dir, Path("reports/daily"))
        self.assertEqual(config.email.smtp_port, 587)

    def test_rejects_non_japanese_markdown_report_language(self):
        raw = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        raw["report"]["language"] = "en"

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(path)

    def test_market_cap_can_be_disabled_without_threshold(self):
        raw = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        raw["universe"]["enable_current_market_cap_filter"] = False
        raw["universe"]["min_current_market_cap"] = None

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")

            config = load_config(path)

        self.assertFalse(config.universe.enable_current_market_cap_filter)
        self.assertIsNone(config.universe.min_current_market_cap)

    def test_short_window_must_be_less_than_long_window(self):
        raw = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        raw["signal"]["short_trading_value_window"] = 20
        raw["signal"]["long_trading_value_window"] = 5

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(path)

    def test_close_lookback_days_must_be_positive(self):
        raw = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        raw["signal"]["close_lookback_days"] = 0

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(path)

    def test_daily_monitoring_days_must_be_ordered(self):
        raw = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        raw["daily"]["monitoring_lookback_days"] = 21
        raw["daily"]["early_monitoring_days"] = 21

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
