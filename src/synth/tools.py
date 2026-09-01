"""The tool surface, transport-independent.

Both the local reactor (stdio MCP) and the remote connector (HTTP MCP) call these. Reads are
safe to expose publicly; writes are wired only into the local transport, so a leaked endpoint
can embarrass but cannot act.
"""
from __future__ import annotations

import os

from synth import actions, config, db, docwrite, facts, ingest
from synth.applekit import call


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
    return db.localize([dict(r) for r in conn.execute(
        "SELECT o.id, o.title, o.due, o.status, o.externally_set, o.ek_identifier, "
        "  e.name AS entity FROM obligation o LEFT JOIN entity e ON e.id = o.entity_id "
        "WHERE (? = 'all' OR o.status = ?) ORDER BY o.due IS NULL, o.due LIMIT ?",
        (status, status, limit))], "due")


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
        "timezone": db.tzname(),
        "events": db.localize(call("events", days=1, timeout=120), "start", "end", "lastModified"),
        "reminders": db.localize(call("reminders", timeout=180), "due", "lastModified"),
    }


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


def activity(conn, limit: int = 50, include_housekeeping: bool = False) -> list[dict]:
    """What Synth has done, newest first, with the mirror's own re-renders left out.

    Pass include_housekeeping to see them; nothing is deleted from the log, only from the view.
    """
    where = "" if include_housekeeping else f"WHERE NOT {db.HOUSEKEEPING} "
    return db.localize([dict(r) for r in conn.execute(
        "SELECT id, at, action, target_kind, target_id, reason, undone_at "
        f"FROM action_log {where}ORDER BY id DESC LIMIT ?", (limit,))], "at", "undone_at")


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


# ---------------------------------------------------------------- scheduling helpers


def _reminder_lists() -> list[dict]:
    """The reminder lists that exist right now, straight from EventKit."""
    return call("lists", timeout=120)


def _resolve_list(name: str | None) -> str | None:
    """The real title of a reminder list, or a refusal that names the ones that exist.

    There used to be an allowlist of four here and nothing domestic fitted in it — a grocery
    list, a chores list, anything household was refused before EventKit was reached. Synth now
    writes to any list Arun has and creates none, which is not a weaker rule so much as a
    differently placed one: synthd's findReminderCalendar throws for a name it cannot find, so
    "cannot create a list" is enforced by the daemon rather than by a name check here. What
    this adds is saying so early, in words the caller can act on, instead of as a socket error.
    """
    if not name:
        return None
    live = _reminder_lists()
    for c in live:
        if c["title"].casefold() == name.casefold():
            if not c.get("allowsModify", True):
                raise ValueError(
                    f"the reminder list {c['title']!r} is read-only — it is subscribed or "
                    f"shared, and EventKit will not let anything write to it.")
            return c["title"]
    writable = sorted(c["title"] for c in live if c.get("allowsModify", True))
    raise ValueError(
        f"there is no reminder list named {name!r}. Synth writes to any list that exists and "
        f"cannot create one — ask Arun to add it in Reminders first. Lists now: "
        f"{', '.join(writable)}.")


def _same_title(title: str, list_name: str | None, existing: list[dict]) -> dict | None:
    """An open reminder with exactly this title, on this list."""
    want = " ".join((title or "").split()).casefold()
    for r in existing:
        if list_name and r.get("list") != list_name:
            continue
        if " ".join((r.get("title") or "").split()).casefold() == want:
            return r
    return None


