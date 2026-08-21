"""The tool surface, transport-independent.

Both the local reactor (stdio MCP) and the remote connector (HTTP MCP) call these. Reads are
safe to expose publicly; writes are wired only into the local transport, so a leaked endpoint
can embarrass but cannot act.
"""
from __future__ import annotations

import os

from synth import actions, config, db, facts
from synth.applekit import call

TEXT_CACHE = os.path.expanduser("~/Developer/synth/.state/text")
DOCUMENTS = os.path.expanduser(config.DOCUMENTS_ROOT)


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


def list_obligations(conn, status: str = "open", limit: int = 100) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT o.id, o.title, o.due, o.status, o.externally_set, o.ek_identifier, "
        "  e.name AS entity FROM obligation o LEFT JOIN entity e ON e.id = o.entity_id "
        "WHERE (? = 'all' OR o.status = ?) ORDER BY o.due IS NULL, o.due LIMIT ?",
        (status, status, limit))]


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


def today(conn) -> dict:
    return {
        "events": call("events", days=1, timeout=120),
        "reminders": call("reminders", timeout=180),
    }


def activity(conn, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, at, action, target_kind, target_id, reason, undone_at "
        "FROM action_log ORDER BY id DESC LIMIT ?", (limit,))]


def why(conn, action_id: int) -> dict:
    row = conn.execute("SELECT * FROM action_log WHERE id = ?", (action_id,)).fetchone()
    if row is None:
        return {"found": False}
    d = dict(row)
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


def import_reminders(conn, run_id=None) -> dict:
    """Record existing reminders as obligations, linked by EventKit identifier.

    This creates nothing in Reminders — it joins what is already there to the database, so
    that obligations and the reminders Arun actually looks at are the same objects. Matching
    to entities is left to a later pass; the link itself is what matters.
    """
    existing = {r["ek_identifier"] for r in conn.execute(
        "SELECT ek_identifier FROM obligation WHERE ek_identifier IS NOT NULL")}
    added = 0
    for r in call("reminders", timeout=300):
        if r["id"] in existing:
            continue
        conn.execute(
            "INSERT INTO obligation (title, due, status, ek_identifier, ek_kind, "
            "externally_set) VALUES (?,?,?,?,'reminder',0) "
            "ON CONFLICT (ek_identifier) DO NOTHING",
            (r["title"], r["due"] or None, "open", r["id"]))
        added += 1
    conn.commit()
    if added:
        db.log_action(conn, "import_reminders", "db",
                      f"link {added} existing reminder(s) to obligations by identifier",
                      run_id=run_id, after={"count": added})
    return {"imported": added, "already_linked": len(existing)}


def create_reminder(conn, title: str, reason: str, due: str = None, list: str = None,
                    notes: str = None, entity: str = None, externally_set: bool = False,
                    run_id=None, evidence_id=None) -> dict:
    if list and list not in config.MANAGED_LISTS:
        raise ValueError(f"{list!r} is not a managed list; expected one of {config.MANAGED_LISTS}")
    action_id, result = actions.create_reminder(
        conn, title=title, reason=reason, due=due, list=list, notes=notes,
        run_id=run_id, evidence_id=evidence_id)
    created = result["created"]
    eid = None
    if entity:
        row = conn.execute("SELECT id FROM entity WHERE name = ? COLLATE NOCASE",
                           (entity,)).fetchone()
        eid = row["id"] if row else None
    conn.execute(
        "INSERT INTO obligation (title, due, status, entity_id, ek_identifier, ek_kind, "
        "externally_set) VALUES (?,?,'open',?,?,'reminder',?) "
        "ON CONFLICT (ek_identifier) DO NOTHING",
        (title, created.get("due"), eid, created["id"], int(externally_set)))
    conn.commit()
    return {"action_id": action_id, "reminder": created}


def complete_reminder(conn, ek_identifier: str, reason: str, evidence_source: str = None,
                      run_id=None) -> dict:
    evidence_id = None
    if evidence_source:
        evidence_id = db.upsert_source(conn, "mail", evidence_source)
    action_id, result = actions.complete_reminder(
        conn, id=ek_identifier, reason=reason, run_id=run_id, evidence_id=evidence_id)
    conn.execute(
        "UPDATE obligation SET status='done', resolved_by=?, updated_at=? WHERE ek_identifier=?",
        (evidence_id, db.now(), ek_identifier))
    conn.commit()
    return {"action_id": action_id, "reminder": result.get("after")}


def update_reminder(conn, ek_identifier: str, reason: str, run_id=None, **fields) -> dict:
    """Edit an existing reminder. Partial: a field not named is never cleared.

    The first brief could not fix three reminders that had a date but no time, because no
    edit tool was exposed. An untimed reminder never appears in Calendar, which is where
    Arun reads his day, so this matters more than it looks.
    """
    action_id, result = actions.update_reminder(
        conn, id=ek_identifier, reason=reason, run_id=run_id, **fields)
    after = result.get("after") or {}
    if after.get("due"):
        conn.execute("UPDATE obligation SET due = ?, updated_at = ? WHERE ek_identifier = ?",
                     (after["due"], db.now(), ek_identifier))
        conn.commit()
    return {"action_id": action_id, "reminder": after}


def update_obligation(conn, ek_identifier: str, reason: str, externally_set: bool = None,
                      entity: str = None, status: str = None, run_id=None) -> dict:
    """Correct an obligation's metadata: whether the deadline is externally imposed, which
    entity it belongs to, its status. Does not touch the reminder itself."""
    row = conn.execute("SELECT * FROM obligation WHERE ek_identifier = ?",
                       (ek_identifier,)).fetchone()
    if row is None:
        raise ValueError(f"no obligation linked to {ek_identifier}")
    before = dict(row)
    sets, params = [], []
    if externally_set is not None:
        sets.append("externally_set = ?")
        params.append(int(externally_set))
    if status:
        sets.append("status = ?")
        params.append(status)
    if entity:
        er = conn.execute("SELECT id FROM entity WHERE name = ? COLLATE NOCASE",
                          (entity,)).fetchone()
        if er:
            sets.append("entity_id = ?")
            params.append(er["id"])
    if not sets:
        return {"changed": False}
    params.extend([db.now(), ek_identifier])
    conn.execute(f"UPDATE obligation SET {', '.join(sets)}, updated_at = ? "
                 f"WHERE ek_identifier = ?", params)
    action_id = db.log_action(conn, "update_obligation", "db", reason, run_id=run_id,
                              target_id=ek_identifier, before=before,
                              after={"externally_set": externally_set, "status": status,
                                     "entity": entity})
    conn.commit()
    return {"action_id": action_id, "changed": True}


def mail_links(conn, account: str, index: int, messageId: str, mailbox: str = "INBOX") -> dict:
    """Destination URLs and their anchor text, parsed from the message's HTML part."""
    from synth import maillinks
    return maillinks.extract(account, index, messageId, mailbox=mailbox)


def draft_email(conn, to: list[str], subject: str, body: str, reason: str,
                account: str = "Work", run_id=None) -> dict:
    """Creates a draft. There is no send path and none may be added."""
    if account not in config.MAIL_ACCOUNTS:
        raise ValueError(f"unknown account {account!r}")
    result = call("mail_draft", to=to, subject=subject, body=body, account=account, timeout=300)
    action_id = db.log_action(conn, "mail_draft", "draft", reason, run_id=run_id,
                              after={"to": to, "subject": subject, "account": account})
    return {"action_id": action_id, "draft": result}


def undo(conn, action_id: int) -> str:
    return db.undo(conn, action_id)
