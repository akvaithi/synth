"""Searching by meaning, next to searching by word.

`search_context` runs four FTS5 MATCH queries, and `_fts_escape` strips punctuation from the
question and ORs every remaining term. So "what did the ARPA-E people say about graphitization
deadlines" matches any document containing any one of those words, and bm25 is left to sort out
the rest. It is very good at the thing it is for -- an exact identifier, a course number, a
person's name -- and cannot answer "which document is ABOUT this" at all.

Embeddings answer the second question and are bad at the first, so both run and the document
results are fused rather than one replacing the other.

## Why numpy and not a vector extension

1,924 documents chunk to roughly 9,000 vectors; at 768 float32 that is 28 MB, and ranking them
is a single matmul taking a few milliseconds. An ANN index buys nothing at that size and costs
a native extension that db.connect() would have to load on *every* connection -- bin/synth, the
connector, the MCP server, pytest -- so a missing .dylib would break every database open in the
system rather than just search.

## Why the queue

Embedding happens through a model that serves one request at a time, over a tunnel to another
machine. indexer.build touches hundreds of rows inside the sweep's write transaction, so
embedding inline there would hold the SQLite write lock for minutes. Documents are marked when
they change and drained separately.
"""
from __future__ import annotations

import struct
import time

from synth import db, ollama

# nomic-embed-text's window is 2048 tokens. 1600 characters is roughly 400, which leaves the
# prefix and any tokeniser disagreement a wide margin -- a chunk that overflows is silently
# truncated, and a silently shorter chunk is a silently worse vector.
CHUNK_CHARS = 1600
CHUNK_OVERLAP = 200

# 55 documents sit at exactly 200,000 characters, the extraction cap, and they are CHEN 201
# and POLS 207 textbooks and NMR spreadsheets. Uncapped they would contribute some 6,900
# vectors of course material -- more than the rest of the corpus put together -- and every
# search would surface a textbook. Capped at the head, a textbook contributes its front matter
# and a resume contributes all of itself.
MAX_CHUNKS_PER_DOC = 24

# Below this, a chunk carries no meaning to embed and is actively harmful. An extracted
# document holding "λ0", or a scanned worksheet whose text layer is "1\n2", still produces a
# unit vector, and a unit vector built from noise sits at a middling cosine distance from
# EVERY query -- so the emptiest documents in the corpus surface against questions they have
# nothing to do with. FTS has the opposite behaviour and simply never matches them.
MIN_CHUNK_CHARS = 40

MAX_ATTEMPTS = 3


def chunks(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP,
           cap: int = MAX_CHUNKS_PER_DOC) -> list[tuple[int, str]]:
    """Split text into overlapping windows, as (start_char, chunk).

    The overlap exists so a sentence spanning a boundary is whole in one of the two windows;
    without it, the fact that straddles the split is in neither vector.
    """
    text = (text or "").strip()
    if len(text) < MIN_CHUNK_CHARS:
        return []
    step = max(1, size - overlap)
    out = []
    for start in range(0, len(text), step):
        piece = text[start:start + size].strip()
        # A trailing sliver is skipped rather than embedded: the overlap means its content is
        # already inside the previous window.
        if len(piece) >= MIN_CHUNK_CHARS:
            out.append((start, piece))
        if len(out) >= cap or start + size >= len(text):
            break
    return out


def pack(vector) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


# ---------------------------------------------------------------- the queue


def mark(conn, kind: str, ref_id: int, text_hash: str) -> bool:
    """Queue something for embedding, if its text actually changed.

    Resetting done_at unconditionally is how every sweep re-embeds all 1,924 documents: the
    indexer reconsiders every row each pass whether or not it wrote anything.
    """
    cur = conn.execute(
        "INSERT INTO embedding_queue (kind, ref_id, text_hash) VALUES (?,?,?) "
        "ON CONFLICT (kind, ref_id) DO UPDATE SET text_hash = excluded.text_hash, "
        "done_at = NULL, attempts = 0, last_error = NULL "
        "WHERE embedding_queue.text_hash != excluded.text_hash",
        (kind, ref_id, text_hash))
    return cur.rowcount > 0


