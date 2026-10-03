"""Concurrent, resumable PDF downloader.

Design notes
------------
* One worker handles one ticker at a time (all years), so the exchange that worked for
  one year is tried first for the next -- this removes most wrong-exchange probes.
* A shared rate limiter caps requests/second no matter how many workers run.
* PDFs are streamed to a ``.part`` file and renamed only after validation, so an
  interrupted run never leaves a corrupt report behind.
* Every outcome is appended to a CSV log (O(1) per write). On restart the log is the
  source of truth: finished work is skipped, transient errors are retried.
* "not_found" (the report really is not there) is kept separate from "error"
  (timeouts, 429, 5xx), so a flaky network never gets mistaken for a missing report.
"""

from __future__ import annotations

import csv
import logging
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .tickers import EXCHANGES, Firm

log = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF"
CHUNK_SIZE = 64 * 1024
SUCCESS_STATUSES = frozenset({"downloaded", "skipped_existing"})
DEFAULT_BASE_URL = "https://static2.vietstock.vn/data"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
LOG_FIELDS = [
    "ticker", "industry", "year", "preferred_exchange", "found_exchange",
    "status", "http_status", "url", "file_path", "tested_urls", "timestamp",
]


@dataclass(frozen=True)
class DownloadConfig:
    years: tuple[int, ...]
    out_dir: Path
    base_url: str = DEFAULT_BASE_URL
    workers: int = 8
    rate: float = 5.0                            # max requests/second, all workers combined
    timeout: tuple[float, float] = (10.0, 25.0)  # (connect, read) seconds
    retries: int = 3                             # automatic retries for 429 / 5xx / connection errors
    backoff: float = 1.0
    retry_missing: bool = False                  # re-probe reports previously recorded as not_found
    user_agent: str = DEFAULT_USER_AGENT

    @property
    def reports_dir(self) -> Path:
        return Path(self.out_dir) / "reports"

    @property
    def log_path(self) -> Path:
        return Path(self.out_dir) / "download_log.csv"


# --------------------------------------------------------------------------- helpers

def build_url(base_url: str, ticker: str, year: int, exchange: str) -> str:
    return (
        f"{base_url.rstrip('/')}/{exchange}/{year}/BCTHQT/VN/NAM/"
        f"{ticker}_Baocaoquantri_{year}.pdf"
    )


def exchange_order(preferred: str) -> list[str]:
    """Preferred exchange first, then the rest (firms can switch exchanges over time)."""
    preferred = (preferred or "").strip().upper()
    if preferred in EXCHANGES:
        return [preferred] + [e for e in EXCHANGES if e != preferred]
    return list(EXCHANGES)


def is_valid_pdf(path: Path) -> bool:
    try:
        if path.stat().st_size < 100:
            return False
        with open(path, "rb") as fh:
            return fh.read(4) == PDF_MAGIC
    except OSError:
        return False


class RateLimiter:
    """Thread-safe limiter that spaces request start times at least 1/rate seconds apart."""

    def __init__(self, rate: float):
        self._interval = 1.0 / rate if rate and rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if not self._interval:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self._interval
        delay = slot - now
        if delay > 0:
            time.sleep(delay)


def build_session(cfg: DownloadConfig) -> requests.Session:
    retry = Retry(
        total=cfg.retries,
        backoff_factor=cfg.backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        respect_retry_after_header=True,
        raise_on_status=False,  # hand the final response back instead of raising
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=cfg.workers, pool_maxsize=cfg.workers)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers["User-Agent"] = cfg.user_agent
    return session


@dataclass(frozen=True)
class FetchResult:
    outcome: str                 # "ok" | "not_found" | "error"
    http_status: Optional[int]
    size: int = 0
    reason: str = ""


def fetch_pdf(
    session: requests.Session,
    url: str,
    dest: Path,
    timeout: tuple[float, float],
    limiter: Optional[RateLimiter] = None,
) -> FetchResult:
    """Download one URL to ``dest`` if (and only if) it is a real PDF."""
    if limiter:
        limiter.wait()
    tmp = dest.with_name(dest.name + ".part")
    try:
        with session.get(url, stream=True, timeout=timeout, allow_redirects=True) as resp:
            status = resp.status_code
            if status == 429 or status >= 500:
                return FetchResult("error", status, reason="server_busy")
            if status != 200:
                return FetchResult("not_found", status)

            chunks = resp.iter_content(CHUNK_SIZE)
            first = next(chunks, b"")
            if not first.startswith(PDF_MAGIC):  # some sites answer 200 with an HTML 'not found' page
                return FetchResult("not_found", status, reason="not_pdf")

            dest.parent.mkdir(parents=True, exist_ok=True)
            size = len(first)
            with open(tmp, "wb") as fh:
                fh.write(first)
                for chunk in chunks:
                    fh.write(chunk)
                    size += len(chunk)

            expected = resp.headers.get("Content-Length")
            if expected and expected.isdigit() and not resp.headers.get("Content-Encoding"):
                if int(expected) != size:
                    return FetchResult("error", status, size, reason="truncated")
            tmp.replace(dest)
            return FetchResult("ok", status, size)
    except requests.RequestException as exc:
        return FetchResult("error", None, reason=type(exc).__name__)
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- log

