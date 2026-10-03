"""Split downloaded reports into ZIP batches (handy for uploads with size/file-count limits)."""

from __future__ import annotations

import math
import zipfile
from pathlib import Path
from typing import Iterable, Optional


def make_zip_batches(
    reports_dir: str | Path,
    zip_dir: str | Path,
    tickers_per_batch: int = 25,
    tickers: Optional[Iterable[str]] = None,
    compress: bool = False,
    suffixes: tuple[str, ...] = (".pdf",),
) -> list[dict]:
    """Zip ticker folders in groups; returns one info dict per batch.

    PDFs are already compressed, so entries are stored without deflate by default
    (much faster, nearly the same size). Pass ``compress=True`` to deflate anyway.
    """
    reports_dir, zip_dir = Path(reports_dir), Path(zip_dir)
    zip_dir.mkdir(parents=True, exist_ok=True)
    wanted = {t.upper() for t in tickers} if tickers is not None else None

    folders = sorted(
        f for f in reports_dir.iterdir()
        if f.is_dir()
        and (wanted is None or f.name.upper() in wanted)
        and any(p.suffix.lower() in suffixes for p in f.iterdir())
    )
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED

    batches = []
    for idx in range(math.ceil(len(folders) / tickers_per_batch)):
        group = folders[idx * tickers_per_batch:(idx + 1) * tickers_per_batch]
        zip_path = zip_dir / f"batch_{idx + 1:02d}.zip"
        tmp_path = zip_path.with_name(zip_path.name + ".tmp")

        n_files = 0
        with zipfile.ZipFile(tmp_path, "w", compression=method) as zf:
            for folder in group:
                for path in sorted(folder.rglob("*")):
                    if path.is_file() and path.suffix.lower() in suffixes:
                        zf.write(path, arcname=path.relative_to(reports_dir))  # TICKER/TICKER_2020_BCTHQT.pdf
                        n_files += 1
        tmp_path.replace(zip_path)  # never leave a half-written zip under the final name

        batches.append({
            "batch": idx + 1,
            "path": zip_path,
            "tickers": [f.name for f in group],
            "files": n_files,
            "size_mb": zip_path.stat().st_size / 1024 / 1024,
        })
    return batches
