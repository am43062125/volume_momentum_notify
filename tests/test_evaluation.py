import unittest

import pandas as pd

from volume_momentum.evaluation import evaluate_events, summarize_evaluations


def _bars(ticker: str, prices: list[float]) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-01", periods=len(prices))
    return pd.DataFrame(
        {
            "ticker": ticker,
            "trade_date": [date.date().isoformat() for date in dates],
            "adj_close": prices,
        }
    )


class EvaluationTests(unittest.TestCase):
    def test_evaluates_event_returns_and_risk_metrics(self):
        events = pd.DataFrame([{"ticker": "AAA.T", "event_date": "2026-01-01"}])
        prices = _bars("AAA.T", [100, 110, 105, 120, 90, 130])
        benchmarks = _bars("^N225", [1000, 1010, 1020, 1030, 1040, 1050])

        result = evaluate_events(
            events,
            prices,
            benchmarks,
            horizons=[3],
            benchmark_tickers={"nikkei225": "^N225"},
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result.loc[0, "target_date"], "2026-01-06")
        self.assertAlmostEqual(result.loc[0, "return"], 0.20)
        self.assertAlmostEqual(result.loc[0, "mfe"], 0.20)
        self.assertAlmostEqual(result.loc[0, "mae"], 0.0)
        self.assertAlmostEqual(result.loc[0, "max_drawdown"], -5 / 110)
        self.assertAlmostEqual(result.loc[0, "nikkei225_return"], 0.03)
        self.assertAlmostEqual(result.loc[0, "nikkei225_excess_return"], 0.17)
        self.assertTrue(result.loc[0, "complete"])

    def test_marks_incomplete_when_horizon_exceeds_available_prices(self):
        events = pd.DataFrame([{"ticker": "AAA.T", "event_date": "2026-01-01"}])
        prices = _bars("AAA.T", [100, 110])

        result = evaluate_events(events, prices, pd.DataFrame(), horizons=[3], benchmark_tickers={})

        self.assertEqual(len(result), 1)
        self.assertFalse(result.loc[0, "complete"])
        self.assertIsNone(result.loc[0, "target_date"])
        self.assertIsNone(result.loc[0, "return"])

    def test_incomplete_stock_return_does_not_break_benchmark_excess_return(self):
        events = pd.DataFrame([{"ticker": "AAA.T", "event_date": "2026-01-01"}])
        prices = _bars("AAA.T", [100, 110])
        benchmarks = _bars("^N225", [1000, 1010, 1020, 1030, 1040])

        result = evaluate_events(
            events,
            prices,
            benchmarks,
            horizons=[3],
            benchmark_tickers={"nikkei225": "^N225"},
        )

        self.assertEqual(len(result), 1)
        self.assertFalse(result.loc[0, "complete"])
        self.assertIsNone(result.loc[0, "return"])
        self.assertAlmostEqual(result.loc[0, "nikkei225_return"], 0.03)
        self.assertIsNone(result.loc[0, "nikkei225_excess_return"])

    def test_skips_event_when_event_price_is_zero(self):
        events = pd.DataFrame([{"ticker": "AAA.T", "event_date": "2026-01-01"}])
        prices = _bars("AAA.T", [0, 110, 120])

        result = evaluate_events(events, prices, pd.DataFrame(), horizons=[1], benchmark_tickers={})

        self.assertTrue(result.empty)

    def test_summarizes_evaluations_by_horizon(self):
        evaluations = pd.DataFrame(
            [
                {"horizon_days": 3, "return": 0.10, "mfe": 0.15, "mae": -0.02, "max_drawdown": -0.03, "average_drawdown": -0.01},
                {"horizon_days": 3, "return": -0.05, "mfe": 0.02, "mae": -0.08, "max_drawdown": -0.10, "average_drawdown": -0.04},
                {"horizon_days": 5, "return": 0.20, "mfe": 0.25, "mae": 0.0, "max_drawdown": 0.0, "average_drawdown": 0.0},
            ]
        )

        summary = summarize_evaluations(evaluations)

        first = summary[summary["horizon_days"] == 3].iloc[0]
        self.assertEqual(first["sample_count"], 2)
        self.assertAlmostEqual(first["average_return"], 0.025)
        self.assertAlmostEqual(first["win_rate"], 0.5)
        self.assertAlmostEqual(first["hit_rate_10pct"], 0.5)
        self.assertAlmostEqual(first["average_mfe"], 0.085)
        self.assertAlmostEqual(first["max_drawdown"], -0.10)


if __name__ == "__main__":
    unittest.main()
