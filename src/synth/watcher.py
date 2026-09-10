"""Change detection, and putting what changed on the queue.

Detection is deliberately model-free: this decides only *whether* something moved, and
`enqueue` records it. Deciding what to do about it belongs to the reactor, and reading it
belongs to the worker.

Two detection routes, because Full Disk Access may or may not be granted to the daemon:
  * FSEvents (cheap, instant) for anything the daemon can see.
  * Content polling through AppleScript (works without FDA) for Mail and Notes.

Both still run. FDA was granted on 2026-09-10, so FSEvents work again -- but the daemon has
lost that grant before, silently, and the only symptom was that mail stopped being noticed
promptly while every AppleScript call kept working. Polling is what makes that survivable
rather than invisible, and `synth doctor` now reports the grant directly.
"""
from __future__ import annotations

import json
import os
import time

from synth import config, db
from synth.applekit import call

STATE = os.path.expanduser("~/Developer/synth/.state")
QUEUE = os.path.join(STATE, "changes.jsonl")
CURSOR = os.path.join(STATE, "watcher.json")
PENDING = os.path.join(STATE, "pending.json")


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


# ---------------------------------------------------------------- sources of change


# Past this, the queue is truncated once it has been fully read. It is a transient hand-off
# between synthd and this process, not a record of anything -- what mattered has already
# become a document row, an obligation or a mail_digest entry by the time the drain returns.
# Append-only, it had reached 831 KB and would have kept going for as long as the daemon runs.
QUEUE_MAX_BYTES = 5 * 1024 * 1024


