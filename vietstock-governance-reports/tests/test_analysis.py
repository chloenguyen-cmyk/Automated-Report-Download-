import unittest

import pandas as pd

from govreports.analysis import count_consecutive_pairs, select_eligible, summarize_firms
from govreports.downloader import LOG_FIELDS


def _latest(rows):
    df = pd.DataFrame(rows, columns=LOG_FIELDS).fillna("")
    df["year"] = df["year"].astype(int)
    return df


class AnalysisTests(unittest.TestCase):
    def test_consecutive_pairs(self):
        self.assertEqual(count_consecutive_pairs([2020, 2021, 2022]), 2)
        self.assertEqual(count_consecutive_pairs([2020, 2022, 2024]), 0)
        self.assertEqual(count_consecutive_pairs([2020, 2021, 2021, 2023, 2024]), 2)  # duplicates ignored
        self.assertEqual(count_consecutive_pairs([]), 0)

    def test_summary_and_eligibility(self):
        def row(t, y, s):
            return {"ticker": t, "industry": "I", "year": y, "status": s}

        rows = (
            [row("AAA", y, "downloaded") for y in range(2020, 2026)]                   # 6 yrs, 5 pairs
            + [row("BBB", y, "downloaded" if y in (2020, 2022, 2024) else "not_found")  # gaps, 0 pairs
               for y in range(2020, 2026)]
            + [row("CCC", y, "skipped_existing" if y >= 2022 else "error")              # 4 yrs, 3 pairs
               for y in range(2020, 2026)]
            + [row("DDD", y, "not_found") for y in range(2020, 2026)]                   # nothing at all
        )
        summary = summarize_firms(_latest(rows)).set_index("ticker")

        self.assertEqual(summary.loc["AAA", "consecutive_pairs"], 5)
        self.assertEqual(summary.loc["BBB", "years_available"], 3)
        self.assertEqual(summary.loc["BBB", "consecutive_pairs"], 0)
        self.assertEqual(summary.loc["CCC", "available_years"], "2022, 2023, 2024, 2025")
        self.assertEqual(summary.loc["DDD", "years_available"], 0)

        eligible = select_eligible(summary.reset_index(), min_consecutive_pairs=3)
        self.assertEqual(eligible["ticker"].tolist(), ["AAA", "CCC"])


if __name__ == "__main__":
    unittest.main()
