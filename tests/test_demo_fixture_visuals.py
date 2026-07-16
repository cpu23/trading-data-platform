import re
import unittest
from pathlib import Path


class DemoFixtureVisualTests(unittest.TestCase):
    def test_comparison_series_have_enough_points_to_draw_lines(self):
        sql = (Path(__file__).resolve().parents[1] / "db" / "demo" / "900_demo_seed.sql").read_text()
        for series_id in ("T10Y2Y", "VIXCLS", "DTWEXBGS", "BAMLH0A0HYM2", "DGS10", "T5YIE"):
            observations = re.findall(rf"\('{series_id}',\s*NOW\(\)\s*-\s*INTERVAL", sql)
            self.assertGreaterEqual(len(observations), 3, series_id)


if __name__ == "__main__":
    unittest.main()