class DownloadLog:
    """Append-only CSV log. Latest row per (ticker, year) wins."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        is_new = not self.path.exists() or self.path.stat().st_size == 0
        if not is_new:
            self._check_header()
        # utf-8-sig so Vietnamese industry names open correctly in Excel
        self._fh = open(self.path, "a", newline="", encoding="utf-8-sig")
        self._writer = csv.DictWriter(self._fh, fieldnames=LOG_FIELDS)
        if is_new:
            self._writer.writeheader()
            self._fh.flush()

    def _check_header(self) -> None:
        with open(self.path, newline="", encoding="utf-8-sig") as fh:
            header = next(csv.reader(fh), [])
        if header != LOG_FIELDS:
            raise ValueError(
                f"{self.path} has an unexpected header (older/other log format?). "
                "Move it aside and re-run; existing PDFs will be picked up automatically."
            )

    def append(self, row: dict) -> None:
        with self._lock:
            self._writer.writerow(row)
            self._fh.flush()

    def latest(self) -> dict[tuple[str, int], dict]:
        return read_latest(self.path)

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "DownloadLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read_latest(path: Path) -> dict[tuple[str, int], dict]:
    state: dict[tuple[str, int], dict] = {}
    if not Path(path).exists():
        return state
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            state[(row["ticker"], int(row["year"]))] = row
    return state


# --------------------------------------------------------------------------- downloader

class Downloader:
    def __init__(self, cfg: DownloadConfig, download_log: DownloadLog):
        self.cfg = cfg
        self.log = download_log
        self.limiter = RateLimiter(cfg.rate)
        self.state = download_log.latest()  # read-only during a run
        self._local = threading.local()
        self._on_progress: Callable[[int], None] = lambda n: None

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            self._local.session = build_session(self.cfg)
        return self._local.session

    def run(self, firms: list[Firm], on_progress: Optional[Callable[[int], None]] = None) -> Counter:
        """Process all firms; returns a Counter of per-report outcomes."""
        if on_progress:
            self._on_progress = on_progress
        totals: Counter = Counter()
        with ThreadPoolExecutor(max_workers=self.cfg.workers) as pool:
            futures = [pool.submit(self._process_ticker, firm) for firm in firms]
            for fut in as_completed(futures):
                totals.update(fut.result())
        return totals

    def _record(self, firm: Firm, year: int, status: str, **extra) -> None:
        self.log.append({
            "ticker": firm.ticker,
            "industry": firm.industry,
            "year": year,
            "preferred_exchange": firm.exchange,
            "found_exchange": extra.get("found_exchange", ""),
            "status": status,
            "http_status": extra.get("http_status", ""),
            "url": extra.get("url", ""),
            "file_path": extra.get("file_path", ""),
            "tested_urls": " | ".join(extra.get("tested_urls", [])),
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })

    def _process_ticker(self, firm: Firm) -> Counter:
        stats: Counter = Counter()
        order = exchange_order(firm.exchange)

        for year in self.cfg.years:
            dest = self.cfg.reports_dir / firm.ticker / f"{firm.ticker}_{year}_BCTHQT.pdf"
            prev = self.state.get((firm.ticker, year))
            prev_status = prev["status"] if prev else None

            if is_valid_pdf(dest):
                if prev_status not in SUCCESS_STATUSES:  # e.g. PDFs from an older run
                    self._record(firm, year, "skipped_existing", http_status=200, file_path=dest)
                stats["skipped_existing"] += 1
            elif prev_status == "not_found" and not self.cfg.retry_missing:
                stats["skipped_not_found"] += 1
            else:
                status, found = self._download_year(firm, year, order, dest)
                if found:  # try the exchange that just worked first for the remaining years
                    order = [found] + [e for e in order if e != found]
                stats[status] += 1

            self._on_progress(1)
        return stats

    def _download_year(self, firm: Firm, year: int, order: list[str], dest: Path):
        tested: list[str] = []
        had_error = False
        last_status: Optional[int] = None

        for exchange in order:
            url = build_url(self.cfg.base_url, firm.ticker, year, exchange)
            tested.append(url)
            result = fetch_pdf(self._session(), url, dest, self.cfg.timeout, self.limiter)
            last_status = result.http_status

            if result.outcome == "ok":
                self._record(
                    firm, year, "downloaded", found_exchange=exchange, http_status=result.http_status,
                    url=url, file_path=dest, tested_urls=tested,
                )
                return "downloaded", exchange
            if result.outcome == "error":
                had_error = True

        # Only call it "not_found" if no probe failed for a transient reason.
        status = "error" if had_error else "not_found"
        self._record(firm, year, status, http_status=last_status or "", tested_urls=tested)
        return status, None