def drain_fsevents(state: dict) -> list[dict]:
    """Consume anything synthd's FSEvents watchers appended since our last read."""
    if not os.path.exists(QUEUE):
        return []
    offset = state.get("queue_offset", 0)
    size = os.path.getsize(QUEUE)
    if size < offset:      # file was rotated or truncated
        offset = 0
    out = []
    with open(QUEUE) as f:
        f.seek(offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        state["queue_offset"] = f.tell()

    # Truncate only when nothing was appended between finishing the read and this moment.
    # The daemon writes to this file continuously, so the window between f.tell() and the
    # open-for-write is real, and anything landing in it would be discarded unread. Checking
    # the size again costs a stat and closes it: if the file has grown, leave it and truncate
    # on the next sweep instead, when the tail has been read too.
    if size > QUEUE_MAX_BYTES and state["queue_offset"] >= size:
        try:
            if os.path.getsize(QUEUE) == state["queue_offset"]:
                with open(QUEUE, "w"):
                    pass
                # drain_fsevents already restarts from 0 when `size < offset`; this is the
                # same condition, created deliberately rather than found.
                state["queue_offset"] = 0
        except OSError:
            pass    # a queue that cannot be truncated is not a reason to lose the events
    return out


def poll_mail(state: dict) -> list[dict]:
    """New inbox messages per account, identified by Message-ID rather than by count."""
    seen = state.setdefault("mail_seen", {})
    sentinels = state.setdefault("mail_sentinel", {})
    events = []
    for account in config.MAIL_ACCOUNTS:
        # Cheap sentinel first. Enumerating headers across four accounts costs well over a
        # minute; probing the newest id costs about two seconds, so the expensive path only
        # runs when the mailbox has actually moved.
        try:
            probe = call("mail_probe", account=account, timeout=240)
        except Exception as e:
            # Bare TimeoutError from the socket is not a SynthdError, and catching only the
            # latter meant one slow call killed the whole detection pass.
            events.append({"kind": "mail_error", "account": account, "detail": str(e)})
            continue
        fingerprint = f"{probe['count']}:{probe['newestId']}"
        if sentinels.get(account) == fingerprint:
            continue
        sentinels[account] = fingerprint
        try:
            msgs = call("mail_recent", account=account,
                        limit=config.MAIL_SCAN_LIMIT, timeout=300)
        except Exception as e:
            events.append({"kind": "mail_error", "account": account, "detail": str(e)})
            continue
        known = set(seen.get(account, []))
        fresh = [m for m in msgs if m["messageId"] and m["messageId"] not in known]
        if known:  # first run only primes the cursor; it is not a flood of "new" mail
            for m in fresh:
                events.append({
                    # The mailbox index travels with the event because reading a body needs
                    # one -- mail_get_at takes (account, index, messageId) and there is no
                    # lookup by Message-ID alone. Without it an event could be triaged on its
                    # subject and never opened. It is a hint, not an address: indexes shift
                    # on every arrival, so anything acting on this must re-resolve through
                    # triage.resolve_indexes first and treat this as the fallback.
                    "kind": "mail_new", "account": account, "index": m.get("index"),
                    "messageId": m["messageId"], "subject": m["subject"],
                    "sender": m["sender"], "receivedAt": m["receivedAt"],
                })
        seen[account] = [m["messageId"] for m in msgs if m["messageId"]]
    return events


def poll_notes(conn, state: dict) -> list[dict]:
    """A note whose live hash differs from what Synth last wrote is a correction from Arun."""
    try:
        notes = call("notes_dump", folder=config.NOTES_FOLDER, timeout=300)
    except Exception as e:
        return [{"kind": "notes_error", "detail": str(e)}]
    events = []
    from synth import notes_sync

    for n in notes:
        live = db.text_hash(n["body"])
        # A note whose content is exactly what Synth last wrote to it is not a correction,
        # whoever wrote it and however long ago. notes_mirror covers the six rendered
        # documents; synth_note covers everything else Synth writes -- create_note,
        # append_note, and the brief -- which was previously invisible here and came back as
        # an edit of Arun's that he never made.
        if notes_sync.written_by_synth(conn, n["id"], live):
            continue
        row = conn.execute(
            "SELECT doc, last_written_hash, last_seen_hash FROM notes_mirror WHERE note_id = ?",
            (n["id"],),
        ).fetchone()
        if row is None:
            # A note Synth did not create. Track it, but do not treat first sight as an edit.
            conn.execute(
                "INSERT OR IGNORE INTO notes_mirror (doc, note_id, note_name, last_seen_hash) "
                "VALUES (?,?,?,?)",
                (n["name"], n["id"], n["name"], live),
            )
            continue
        if row["last_written_hash"] and live != row["last_written_hash"] \
                and live != row["last_seen_hash"]:
            events.append({
                # The hash is part of this event's identity: a second, different edit is new
                # work, while re-detecting the same one every ninety seconds is not.
                "kind": "note_edited", "noteId": n["id"], "doc": row["doc"],
                "name": n["name"], "hash": live,
            })
        conn.execute(
            "UPDATE notes_mirror SET last_seen_hash = ?, last_edit_at = "
            "CASE WHEN ? THEN ? ELSE last_edit_at END WHERE note_id = ?",
            (live, live != (row["last_written_hash"] or live), db.now(), n["id"]),
        )
    conn.commit()
    return events


# ---------------------------------------------------------------- triage

# Only these are worth a model's attention. The `*_changed` kinds are FSEvents telling the
# watcher that a directory moved -- they are a signal to poll, not work in themselves, and
# Mail rewrites its store constantly. Treating them as work drove 800 reactor runs that
# produced 13 reminders between them.
ACTIONABLE = {"mail_new", "note_edited"}


def actionable(conn, events: list[dict]) -> list[dict]:
    """The events worth queueing, out of everything the detectors saw.

    `eventkit_changed` used to be suppressed here whenever Synth had written to Calendar or
    Reminders in the previous ten minutes, because the observer fires on Synth's own writes as
    loudly as on Arun's. That guard was wrong in both directions: it swallowed a genuine edit
    of his made just after a Synth write, and it failed open the moment the window passed.

    It is gone, and nothing replaces it, because the work this event schedules is a *diff*.
    tools.sync_obligations compares EventKit against the obligation table, and Synth's own
    create_reminder has already written that row -- so a notification Synth caused finds
    nothing and costs one free comparison. Idempotent by construction beats a timestamp
    heuristic, and the worker never hands this kind to a model unless the diff found something
    a person would care about.
    """
    out = [e for e in events if e.get("kind") in ACTIONABLE]
    if any(e.get("kind") == "eventkit_changed" for e in events):
        out.append({"kind": "eventkit_changed",
                    "detail": "Calendar or Reminders changed; reconcile and see what moved"})
    return out


# ---------------------------------------------------------------- debounce


def collect(conn) -> list[dict]:
    """Run every detector, and let none of them take the others down with it."""
    state = _load(CURSOR, {})
    events = []
    for name, fn in (("fsevents", lambda: drain_fsevents(state)),
                     ("mail", lambda: poll_mail(state)),
                     ("notes", lambda: poll_notes(conn, state))):
        try:
            events += fn()
        except Exception as e:
            events.append({"kind": f"{name}_error", "detail": f"{type(e).__name__}: {e}"})
    _save(CURSOR, state)
    return events


def dedupe_key(event: dict) -> str:
    """What makes two detections the same piece of work.

    A message is itself, whoever noticed it and however often. A note edit is the note AND its
    content, so a second, different edit is new work while re-detecting the same one is not --
    which is the difference between a correction being handled once and being handled every
    ninety seconds until Arun changes it again.
    """
    kind = event.get("kind", "unknown")
    if kind == "mail_new":
        return f"mail_new:{event.get('messageId')}"
    if kind == "note_edited":
        return f"note_edited:{event.get('noteId')}:{event.get('hash', '')}"
    if kind == "eventkit_changed":
        # Not identity: EventKit tells us something moved and nothing about what. One pending
        # row at a time is the whole point -- the work is a diff, and diffing twice for two
        # notifications finds the same thing.
        return "eventkit_changed"
    return f"{kind}:{event.get('detail', '')[:120]}"


def enqueue(conn, events: list[dict]) -> int:
    """Put detected work on the queue, once each.

    The queue is a table rather than the old pending.json because the worker and the sweep are
    separate processes: a JSON file has no way to say "claimed, in flight", and that is
    precisely how one event gets handled twice.
    """
    added = 0
    for e in actionable(conn, events):
        kind = e.get("kind", "unknown")
        key = dedupe_key(e)
        if kind == "eventkit_changed":
            # Identity is global for a message and per-outstanding-batch for this one, because
            # the work is a diff: two notifications and one notification find exactly the same
            # thing. A constant key against a UNIQUE column would mean EventKit could be
            # queued once in the lifetime of the database and never again, so the collapse is
            # against PENDING rows and the key carries a timestamp to stay unique afterwards.
            if conn.execute("SELECT 1 FROM reaction_queue WHERE kind = 'eventkit_changed' "
                            "AND done_at IS NULL LIMIT 1").fetchone():
                continue
            # Sub-second, because db.now() is second-resolution: a notification processed
            # and another arriving inside the same second produced the same key, hit the
            # UNIQUE constraint, and the second one was silently dropped.
            key = f"eventkit_changed:{time.time():.6f}"
        cur = conn.execute(
            "INSERT INTO reaction_queue (kind, dedupe_key, payload) VALUES (?,?,?) "
            "ON CONFLICT (dedupe_key) DO NOTHING",
            (kind, key, json.dumps(e, default=str)))
        added += cur.rowcount
    conn.commit()
    return added


def backfill_mail(conn, days: int = 14, limit: int = 0, verdicts=("urgent", "consider"),
                  dry_run: bool = False) -> dict:
    """Queue mail that was indexed before the reactor existed.

    poll_mail emits an event only for a Message-ID absent from its per-account cursor, and the
    sweep primed that cursor months ago. So everything already in the index is invisible to
    detection: turning the reactor on helps with mail that arrives afterwards and does nothing
    at all about what is already sitting there. On the day it went live that was 89 messages
    the free rules had called urgent or worth considering, going back a fortnight, including
    an RSVP marked "Action Required" and a support ticket awaiting a reply.

    Only what the free rules could not settle is offered. `digest` and `ignore` were decided by
    static rules and learned sender policy, and re-deciding them with a model is how a backlog
    becomes a flood.

    Bounded by age because a reminder for a deadline that has already passed is noise, and
    newest first because that is where the live obligations are.
    """
    rows = conn.execute(
        f"SELECT message_id, account, sender, subject, received_at, mail_index "
        f"FROM mail_digest WHERE verdict IN ({','.join('?' * len(verdicts))}) "
        f"AND received_at > datetime('now', ?) "
        f"ORDER BY received_at DESC",
        (*verdicts, f"-{days} days")).fetchall()

    already = {r[0] for r in conn.execute(
        "SELECT replace(dedupe_key, 'mail_new:', '') FROM reaction_queue "
        "WHERE kind = 'mail_new'")}
    fresh = [r for r in rows if r["message_id"] not in already]
    if limit:
        fresh = fresh[:limit]

    if dry_run:
        return {"would_queue": len(fresh), "considered": len(rows),
                "messages": [{"received": db.local(r["received_at"]),
                              "account": r["account"], "sender": r["sender"],
                              "subject": r["subject"]} for r in fresh]}

    added = 0
    for r in fresh:
        added += enqueue(conn, [{
            "kind": "mail_new", "account": r["account"], "index": r["mail_index"],
            "messageId": r["message_id"], "subject": r["subject"],
            "sender": r["sender"], "receivedAt": r["received_at"],
            "backfilled": True,
        }])
    return {"queued": added, "considered": len(rows), "skipped_already_seen":
            len(rows) - len(fresh)}


# The pending.json queue that used to live here -- accumulate, due, _has_urgent, keep_only
# and clear_pending -- is gone. reaction_queue replaced it: a file cannot express "claimed, in
# flight", and the worker and the sweep are separate processes, which is exactly how one event
# gets handled twice.
#
# What those functions knew is not lost. The two-tier debounce became reaction_queue's
# enqueued_at window, and `keep_only`'s rule -- that an event a run did not finish with stays
# queued -- is now worker.release(), written the same way and for the same reason: the code
# before it cleared the queue whenever the reactor returned, including when every call had
# been refused, and two days of detected mail was discarded that way and never came back.
