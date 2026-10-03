"""Integration tests against a throw-away local HTTP server (no internet needed)."""

import contextlib
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from govreports.cli import main as cli_main, parse_years
from govreports.downloader import (
    DownloadConfig, DownloadLog, Downloader, RateLimiter, build_url, exchange_order,
    is_valid_pdf, read_latest,
)
from govreports.tickers import Firm

PDF = b"%PDF-1.4\n" + b"x" * 300 + b"\n%%EOF"


class FakeVietstock(BaseHTTPRequestHandler):
    """AAA: only on HNX (2021, 2022). BBB: 200 + HTML everywhere. CCC: always 503. DDD: truncated body."""

    seen: list = []

    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        FakeVietstock.seen.append(self.path)
        name = self.path.rsplit("/", 1)[-1]
        if name.startswith("AAA_") and "/HNX/" in self.path and name.endswith(("2021.pdf", "2022.pdf")):
            return self._send(200, PDF)
        if name.startswith("BBB_"):
            return self._send(200, b"<html>Not found</html>")
        if name.startswith("CCC_"):
            return self._send(503, b"busy")
        if name.startswith("DDD_") and "/HOSE/" in self.path:
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(PDF[:150])  # then hang up early
            return
        return self._send(404, b"nope")

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextlib.contextmanager
def fake_server():
    FakeVietstock.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeVietstock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/data"
    finally:
        server.shutdown()
        server.server_close()


def run_download(out, base_url, firms, years=(2021, 2022), **overrides):
    opts = dict(workers=2, rate=0, retries=1, backoff=0.0, timeout=(2.0, 2.0))
    opts.update(overrides)
    cfg = DownloadConfig(years=years, out_dir=out, base_url=base_url, **opts)
    with DownloadLog(cfg.log_path) as log:
        return Downloader(cfg, log).run(firms)


