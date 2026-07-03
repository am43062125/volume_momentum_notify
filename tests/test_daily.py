import unittest

import pandas as pd

from volume_momentum.config import load_config
from volume_momentum.daily import (
    classify_daily_events,
    daily_email_subject,
    render_email_body,
    should_send_email,
    signal_key,
)


class DailyNotificationTests(unittest.TestCase):
    def test_classifies_latest_event_and_monitoring_events(self):
        config = load_config("config.example.json")
        latest_trade_date = "2026-07-10"
        trading_dates = pd.bdate_range("2026-04-01", latest_trade_date)
        price_bars = _price_bars(["AAA.T", "BBB.T", "CCC.T"], trading_dates)
        events = pd.DataFrame(
            [
                _event("AAA.T", latest_trade_date, 100.0),
                _event("BBB.T", "2026-07-03", 90.0),
                _event("CCC.T", "2026-05-22", 80.0),
            ]
        )

        daily_events = classify_daily_events(
            events=events,
            price_bars=price_bars,
            latest_trade_date=latest_trade_date,
            config=config,
            signal_key_value=signal_key(config),
            has_history=False,
            notified_keys=set(),
            names={"AAA.T": "AAA", "BBB.T": "BBB", "CCC.T": "CCC"},
        )

        self.assertEqual(
            list(daily_events["notification_group"]),
            ["new", "monitoring_early", "monitoring_late"],
        )
        self.assertEqual(list(daily_events["elapsed_trading_days"]), [0, 5, 35])

    def test_does_not_treat_notified_latest_event_as_new(self):
        config = load_config("config.example.json")
        latest_trade_date = "2026-07-10"
        trading_dates = pd.bdate_range("2026-07-01", latest_trade_date)
        price_bars = _price_bars(["AAA.T"], trading_dates)
        events = pd.DataFrame([_event("AAA.T", latest_trade_date, 100.0)])

        daily_events = classify_daily_events(
            events=events,
            price_bars=price_bars,
            latest_trade_date=latest_trade_date,
            config=config,
            signal_key_value=signal_key(config),
            has_history=True,
            notified_keys={("AAA.T", latest_trade_date)},
        )

        self.assertTrue(daily_events.empty)

    def test_email_body_uses_block_format(self):
        config = load_config("config.example.json")
        latest_trade_date = "2026-07-10"
        trading_dates = pd.bdate_range("2026-07-01", latest_trade_date)
        daily_events = classify_daily_events(
            events=pd.DataFrame([_event("AAA.T", latest_trade_date, 100.0)]),
            price_bars=_price_bars(["AAA.T"], trading_dates),
            latest_trade_date=latest_trade_date,
            config=config,
            signal_key_value=signal_key(config),
            has_history=False,
            notified_keys=set(),
            names={"AAA.T": "AAA"},
        )

        body = render_email_body(daily_events, latest_trade_date, universe_count=1)
        subject = daily_email_subject(latest_trade_date, daily_events)

        self.assertIn("■ 本日新規ヒット", body)
        self.assertIn("1. AAA.T AAA", body)
        self.assertNotIn("| 銘柄 |", body)
        self.assertEqual(subject, "[売買代金モメンタム] 2026-07-10 新規1件 / 監視中0件")
        self.assertTrue(should_send_email(config, daily_events))


def _event(ticker: str, event_date: str, adj_close: float) -> dict[str, object]:
    return {
        "ticker": ticker,
        "event_date": event_date,
        "adj_close": adj_close,
        "trading_value": 1_000_000_000.0,
        "short_trading_value_avg": 2_000_000_000.0,
        "long_trading_value_avg": 1_000_000_000.0,
        "inflow_ratio": 2.0,
        "moving_average": 75.0,
    }


def _price_bars(tickers: list[str], dates: pd.DatetimeIndex) -> pd.DataFrame:
    rows = []
    for ticker in tickers:
        for index, date in enumerate(dates):
            rows.append(
                {
                    "ticker": ticker,
                    "trade_date": date.date().isoformat(),
                    "open": 100.0 + index,
                    "high": 101.0 + index,
                    "low": 99.0 + index,
                    "close": 100.0 + index,
                    "adj_close": 100.0 + index,
                    "volume": 1000,
                    "trading_value": (100.0 + index) * 1000,
                    "source": "test",
                }
            )
    return pd.DataFrame(rows)


if __name__ == "__main__":
    unittest.main()
