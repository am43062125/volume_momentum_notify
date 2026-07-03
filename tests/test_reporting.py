import tempfile
import unittest
from pathlib import Path

import pandas as pd

from volume_momentum.config import load_config
from volume_momentum.reporting import render_markdown_report, write_reports


class ReportingTests(unittest.TestCase):
    def test_render_markdown_report_is_japanese(self):
        config = load_config("config.example.json")
        events = pd.DataFrame(
            [
                {
                    "ticker": "AAA.T",
                    "event_date": "2026-01-05",
                    "adj_close": 100.0,
                    "inflow_ratio": 1.8,
                    "long_trading_value_avg": 3_000_000_000.0,
                }
            ]
        )
        evaluations = pd.DataFrame(
            [
                {
                    "ticker": "AAA.T",
                    "event_date": "2026-01-05",
                    "horizon_days": 21,
                    "target_date": "2026-02-03",
                    "return": 0.12,
                    "mfe": 0.2,
                    "mae": -0.03,
                    "max_drawdown": -0.05,
                    "complete": True,
                }
            ]
        )
        summary = pd.DataFrame(
            [
                {
                    "horizon_days": 21,
                    "sample_count": 1,
                    "average_return": 0.12,
                    "median_return": 0.12,
                    "max_return": 0.12,
                    "min_return": 0.12,
                    "std_return": 0.0,
                    "win_rate": 1.0,
                    "hit_rate_10pct": 1.0,
                    "hit_rate_20pct": 0.0,
                    "hit_rate_30pct": 0.0,
                    "hit_rate_50pct": 0.0,
                    "hit_rate_100pct": 0.0,
                    "average_mfe": 0.2,
                    "average_mae": -0.03,
                    "max_drawdown": -0.05,
                    "average_drawdown": -0.02,
                    "average_topix_return": 0.04,
                    "average_topix_excess_return": 0.08,
                }
            ]
        )

        markdown = render_markdown_report(
            config,
            events,
            evaluations,
            summary,
            universe_count=1,
            price_row_count=100,
            analysis_start_date="2021-07-02",
            analysis_end_date="2026-07-02",
        )

        self.assertIn("# 売買代金モメンタム検証レポート", markdown)
        self.assertIn("| 分析開始日 | 2021-07-02 |", markdown)
        self.assertIn("| 分析終了日 | 2026-07-02 |", markdown)
        self.assertIn("| 終値上昇条件 | 有効 |", markdown)
        self.assertIn("| 過去終値比較 | 5営業日前 |", markdown)
        self.assertIn("## 評価サマリー", markdown)
        self.assertIn("現在時点の情報を使った近似的なユニバースフィルタ", markdown)
        self.assertIn("サバイバーシップバイアス", markdown)
        self.assertIn("| 評価期間 |", markdown)
        self.assertIn("最大リターン", markdown)
        self.assertIn("+100%以上", markdown)
        self.assertIn("平均DD", markdown)
        self.assertIn("topix超過平均", markdown)

    def test_write_reports_creates_csv_and_markdown(self):
        config = load_config("config.example.json")
        events = pd.DataFrame([{"ticker": "AAA.T", "event_date": "2026-01-05"}])
        evaluations = pd.DataFrame([{"ticker": "AAA.T", "event_date": "2026-01-05", "horizon_days": 21, "return": 0.1}])
        summary = pd.DataFrame([{"horizon_days": 21, "sample_count": 1, "average_return": 0.1}])

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = write_reports(
                config,
                events,
                evaluations,
                summary,
                universe_count=1,
                price_row_count=100,
                analysis_start_date="2021-07-02",
                analysis_end_date="2026-07-02",
                output_dir=tmpdir,
            )

            self.assertTrue(paths.events_csv.exists())
            self.assertTrue(paths.evaluations_csv.exists())
            self.assertTrue(paths.summary_csv.exists())
            self.assertTrue(paths.markdown.exists())
            self.assertIn("売買代金モメンタム検証レポート", paths.markdown.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
