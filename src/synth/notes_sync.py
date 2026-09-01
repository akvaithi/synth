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


# The last line of a rendered note, and the two are not interchangeable. The mirror's promise
# is only true inside the Synth folder, which is the one place the watcher polls; printing it
# on a recipe would promise a read-back that never happens.
MIRROR_FOOTER = "Rendered by Synth. Edit freely — corrections are read back."
NOTE_FOOTER = "Written by Synth."
FOOTERS = (MIRROR_FOOTER, NOTE_FOOTER)


def render_blocks(blocks: list) -> list[str]:
    parts = []
    for block in blocks:
        kind = block[0]
        if kind == "h":
            parts.append(f"<div><br></div><div><b>{_esc(block[1])}</b></div>")
        elif kind == "p":
            parts.append(f"<div>{_esc(block[1])}</div>")
        elif kind == "ul":
            items = "".join(f"<li>{_esc(i)}</li>" for i in block[1])
            parts.append(f"<ul>{items}</ul>")
    return parts


def to_html(title: str, blocks: list, footer: str = MIRROR_FOOTER) -> str:
    """Notes treats the first line of the body as the title, so it leads and no <h1> follows."""
    parts = [f"<div><b>{_esc(title)}</b></div>", "<div><br></div>"]
    parts.extend(render_blocks(blocks))
    if footer:
        parts.append("<div><br></div>")
        parts.append(f"<div><i>{_esc(footer)}</i></div>")
    return "\n".join(parts)


# Matches a rendered footer at the very end of a body, tolerantly: Notes rewrites HTML on
# save, so what comes back is the same text inside whatever tags and inline styles it decided
# on. Anchored to the end, because a footer's words appearing mid-note are Arun's, not ours.
_FOOTER_RE = re.compile(
    r"(?:<div[^>]*>\s*(?:<br\s*/?>)?\s*</div>\s*)*"
    r"<div[^>]*>(?:\s*<[^>]+>)*\s*(?:%s)\s*(?:</[^>]+>\s*)*</div>\s*$"
    % "|".join(re.escape(html.escape(f)) for f in FOOTERS),
    re.I)


def append_html(body: str, blocks: list, footer: str = NOTE_FOOTER) -> str:
    """Add rendered blocks to the end of a note's existing HTML, keeping what is already there.

    Deliberately not a re-render. html_to_text is lossy — a heading and a bullet both come
    back as a plain line — so rebuilding the note from its own text would silently flatten
    formatting Arun applied by hand. The existing markup is passed through untouched and only
    added to, which is the same reason update_document replaces an anchored passage instead of
    accepting a whole file.

    The old footer is lifted off the end first so appends do not stack one per edit. When it
    cannot be found — Notes rewrote it past recognition, or the note never had one — nothing
    is guessed at and no new footer is added.
    """
    trimmed, found = _FOOTER_RE.subn("", body or "")
    parts = [trimmed.rstrip(), "<div><br></div>"]
    parts.extend(render_blocks(blocks))
    if footer and found:
        parts.append("<div><br></div>")
        parts.append(f"<div><i>{_esc(footer)}</i></div>")
    return "\n".join(parts)


def blocks_from_markdown(text: str) -> list:
    """Turn plain markdown-ish text into the block tuples to_html renders.

    Three shapes only, because three is what Notes renders well through AppleScript: a '#'
    heading, a '- ' or '* ' bullet, and a paragraph. Consecutive bullets fold into one list,
    so a recipe's ingredients come out as a single <ul> rather than a run of one-item lists.

    Shared by the brief renderer and by create_note/append_note, so a note Synth writes is
    laid out the same way a brief is.
    """
    blocks: list = []
    for line in (text or "").split("\n"):
        body = line.rstrip()
        if not body:
            continue
        lead = body.lstrip()
        if lead.startswith("#"):
            blocks.append(("h", lead.lstrip("#").strip()))
        elif lead.startswith(("- ", "* ")):
            if blocks and blocks[-1][0] == "ul":
                blocks[-1][1].append(lead[2:])
            else:
                blocks.append(("ul", [lead[2:]]))
        else:
            blocks.append(("p", body))
    return blocks


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
    # Excluding housekeeping is not only tidiness here, it is what stops the loop. This note
    # renders the action log; rendering it logs a notes_update; that changed its own content
    # and guaranteed another re-render on the next sweep, for ever. With its own churn out of
    # the query the content settles and render() reports "unchanged".
    rows = conn.execute(
        "SELECT at, action, target_kind, reason, undone_at FROM action_log "
        f"WHERE NOT {db.HOUSEKEEPING} ORDER BY id DESC LIMIT 40"
    ).fetchall()
    blocks = [("p", "What Synth has done, newest first. Nothing here is hidden from you.")]
    blocks.append(("ul", [
        f"{db.local(r['at'])} — {r['action']} ({r['target_kind']}): {r['reason']}"
        + ("  [UNDONE]" if r["undone_at"] else "") for r in rows]))
    return "Synth — Activity", blocks


