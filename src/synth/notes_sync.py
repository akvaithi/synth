"""The Notes mirror: a readable, editable view of the database on every Apple device.

Two rules make the two-way sync safe:

1. **Never overwrite an unread correction.** If the live note differs from what Synth last
   wrote, Arun edited it. That edit is queued as a correction and the note is left alone
   until it has been read. Rendering over it would destroy the very thing we came for.
2. **The note is a rendered view, not a file mirror.** Notes rewrites HTML on save, so the
   comparison is a whitespace-normalised text hash, and a detected edit is handed to the
   model as a signal to interpret rather than a diff to replay.
"""
from __future__ import annotations

import html
import re

from synth import config, db
from synth.applekit import call


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def to_html(title: str, blocks: list) -> str:
    """Notes treats the first line of the body as the title, so it leads and no <h1> follows."""
    parts = [f"<div><b>{_esc(title)}</b></div>", "<div><br></div>"]
    for block in blocks:
        kind = block[0]
        if kind == "h":
            parts.append(f"<div><br></div><div><b>{_esc(block[1])}</b></div>")
        elif kind == "p":
            parts.append(f"<div>{_esc(block[1])}</div>")
        elif kind == "ul":
            items = "".join(f"<li>{_esc(i)}</li>" for i in block[1])
            parts.append(f"<ul>{items}</ul>")
    parts.append("<div><br></div>")
    parts.append("<div><i>Rendered by Synth. Edit freely — corrections are read back.</i></div>")
    return "\n".join(parts)


def html_to_text(body: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    text = re.sub(r"</(div|p|li|ul|ol|h\d)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


# ---------------------------------------------------------------- documents


def doc_obligations(conn) -> tuple[str, list]:
    rows = conn.execute(
        "SELECT o.title, o.due, o.status, o.externally_set, e.name AS entity "
        "FROM obligation o LEFT JOIN entity e ON e.id = o.entity_id "
        "WHERE o.status IN ('open','waiting') ORDER BY o.due IS NULL, o.due"
    ).fetchall()
    blocks = [("p", f"{len(rows)} open.")]
    hard = [r for r in rows if r["externally_set"]]
    soft = [r for r in rows if not r["externally_set"]]
    if hard:
        blocks.append(("h", "Real deadlines"))
        blocks.append(("ul", [f"{r['due'] or 'no date'} — {r['title']}"
                              + (f" ({r['entity']})" if r["entity"] else "") for r in hard]))
    if soft:
        blocks.append(("h", "Self-set targets"))
        blocks.append(("ul", [f"{r['due'] or 'no date'} — {r['title']}" for r in soft]))
    return "Synth — Obligations", blocks


def doc_programs(conn) -> tuple[str, list]:
    rows = conn.execute(
        "SELECT id, name, status, description FROM entity "
        "WHERE kind IN ('program','application','award') ORDER BY name"
    ).fetchall()
    blocks = []
    for r in rows:
        blocks.append(("h", r["name"] + (f" — {r['status']}" if r["status"] else "")))
        if r["description"]:
            blocks.append(("p", r["description"]))
        facts = conn.execute(
            "SELECT predicate, value_text, value_num, value_date, confidence "
            "FROM assertion WHERE entity_id = ? AND superseded_by IS NULL "
            "ORDER BY predicate", (r["id"],)
        ).fetchall()
        if facts:
            blocks.append(("ul", [
                f"{f['predicate']}: {f['value_text'] or f['value_date'] or f['value_num']}"
                + ("" if f["confidence"] >= 0.99 else f"  (confidence {f['confidence']:.0%})")
                for f in facts]))
    if not blocks:
        blocks = [("p", "Nothing recorded yet.")]
    return "Synth — Programs and Applications", blocks


def doc_people(conn) -> tuple[str, list]:
    rows = conn.execute(
        "SELECT id, name, description FROM entity WHERE kind = 'person' ORDER BY name"
    ).fetchall()
    blocks = [("ul", [f"{r['name']}" + (f" — {r['description']}" if r["description"] else "")
                      for r in rows])] if rows else [("p", "Nothing recorded yet.")]
    return "Synth — People", blocks


def doc_recent_actions(conn) -> tuple[str, list]:
    rows = conn.execute(
        "SELECT at, action, target_kind, reason, undone_at FROM action_log "
        "ORDER BY id DESC LIMIT 40"
    ).fetchall()
    blocks = [("p", "What Synth has done, newest first. Nothing here is hidden from you.")]
    blocks.append(("ul", [
        f"{r['at'][:16]} — {r['action']} ({r['target_kind']}): {r['reason']}"
        + ("  [UNDONE]" if r["undone_at"] else "") for r in rows]))
    return "Synth — Activity", blocks


DOCS = {
    "obligations": doc_obligations,
    "programs": doc_programs,
    "people": doc_people,
    "activity": doc_recent_actions,
}


# ---------------------------------------------------------------- sync


def pending_corrections(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT doc, note_id, note_name, last_edit_at FROM notes_mirror "
        "WHERE last_written_hash IS NOT NULL AND last_seen_hash IS NOT NULL "
        "AND last_seen_hash != last_written_hash"
    ).fetchall()
    return [dict(r) for r in rows]


def render(conn, doc: str, run_id=None, force: bool = False) -> str:
    from synth import actions

    title, blocks = DOCS[doc](conn)
    body = to_html(title, blocks)
    new_hash = db.text_hash(html_to_text(body))

    row = conn.execute("SELECT * FROM notes_mirror WHERE doc = ?", (doc,)).fetchone()

    if row and row["note_id"]:
        if row["last_written_hash"] and row["last_seen_hash"] \
                and row["last_seen_hash"] != row["last_written_hash"] and not force:
            return "held: unread correction in the note"
        if row["last_written_hash"] == new_hash:
            return "unchanged"
        actions.notes_update(conn, id=row["note_id"], body=body, name=title,
                             reason=f"re-render {doc} from the database", run_id=run_id)
        note_id = row["note_id"]
    else:
        created = call("notes_create", folder=config.NOTES_FOLDER, name=title,
                       body=body, timeout=180)
        note_id = created["id"]
        db.log_action(conn, "notes_create", "note", f"create {doc} mirror note",
                      run_id=run_id, target_id=note_id, after={"id": note_id, "doc": doc})

    conn.execute(
        "INSERT INTO notes_mirror (doc, note_id, note_name, last_written_hash, "
        "last_written_at, last_seen_hash) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT (doc) DO UPDATE SET note_id=excluded.note_id, "
        "note_name=excluded.note_name, last_written_hash=excluded.last_written_hash, "
        "last_written_at=excluded.last_written_at, last_seen_hash=excluded.last_seen_hash",
        (doc, note_id, title, new_hash, db.now(), new_hash),
    )
    conn.commit()
    return "written"


def sync_all(conn, run_id=None) -> dict:
    return {doc: render(conn, doc, run_id=run_id) for doc in DOCS}
