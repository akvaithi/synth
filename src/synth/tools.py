"""The tool surface.

What is left of Synth after the mail, calendar, reminder and brief machinery was removed:
his personal context, the documents indexed out of iCloud Drive, and the tools that edit the
markdown masters. Everything here is SQLite or the filesystem -- nothing reaches an Apple
app, and nothing spends a token except enrich_documents, which only runs when he asks.

Reached two ways: the connector over HTTP, and `bin/synth call` on the VM.
"""
from __future__ import annotations

import os

from synth import actions, config, db, docwrite, facts, ingest


def _fts_escape(q: str) -> str:
    """FTS5 treats a lot of punctuation as syntax; quote each term so user text is literal."""
    terms = [t for t in "".join(c if c.isalnum() or c.isspace() else " " for c in q).split() if t]
    return " OR ".join(f'"{t}"' for t in terms) if terms else '""'


# ---------------------------------------------------------------- reads


def search_context(conn, query: str, limit: int = 20) -> dict:
    q = _fts_escape(query)
    out: dict = {"query": query}
    out["entities"] = [dict(r) for r in conn.execute(
        "SELECT e.id, e.kind, e.name, e.description, e.status FROM entity_fts f "
        "JOIN entity e ON e.id = f.rowid WHERE entity_fts MATCH ? "
        "ORDER BY rank LIMIT ?", (q, limit))]
    out["facts"] = [dict(r) for r in conn.execute(
        "SELECT a.id, a.predicate, a.value_text, a.value_date, a.confidence, "
        "  e.name AS entity, s.kind AS source_kind, s.native_id AS source "
        "FROM assertion_fts f JOIN assertion a ON a.id = f.rowid "
        "LEFT JOIN entity e ON e.id = a.entity_id LEFT JOIN source s ON s.id = a.source_id "
        "WHERE assertion_fts MATCH ? AND a.superseded_by IS NULL "
        "ORDER BY rank LIMIT ?", (q, limit))]
    out["documents"] = [dict(r) for r in conn.execute(
        "SELECT d.id, d.path, d.title, d.chars, snippet(document_fts, 1, '<<', '>>', ' … ', 24) AS excerpt "
        "FROM document_fts f JOIN document d ON d.id = f.rowid "
        "WHERE document_fts MATCH ? ORDER BY rank LIMIT ?", (q, limit))]
    out["links"] = [dict(r) for r in conn.execute(
        "SELECT l.id, l.url, l.title, l.kind FROM link_fts f JOIN link l ON l.id = f.rowid "
        "WHERE link_fts MATCH ? AND l.dismissed = 0 ORDER BY rank LIMIT ?", (q, limit))]
    return out


