"""The semantic index: chunking, the queue that feeds it, and the fusion that uses it.

The queue invariants matter more than the ranking. Ranking that is slightly wrong is a worse
search result; a queue that is wrong re-embeds 1,924 documents on every half-hourly sweep, or
retires the whole backlog because a tunnel was down for an afternoon.
"""
from __future__ import annotations

import pytest

from synth import embed


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Deterministic vectors. Nothing in this suite is allowed near the tunnel."""
    def fake_embed(texts, is_query=False, **kw):
        # One dimension per input so similarity is predictable and order is checkable.
        return [[float(len(t)), 1.0, 0.0] for t in texts]
    monkeypatch.setattr(embed.ollama, "embed", fake_embed)
    monkeypatch.setattr(embed.ollama, "model_prefix_scheme", lambda: "test-model/prefixed")


def _document(conn, text, path="a/b.md"):
    from synth import db
    src = db.upsert_source(conn, "file", path)
    cur = conn.execute("INSERT INTO document (source_id, path, title, text, chars) "
                       "VALUES (?,?,?,?,?) RETURNING id",
                       (src, path, "b.md", text, len(text)))
    doc_id = cur.fetchone()[0]
    conn.commit()
    return doc_id


# ---------------------------------------------------------------- chunking

def test_a_long_document_is_capped():
    """55 documents sit at exactly 200,000 characters and they are course textbooks.
    Uncapped they would contribute more vectors than the rest of the corpus together, and
    every search would return a textbook."""
    pieces = embed.chunks("word " * 60_000)
    assert len(pieces) == embed.MAX_CHUNKS_PER_DOC


def test_chunks_overlap_so_a_straddling_sentence_survives():
    text = "".join(f"{i:04d} " for i in range(2000))
    pieces = embed.chunks(text)
    assert len(pieces) > 1
    first_end = pieces[0][0] + len(pieces[0][1])
    assert pieces[1][0] < first_end, "the second window must start before the first ends"


def test_a_document_with_almost_no_text_produces_no_vector():
    """An extracted document holding 'λ0' still produces a unit vector, and a unit vector
    built from noise sits at a middling distance from EVERY query -- so the emptiest documents
    in the corpus came back against questions they had nothing to do with."""
    assert embed.chunks("λ0") == []
    assert embed.chunks("1\n2") == []
    assert embed.chunks("") == []


def test_a_real_paragraph_is_chunked():
    assert len(embed.chunks("This is a genuine sentence with enough substance to mean "
                            "something at all.")) == 1


# ---------------------------------------------------------------- the queue

def test_a_changed_document_is_queued(conn):
    doc = _document(conn, "the original text, long enough to be worth embedding at all")
    assert embed.mark(conn, "document", doc, "hash-1") is True
    assert embed.mark(conn, "document", doc, "hash-2") is True


def test_a_document_rewritten_to_the_same_text_is_not_requeued(conn):
    """indexer.build reconsiders every row on every sweep whether or not it wrote anything.
    Resetting done_at unconditionally is how all 1,924 documents get re-embedded every half
    hour, for ever, against a model on the other end of a tunnel."""
    doc = _document(conn, "some text that will not change between sweeps")
    embed.mark(conn, "document", doc, "hash-1")
    conn.commit()
    assert embed.mark(conn, "document", doc, "hash-1") is False


def test_writing_a_document_queues_it(conn, documents):
    """The hook belongs at the one function every document write passes through, which is the
    same place document_fts is hand-maintained."""
    from synth import db, docwrite
    src = db.upsert_source(conn, "file", "x/y.md")
    doc_id = docwrite._upsert_document(conn, src, "x/y.md",
                                       "a document with real content in it, at some length")
    conn.commit()
    queued = conn.execute("SELECT count(*) FROM embedding_queue WHERE ref_id = ? "
                          "AND done_at IS NULL", (doc_id,)).fetchone()[0]
    assert queued == 1


def test_indexing_still_succeeds_when_embedding_cannot_be_queued(conn, monkeypatch):
    """Search degrading is acceptable. Indexing failing is not."""
    from synth import db, docwrite
    monkeypatch.setattr(embed, "mark", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    src = db.upsert_source(conn, "file", "x/z.md")
    assert docwrite._upsert_document(conn, src, "x/z.md", "text that is long enough to keep")


# ---------------------------------------------------------------- draining

def test_draining_works_past_one_page(conn):
    """A backfill of 1,864 documents finished after 500 and reported success with 1,364 still
    queued, because the page size was being used as a total."""
    for i in range(12):
        doc = _document(conn, f"document number {i} with enough text to be embedded properly",
                        path=f"d/{i}.md")
        embed.mark(conn, "document", doc, f"h{i}")
    conn.commit()
    result = embed.drain(conn, batch=5)
    assert result["embedded"] == 12
    assert result["remaining"] == 0


def test_an_outage_does_not_burn_the_retry_budget(conn, monkeypatch):
    """attempts is for a row that cannot be embedded. A service that is away is not that, and
    counting it would retire the entire backlog over one afternoon."""
    doc = _document(conn, "a document that will not get embedded this time around")
    embed.mark(conn, "document", doc, "h")
    conn.commit()

    def down(*a, **k):
        raise embed.ollama.OllamaDown("tunnel is down")
    monkeypatch.setattr(embed.ollama, "embed", down)
    result = embed.drain(conn)
    assert "stopped" in result
    row = conn.execute("SELECT attempts, done_at FROM embedding_queue").fetchone()
    assert row["attempts"] == 0
    assert row["done_at"] is None


def test_a_row_that_keeps_failing_is_eventually_left_alone(conn, monkeypatch):
    doc = _document(conn, "a document that raises every single time it is embedded")
    embed.mark(conn, "document", doc, "h")
    conn.commit()
    monkeypatch.setattr(embed.ollama, "embed",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
    for _ in range(embed.MAX_ATTEMPTS + 1):
        embed.drain(conn)
    assert embed.pending(conn) == []


def test_re_embedding_replaces_rather_than_accumulates(conn):
    """A document that got shorter must not keep the tail of the longer version."""
    doc = _document(conn, "word " * 500)
    embed.mark(conn, "document", doc, "h1")
    conn.commit()
    embed.drain(conn)
    many = conn.execute("SELECT count(*) FROM embedding WHERE ref_id = ?", (doc,)).fetchone()[0]

    conn.execute("UPDATE document SET text = ? WHERE id = ?",
                 ("a much shorter document body here", doc))
    embed.mark(conn, "document", doc, "h2")
    conn.commit()
    embed.drain(conn)
    few = conn.execute("SELECT count(*) FROM embedding WHERE ref_id = ?", (doc,)).fetchone()[0]
    assert few < many


# ---------------------------------------------------------------- fusion

def test_reciprocal_rank_fusion_prefers_what_both_methods_found():
    text = [{"id": 1}, {"id": 2}, {"id": 3}]
    meaning = [{"id": 3}, {"id": 4}, {"id": 5}]
    assert embed.rrf(text, meaning)[0]["id"] == 3


def test_fusion_keeps_items_only_one_method_found():
    fused = embed.rrf([{"id": 1}], [{"id": 2}])
    assert {r["id"] for r in fused} == {1, 2}


# ---------------------------------------------------------------- graceful degradation

def test_search_keeps_its_shape_when_inference_is_unreachable(conn, monkeypatch):
    """search_context is the most-called tool in the system and the doctrine tells every model
    to begin with it. It must not be able to fail because a second machine is down."""
    from synth import tools
    monkeypatch.setattr(embed, "search",
                        lambda *a, **k: (_ for _ in ()).throw(embed.ollama.OllamaDown("gone")))
    out = tools.search_context(conn, "anything at all")
    assert set(out) >= {"query", "entities", "facts", "documents", "links"}
    assert "unavailable" in out["semantic"]


def test_text_mode_returns_exactly_the_original_shape(conn):
    """mode="text" is the escape hatch back to the behaviour every existing caller expects."""
    from synth import tools

    out = tools.search_context(conn, "anything", mode="text")
    assert "semantic" not in out
    assert set(out) >= {"query", "entities", "facts", "documents", "links"}
