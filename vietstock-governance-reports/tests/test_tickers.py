import tempfile
import unittest
from pathlib import Path

import pandas as pd

from govreports.tickers import Firm, load_firms


class LoadFirmsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_markdown_table(self):
        md = self.tmp / "t.md"
        md.write_text(
            "| Exchange | Industry | Ticker |\n|---|---|---|\n"
            "| HOSE | Bank | acb |\n| HNX | Energy | PVS |\n| NOPE | X | ZZZ |\n",
            encoding="utf-8",
        )
        self.assertEqual(load_firms(md), [Firm("ACB", "HOSE", "Bank"), Firm("PVS", "HNX", "Energy")])

    def test_csv_case_insensitive_columns_dedupe_and_invalid(self):
        csv = self.tmp / "t.csv"
        pd.DataFrame({
            "TICKER": ["vnm", "VNM", "bad/../x", "HPG"],
            "exchange": ["hose", "HNX", "HOSE", None],
        }).to_csv(csv, index=False)
        firms = load_firms(csv)
        self.assertEqual([f.ticker for f in firms], ["VNM", "HPG"])
        self.assertEqual(firms[0].exchange, "HOSE")  # first occurrence wins
        self.assertEqual(firms[1].exchange, "")      # missing exchange -> try all

    def test_missing_ticker_column(self):
        csv = self.tmp / "t.csv"
        pd.DataFrame({"code": ["A"]}).to_csv(csv, index=False)
        with self.assertRaises(ValueError):
            load_firms(csv)

    def test_unsupported_extension(self):
        txt = self.tmp / "t.txt"
        txt.write_text("VNM")
        with self.assertRaises(ValueError):
            load_firms(txt)

    def test_missing_file_gives_helpful_error(self):
        with self.assertRaisesRegex(FileNotFoundError, "input/"):
            load_firms(self.tmp / "nope.csv")


if __name__ == "__main__":
    unittest.main()