def get_entity(conn, name: str) -> dict:
    row = conn.execute(
        "SELECT * FROM entity WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row is None:
        near = [r["name"] for r in conn.execute(
            "SELECT e.name FROM entity_fts f JOIN entity e ON e.id = f.rowid "
            "WHERE entity_fts MATCH ? LIMIT 5", (_fts_escape(name),))]
        return {"found": False, "name": name, "did_you_mean": near}
    eid = row["id"]
    return {
        "found": True,
        "entity": dict(row),
        "facts": [dict(r) for r in conn.execute(
            "SELECT a.predicate, a.value_text, a.value_num, a.value_date, a.confidence, "
            "  a.mail_derived, a.observed_at, s.kind AS source_kind, s.native_id AS source "
            "FROM assertion a LEFT JOIN source s ON s.id = a.source_id "
            "WHERE a.entity_id = ? AND a.superseded_by IS NULL ORDER BY a.predicate", (eid,))],
        "related": [dict(r) for r in conn.execute(
            "SELECT g.relation, e2.kind, e2.name FROM edge g JOIN entity e2 ON e2.id = g.dst_id "
            "WHERE g.src_id = ? UNION ALL "
            "SELECT g.relation, e2.kind, e2.name FROM edge g JOIN entity e2 ON e2.id = g.src_id "
            "WHERE g.dst_id = ?", (eid, eid))],
        "obligations": [dict(r) for r in conn.execute(
            "SELECT title, due, status FROM obligation WHERE entity_id = ? ORDER BY due", (eid,))],
        "links": [dict(r) for r in conn.execute(
            "SELECT url, title, kind FROM link WHERE entity_id = ? AND dismissed = 0", (eid,))],
    }


def fact_history(conn, name: str, predicate: str) -> dict:
    row = conn.execute("SELECT id FROM entity WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row is None:
        return {"found": False}
    return {"found": True, "history": facts.history(conn, row["id"], predicate)}


def read_document(conn, doc_id: int = None, path: str = None, max_chars: int = 20000) -> dict:
    if doc_id:
        row = conn.execute("SELECT * FROM document WHERE id = ?", (doc_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM document WHERE path = ?", (path,)).fetchone()
    if row is None:
        return {"found": False}
    return {"found": True, "path": row["path"], "chars": row["chars"],
            "text": row["text"][:max_chars],
            "truncated": row["chars"] > max_chars}


def _unpack_state(d: dict) -> dict:
    """Return the before/after blobs as objects with their times localised.

    The audit surface is read straight into the brief's "what Synth did" section, and the
    times that matter there -- when a reminder it created is actually due -- live inside these
    JSON strings, where localize cannot reach them. Handed over as raw UTC they get converted
    by hand and land a day out: a reminder due Thursday 7pm was reported as Wednesday.
    """
    import json as _json
    for key in ("before_json", "after_json", "args_json"):
        raw = d.get(key)
        if not raw:
            continue
        try:
            obj = _json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            db.localize(obj, "due", "start", "end", "at")
        d[key[:-5]] = obj
    return d


def activity(conn, limit: int = 50) -> list[dict]:
    return db.localize([dict(r) for r in conn.execute(
        "SELECT id, at, action, target_kind, target_id, reason, undone_at "
        "FROM action_log ORDER BY id DESC LIMIT ?", (limit,))], "at", "undone_at")


def why(conn, action_id: int) -> dict:
    row = conn.execute("SELECT * FROM action_log WHERE id = ?", (action_id,)).fetchone()
    if row is None:
        return {"found": False}
    d = _unpack_state(db.localize(dict(row), "at", "undone_at"))
    if d.get("evidence_id"):
        ev = conn.execute("SELECT kind, native_id, detail FROM source WHERE id = ?",
                          (d["evidence_id"],)).fetchone()
        d["evidence"] = dict(ev) if ev else None
    return {"found": True, **d}


# ---------------------------------------------------------------- writes


def add_facts(conn, payload: dict, source_kind: str = "conversation",
              source_ref: str = "interview", mail_derived: bool = False,
              document_id: int | None = None) -> dict:
    """Record facts. When they came from a document, pass document_id so the provenance
    points at the file itself rather than at a generic 'conversation' source."""
    if document_id:
        row = conn.execute("SELECT source_id FROM document WHERE id = ?",
                           (document_id,)).fetchone()
        if row is None:
            raise ValueError(f"no document {document_id}")
        sid = row["source_id"]
    else:
        sid = db.upsert_source(conn, source_kind, source_ref)
    return facts.ingest_batch(conn, payload, source_id=sid, mail_derived=mail_derived)


def update_document(conn, path: str, old: str, new: str, reason: str, run_id=None) -> dict:
    """Replace one exact passage in one of Arun's files, on disk, where his phone sees it.

    `old` must appear exactly once. Zero matches and two matches are both refused and
    nothing is written, and that refusal is the whole safety mechanism: read_document
    truncates at max_chars and extract._clean truncates at 200,000, so a tool that accepted
    "the whole updated document" would let a model that had seen only the first 20,000
    characters silently delete the rest. An anchored replace cannot lose text it never saw,
    which is why there is no full-replace tool and none may be added.
    """
    # Before anything that can return or raise early — see the comment in create_reminder
    # for the bug that comes of checking the reason after the first early return.
    actions._require_reason(reason)
    if not old:
        raise ValueError(
            "old is required: paste the exact passage to replace, copied verbatim from "
            "read_document, including its punctuation and line breaks. update_document "
            "never replaces a whole file — that is how a truncated read destroys the tail.")
    if old == new:
        raise ValueError(
            "old and new are identical, so this write would change nothing. Never write to "
            "Arun's files to check how a tool behaves.")

    abs_path = docwrite.resolve(path)
    rel_path = docwrite.relative(abs_path)
    if not os.path.exists(abs_path):
        raise ValueError(
            f"{path!r} does not exist under Documents. Use create_document to make a new "
            f"file, or read_document to check the path you have.")

    text = docwrite.read_current(rel_path, abs_path)
    count = text.count(old)
    if count == 0:
        # Whitespace is the failure a model actually hits, so say so when that is the cause
        # rather than leaving it to guess at invisible characters.
        hint = ""
        if " ".join(text.split()).count(" ".join(old.split())) == 1:
            hint = (" The passage IS in the file but with different whitespace — copy it "
                    "exactly as read_document returned it, including line breaks and "
                    "indentation.")
        raise ValueError(
            f"the passage was not found in {path!r}. It must match byte for byte, including "
            f"line breaks, indentation, and the exact dashes and quote marks. Nothing was "
            f"written. Re-read the file with read_document and copy the passage from what "
            f"it returned." + hint)
    if count > 1:
        raise ValueError(
            f"the passage appears {count} times in {path!r}, so Synth cannot tell which one "
            f"you mean. Nothing was written. Extend `old` upward with the line or heading "
            f"above it until it is unique.")

    new_text = text.replace(old, new, 1)
    if len(new_text.encode("utf-8")) > config.MAX_DOCUMENT_BYTES:
        raise ValueError(
            f"the result would be {len(new_text.encode('utf-8')):,} bytes, over the "
            f"{config.MAX_DOCUMENT_BYTES:,}-byte limit. Nothing was written.")

    action_id, index = actions.write_document(
        conn, action="update_document", rel_path=rel_path, abs_path=abs_path, text=new_text,
        reason=reason, expect_hash=ingest.file_hash(abs_path), run_id=run_id,
        args={"path": path, "old": old, "new": new, "occurrences_replaced": 1})
    return {"written": True, "action_id": action_id, "path": rel_path,
            "document_id": index["document_id"],
            "chars_before": len(text), "chars_after": len(new_text),
            "undo": f"undo({action_id}) puts the previous version back"}


def append_document(conn, path: str, text: str, reason: str, run_id=None) -> dict:
    """Add text to the end of one of Arun's files, after a blank line.

    Nothing already in the file is touched, so this is the safe way to add an entry, a row
    or a new section.
    """
    actions._require_reason(reason)
    if not text or not text.strip():
        raise ValueError(
            f"text is required: append_document adds text to the end of {path!r}, and there "
            f"is nothing here to add.")

    abs_path = docwrite.resolve(path)
    rel_path = docwrite.relative(abs_path)
    if not os.path.exists(abs_path):
        raise ValueError(
            f"{path!r} does not exist under Documents. Use create_document to make a new "
            f"file, or read_document to check the path you have.")

    current = docwrite.read_current(rel_path, abs_path)
    new_text = current.rstrip("\n") + "\n\n" + text.strip() + "\n"
    if len(new_text.encode("utf-8")) > config.MAX_DOCUMENT_BYTES:
        raise ValueError(
            f"the result would be {len(new_text.encode('utf-8')):,} bytes, over the "
            f"{config.MAX_DOCUMENT_BYTES:,}-byte limit. Nothing was written.")

    action_id, index = actions.write_document(
        conn, action="append_document", rel_path=rel_path, abs_path=abs_path, text=new_text,
        reason=reason, expect_hash=ingest.file_hash(abs_path), run_id=run_id,
        args={"path": path, "text": text})
    return {"written": True, "action_id": action_id, "path": rel_path,
            "document_id": index["document_id"],
            "chars_before": len(current), "chars_after": len(new_text),
            "undo": f"undo({action_id}) removes the addition"}


def create_document(conn, path: str, text: str, reason: str, run_id=None) -> dict:
    """Create a new file. Refuses to overwrite anything that already exists."""
    actions._require_reason(reason)
    if not text or not text.strip():
        raise ValueError("text is required: create_document will not create an empty file.")

    abs_path = docwrite.resolve(path)
    rel_path = docwrite.relative(abs_path)
    if os.path.exists(abs_path):
        raise FileExistsError(
            f"{path!r} already exists. create_document never overwrites — use "
            f"update_document to change a passage, or append_document to add to the end.")
    # An evicted file is not visible to os.path.exists under its own name; iCloud leaves a
    # hidden .name.icloud stub in its place. Without this check create_document would
    # silently clobber a file whose contents are merely off the machine.
    stub = os.path.join(os.path.dirname(abs_path), "." + os.path.basename(abs_path) + ".icloud")
    if os.path.exists(stub):
        raise FileExistsError(
            f"{path!r} exists but its contents are evicted from this machine by iCloud. "
            f"create_document never overwrites. Read it first so iCloud downloads it, then "
            f"use update_document.")

    action_id, index = actions.write_document(
        conn, action="create_document", rel_path=rel_path, abs_path=abs_path,
        text=text if text.endswith("\n") else text + "\n", reason=reason, run_id=run_id,
        args={"path": path, "text": text})
    return {"created": True, "action_id": action_id, "path": rel_path,
            "document_id": index["document_id"], "chars": index["chars"],
            "note": "Synth never deletes; a file it created has to be removed by hand"}


def reindex_documents(conn, path: str = "") -> dict:
    """Pick up documents changed outside Synth, or added since the last pass.

    Costs nothing -- extraction is Python and the index is SQLite, with no model anywhere in
    it. Writes made through update_document and friends already refresh the index inline, so
    this is for files edited on his phone, in an editor, or dropped into the folder.
    """
    from synth import indexer

    if path:
        abs_path = docwrite.resolve(path)
        rel = docwrite.relative(abs_path)
        status, chars = ingest.ingest_file(conn, abs_path)
        conn.commit()
        return {"scope": rel, "extraction": status, "chars": chars,
                **indexer.build(conn)}
    scan = ingest.scan(conn)
    return {"scope": "everything under Documents", "scanned": scan, **indexer.build(conn)}


def enrich_documents(conn, extra_limit: int = 0, dry_run: bool = False) -> dict:
    """Read documents with a model and record the facts they state.

    The one tool here that spends tokens, and the only reason a model still runs on this
    machine at all. It never fires on a schedule -- it runs because Arun asked, from here or
    from `bin/synth enrich`. Each document is enriched once; `enrichment` remembers which.

    Start with dry_run to see what would be read and what it would cost in attention.
    """
    from synth import enrich

    docs = enrich.select(conn, extra_limit=extra_limit)
    if dry_run or not docs:
        return {"selected": len(docs), "dry_run": True,
                "documents": [{"id": d["id"], "path": d["path"], "chars": d["chars"]}
                              for d in docs]}
    results = enrich.run(conn, docs)
    return {"selected": len(docs), "batches": len(results),
            "errors": sum(1 for r in results if r.get("error")),
            "summaries": [r.get("summary", "")[:600] for r in results]}


def undo(conn, action_id: int) -> str:
    return db.undo(conn, action_id)
