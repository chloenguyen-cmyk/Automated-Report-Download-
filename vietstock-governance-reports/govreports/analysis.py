"""Coverage analysis: which firms have enough years of reports to be usable?"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

from .downloader import LOG_FIELDS, SUCCESS_STATUSES, read_latest

SUMMARY_COLUMNS = [
    "ticker", "industry", "years_available", "total_years",
    "missing_years", "consecutive_pairs", "available_years",
]


def load_latest(log_path: str | Path) -> pd.DataFrame:
    """Latest log row per (ticker, year) as a DataFrame."""
    rows = list(read_latest(Path(log_path)).values())
    df = pd.DataFrame(rows, columns=LOG_FIELDS)
    df["year"] = df["year"].astype(int)
    return df


def count_consecutive_pairs(years: Iterable[int]) -> int:
    """Number of (y, y+1) pairs present, e.g. [2020, 2021, 2022] -> 2."""
    present = set(years)
    return sum(1 for y in present if y + 1 in present)


def summarize_firms(latest: pd.DataFrame) -> pd.DataFrame:
    """One row per firm: years found, years missing, consecutive-year pairs."""
    rows = []
    for ticker, group in latest.groupby("ticker", sort=True):
        found = sorted(set(group.loc[group["status"].isin(SUCCESS_STATUSES), "year"]))
        total = group["year"].nunique()
        rows.append({
            "ticker": ticker,
            "industry": group["industry"].iloc[0],
            "years_available": len(found),
            "total_years": total,
            "missing_years": total - len(found),
            "consecutive_pairs": count_consecutive_pairs(found),
            "available_years": ", ".join(map(str, found)),
        })
    return pd.DataFrame(rows, columns=SUMMARY_COLUMNS)


def select_eligible(summary: pd.DataFrame, min_consecutive_pairs: int = 3) -> pd.DataFrame:
    """Firms with at least `min_consecutive_pairs` pairs of back-to-back years."""
    mask = summary["consecutive_pairs"] >= min_consecutive_pairs
    return summary[mask].reset_index(drop=True)


def text_report(latest: pd.DataFrame, summary: pd.DataFrame, eligible: pd.DataFrame, min_pairs: int) -> str:
    status = latest["status"].value_counts()
    found = int(latest["status"].isin(SUCCESS_STATUSES).sum())
    dist = summary["years_available"].value_counts().sort_index()

    lines = [
        f"Firms: {len(summary)} | report slots: {len(latest)}",
        f"Found: {found} | not found: {int(status.get('not_found', 0))} | "
        f"errors (retry me): {int(status.get('error', 0))}",
        "",
        "Firms by number of years available:",
        *[f"  {int(n)} year(s): {int(c)} firms" for n, c in dist.items()],
        "",
        f"Eligible (>= {min_pairs} consecutive-year pairs): {len(eligible)} of {len(summary)} firms",
    ]
    return "\n".join(lines)