def _refuse_duplicate(title: str, due: str | None, list_name: str | None,
                      existing: list[dict] | None = None) -> tuple[dict | None, list]:
    """The duplicate check that fits the kind of thing being created, or None to go ahead.

    An appointment — something with a due *time* — is checked against everything near that
    time, because titles differ wildly for one commitment and proximity is the reliable
    signal. That is what stopped the second Dell Night.

    A line on a list is not an appointment, and near-time matching is exactly wrong for it: a
    grocery batch filed against one evening would refuse itself item by item, each new entry
    matching the last. So anything untimed, date-only, or on a list-style list gets an exact
    title match within its own list instead — which is the duplicate that actually happens on
    a list. Milk twice.
    """
    timed = bool(due) and "T" in due
    if timed and list_name not in config.LIST_STYLE_LISTS:
        from synth import agenda as _a
        try:
            check = _a.already_scheduled(title, due)
        except Exception:
            check = {"matches": [], "time_conflicts": []}
        overlaps = check.get("time_conflicts") or []
        if check.get("matches"):
            m = check["matches"][0]
            return {"created": False, "refused": "already scheduled", "match": m,
                    "advice": (f"{m['kind']} {m['title']!r} is already at {m['when']}, "
                               f"{m['minutes_apart']} minutes from the proposed time, and "
                               f"they share {m['shared_words']}. Do nothing unless this is "
                               f"genuinely a different commitment, in which case pass "
                               f"force=true and say why in the reason.")}, overlaps
        # Something close in time sharing no word is an overlap, not a duplicate. It used to
        # refuse the write; now it rides along on the success so it can be reported.
        return None, overlaps

    dup = _same_title(title, list_name, existing if existing is not None
                      else call("reminders", timeout=240))
    if dup:
        return {"created": False, "refused": "already on the list",
                "match": {"kind": "reminder", "title": dup["title"], "list": dup["list"],
                          "id": dup["id"], "due": dup.get("due") or None},
                "advice": (f"{dup['title']!r} is already on {dup['list']}. Do nothing unless "
                           f"this is genuinely a second one, in which case pass force=true "
                           f"and say why in the reason.")}, []
    return None, []


def _file_obligation(conn, title: str, due, ek_id: str, entity_id=None,
                     externally_set: bool = False) -> None:
    """Record a reminder as an obligation — unless it is a line on a list.

    An obligation is something owed, and it shows up as one: in list_obligations, in the
    Notes mirror, in every brief until it is closed. A grocery item is not owed to anyone, and
    twenty-nine of them would bury the four things that are.
    """
    conn.execute(
        "INSERT INTO obligation (title, due, status, entity_id, ek_identifier, ek_kind, "
        "externally_set) VALUES (?,?,'open',?,?,'reminder',?) "
        "ON CONFLICT (ek_identifier) DO NOTHING",
        (title, due, entity_id, ek_id, int(externally_set)))


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
                    force: bool = False, run_id=None, evidence_id=None) -> dict:
    """Create a reminder, refusing to duplicate something already there.

    The check runs here rather than only in the prompt because prompts are advice and this
    is a rule. Two concurrent reactor batches created the same USAC reminder twice; a
    structural check makes that impossible instead of merely discouraged. Which check applies
    depends on whether this is an appointment or a line on a list — see _refuse_duplicate.
    """
    # Validate the reason before anything else. The duplicate check returns early, and with
    # the order reversed a placeholder reason slipped through unexamined whenever the
    # proposed time happened to clash.
    actions._require_reason(reason)
    lst = _resolve_list(list)
    overlaps = []
    if not force:
        refusal, overlaps = _refuse_duplicate(title, due, lst)
        if refusal:
            return refusal
    action_id, result = actions.create_reminder(
        conn, title=title, reason=reason, due=due, list=lst, notes=notes,
        run_id=run_id, evidence_id=evidence_id)
    created = result["created"]
    eid = None
    if entity:
        row = conn.execute("SELECT id FROM entity WHERE name = ? COLLATE NOCASE",
                           (entity,)).fetchone()
        eid = row["id"] if row else None
    if lst not in config.LIST_STYLE_LISTS:
        _file_obligation(conn, title, created.get("due"), created["id"], eid, externally_set)
    conn.commit()
    # Same shape as the refusal path, so a caller can always read `created` to know.
    out = {"created": True, "action_id": action_id, "reminder": created}
    if overlaps:
        out["time_conflicts"] = overlaps
        out["note"] = ("This shares the hour with something already scheduled but nothing in "
                       "its name, so it was created. Tell Arun about the overlap rather than "
                       "treating it as a duplicate.")
    return out