class HelperTests(unittest.TestCase):
    def test_build_url_matches_vietstock_pattern(self):
        self.assertEqual(
            build_url("https://static2.vietstock.vn/data/", "CCI", 2025, "HOSE"),
            "https://static2.vietstock.vn/data/HOSE/2025/BCTHQT/VN/NAM/CCI_Baocaoquantri_2025.pdf",
        )

    def test_exchange_order(self):
        self.assertEqual(exchange_order("hnx"), ["HNX", "HOSE", "UPCOM"])
        self.assertEqual(exchange_order(""), ["HOSE", "HNX", "UPCOM"])
        self.assertEqual(exchange_order("???"), ["HOSE", "HNX", "UPCOM"])

    def test_parse_years(self):
        self.assertEqual(parse_years("2020-2022"), (2020, 2021, 2022))
        self.assertEqual(parse_years("2019,2021-2022"), (2019, 2021, 2022))

    def test_is_valid_pdf(self):
        d = Path(tempfile.mkdtemp())
        (d / "ok.pdf").write_bytes(PDF)
        (d / "html.pdf").write_bytes(b"<html>" + b"x" * 300)
        (d / "tiny.pdf").write_bytes(b"%PDF")
        self.assertTrue(is_valid_pdf(d / "ok.pdf"))
        self.assertFalse(is_valid_pdf(d / "html.pdf"))
        self.assertFalse(is_valid_pdf(d / "tiny.pdf"))
        self.assertFalse(is_valid_pdf(d / "missing.pdf"))

    def test_rate_limiter_spaces_requests(self):
        limiter = RateLimiter(50)  # one slot per 20 ms
        start = time.monotonic()
        for _ in range(6):
            limiter.wait()
        self.assertGreaterEqual(time.monotonic() - start, 0.09)


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.firms = [Firm("AAA", "HOSE"), Firm("BBB", "HNX"), Firm("CCC", "UPCOM"), Firm("DDD", "HOSE")]

    def test_outcomes_fallback_and_exchange_memory(self):
        with fake_server() as url:
            totals = run_download(self.out, url, self.firms, workers=1)
            seen = list(FakeVietstock.seen)

        state = read_latest(self.out / "download_log.csv")
        # AAA is listed on HOSE but lives on HNX -> fallback finds it
        self.assertEqual(state[("AAA", 2021)]["status"], "downloaded")
        self.assertEqual(state[("AAA", 2021)]["found_exchange"], "HNX")
        self.assertTrue(is_valid_pdf(self.out / "reports" / "AAA" / "AAA_2021_BCTHQT.pdf"))
        # exchange memory: for 2022 the first (and only) probe goes straight to HNX
        self.assertEqual(state[("AAA", 2022)]["tested_urls"], build_url(url, "AAA", 2022, "HNX"))
        aaa_2022 = [p for p in seen if "AAA_Baocaoquantri_2022" in p]
        self.assertEqual(len(aaa_2022), 1)
        # HTML 'soft 404' is not a PDF -> not_found, nothing saved
        self.assertEqual(state[("BBB", 2021)]["status"], "not_found")
        self.assertFalse((self.out / "reports" / "BBB").exists())
        # persistent 503 is an error (retryable), not a missing report
        self.assertEqual(state[("CCC", 2021)]["status"], "error")
        # connection dropped mid-download -> error, and no partial file survives
        self.assertEqual(state[("DDD", 2021)]["status"], "error")
        self.assertEqual(list(self.out.rglob("*.part")), [])
        self.assertEqual(totals["downloaded"], 2)

    def test_resume_skips_done_and_not_found_but_retries_errors(self):
        with fake_server() as url:
            run_download(self.out, url, self.firms, workers=1)

            FakeVietstock.seen.clear()
            run_download(self.out, url, self.firms, workers=1)
            second = list(FakeVietstock.seen)
            self.assertFalse(any("AAA_" in p for p in second))  # already downloaded
            self.assertFalse(any("BBB_" in p for p in second))  # known not_found
            self.assertTrue(any("CCC_" in p for p in second))   # errors get retried

            FakeVietstock.seen.clear()
            run_download(self.out, url, self.firms, workers=1, retry_missing=True)
            self.assertTrue(any("BBB_" in p for p in FakeVietstock.seen))

    def test_adopts_pdfs_from_an_older_run(self):
        pdf = self.out / "reports" / "AAA" / "AAA_2021_BCTHQT.pdf"
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(PDF)
        with fake_server() as url:
            run_download(self.out, url, [Firm("AAA", "HOSE")], years=(2021,))
            self.assertEqual(FakeVietstock.seen, [])  # no network needed
        self.assertEqual(read_latest(self.out / "download_log.csv")[("AAA", 2021)]["status"], "skipped_existing")

    def test_cli_end_to_end(self):
        tickers = self.out / "tickers.csv"
        tickers.write_text("Exchange,Industry,Ticker\nHOSE,Tiền tệ,AAA\n", encoding="utf-8")
        out = self.out / "run"
        with fake_server() as url:
            self.assertEqual(cli_main([
                "download", "--tickers", str(tickers), "--out", str(out), "--years", "2021-2022",
                "--rate", "0", "--retries", "1", "--base-url", url,
            ]), 0)
        self.assertEqual(cli_main(["analyze", "--out", str(out), "--min-pairs", "1"]), 0)
        self.assertTrue((out / "eligible_firms.xlsx").exists())
        self.assertTrue((out / "firm_coverage.csv").exists())
        self.assertEqual(cli_main(["zip", "--out", str(out), "--eligible-only", "--min-pairs", "1"]), 0)
        with zipfile.ZipFile(out / "zip_batches" / "batch_01.zip") as zf:
            self.assertEqual(sorted(zf.namelist()), ["AAA/AAA_2021_BCTHQT.pdf", "AAA/AAA_2022_BCTHQT.pdf"])


if __name__ == "__main__":
    unittest.main()
