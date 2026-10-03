"""Load and validate the list of firms to download (.csv, .xlsx or Markdown table)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

EXCHANGES = ("HOSE", "HNX", "UPCOM")

# Tickers end up in URLs and file paths, so only accept safe characters.
_TICKER_RE = re.compile(r"^[A-Z0-9]{2,12}$")
_MD_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$")


@dataclass(frozen=True)
class Firm:
    ticker: str
    exchange: str = ""
    industry: str = ""


def _read_markdown_table(path: Path) -> pd.DataFrame:
    """Parse `| Exchange | Industry | Ticker |` rows out of a Markdown file."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _MD_ROW.match(line.strip())
        if not match:
            continue
        exchange, industry, ticker = (g.strip() for g in match.groups())
        if exchange.upper() in EXCHANGES:  # skips header and |---| separator rows
            rows.append({"exchange": exchange, "industry": industry, "ticker": ticker})
    return pd.DataFrame(rows, columns=["exchange", "industry", "ticker"])


def load_firms(path: str | Path) -> list[Firm]:
    """Return de-duplicated firms. Only a `Ticker` column is required.

    `Exchange` is the preferred exchange to try first (all three are tried anyway).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Ticker file not found: {path}. Put your .csv/.xlsx in the input/ folder "
            "(default input/tickers.csv) or pass --tickers <file>."
        )
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        df = pd.read_excel(path)
    elif suffix == ".csv":
        df = pd.read_csv(path)
    elif suffix == ".md":
        df = _read_markdown_table(path)
    else:
        raise ValueError(f"Unsupported ticker file '{path.name}': use .csv, .xlsx or .md")

    df.columns = [str(c).strip().lower() for c in df.columns]
    if "ticker" not in df.columns:
        raise ValueError(f"'{path.name}' needs a 'Ticker' column (found: {list(df.columns)})")
    for col in ("exchange", "industry"):
        if col not in df.columns:
            df[col] = ""

    df = df.dropna(subset=["ticker"]).copy()
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["exchange"] = df["exchange"].fillna("").astype(str).str.strip().str.upper()
    df["industry"] = df["industry"].fillna("").astype(str).str.strip()

    bad = ~df["ticker"].str.match(_TICKER_RE)
    if bad.any():
        log.warning("Skipping %d invalid ticker(s): %s", int(bad.sum()), df.loc[bad, "ticker"].tolist()[:10])
        df = df[~bad]

    df = df.drop_duplicates(subset="ticker").reset_index(drop=True)
    return [Firm(r.ticker, r.exchange, r.industry) for r in df.itertuples(index=False)]