def create_reminders(conn, titles: list, reason: str, list: str = None, due: str = None,
                     notes: str = None, entity: str = None, force: bool = False,
                     run_id=None, evidence_id=None) -> dict:
    """File a batch of reminders onto one list in a single call.

    A grocery run is twenty-nine items, and twenty-nine create_reminder calls is twenty-nine
    trips through a tool schema for one errand. The list is resolved once and the reason is
    checked once, both applying to the whole batch — which is the honest shape, because one
    reason is what Arun would want to read against all of them.

    What is deliberately *not* batched is the audit trail: every item still gets its own
    action_log row, so retract_reminder and undo work per item exactly as they do for a single
    write. A batch that could only be undone wholesale would be a worse tool than the loop it
    replaces.
    """
    actions._require_reason(reason)
    names = [" ".join(t.split()) for t in (titles or []) if t and str(t).strip()]
    if not names:
        raise ValueError(
            "titles is required: the reminders to file, as a list of strings.")
    if len(names) > config.MAX_BATCH_REMINDERS:
        raise ValueError(
            f"{len(names)} titles is over the {config.MAX_BATCH_REMINDERS}-item limit for one "
            f"batch. Nothing was created — split it, or look again at what produced this many.")
    lst = _resolve_list(list)

    eid = None
    if entity:
        row = conn.execute("SELECT id FROM entity WHERE name = ? COLLATE NOCASE",
                           (entity,)).fetchone()
        eid = row["id"] if row else None

    # One reminders read for the whole batch. Checking each item on its own would be a
    # 240-second EventKit fetch per grocery item, which is most of what the batch is here to
    # avoid, and the answers would not differ.
    existing = [] if force else call("reminders", timeout=240)

    created, skipped, action_ids, seen = [], [], [], set()
    for title in names:
        key = title.casefold()
        if key in seen:
            skipped.append({"title": title, "why": "listed twice in this batch"})
            continue
        seen.add(key)
        if not force:
            dup = _same_title(title, lst, existing)
            if dup:
                skipped.append({"title": title, "why": f"already on {dup['list']}",
                                "id": dup["id"]})
                continue
        action_id, result = actions.create_reminder(
            conn, title=title, reason=reason, due=due, list=lst, notes=notes,
            run_id=run_id, evidence_id=evidence_id)
        made = result["created"]
        if lst not in config.LIST_STYLE_LISTS:
            _file_obligation(conn, title, made.get("due"), made["id"], eid)
        created.append(made)
        action_ids.append(action_id)
    conn.commit()
    return {"list": lst, "created": created, "skipped": skipped, "action_ids": action_ids,
            "counts": {"created": len(created), "skipped": len(skipped)}}


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


def create_event(conn, title: str, start: str, reason: str, end: str = None,
                 calendar: str = None, location: str = None, notes: str = None,
                 all_day: bool = False, force: bool = False,
                 run_id=None, evidence_id=None) -> dict:
    """Put an event on the calendar, refusing to duplicate one that is already there.

    The guard matters more here than anywhere else: the reminders Arun objected to were all
    for things already on his calendar, because Synth had no way to see that an invitation
    had been accepted. Creating the event blind would make the same mistake in the other
    direction — two Dell Nights instead of one.
    """
    actions._require_reason(reason)
    cal = calendar or config.DEFAULT_CALENDAR
    if cal not in config.MANAGED_CALENDARS:
        raise ValueError(
            f"{cal!r} is not a managed calendar; expected one of {config.MANAGED_CALENDARS}")
    if not force:
        from synth import agenda as _a
        try:
            check = _a.already_scheduled(title, start)
        except Exception:
            check = {"matches": []}
        if check.get("matches"):
            m = check["matches"][0]
            return {"created": False, "refused": "already scheduled",
                    "match": m,
                    "advice": (f"{m['kind']} {m['title']!r} is already at {m['when']}, "
                               f"{m['minutes_apart']} minutes from the proposed start. "
                               f"Assume the invitation was already accepted. Create this "
                               f"only if it is genuinely a separate commitment, with "
                               f"force=true and the reason saying why.")}
    action_id, result = actions.create_event(
        conn, title=title, start=start, reason=reason, calendar=cal, end=end,
        notes=notes, location=location, allDay=all_day,
        run_id=run_id, evidence_id=evidence_id)
    created = result["created"]
    # No obligation row. An event is a commitment, not a task, and nothing reconciles event
    # state back from EventKit — an 'open' row for a meeting that simply happened would sit
    # in every brief as overdue, which is the exact bug sync_obligations was written to fix.
    out = {"created": True, "action_id": action_id, "event": created}
    try:
        from synth import agenda as _a
        clash = _a.conflicts(created["start"], minutes=45)
        if clash.get("conflicts"):
            out["conflicts"] = clash["conflicts"]
            out["note"] = ("This lands on top of something already scheduled. It was still "
                           "created — say so in the brief so Arun can decide.")
    except Exception:
        pass
    return out