def doc_brief(conn) -> tuple[str, list]:
    """The latest brief, rendered where Arun will actually read it.

    Until Remote Control is connected there is no push channel, so the Notes mirror is the
    delivery mechanism, not merely an archive.
    """
    row = conn.execute(
        "SELECT trigger, started_at, summary FROM run_log WHERE job = 'brief' "
        "AND status = 'ok' AND summary IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return "Synth — Brief", [("p", "No brief has run yet.")]
    blocks = [("p", f"{row['trigger']} brief — {db.local(row['started_at'])}"), ("p", "")]
    blocks.extend(blocks_from_markdown(row["summary"] or ""))
    return "Synth — Brief", blocks


DOCS = {
    "brief": doc_brief,
    "obligations": doc_obligations,
    "programs": doc_programs,
    "people": doc_people,
    "activity": doc_recent_actions,
}


# ---------------------------------------------------------------- sync


def reconcile(conn) -> dict:
    """Align stored hashes with what Notes actually holds.

    Needed because Notes rewrites HTML on save: the hash of what Synth sent never equals the
    hash of what Notes stored, so without this every note reads as edited forever and the
    mirror holds on phantom corrections. Only safe to call when you know the divergence is
    formatting rather than a real edit.
    """
    hashes = live_hashes()
    out = {}
    for row in conn.execute("SELECT doc, note_id FROM notes_mirror WHERE note_id IS NOT NULL"):
        h = hashes.get(row["note_id"])
        if h is None:
            out[row["doc"]] = "not found in folder"
            continue
        conn.execute("UPDATE notes_mirror SET last_written_hash = ?, last_seen_hash = ? "
                     "WHERE doc = ?", (h, h, row["doc"]))
        out[row["doc"]] = "reconciled"
    conn.commit()
    return out


def live_hashes() -> dict[str, str]:
    """Hash every note in the mirror folder, read through notes_dump.

    notes_get and notes_dump return subtly different HTML for the same note, so hashing one
    and comparing against the other marks every note as edited. The watcher uses dump, so
    dump is the single source of truth for hashing.
    """
    return {n["id"]: db.text_hash(n["body"])
            for n in call("notes_dump", folder=config.NOTES_FOLDER, timeout=300)}


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
    # Two hashes, and the distinction is the whole of this function's correctness. This one is
    # of what Synth COMPOSED. The one read back after writing is of what Notes STORED, which
    # differs because Notes rewrites HTML on save.
    #
    # They used to be compared against each other to decide whether anything had changed. They
    # are never equal, so "unchanged" was unreachable and every mirror note was rewritten on
    # every sweep whether or not a single fact had moved -- 142 writes a day, 100% of the
    # action log, which buried the decisions the log exists to show. The activity note made it
    # self-sustaining: it renders the action log, so its own re-render changed its content and
    # guaranteed the next one.
    render_hash = db.text_hash(html_to_text(body))

    row = conn.execute("SELECT * FROM notes_mirror WHERE doc = ?", (doc,)).fetchone()

    if row and row["note_id"]:
        if row["last_written_hash"] and row["last_seen_hash"] \
                and row["last_seen_hash"] != row["last_written_hash"] and not force:
            return "held: unread correction in the note"
        if row["last_render_hash"] == render_hash:
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

    # Read back what Notes actually stored, through the same path the watcher uses. This is
    # what a later poll is compared against to detect an edit of Arun's.
    stored_hash = render_hash
    try:
        stored_hash = live_hashes().get(note_id, render_hash)
    except Exception:
        pass  # keep the composed hash; reconcile will settle it

    conn.execute(
        "INSERT INTO notes_mirror (doc, note_id, note_name, last_written_hash, "
        "last_written_at, last_seen_hash, last_render_hash) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT (doc) DO UPDATE SET note_id=excluded.note_id, "
        "note_name=excluded.note_name, last_written_hash=excluded.last_written_hash, "
        "last_written_at=excluded.last_written_at, last_seen_hash=excluded.last_seen_hash, "
        "last_render_hash=excluded.last_render_hash",
        (doc, note_id, title, stored_hash, db.now(), stored_hash, render_hash),
    )
    conn.commit()
    return "written"


def sync_all(conn, run_id=None) -> dict:
    return {doc: render(conn, doc, run_id=run_id) for doc in DOCS}
