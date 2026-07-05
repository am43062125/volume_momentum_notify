import unittest
import tempfile

import pandas as pd

from volume_momentum.config import load_config
from volume_momentum.trade_backtest import (
    TradeBacktestInputs,
    evaluate_trade_strategies,
    summarize_trades,
    write_trade_backtest_reports,
)


class TradeBacktestTests(unittest.TestCase):
    def test_evaluates_a_b_c_strategies(self):
        config = load_config("config.example.json").trade_backtest
        price_bars = _bars(
            "AAA.T",
            close_prices=[
                100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
                110, 111, 112, 113, 114, 115, 116, 117, 118, 119,
                120, 121, 122, 123, 124, 125, 126, 127, 128, 129,
                130, 131, 132, 133, 134, 135, 136, 137, 138, 139,
                140, 141, 142, 143, 144,
            ],
        )
        benchmark_bars = _bars("1306.T", [100] * len(price_bars))
        events = pd.DataFrame([{"ticker": "AAA.T", "event_date": "2026-01-29"}])

        trades = evaluate_trade_strategies(
            TradeBacktestInputs(
                events=events,
                price_bars=price_bars,
                benchmark_bars=benchmark_bars,
                benchmark_ticker="1306.T",
                config=config,
            )
        )

        self.assertEqual(set(trades["strategy"]), {"A", "B", "C"})
        self.assertTrue((trades["entry_date"] > trades["event_date"]).all())
        self.assertTrue((trades["return"] > 0).all())

    def test_risk_strategy_uses_stop_before_take_profit_on_same_day(self):
        config = load_config("config.example.json").trade_backtest
        price_bars = _bars("AAA.T", [100 + index for index in range(30)])
        event_date = str(price_bars.loc[20, "trade_date"])
        entry_position = 21
        price_bars.loc[entry_position, "open"] = 120.0
        price_bars.loc[entry_position, "high"] = 125.0
        price_bars.loc[entry_position, "low"] = 115.0
        events = pd.DataFrame([{"ticker": "AAA.T", "event_date": event_date}])

        trades = evaluate_trade_strategies(
            TradeBacktestInputs(
                events=events,
                price_bars=price_bars,
                benchmark_bars=_bars("1306.T", [100] * len(price_bars)),
                benchmark_ticker="1306.T",
                config=config,
            )
        )
        b_trade = trades[trades["strategy"] == "B"].iloc[0]

        self.assertEqual(b_trade["exit_reason"], "stop_loss")
        self.assertLess(b_trade["return"], 0)

    def test_strategy_c_filters_weak_relative_return(self):
        config = load_config("config.example.json").trade_backtest
        price_bars = _bars("AAA.T", [100] * 45)
        benchmark_bars = _bars("1306.T", [100 + index for index in range(45)])
        events = pd.DataFrame([{"ticker": "AAA.T", "event_date": str(price_bars.loc[25, "trade_date"])}])

        trades = evaluate_trade_strategies(
            TradeBacktestInputs(
                events=events,
                price_bars=price_bars,
                benchmark_bars=benchmark_bars,
                benchmark_ticker="1306.T",
                config=config,
            )
        )

        self.assertIn("B", set(trades["strategy"]))
        self.assertNotIn("C", set(trades["strategy"]))

    def test_summarizes_trades_by_strategy(self):
        trades = pd.DataFrame(
            [
                {"strategy": "A", "return": 0.10, "exit_reason": "time_exit", "holding_days": 21},
                {"strategy": "A", "return": -0.05, "exit_reason": "time_exit", "holding_days": 21},
                {"strategy": "B", "return": 0.20, "exit_reason": "take_profit", "holding_days": 3},
            ]
        )

        summary = summarize_trades(trades)
        strategy_a = summary[summary["strategy"] == "A"].iloc[0]
        strategy_b = summary[summary["strategy"] == "B"].iloc[0]

        self.assertEqual(strategy_a["trade_count"], 2)
        self.assertAlmostEqual(strategy_a["win_rate"], 0.5)
        self.assertAlmostEqual(strategy_a["average_return"], 0.025)
        self.assertAlmostEqual(strategy_b["take_profit_rate"], 1.0)

    def test_summary_drawdown_uses_exit_date_order(self):
        trades = pd.DataFrame(
            [
                {"strategy": "A", "ticker": "B.T", "entry_date": "2026-01-03", "exit_date": "2026-01-04", "return": 0.50, "exit_reason": "time_exit", "holding_days": 1},
                {"strategy": "A", "ticker": "A.T", "entry_date": "2026-01-01", "exit_date": "2026-01-02", "return": -0.20, "exit_reason": "time_exit", "holding_days": 1},
            ]
        )

        summary = summarize_trades(trades)

        self.assertAlmostEqual(summary.loc[0, "max_drawdown"], 0.0)

    def test_writes_trade_backtest_reports(self):
        trades = pd.DataFrame(
            [
                {
                    "strategy": "A",
                    "ticker": "AAA.T",
                    "event_date": "2026-01-05",
                    "entry_date": "2026-01-06",
                    "entry_price": 100.0,
                    "exit_date": "2026-02-03",
                    "exit_price": 110.0,
                    "exit_reason": "time_exit",
                    "holding_days": 21,
                    "return": 0.10,
                }
            ]
        )
        summary = summarize_trades(trades)

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = write_trade_backtest_reports(
                trades=trades,
                summary=summary,
                output_dir=tmpdir,
                analysis_start_date="2021-07-02",
                analysis_end_date="2026-07-02",
                event_count=1,
                universe_count=1,
            )

            self.assertTrue(paths.trades_csv.exists())
            self.assertTrue(paths.summary_csv.exists())
            self.assertTrue(paths.strategy_comparison_csv.exists())
            self.assertTrue(paths.markdown.exists())
            markdown = paths.markdown.read_text(encoding="utf-8")
            self.assertIn("仮想売買バックテストレポート", markdown)
            self.assertIn("## 戦略比較", markdown)


def _bars(ticker: str, close_prices: list[float]) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-01", periods=len(close_prices))
    rows = []
    for index, close in enumerate(close_prices):
        rows.append(
            {
                "ticker": ticker,
                "trade_date": dates[index].date().isoformat(),
                "open": close,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "adj_close": close,
                "volume": 1000,
                "trading_value": close * 1000,
                "source": "test",
            }
        )
    return pd.DataFrame(rows)


if __name__ == "__main__":
    unittest.main()
