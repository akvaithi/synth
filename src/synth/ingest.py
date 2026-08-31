"""Mechanical ingest: inventory files, extract their text, cache it, record provenance.

This stage deliberately involves no model at all. It answers "what exists and what does it
say", cheaply and completely, so that the expensive judgement stage can run over a curated
subset instead of over four thousand files.

Extracted text is cached by content hash under .state/text/, so re-running is nearly free and
a file that has not changed is never read twice.
"""
from __future__ import annotations

import hashlib
import os
import time

from synth import config, db
from synth.extract import extract, ExtractionError, SKIP

# realpath, not just expanduser: docwrite.resolve() hands back a fully resolved path, and
# relpath against a differently-spelled root yields a "../../.." native_id and a second
# source row for a file already indexed. The two roots have to be spelled the same way.
DOCUMENTS = os.path.realpath(os.path.expanduser(config.DOCUMENTS_ROOT))
TEXT_CACHE = os.path.expanduser("~/Developer/synth/.state/text")

# Directories that are noise for a personal-context database.
SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "__pycache__", "build", "dist",
             "Adobe", "target", ".idea", "DerivedData"}
# Extensions that carry no extractable meaning for our purposes.
SKIP_EXT = SKIP | {".java", ".class", ".jar", ".cmbl", ".prproj", ".xmp", ".aep",
                   ".mpeg", ".m4a", ".wav", ".aif", ".srt", ".lrcat", ".icloud"}
MAX_BYTES = 40 * 1024 * 1024


def file_hash(path: str, limit: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(limit))
    return h.hexdigest()[:32]


def candidates(since: float | None = None) -> list[str]:
    """Files worth ingesting. With `since`, only those modified after that epoch time.

    A full pass hashes every file, and on an iCloud-backed folder that is not cheap: 1,919
    files took 582 seconds, which is longer than the sweep interval it was meant to run
    inside. Nothing changes content without changing mtime, so the sweep filters on the stat
    it was already making for the size check and the steady-state pass costs almost nothing.
    """
    """Every file under Documents worth trying to read."""
    out = []
    for root, dirs, files in os.walk(DOCUMENTS):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
        for name in files:
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in SKIP_EXT:
                continue
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_size > MAX_BYTES:
                continue
            if since is not None and st.st_mtime <= since:
                continue
            out.append(path)
    return sorted(out)


def cached_text_path(digest: str) -> str:
    return os.path.join(TEXT_CACHE, f"{digest}.txt")


def ingest_file(conn, path: str) -> tuple[str, int]:
    """Returns (status, chars). Status is cached / extracted / failed / skipped."""
    rel = os.path.relpath(path, DOCUMENTS)
    try:
        digest = file_hash(path)
    except OSError as e:
        return f"failed:{e}", 0

    src_id = db.upsert_source(conn, "file", rel, detail=os.path.basename(path),
                              content_hash=digest)
    target = cached_text_path(digest)
    if os.path.exists(target):
        return "cached", os.path.getsize(target)

    try:
        text = extract(path)
    except ExtractionError as e:
        conn.execute("UPDATE source SET detail = ? WHERE id = ?",
                     (f"{os.path.basename(path)} [unreadable: {str(e)[:80]}]", src_id))
        return f"failed:{e}", 0
    except Exception as e:  # a broken file must not stop the sweep
        return f"failed:{type(e).__name__}: {e}", 0

    os.makedirs(TEXT_CACHE, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(text)
    return "extracted", len(text)


def scan(conn, limit: int | None = None, progress_every: int = 100,
         since: float | None = None, extra: list[str] | None = None) -> dict:
    files = candidates(since=since)
    if extra:
        files = sorted(set(files) | set(extra))
    if limit:
        files = files[:limit]
    stats = {"total": len(files), "extracted": 0, "cached": 0, "failed": 0, "chars": 0}
    failed_paths: list[str] = []
    failures: dict[str, int] = {}
    t0 = time.time()
    for i, path in enumerate(files, 1):
        status, chars = ingest_file(conn, path)
        if status == "cached":
            stats["cached"] += 1
        elif status == "extracted":
            stats["extracted"] += 1
            stats["chars"] += chars
        else:
            stats["failed"] += 1
            failed_paths.append(path)
            reason = status.split(":", 1)[1].strip()[:60] if ":" in status else status
            failures[reason] = failures.get(reason, 0) + 1
        if i % progress_every == 0:
            conn.commit()
            rate = i / max(time.time() - t0, 0.01)
            print(f"  {i}/{len(files)}  {rate:.1f} files/s  "
                  f"extracted={stats['extracted']} cached={stats['cached']} "
                  f"failed={stats['failed']}", flush=True)
    conn.commit()
    stats["failure_reasons"] = sorted(failures.items(), key=lambda kv: -kv[1])[:12]
    # Named, not just counted. A sweep that filters on mtime would otherwise advance its
    # watermark past a file that failed and never look at it again -- an iCloud file evicted
    # at the wrong moment would be silently missing from the index forever.
    stats["failed_paths"] = failed_paths[:200]
    stats["seconds"] = round(time.time() - t0, 1)
    return stats
