"""The FTS index maintained by hand, and what happens when it drifts.

document_fts is an external-content FTS5 table with only document_ai (INSERT) and document_ad
(DELETE) triggers -- there is no AFTER UPDATE, unlike entity_au. So an UPDATE has to maintain
the index itself, and the 'delete' command must be handed the OLD column values, or the index
keeps postings pointing at text that is no longer there. That surfaces later as 'database disk
image is malformed' on an unrelated search, which is about as far from the cause as a symptom
can get.
"""
from __future__ import annotations

import pytest

from synth import config, docwrite, ingest


@pytest.fixture(autouse=True)
def text_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "TEXT_CACHE", str(tmp_path / "text"))


def _write(documents, name, text):
    path = documents / config.WRITABLE_DOCUMENTS / name
    path.write_text(text)
    return f"{config.WRITABLE_DOCUMENTS}/{name}"


def _search(conn, term):
    return [r["path"] for r in conn.execute(
        "SELECT d.path FROM document_fts f JOIN document d ON d.id = f.rowid "
        "WHERE document_fts MATCH ?", (term,))]


def test_a_new_document_is_indexed_once(conn, documents):
    rel = _write(documents, "notes.md", "the peregrine falcon stoops at speed")
    docwrite.reindex(conn, rel, docwrite.resolve(rel))

    assert _search(conn, "peregrine") == [rel]
    assert conn.execute("SELECT count(*) FROM document").fetchone()[0] == 1


def test_reindexing_an_unchanged_document_does_not_double_index(conn, documents):
    rel = _write(documents, "notes.md", "the peregrine falcon stoops at speed")
    abs_path = docwrite.resolve(rel)
    docwrite.reindex(conn, rel, abs_path)
    docwrite.reindex(conn, rel, abs_path)

    assert _search(conn, "peregrine") == [rel]
    assert conn.execute("SELECT count(*) FROM document").fetchone()[0] == 1


def test_an_update_removes_the_postings_for_the_old_text(conn, documents):
    """The failure mode this whole function exists for: search for what the file used to say
    and get a hit on a document that no longer contains it."""
    rel = _write(documents, "notes.md", "the peregrine falcon stoops at speed")
    abs_path = docwrite.resolve(rel)
    docwrite.reindex(conn, rel, abs_path)

    abs_path_2 = docwrite.resolve(rel)
    (documents / config.WRITABLE_DOCUMENTS / "notes.md").write_text(
        "the kestrel hovers instead")
    docwrite.reindex(conn, rel, abs_path_2)

    assert _search(conn, "kestrel") == [rel]
    assert _search(conn, "peregrine") == [], "stale posting left pointing at removed text"


def test_an_update_keeps_the_same_row(conn, documents):
    """UPDATE, never DELETE+INSERT: enrichment.document_id is a foreign key onto document(id),
    so deleting the row would lose the marker saying this document already went through the
    expensive LLM pass."""
    rel = _write(documents, "notes.md", "first")
    docwrite.reindex(conn, rel, docwrite.resolve(rel))
    first = conn.execute("SELECT id FROM document").fetchone()[0]
    conn.execute("INSERT INTO enrichment (document_id) VALUES (?)", (first,))
    conn.commit()

    (documents / config.WRITABLE_DOCUMENTS / "notes.md").write_text("second")
    result = docwrite.reindex(conn, rel, docwrite.resolve(rel))

    assert result["document_id"] == first
    assert conn.execute("SELECT count(*) FROM enrichment").fetchone()[0] == 1


def test_the_index_matches_what_a_fresh_ingest_would_produce(conn, documents):
    """Storing the raw file string instead of the extracted text would make every later
    ingest see a change that is not there -- the file has a trailing newline that _clean
    strips."""
    from synth import extract

    rel = _write(documents, "notes.md", "a line with a trailing newline\n")
    abs_path = docwrite.resolve(rel)
    docwrite.reindex(conn, rel, abs_path)

    stored = conn.execute("SELECT text FROM document").fetchone()[0]
    assert stored == extract.extract(abs_path)


def test_reindex_records_provenance_for_the_file(conn, documents):
    rel = _write(documents, "notes.md", "content")
    result = docwrite.reindex(conn, rel, docwrite.resolve(rel))

    row = conn.execute("SELECT kind, native_id, content_hash FROM source WHERE id = ?",
                       (result["source_id"],)).fetchone()
    assert row["kind"] == "file"
    assert row["native_id"] == rel
    assert row["content_hash"] == result["content_hash"]