def update_event(conn, ek_identifier: str, reason: str, run_id=None, **fields) -> dict:
    """Edit an existing event. Partial: a field not named is never cleared.

    Reversible — db.REVERSALS restores the previous title, times, location and notes from
    the snapshot taken before the write.
    """
    action_id, result = actions.update_event(
        conn, id=ek_identifier, reason=reason, run_id=run_id, **fields)
    return {"action_id": action_id, "event": result.get("after")}


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


def sync_obligations(conn, run_id=None) -> dict:
    """Reconcile obligation status against what Reminders actually holds.

    Synth updated status only when Synth did the completing, so anything Arun ticked off
    himself stayed 'open' in the database and kept appearing in briefs as overdue. Nine
    obligations were nagging him about work he had already finished.

    EventKit is the source of truth for state here -- for the due date and title as much as
    for completion -- and the database holds the reasoning.
    """
    live = {r["id"]: r for r in call("reminders", includeCompleted=True, timeout=300)}
    completed, vanished, reopened, redated = [], [], [], []
    for o in conn.execute(
        "SELECT ek_identifier, title, due, status FROM obligation "
        "WHERE ek_identifier IS NOT NULL AND ek_kind = 'reminder'"
    ).fetchall():
        r = live.get(o["ek_identifier"])
        if r is None:
            if o["status"] in ("open", "waiting"):
                conn.execute("UPDATE obligation SET status='dropped', updated_at=? "
                             "WHERE ek_identifier=?", (db.now(), o["ek_identifier"]))
                vanished.append(o["title"])
            continue
        if r["completed"] and o["status"] in ("open", "waiting"):
            conn.execute("UPDATE obligation SET status='done', updated_at=? "
                         "WHERE ek_identifier=?", (db.now(), o["ek_identifier"]))
            completed.append(o["title"])
        elif not r["completed"] and o["status"] == "done":
            # He un-ticked it; it is live again.
            conn.execute("UPDATE obligation SET status='open', updated_at=? "
                         "WHERE ek_identifier=?", (db.now(), o["ek_identifier"]))
            reopened.append(o["title"])

        # The date and the title, not only the status. This reconciled completion alone, so a
        # reminder Arun rescheduled in the app kept Synth's date for ever: "Register to vote in
        # College Station" sat in the database as 28 September while Reminders had said
        # 2 September since the 25th of August. list_obligations sorts on this column, so a
        # stale date does not misreport one row -- it puts every row in the wrong order and
        # anything reasoning about urgency reads it backwards.
        live_due = r.get("due") or None
        live_title = (r.get("title") or "").strip() or o["title"]
        if live_due != o["due"] or live_title != o["title"]:
            conn.execute(
                "UPDATE obligation SET due=?, title=?, updated_at=? WHERE ek_identifier=?",
                (live_due, live_title, db.now(), o["ek_identifier"]))
            if live_due != o["due"]:
                redated.append(f"{live_title[:44]}: {db.local(o['due']) or 'no date'} -> "
                               f"{db.local(live_due) or 'no date'}")
    conn.commit()
    if completed or vanished or reopened or redated:
        # Name them in the reason itself. "9 completed by Arun" told the brief a number but
        # not which, so it had to report that it could not say -- and Arun's standing rule is
        # that nothing Synth does goes unnamed.
        named = "; ".join(t[:48] for t in (completed + reopened)[:12])
        db.log_action(conn, "sync_obligations", "db",
                      f"reconciled against Reminders: {len(completed)} completed by Arun, "
                      f"{len(vanished)} deleted, {len(reopened)} reopened, "
                      f"{len(redated)} re-dated"
                      + (f" — {named}" if named else "")
                      + (f" | dates: {'; '.join(redated[:6])}" if redated else ""),
                      run_id=run_id,
                      after={"completed": completed, "vanished": vanished,
                             "reopened": reopened, "redated": redated})
    return {"completed_by_arun": completed, "deleted": vanished, "reopened": reopened,
            "redated": redated}


def agenda(conn, start: str = "", end: str = "", date: str = "") -> dict:
    """Everything already committed across a span of local days.

    One call per span, not one per day. Reading a week used to cost seven calls and fourteen
    daemon round trips; it now costs one and two.

    `date` is accepted as an alias for `start` so anything written against the one-day version
    keeps working unchanged.
    """
    from synth import agenda as _a
    first = start or date
    if not first:
        raise ValueError("start is required: a local date as YYYY-MM-DD.")
    return _a.agenda_range(first, end or None)


