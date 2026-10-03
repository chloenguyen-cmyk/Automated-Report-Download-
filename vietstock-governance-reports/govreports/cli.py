"""Command-line interface: ``govreports download | analyze | zip``."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .analysis import load_latest, select_eligible, summarize_firms, text_report
from .archive import make_zip_batches
from .downloader import (
    DEFAULT_BASE_URL, DEFAULT_USER_AGENT, DownloadConfig, DownloadLog, Downloader,
)
from .tickers import load_firms

try:  # tqdm is optional; fall back to periodic log lines
    from tqdm.auto import tqdm
except ImportError:  # pragma: no cover
    tqdm = None

log = logging.getLogger("govreports")


def parse_years(spec: str) -> tuple[int, ...]:
    """'2020-2025' -> 2020..2025, '2021,2023' -> (2021, 2023), '2019,2021-2023' also works."""
    years: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            start, end = (int(x) for x in part.split("-", 1))
            if start > end:
                raise argparse.ArgumentTypeError(f"bad year range: {part}")
            years.update(range(start, end + 1))
        elif part:
            years.add(int(part))
    if not years:
        raise argparse.ArgumentTypeError("no years given")
    return tuple(sorted(years))


class _Progress:
    """Thread-safe progress: tqdm bar if installed, else a log line every 5%."""

    def __init__(self, total: int):
        self.total, self.done, self._lock = total, 0, threading.Lock()
        self._bar = tqdm(total=total, desc="Downloading", unit="report") if tqdm else None
        self._step = max(1, total // 20)

    def update(self, n: int = 1) -> None:
        if self._bar:
            self._bar.update(n)
            return
        with self._lock:
            self.done += n
            if self.done % self._step == 0 or self.done == self.total:
                log.info("Progress: %d/%d", self.done, self.total)

    def close(self) -> None:
        if self._bar:
            self._bar.close()


# --------------------------------------------------------------------------- commands

def cmd_download(args: argparse.Namespace) -> int:
    firms = load_firms(args.tickers)
    cfg = DownloadConfig(
        years=args.years, out_dir=args.out, base_url=args.base_url, workers=args.workers,
        rate=args.rate, timeout=(10.0, args.timeout), retries=args.retries,
        retry_missing=args.retry_missing, user_agent=args.user_agent,
    )
    log.info("%d firms x %d years -> %s", len(firms), len(cfg.years), cfg.out_dir)

    progress = _Progress(len(firms) * len(cfg.years))
    with DownloadLog(cfg.log_path) as download_log:
        totals = Downloader(cfg, download_log).run(firms, on_progress=progress.update)
    progress.close()

    log.info("Done: %s", ", ".join(f"{k}={v}" for k, v in sorted(totals.items())))
    if totals.get("error"):
        log.warning("%d report(s) hit transient errors; re-run the same command to retry them.", totals["error"])
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    out = Path(args.out)
    latest = load_latest(out / "download_log.csv")
    summary = summarize_firms(latest)
    eligible = select_eligible(summary, args.min_pairs)

    summary.to_csv(out / "firm_coverage.csv", index=False, encoding="utf-8-sig")
    if args.format == "xlsx":
        eligible_path = out / "eligible_firms.xlsx"
        eligible.to_excel(eligible_path, index=False)
    else:
        eligible_path = out / "eligible_firms.csv"
        eligible.to_csv(eligible_path, index=False, encoding="utf-8-sig")

    print(text_report(latest, summary, eligible, args.min_pairs))
    log.info("Wrote %s and %s", out / "firm_coverage.csv", eligible_path)
    return 0


def cmd_zip(args: argparse.Namespace) -> int:
    out = Path(args.out)
    tickers: Optional[list[str]] = None
    if args.eligible_only:
        summary = summarize_firms(load_latest(out / "download_log.csv"))
        tickers = select_eligible(summary, args.min_pairs)["ticker"].tolist()
        log.info("Restricting to %d eligible firms", len(tickers))

    batches = make_zip_batches(
        out / "reports", args.zip_dir or out / "zip_batches",
        tickers_per_batch=args.tickers_per_batch, tickers=tickers, compress=args.compress,
    )
    for b in batches:
        print(f"Batch {b['batch']:02d}: {len(b['tickers'])} tickers | {b['files']} files | "
              f"{b['size_mb']:.1f} MB | {b['tickers'][0]} -> {b['tickers'][-1]}")
    if not batches:
        print("Nothing to zip.")
    return 0


# --------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="govreports", description=__doc__)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("download", help="download reports for a list of tickers (resumable)")
    d.add_argument("--tickers", type=Path, default=Path("input/tickers.csv"),
                   help=".csv/.xlsx/.md with Ticker [, Exchange, Industry] (default: input/tickers.csv)")
    d.add_argument("--out", type=Path, default=Path("output"), help="output directory (default: output)")
    d.add_argument("--years", type=parse_years, default=parse_years("2020-2025"), help="e.g. 2020-2025 or 2021,2023")
    d.add_argument("--workers", type=int, default=8, help="parallel workers (default 8)")
    d.add_argument("--rate", type=float, default=5.0, help="max requests/second overall (default 5)")
    d.add_argument("--timeout", type=float, default=25.0, help="read timeout in seconds (default 25)")
    d.add_argument("--retries", type=int, default=3, help="retries for 429/5xx/network errors (default 3)")
    d.add_argument("--retry-missing", action="store_true", help="re-check reports previously recorded as not_found")
    d.add_argument("--base-url", default=DEFAULT_BASE_URL, help=argparse.SUPPRESS)
    d.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="HTTP User-Agent header")
    d.set_defaults(func=cmd_download)

    a = sub.add_parser("analyze", help="coverage per firm + eligibility list")
    a.add_argument("--out", type=Path, default=Path("output"), help="directory used for `download` (default: output)")
    a.add_argument("--min-pairs", type=int, default=3, help="min consecutive-year pairs to be eligible (default 3)")
    a.add_argument("--format", choices=["xlsx", "csv"], default="xlsx")
    a.set_defaults(func=cmd_analyze)

    z = sub.add_parser("zip", help="pack reports into ZIP batches")
    z.add_argument("--out", type=Path, default=Path("output"), help="directory used for `download` (default: output)")
    z.add_argument("--zip-dir", type=Path, help="where to write ZIPs (default: <out>/zip_batches)")
    z.add_argument("--tickers-per-batch", type=int, default=25)
    z.add_argument("--eligible-only", action="store_true", help="only include eligible firms")
    z.add_argument("--min-pairs", type=int, default=3, help="used with --eligible-only (default 3)")
    z.add_argument("--compress", action="store_true", help="deflate entries (slower, PDFs rarely shrink)")
    z.set_defaults(func=cmd_zip)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
    )
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