def pending(conn, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT kind, ref_id, text_hash FROM embedding_queue "
        "WHERE done_at IS NULL AND attempts < ? ORDER BY enqueued_at LIMIT ?",
        (MAX_ATTEMPTS, limit)).fetchall()
    return [dict(r) for r in rows]


def _text_for(conn, kind: str, ref_id: int) -> str | None:
    if kind == "document":
        row = conn.execute("SELECT text FROM document WHERE id = ?", (ref_id,)).fetchone()
        return row["text"] if row else None
    if kind == "assertion":
        row = conn.execute(
            "SELECT e.name, a.predicate, a.value_text FROM assertion a "
            "JOIN entity e ON e.id = a.entity_id WHERE a.id = ?", (ref_id,)).fetchone()
        if not row:
            return None
        return f"{row['name']} — {row['predicate']}: {row['value_text'] or ''}"
    if kind == "entity":
        row = conn.execute("SELECT name, kind FROM entity WHERE id = ?", (ref_id,)).fetchone()
        return f"{row['name']} ({row['kind']})" if row else None
    return None


def embed_one(conn, kind: str, ref_id: int, text_hash: str) -> dict:
    """Embed one row's chunks, replacing whatever was there for it."""
    text = _text_for(conn, kind, ref_id)
    if not text:
        conn.execute("UPDATE embedding_queue SET done_at = ?, last_error = ? "
                     "WHERE kind = ? AND ref_id = ?",
                     (db.now(), "no text to embed", kind, ref_id))
        conn.commit()
        return {"kind": kind, "ref_id": ref_id, "chunks": 0, "note": "no text"}

    pieces = chunks(text)
    vectors = ollama.embed([p for _, p in pieces])
    model = ollama.model_prefix_scheme()
    # Replace rather than merge: a shorter document must not keep the tail of a longer one.
    conn.execute("DELETE FROM embedding WHERE kind = ? AND ref_id = ?", (kind, ref_id))
    conn.executemany(
        "INSERT INTO embedding (kind, ref_id, chunk, start_char, chars, text_hash, "
        "model, dims, vector) VALUES (?,?,?,?,?,?,?,?,?)",
        [(kind, ref_id, i, start, len(piece), db.text_hash(piece), model, len(vec), pack(vec))
         for i, ((start, piece), vec) in enumerate(zip(pieces, vectors))])
    conn.execute("UPDATE embedding_queue SET done_at = ?, text_hash = ?, last_error = NULL "
                 "WHERE kind = ? AND ref_id = ?", (db.now(), text_hash, kind, ref_id))
    conn.commit()
    return {"kind": kind, "ref_id": ref_id, "chunks": len(pieces)}


def drain(conn, budget_seconds: float = 120.0, batch: int = 200) -> dict:
    """Work the queue until it is empty or the time budget runs out.

    `batch` is how many rows are claimed per query, not a ceiling on the run: fetching one
    fixed page and stopping is how a backfill of 1,864 documents quietly finished after 500
    and reported success with 1,364 still queued.
    """
    started = time.time()
    done = failed = written = 0
    while True:
        if time.time() - started > budget_seconds:
            break
        items = pending(conn, limit=batch)
        if not items:
            break
        for item in items:
            if time.time() - started > budget_seconds:
                break
            try:
                result = embed_one(conn, item["kind"], item["ref_id"], item["text_hash"])
                written += result.get("chunks", 0)
                done += 1
            except ollama.OllamaDown as e:
                # The service is away, not this row's fault. Leaving attempts alone matters:
                # otherwise a two-hour outage burns every row's retry budget and permanently
                # retires the whole backlog.
                return {"embedded": done, "chunks": written, "failed": failed,
                        "stopped": f"ollama unavailable: {e}",
                        "remaining": len(pending(conn, limit=1))}
            except Exception as e:
                failed += 1
                conn.execute(
                    "UPDATE embedding_queue SET attempts = attempts + 1, last_error = ? "
                    "WHERE kind = ? AND ref_id = ?",
                    (f"{type(e).__name__}: {e}", item["kind"], item["ref_id"]))
                conn.commit()
    return {"embedded": done, "chunks": written, "failed": failed,
            "remaining": len(pending(conn, limit=1))}