def already_scheduled(conn, title: str, when: str, window_minutes: int = 240) -> dict:
    """Whether this commitment is already on the calendar or in reminders."""
    from synth import agenda as _a
    return _a.already_scheduled(title, when, window_minutes)


def conflicts(conn, start: str, minutes: int = 30) -> dict:
    """What overlaps a proposed slot."""
    from synth import agenda as _a
    return _a.conflicts(start, minutes)


def find_free_slot(conn, date: str, minutes: int = 30, earliest_hour: int = 8,
                   latest_hour: int = 21, buffer_minutes: int = None) -> dict:
    """First free slot on a day, avoiding events and other reminders."""
    from synth import agenda as _a
    buf = _a.DEFAULT_BUFFER if buffer_minutes is None else buffer_minutes
    return _a.find_free_slot(date, minutes, earliest_hour, latest_hour, buf)


def free_slots(conn, start: str = "", end: str = "", minutes: int = 45,
               earliest_hour: int = 8, latest_hour: int = 21,
               buffer_minutes: int = None) -> dict:
    """Every opening of at least `minutes` across a span of days.

    find_free_slot answers "when could this go today". Anything recurring asks a different
    question — where are all the gaps this week — and building that by hand out of seven days
    of agenda is the work this saves.
    """
    from synth import agenda as _a
    if not start:
        raise ValueError("start is required: a local date as YYYY-MM-DD.")
    buf = _a.DEFAULT_BUFFER if buffer_minutes is None else buffer_minutes
    return _a.free_slots(start, end or None, minutes, earliest_hour, latest_hour, buf)


def mail_recent(conn, account: str, limit: int = 25, mailbox: str = "INBOX") -> list:
    """Inbox or any other mailbox. Use mailbox='Sent Mail' to check what has been replied to."""
    return call("mail_recent", account=account, limit=limit, mailbox=mailbox, timeout=300)


def mail_read(conn, account: str, index: int, messageId: str, mailbox: str = "INBOX") -> dict:
    return call("mail_get_at", account=account, index=index, messageId=messageId,
                mailbox=mailbox, timeout=600)


def mail_attachments(conn, account: str, index: int, messageId: str,
                     mailbox: str = "INBOX") -> dict:
    return call("mail_attachments", account=account, index=index, messageId=messageId,
                mailbox=mailbox, timeout=300)


def _note_folders() -> list[str]:
    return list(call("notes_folders", timeout=180))


def _resolve_note_folder(name: str | None) -> str:
    """The real name of a Notes folder to write into, or a refusal naming the ones that exist.

    The same rule as reminder lists — write into anything that is there, create nothing — but
    unlike Reminders it has to be enforced on this side. synthd's notesCreate calls
    notesEnsureFolder, so without this check a typo would quietly make a folder rather than
    fail, and Arun would find a "Recipies" sitting next to his "Recipes".
    """
    want = (name or config.DEFAULT_NOTE_FOLDER).strip()
    live = _note_folders()
    match = next((f for f in live if f.casefold() == want.casefold()), None)
    if match is None:
        raise ValueError(
            f"there is no Notes folder named {want!r}. Synth writes into any folder that "
            f"exists and cannot create one — make it in Notes first. Folders now: "
            f"{', '.join(sorted(live))}.")
    if any(match.casefold() == p.casefold() for p in config.PROTECTED_NOTE_FOLDERS):
        raise PermissionError(
            f"{match!r} is not Synth's to write into. {config.NOTES_FOLDER} is the mirror of "
            f"the database: notes_sync owns those notes and re-renders them, so a note added "
            f"by hand is either written over or read as an unread correction that stops the "
            f"mirror. Put it in another folder.")
    return match


def list_notes(conn, folder: str = "") -> dict:
    """What is in a Notes folder: names and openings, not bodies.

    A dump with full bodies is large and almost never what was wanted — read_note fetches the
    one that turns out to matter. Reading is unrestricted, the mirror folder included; it is
    only writing that carves the mirror out.
    """
    want = (folder or config.DEFAULT_NOTE_FOLDER).strip()
    live = _note_folders()
    match = next((f for f in live if f.casefold() == want.casefold()), None)
    if match is None:
        return {"found": False, "folder": want, "folders": sorted(live)}
    from synth.notes_sync import html_to_text
    notes = []
    for n in call("notes_dump", folder=match, timeout=300):
        text = html_to_text(n.get("body", "")).strip()
        notes.append({"id": n["id"], "name": n.get("name", ""), "chars": len(text),
                      "preview": " ".join(text.split())[:120]})
    notes.sort(key=lambda n: (n["name"] or "").casefold())
    return {"found": True, "folder": match, "count": len(notes), "notes": notes}


