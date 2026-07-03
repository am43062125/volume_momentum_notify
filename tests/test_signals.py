import unittest

import pandas as pd

from volume_momentum.config import SignalConfig
from volume_momentum.signals import calculate_indicators, detect_events


def _signal_config() -> SignalConfig:
    return SignalConfig(
        short_trading_value_window=2,
        long_trading_value_window=5,
        inflow_ratio_threshold=1.5,
        min_consecutive_days=3,
        min_long_trading_value=100.0,
        moving_average_window=3,
        require_close_above_previous=True,
        close_lookback_days=5,
    )


def _price_frame(ticker: str, trading_values: list[float]) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-01", periods=len(trading_values))
    prices = [100 + i for i in range(len(trading_values))]
    return pd.DataFrame(
        {
            "ticker": ticker,
            "trade_date": [date.date().isoformat() for date in dates],
            "adj_close": prices,
            "trading_value": trading_values,
        }
    )


class SignalTests(unittest.TestCase):
    def test_calculates_inflow_ratio_and_moving_average(self):
        frame = _price_frame("AAA.T", [100, 100, 100, 100, 100, 300, 300])

        indicators = calculate_indicators(frame, _signal_config())

        self.assertIn("short_trading_value_avg", indicators.columns)
        self.assertIn("long_trading_value_avg", indicators.columns)
        self.assertIn("inflow_ratio", indicators.columns)
        self.assertIn("moving_average_rising", indicators.columns)
        self.assertIn("close_above_previous", indicators.columns)
        self.assertIn("close_above_lookback", indicators.columns)
        self.assertAlmostEqual(indicators.loc[6, "short_trading_value_avg"], 300.0)
        self.assertAlmostEqual(indicators.loc[6, "long_trading_value_avg"], 180.0)
        self.assertAlmostEqual(indicators.loc[6, "inflow_ratio"], 1.6666666666666667)

    def test_detects_first_event_after_required_streak(self):
        frame = _price_frame("AAA.T", [100, 100, 100, 100, 100, 1000, 1000, 1000, 1000])

        events = detect_events(frame, _signal_config(), cooldown_days=10)

        self.assertEqual(len(events), 1)
        self.assertEqual(events.loc[0, "ticker"], "AAA.T")
        self.assertEqual(events.loc[0, "event_date"], "2026-01-12")

    def test_does_not_register_while_condition_continues(self):
        frame = _price_frame("AAA.T", [100, 100, 100, 100, 100, 1000, 1000, 1000, 1000, 1000])

        events = detect_events(frame, _signal_config(), cooldown_days=0)

        self.assertEqual(len(events), 1)

    def test_allows_new_event_after_condition_resets_and_cooldown_passes(self):
        frame = _price_frame(
            "AAA.T",
            [
                100,
                100,
                100,
                100,
                100,
                1000,
                1000,
                1000,
                100,
                100,
                100,
                100,
                100,
                1000,
                1000,
                1000,
                1000,
            ],
        )

        events = detect_events(frame, _signal_config(), cooldown_days=3)

        self.assertEqual(len(events), 2)
        self.assertEqual(events.loc[0, "event_date"], "2026-01-12")
        self.assertEqual(events.loc[1, "event_date"], "2026-01-22")

    def test_rejects_event_when_event_close_is_not_above_previous_close(self):
        frame = _price_frame("AAA.T", [100, 100, 100, 100, 100, 1000, 1000, 1000, 1000])
        frame.loc[7, "adj_close"] = frame.loc[6, "adj_close"] - 1
        frame.loc[8, "adj_close"] = frame.loc[7, "adj_close"]

        events = detect_events(frame, _signal_config(), cooldown_days=10)

        self.assertTrue(events.empty)

    def test_rejects_event_when_event_close_is_not_above_lookback_close(self):
        frame = _price_frame("AAA.T", [100, 100, 100, 100, 100, 1000, 1000, 1000, 1000])
        frame.loc[7, "adj_close"] = frame.loc[2, "adj_close"]
        frame.loc[8, "adj_close"] = frame.loc[3, "adj_close"]

        events = detect_events(frame, _signal_config(), cooldown_days=10)

        self.assertTrue(events.empty)

    def test_can_register_later_when_base_streak_continues_and_price_condition_recovers(self):
        frame = _price_frame("AAA.T", [100, 100, 100, 100, 100, 1000, 1000, 1000, 1000, 1000])
        frame.loc[6, "adj_close"] = frame.loc[5, "adj_close"]
        config = SignalConfig(
            short_trading_value_window=2,
            long_trading_value_window=5,
            inflow_ratio_threshold=1.0,
            min_consecutive_days=3,
            min_long_trading_value=100.0,
            moving_average_window=3,
            require_close_above_previous=True,
            close_lookback_days=5,
        )

        events = detect_events(frame, config, cooldown_days=10)

        self.assertEqual(len(events), 1)
        self.assertEqual(events.loc[0, "event_date"], "2026-01-12")


if __name__ == "__main__":
    unittest.main()
