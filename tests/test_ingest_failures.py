"""Which failures are worth trying again, and which are not.

A sweep reported 856 failures, 748 of them "Resource deadlock avoided", and that was read as
the document index being stale. It was two different things wearing one label. The deadlocks
are the iCloud file provider under load and they clear on their own. The ones that persisted
were scanned PDFs, which ingest can never read however many times it tries -- they need OCR,
and they were being retried every thirty minutes instead.
"""
from __future__ import annotations

import os

import pytest

from synth import extract, ingest


@pytest.fixture
def docroot(tmp_path, monkeypatch):
    """A Documents root that is not tmp_path itself -- the temporary database lives there,
    and scan() would otherwise try to ingest it."""
    root = tmp_path / "Documents"
    root.mkdir()
    monkeypatch.setattr(ingest, "DOCUMENTS", str(root))
    return root


# ---------------------------------------------------------------- classification

@pytest.mark.parametrize("reason", [
    "no text layer (5 pages, likely scanned)",
    "unsupported type .pages",
    "binary or media type (.mov)",
    "PDFKit could not open the document",
    "iWork document with no readable preview",
    "all engines failed for .docx: boom",
])
def test_a_failure_a_retry_cannot_fix_is_not_retried(reason):
    assert not ingest.is_transient(reason)


@pytest.mark.parametrize("reason", [
    "[Errno 11] Resource deadlock avoided",
    "evicted from this machine; iCloud did not return it",
    "[Errno 5] Input/output error",
])
def test_a_failure_that_may_clear_on_its_own_is_retried(reason):
    assert ingest.is_transient(reason)


def test_scan_keeps_transient_failures_and_counts_the_rest(conn, docroot, monkeypatch):
    (docroot / "transcript.pdf").write_bytes(b"%PDF-1.4 not really")
    (docroot / "evicted.md").write_text("x")

    def fake_extract(path):
        if path.endswith(".pdf"):
            raise extract.ExtractionError("no text layer (2 pages, likely scanned)")
        raise OSError(11, "Resource deadlock avoided")

    monkeypatch.setattr(ingest, "extract", fake_extract)
    stats = ingest.scan(conn)

    assert stats["failed"] == 2
    assert stats["permanent"] == 1
    assert [os.path.basename(p) for p in stats["failed_paths"]] == ["evicted.md"]


def test_the_scanned_pdf_is_still_flagged_for_ocr_to_find(conn, docroot, monkeypatch):
    """Not retrying them is only safe because ocr_pass collects them by this marker."""
    (docroot / "transcript.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(ingest, "extract", lambda p: (_ for _ in ()).throw(
        extract.ExtractionError("no text layer (2 pages, likely scanned)")))

    ingest.scan(conn)
    detail = conn.execute("SELECT detail FROM source").fetchone()[0]
    assert "no text layer" in detail


# ---------------------------------------------------------------- eviction

def test_an_evicted_file_is_downloaded_before_it_is_called_a_failure(conn, docroot,
                                                                    monkeypatch):
    """The first read of a file was also the first thing to touch an evicted one, and nothing
    had asked iCloud for it: materialise lived only on the document-write path."""
    path = docroot / "paper.md"
    path.write_text("the real contents")

    reads = {"n": 0}
    real_hash = ingest.file_hash

    def hash_that_fails_once(p, limit=None):
        reads["n"] += 1
        if reads["n"] == 1:
            raise OSError(11, "Resource deadlock avoided")
        return real_hash(p)

    called = []
    monkeypatch.setattr(ingest, "file_hash", hash_that_fails_once)
    monkeypatch.setattr(ingest, "materialise", lambda p: called.append(p) or True)

    status, _ = ingest.ingest_file(conn, str(path))
    assert called == [str(path)]
    assert status in ("extracted", "cached"), status


def test_a_file_icloud_will_not_return_is_reported_as_evicted(conn, docroot, monkeypatch):
    path = docroot / "paper.md"
    path.write_text("x")
    monkeypatch.setattr(ingest, "file_hash", lambda p, limit=None: (_ for _ in ()).throw(
        OSError(11, "Resource deadlock avoided")))
    monkeypatch.setattr(ingest, "materialise", lambda p: False)

    status, _ = ingest.ingest_file(conn, str(path))
    assert "evicted" in status
    assert ingest.is_transient(status.split(":", 1)[1]), "an evicted file must be retried"


# ---------------------------------------------------------------- dataless detection

def test_a_present_file_is_not_dataless(tmp_path):
    f = tmp_path / "here.md"
    f.write_text("bytes actually on disk")
    assert not extract.is_dataless(str(f))


def test_an_empty_file_is_not_mistaken_for_an_evicted_one(tmp_path):
    f = tmp_path / "empty.md"
    f.write_text("")
    assert not extract.is_dataless(str(f))


def test_a_missing_file_is_not_dataless(tmp_path):
    assert not extract.is_dataless(str(tmp_path / "nope.md"))


def test_size_alone_cannot_decide_it(tmp_path, monkeypatch):
    """The check that was there before: an evicted file reports its full logical size, so
    `getsize(path) > 0` was true for a file whose bytes were entirely absent, and materialise
    returned True without downloading anything."""
    f = tmp_path / "evicted.pdf"
    f.write_bytes(b"x" * 4096)
    real_stat = os.stat

    class Dataless:
        st_blocks = 0
        st_size = 840667

    monkeypatch.setattr(extract.os, "stat",
                        lambda p, *a, **k: Dataless() if str(p) == str(f) else real_stat(p))
    assert extract.is_dataless(str(f))
    assert os.path.getsize(str(f)) > 0, "size still looks fine, which was the trap"