def read_note(conn, doc: str = "", note_id: str = "", folder: str = "",
              name: str = "") -> dict:
    """Read a note's current body.

    Three ways in: `doc` for one of the mirror documents, which is the channel for corrections
    Arun types into the Synth folder; `note_id` for one already identified; `folder` with
    `name` for anything else.
    """
    from synth.notes_sync import html_to_text
    if doc and not note_id:
        row = conn.execute("SELECT note_id FROM notes_mirror WHERE doc = ?", (doc,)).fetchone()
        if row is None or not row["note_id"]:
            return {"found": False, "doc": doc}
        note_id = row["note_id"]
    if not note_id and name:
        listed = list_notes(conn, folder)
        if not listed.get("found"):
            return listed
        want = " ".join(name.split()).casefold()
        hits = [n for n in listed["notes"]
                if " ".join((n["name"] or "").split()).casefold() == want]
        if not hits:
            return {"found": False, "folder": listed["folder"], "name": name,
                    "did_you_mean": [n["name"] for n in listed["notes"][:8]]}
        if len(hits) > 1:
            # Two notes really can share a name -- Recipes holds two called Black Bean Soup.
            # Picking one silently would be a guess about which recipe he meant, so hand back
            # both and let the caller choose by id.
            return {"found": False, "folder": listed["folder"], "name": name,
                    "ambiguous": hits,
                    "error": f"{len(hits)} notes in {listed['folder']} are called {name!r}. "
                             f"Read one by note_id."}
        note_id = hits[0]["id"]
    if not note_id:
        return {"found": False, "error": "doc, note_id, or folder and name is required"}
    n = call("notes_get", id=note_id, timeout=180)
    return {"found": True, "id": note_id, "name": n.get("name"),
            "text": html_to_text(n.get("body", ""))}


def create_note(conn, name: str, body: str, reason: str, folder: str = "",
                run_id=None) -> dict:
    """Create a note in Notes. Refuses to overwrite one that is already there.

    `body` is written the way a brief is — '#' headings, '- ' bullets, paragraphs — so what
    lands in Notes reads as prose rather than as markdown source.
    """
    actions._require_reason(reason)
    if not name or not name.strip():
        raise ValueError("name is required: the note's title, which is also its first line.")
    if not body or not body.strip():
        raise ValueError("body is required: create_note will not make an empty note.")
    dest = _resolve_note_folder(folder)
    title = " ".join(name.split())

    clash = next((n for n in call("notes_dump", folder=dest, timeout=300)
                  if " ".join((n.get("name") or "").split()).casefold() == title.casefold()),
                 None)
    if clash is not None:
        raise FileExistsError(
            f"a note called {title!r} is already in {dest}. create_note never overwrites — "
            f"append to it with append_note and id {clash['id']}, or pick another name.")

    from synth import notes_sync
    html = notes_sync.to_html(title, notes_sync.blocks_from_markdown(body),
                              footer=notes_sync.NOTE_FOOTER)
    created = call("notes_create", folder=dest, name=title, body=html, timeout=300)
    action_id = db.log_action(conn, "notes_create", "note", reason, run_id=run_id,
                              target_id=created.get("id"),
                              after={"id": created.get("id"), "name": title, "folder": dest})
    return {"created": True, "action_id": action_id, "id": created.get("id"),
            "name": title, "folder": dest,
            "note": "Synth never deletes; a note it created has to be removed by hand"}


