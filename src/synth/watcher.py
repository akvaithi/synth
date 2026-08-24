"""Change detection.

Detection is deliberately model-free: this process decides only *whether* something moved
and hands a compact description to the reactor. That keeps a 2-minute cadence affordable,
because quota is spent only when there is genuinely something to think about.

Two detection routes, because Full Disk Access may or may not be granted to the daemon:
  * FSEvents (cheap, instant) for anything the daemon can see.
  * Content polling through AppleScript (works without FDA) for Mail and Notes.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from synth import config, db
from synth.applekit import call, SynthdError

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
        except SynthdError as e:
            events.append({"kind": "mail_error", "account": account, "detail": str(e)})
            continue
        fingerprint = f"{probe['count']}:{probe['newestId']}"
        if sentinels.get(account) == fingerprint:
            continue
        sentinels[account] = fingerprint
        try:
            msgs = call("mail_recent", account=account,
                        limit=config.MAIL_SCAN_LIMIT, timeout=300)
        except SynthdError as e:
            events.append({"kind": "mail_error", "account": account, "detail": str(e)})
            continue
        known = set(seen.get(account, []))
        fresh = [m for m in msgs if m["messageId"] and m["messageId"] not in known]
        if known:  # first run only primes the cursor; it is not a flood of "new" mail
            for m in fresh:
                events.append({
                    "kind": "mail_new", "account": account,
                    "messageId": m["messageId"], "subject": m["subject"],
                    "sender": m["sender"], "receivedAt": m["receivedAt"],
                })
        seen[account] = [m["messageId"] for m in msgs if m["messageId"]]
    return events


def poll_notes(conn, state: dict) -> list[dict]:
    """A note whose live hash differs from what Synth last wrote is a correction from Arun."""
    try:
        notes = call("notes_dump", folder=config.NOTES_FOLDER, timeout=300)
    except SynthdError as e:
        return [{"kind": "notes_error", "detail": str(e)}]
    events = []
    for n in notes:
        live = db.text_hash(n["body"])
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
                "kind": "note_edited", "noteId": n["id"], "doc": row["doc"],
                "name": n["name"],
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
# EventKit changes are actionable, but only when Synth did not cause them itself.
SELF_WINDOW_SECONDS = 600


def caused_by_synth(conn, seconds: int = SELF_WINDOW_SECONDS) -> bool:
    """Did Synth write to Calendar or Reminders just now?

    The EventKit observer fires on Synth's own writes as loudly as on Arun's, so without this
    every reminder Synth creates schedules another run to look at it.
    """
    row = conn.execute(
        "SELECT at FROM action_log WHERE target_kind IN ('reminder','event') "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return False
    try:
        last = datetime.fromisoformat(row["at"].replace("Z", "+00:00"))
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() < seconds


def actionable(conn, events: list[dict]) -> list[dict]:
    out = [e for e in events if e.get("kind") in ACTIONABLE]
    if any(e.get("kind") == "eventkit_changed" for e in events) and not caused_by_synth(conn):
        out.append({"kind": "eventkit_changed",
                    "detail": "Calendar or Reminders changed and Synth did not cause it"})
    return out


# ---------------------------------------------------------------- debounce


def collect(conn) -> list[dict]:
    state = _load(CURSOR, {})
    events = drain_fsevents(state)
    events += poll_mail(state)
    events += poll_notes(conn, state)
    _save(CURSOR, state)
    return events


def accumulate(events: list[dict]) -> dict:
    """Hold events in a pending set until the debounce window closes.

    A burst of edits collapses into one reactor run rather than one run per event, which is
    what makes a short cadence safe for a subscription quota.
    """
    pending = _load(PENDING, {"events": [], "first_at": None})
    if events:
        pending["events"].extend(events)
        pending["first_at"] = pending["first_at"] or time.time()
        _save(PENDING, pending)
    return pending


def due(pending: dict) -> bool:
    if not pending.get("events"):
        return False
    return (time.time() - (pending.get("first_at") or 0)) >= config.DEBOUNCE_SECONDS


def clear_pending():
    _save(PENDING, {"events": [], "first_at": None})
