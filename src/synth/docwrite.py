"""Writing Arun's documents. The file is the source of truth.

A document exists in three places: the file under DOCUMENTS_ROOT, the extracted text cache
under .state/text, and the `document` row that read_document and search_context actually
read. Only the first is real — it is what syncs to his phone and what the next ingest
re-reads. So every write lands on the file first and refreshes the other two after; an index
refreshed against a write that never landed would be a lie.

Nothing here logs or takes a reason. That belongs to actions.write_document, which is the
only thing that should call the writing half of this module.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

from synth import config, db, extract, ingest

DOCUMENTS = os.path.expanduser(config.DOCUMENTS_ROOT)

# Every prior version of every file Synth has written. Doctrine says nothing of Arun's is
# deleted, and a read-modify-write destroys the previous bytes unless they go somewhere
# first. .state/ is gitignored and already holds text/ and attachments/.
VERSIONS = os.path.expanduser("~/Developer/synth/.state/docversions")


# ---------------------------------------------------------------- the guard


def resolve(rel_path: str) -> str:
    """Absolute path for a writable document, or a refusal explaining why not.

    Mail is data an attacker can reach, and the thing calling these tools is a model that
    may have just read a hostile message. So the path is checked on its own merits rather
    than trusted for where it came from. ingest.py normalises paths with relpath but never
    validates containment; nothing else in Synth does either.
    """
    p = (rel_path or "").strip()
    if not p:
        raise ValueError(
            "path is required: a path relative to Documents, the same form read_document "
            "takes, e.g. 'Archive/Synth/markdown/RESUME-MASTER.md'")
    if p.startswith("~") or os.path.isabs(p):
        raise ValueError(
            f"{rel_path!r} is an absolute path. Give a path relative to Documents, the same "
            f"form read_document takes, e.g. {config.WRITABLE_DOCUMENTS}/RESUME-MASTER.md")
    if "\x00" in p:
        raise ValueError(f"{rel_path!r} contains a null byte and is not a real path.")
    if p.endswith("/") or os.path.basename(p) in ("", ".", ".."):
        raise ValueError(f"{rel_path!r} names a folder, not a file.")

    name = os.path.basename(p)
    ext = os.path.splitext(name)[1].lower()
    if ext not in config.WRITABLE_EXTENSIONS:
        raise ValueError(
            f"{rel_path!r} has extension {ext or '(none)'}, which Synth cannot write. Only "
            f"{config.WRITABLE_EXTENSIONS} round-trip losslessly; a .docx or .pdf is "
            f"extracted lossily by MarkItDown and PDFKit and stays read-only.")
    if name in config.PROTECTED_DOCUMENTS:
        raise PermissionError(
            f"{name} is read-only. It is one of the files Synth reads to learn what it is "
            f"({config.PROTECTED_DOCUMENTS}), so a write here would let Synth teach itself. "
            f"Ask Arun to edit it by hand.")

    # Resolve the PARENT, not the leaf. For create_document the leaf does not exist yet, and
    # realpath of a missing leaf succeeds silently — which would let a symlinked parent
    # through. The parent must be the writable folder itself or somewhere below it.
    root = os.path.realpath(DOCUMENTS)
    allowed = os.path.realpath(os.path.join(root, config.WRITABLE_DOCUMENTS))
    parent = os.path.realpath(os.path.dirname(os.path.join(root, p)))
    if parent != allowed and not parent.startswith(allowed + os.sep):
        raise PermissionError(
            f"{rel_path!r} is outside the one folder Synth may write. Writable: "
            f"{config.WRITABLE_DOCUMENTS}/ under Documents. The rest of Documents, and "
            f"anything reached through '..' or a symbolic link, is read-only.")

    real = os.path.join(parent, name)
    if os.path.islink(real):
        raise PermissionError(
            f"{rel_path!r} is a symbolic link. Synth writes real files only, because a link "
            f"can point anywhere on the disk.")
    return real


def relative(abs_path: str) -> str:
    """The path as source.native_id and document.path store it: relative to Documents.

    Derived from the resolved absolute path rather than from what the caller typed, so
    'markdown/./FILE.md' and 'markdown/FILE.md' cannot become two source rows for one file.
    """
    return os.path.relpath(abs_path, os.path.realpath(DOCUMENTS))


# ---------------------------------------------------------------- reading and keeping


def read_current(rel_path: str, abs_path: str) -> str:
    """The bytes actually on disk, downloading them from iCloud first if they are evicted."""
    if not extract.materialise(abs_path):
        raise FileNotFoundError(
            f"{rel_path!r} is evicted from this machine by iCloud and brctl could not "
            f"download it. Nothing was written. Without the real bytes the passage you are "
            f"replacing cannot be found. Wait a moment and try again.")
    size = os.path.getsize(abs_path)
    if size > config.MAX_DOCUMENT_BYTES:
        raise ValueError(
            f"{rel_path!r} is {size:,} bytes, over the {config.MAX_DOCUMENT_BYTES:,}-byte "
            f"limit for a file Synth rewrites in one piece. Nothing was written.")
    with open(abs_path, encoding="utf-8") as f:
        return f.read()


def read_back(abs_path: str) -> str:
    """Re-read a file Synth has just written, to confirm the bytes landed.

    Deliberately not read_current: no materialise (the file was written a moment ago, so it
    is local by definition) and no size ceiling (the point is to see what is actually there,
    including something unexpected and large).
    """
    with open(abs_path, encoding="utf-8") as f:
        return f.read()


def snapshot(rel_path: str, abs_path: str) -> dict | None:
    """Keep the current bytes before replacing them. None when the file is new."""
    if not os.path.exists(abs_path):
        return None
    digest = ingest.file_hash(abs_path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.makedirs(VERSIONS, exist_ok=True)
    backup = os.path.join(
        VERSIONS, f"{rel_path.replace('/', '__')}.{stamp}.{digest[:12]}.bak")
    with open(abs_path, "rb") as f:
        data = f.read()
    _write_bytes(backup, data)
    return {"path": rel_path, "backup": backup, "content_hash": digest, "bytes": len(data)}


# ---------------------------------------------------------------- writing


def _write_bytes(abs_path: str, data: bytes) -> None:
    """Atomic replace, with two additions for an iCloud folder.

    The temp name leads with a dot so ingest.candidates() skips it — it skips anything
    starting with '.' — which means a crash between write and replace can never leave behind
    a file that gets ingested as a document of its own. fsync before replace because this
    folder uploads to his phone, and a half-flushed file must not be what syncs.
    """
    tmp = os.path.join(os.path.dirname(abs_path),
                       "." + os.path.basename(abs_path) + ".synthtmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, abs_path)


def write_atomic(abs_path: str, text: str) -> None:
    _write_bytes(abs_path, text.encode("utf-8"))


# ---------------------------------------------------------------- the derived copies


def reindex(conn, rel_path: str, abs_path: str) -> dict:
    """Bring the text cache and the database copy back in step with the file.

    Extraction goes through extract.extract, the same function the ingest path uses, so
    document.text after a write is identical to what `synth index` would produce from the
    same file. Storing the raw file string instead would make every later ingest see a
    change that is not there — the file has a trailing newline that _clean strips.
    """
    digest = ingest.file_hash(abs_path)
    text = extract.extract(abs_path)

    cached = ingest.cached_text_path(digest)
    if not os.path.exists(cached):
        os.makedirs(ingest.TEXT_CACHE, exist_ok=True)
        _write_bytes(cached, text.encode("utf-8"))

    src_id = db.upsert_source(conn, "file", rel_path,
                              detail=os.path.basename(rel_path), content_hash=digest)
    doc_id = _upsert_document(conn, src_id, rel_path, text)
    conn.commit()
    return {"document_id": doc_id, "source_id": src_id, "content_hash": digest,
            "chars": len(text), "cached_text": cached}


def _upsert_document(conn, src_id: int, rel_path: str, text: str) -> int:
    """Insert or refresh one document row, keeping document_fts in step by hand.

    document_fts is an external-content FTS5 table with only document_ai (INSERT) and
    document_ad (DELETE) triggers — there is no AFTER UPDATE, unlike entity_au. So an UPDATE
    has to maintain the index itself, and the 'delete' command must be handed the OLD column
    values or the index keeps postings pointing at text that is no longer there, which
    surfaces later as 'database disk image is malformed' on an unrelated search.

    That maintenance lives here rather than in a schema trigger simply because the trigger
    does not exist. An earlier note here justified it differently -- that nothing in the repo
    calls db.init() or db.migrate(), so a trigger added to sql/schema.sql would never reach
    synth.db -- and that is not true: `synth migrate` is cmd_migrate, and the schema file and
    the live database were checked against each other and match in both directions. The line
    below is right; the reason given for it was wrong, which is how a correct line gets
    deleted by someone who checks the reason.
    """
    title = os.path.basename(rel_path)
    row = conn.execute("SELECT id, title, text FROM document WHERE source_id = ?",
                       (src_id,)).fetchone()
    if row is None:
        # document_ai maintains FTS for an insert; doing it here too would double-index.
        cur = conn.execute(
            "INSERT INTO document (source_id, path, title, text, chars) VALUES (?,?,?,?,?) "
            "RETURNING id", (src_id, rel_path, title, text, len(text)))
        doc_id = cur.fetchone()[0]
        _mark_for_embedding(conn, doc_id, text)
        return doc_id

    # UPDATE, never DELETE+INSERT: enrichment.document_id is a foreign-key primary key on
    # document(id) and foreign_keys is ON, so deleting the row would either fail outright or
    # lose the marker saying this document already went through the expensive LLM pass.
    conn.execute(
        "UPDATE document SET path = ?, title = ?, text = ?, chars = ?, indexed_at = ? "
        "WHERE id = ?", (rel_path, title, text, len(text), db.now(), row["id"]))
    conn.execute("INSERT INTO document_fts (document_fts, rowid, title, text) "
                 "VALUES ('delete', ?, ?, ?)", (row["id"], row["title"], row["text"]))
    conn.execute("INSERT INTO document_fts (rowid, title, text) VALUES (?,?,?)",
                 (row["id"], title, text))
    _mark_for_embedding(conn, row["id"], text)
    return row["id"]


def _mark_for_embedding(conn, doc_id: int, text: str) -> None:
    """Queue this document for a vector, next to the FTS maintenance above.

    Here for the same reason the FTS maintenance is here: this is the one function every
    document write passes through, so it is the only place the semantic index can be kept in
    step without a second list of callers to remember.

    It only ENQUEUES. Embedding runs through a model that serves one request at a time over a
    tunnel to another machine, and indexer.build calls this for hundreds of rows inside the
    sweep's write transaction -- doing the work here would hold the SQLite write lock for
    minutes. embed.mark is also a no-op when the text has not actually changed, which is what
    stops every sweep from re-embedding all 1,924 documents: the indexer reconsiders every
    row on every pass whether or not it wrote anything.
    """
    try:
        from synth import embed
        embed.mark(conn, "document", doc_id, db.text_hash(text or ""))
    except Exception:
        # A document that cannot be queued for a vector is still a document that was written.
        # Search degrades; indexing must not fail.
        pass


# ---------------------------------------------------------------- undo


def restore(conn, before: dict) -> str:
    """Put a saved previous version back. The Python half of db.undo."""
    rel_path = before["path"]
    abs_path = resolve(rel_path)          # the guard applies to undo too
    backup = before.get("backup") or ""
    if not os.path.exists(backup):
        raise FileNotFoundError(
            f"the saved previous version of {rel_path!r} is gone from {backup}. Synth will "
            f"not guess at the old text. Recover it from iCloud version history or Time "
            f"Machine.")
    with open(backup, "rb") as f:
        data = f.read()
    if hashlib.sha256(data).hexdigest()[:32] != before["content_hash"]:
        raise ValueError(
            f"the saved previous version of {rel_path!r} no longer hashes to what was "
            f"recorded when it was saved; refusing to restore it.")
    # Keep what is being replaced, so undoing an undo works and no version is ever lost.
    snapshot(rel_path, abs_path)
    _write_bytes(abs_path, data)
    index = reindex(conn, rel_path, abs_path)
    return (f"{rel_path} — {index['chars']} chars restored to the file, the text cache and "
            f"the search index")