def enqueue_all(conn, kind: str = "document") -> int:
    """Put everything of a kind on the queue. The one-time backfill."""
    if kind == "document":
        rows = conn.execute("SELECT id, text FROM document WHERE text IS NOT NULL "
                            "AND text != ''").fetchall()
        marked = 0
        for r in rows:
            if mark(conn, "document", r["id"], db.text_hash(r["text"])):
                marked += 1
        conn.commit()
        return marked
    raise ValueError(f"no backfill defined for {kind!r}")


# ---------------------------------------------------------------- searching


def matrix(conn, kind: str = "document"):
    """Every vector of a kind, as one array, with the rows that identify them."""
    import numpy as np

    rows = conn.execute(
        "SELECT e.id, e.ref_id, e.chunk, e.start_char, e.chars, e.vector, e.dims "
        "FROM embedding e WHERE e.kind = ? ORDER BY e.id", (kind,)).fetchall()
    if not rows:
        return np.zeros((0, ollama.EMBED_DIMS), dtype="float32"), []
    dims = rows[0]["dims"]
    usable = [r for r in rows if r["dims"] == dims]
    m = np.frombuffer(b"".join(r["vector"] for r in usable),
                      dtype="<f4").reshape(len(usable), dims)
    # Normalise once so ranking is a dot product rather than a cosine per row.
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype("float32"), [dict(r) for r in usable]


def search(conn, query: str, limit: int = 20, kind: str = "document") -> list[dict]:
    """Rank by meaning. Returns one row per document, its best chunk."""
    import numpy as np

    m, rows = matrix(conn, kind)
    if not rows:
        return []
    q = np.asarray(ollama.embed([query], is_query=True)[0], dtype="float32")
    n = np.linalg.norm(q) or 1.0
    scores = m @ (q / n)

    best: dict[int, tuple[float, dict]] = {}
    for score, row in zip(scores, rows):
        ref = row["ref_id"]
        if ref not in best or score > best[ref][0]:
            best[ref] = (float(score), row)
    ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)[:limit]

    out = []
    for ref_id, (score, row) in ranked:
        doc = conn.execute("SELECT id, path, title, chars FROM document WHERE id = ?",
                           (ref_id,)).fetchone()
        if doc is None:
            continue
        text = conn.execute("SELECT text FROM document WHERE id = ?",
                            (ref_id,)).fetchone()["text"] or ""
        excerpt = text[row["start_char"]:row["start_char"] + row["chars"]].strip()
        out.append({"id": doc["id"], "path": doc["path"], "title": doc["title"],
                    "chars": doc["chars"], "score": round(score, 4),
                    "excerpt": excerpt[:400]})
    return out


def rrf(*ranked_lists, k: int = 60, key=lambda r: r["id"]) -> list:
    """Reciprocal rank fusion: score = sum(1 / (k + rank)) over the lists an item appears in.

    Chosen over normalising the two scores onto one scale because FTS5's `rank` and a cosine
    similarity are not comparable quantities, and any constant that made them comparable would
    be a tuning parameter nobody would ever re-tune.
    """
    scores: dict = {}
    holder: dict = {}
    for ranked in ranked_lists:
        for position, row in enumerate(ranked):
            ident = key(row)
            scores[ident] = scores.get(ident, 0.0) + 1.0 / (k + position + 1)
            # Keep the richer row: a semantic hit carries an excerpt the FTS row may not.
            if ident not in holder or len(str(row)) > len(str(holder[ident])):
                holder[ident] = row
    order = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [holder[i] for i, _ in order]


def stats(conn) -> dict:
    row = conn.execute(
        "SELECT count(*) AS vectors, count(DISTINCT ref_id) AS refs, "
        "max(embedded_at) AS newest FROM embedding WHERE kind = 'document'").fetchone()
    queued = conn.execute("SELECT count(*) FROM embedding_queue "
                          "WHERE done_at IS NULL").fetchone()[0]
    stuck = conn.execute("SELECT count(*) FROM embedding_queue "
                         "WHERE done_at IS NULL AND attempts >= ?",
                         (MAX_ATTEMPTS,)).fetchone()[0]
    return {"vectors": row["vectors"], "documents": row["refs"], "newest": row["newest"],
            "queued": queued, "gave_up_on": stuck}