def append_note(conn, note_id: str, text: str, reason: str, run_id=None) -> dict:
    """Add to the end of an existing note. Nothing already in it is touched.

    The existing markup is passed through and only added to, never rebuilt — html_to_text is
    lossy, so re-rendering a note from its own text would flatten headings and lists Arun may
    have made by hand. Same reasoning as update_document's anchored replace.

    Reversible: actions.notes_update records the previous body and db.REVERSALS puts it back.
    """
    actions._require_reason(reason)
    if not note_id:
        raise ValueError("note_id is required — find it with list_notes.")
    if not text or not text.strip():
        raise ValueError("text is required: there is nothing here to add.")

    from synth import notes_sync
    row = conn.execute("SELECT doc FROM notes_mirror WHERE note_id = ?", (note_id,)).fetchone()
    # notes_mirror also holds a row for every note the watcher has merely *seen* in the Synth
    # folder, so the presence of a row proves nothing. Only the rendered documents are barred.
    if row is not None and row["doc"] in notes_sync.DOCS:
        raise PermissionError(
            f"{row['doc']!r} is a mirror note, rendered from the database. An append would be "
            f"written over on the next sweep, and would read as an unread correction until it "
            f"was — which stops the mirror re-rendering at all. Record the fact with add_facts "
            f"instead.")

    current = call("notes_get", id=note_id, timeout=180)
    html = notes_sync.append_html(current.get("body", ""),
                                  notes_sync.blocks_from_markdown(text))
    action_id, _result = actions.notes_update(
        conn, id=note_id, body=html, reason=reason, run_id=run_id)
    return {"appended": True, "action_id": action_id, "id": note_id,
            "name": current.get("name"),
            "undo": f"undo({action_id}) puts the previous body back"}


def accept_correction(conn, doc: str, reason: str, run_id=None) -> dict:
    """Mark a note's correction as read, so the mirror stops holding and re-renders.

    Call this only after the correction has actually been recorded with add_facts.
    """
    row = conn.execute("SELECT note_id FROM notes_mirror WHERE doc = ?", (doc,)).fetchone()
    if row is None:
        raise ValueError(f"no mirrored note for {doc!r}")
    n = call("notes_get", id=row["note_id"], timeout=180)
    from synth.notes_sync import html_to_text
    h = db.text_hash(html_to_text(n.get("body", "")))
    conn.execute("UPDATE notes_mirror SET last_written_hash = ?, last_seen_hash = ? "
                 "WHERE doc = ?", (h, h, doc))
    db.log_action(conn, "accept_correction", "note", reason, run_id=run_id,
                  target_id=row["note_id"])
    conn.commit()
    return {"doc": doc, "accepted": True}


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


def mail_links(conn, account: str, index: int, messageId: str, mailbox: str = "INBOX") -> dict:
    """Destination URLs and their anchor text, parsed from the message's HTML part."""
    from synth import maillinks
    return maillinks.extract(account, index, messageId, mailbox=mailbox)


def retract_reminder(conn, ek_identifier: str, reason: str, run_id=None) -> dict:
    """Remove a reminder Synth created by mistake.

    The only deletion Synth can perform, and it is gated on provenance: action_log must show
    that Synth created this exact reminder. Anything Arun made is refused outright. Cleaning
    up its own noise is Synth's responsibility; his reminders are not Synth's to touch.
    """
    made = conn.execute(
        "SELECT id FROM action_log WHERE action = 'create_reminder' AND target_id = ?",
        (ek_identifier,)).fetchone()
    if made is None:
        raise PermissionError(
            f"refusing to remove {ek_identifier}: no action_log entry shows Synth created it")
    result = call("delete_reminder", id=ek_identifier, timeout=180)
    action_id = db.log_action(conn, "retract_reminder", "reminder", reason, run_id=run_id,
                              target_id=ek_identifier, before=result.get("before"))
    conn.execute("UPDATE obligation SET status = 'dropped', updated_at = ? "
                 "WHERE ek_identifier = ?", (db.now(), ek_identifier))
    conn.commit()
    return {"action_id": action_id, "retracted": result.get("before", {}).get("title")}


def draft_email(conn, to: list[str], subject: str, body: str, reason: str,
                account: str = "Work", run_id=None) -> dict:
    """Creates a draft. There is no send path and none may be added."""
    if account not in config.MAIL_ACCOUNTS:
        raise ValueError(f"unknown account {account!r}")
    result = call("mail_draft", to=to, subject=subject, body=body, account=account, timeout=300)
    action_id = db.log_action(conn, "mail_draft", "draft", reason, run_id=run_id,
                              after={"to": to, "subject": subject, "account": account})
    return {"action_id": action_id, "draft": result}


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
        return {"scope": rel, "extraction": status, "chars": chars, **indexer.build(conn)}
    scan = ingest.scan(conn)
    return {"scope": "everything under Documents", "scanned": scan, **indexer.build(conn)}


def enrich_documents(conn, extra_limit: int = 0, dry_run: bool = False) -> dict:
    """Read documents with a model and record the facts they state.

    The one tool here that spends tokens beyond the cheap mail triage. A sweep runs nightly
    over whatever is new; this is for when he does not want to wait for it.
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
