"""Synth context database: connection, migrations, audit trail and undo."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

ROOT = os.path.expanduser("~/Developer/synth")
DB_PATH = os.path.join(ROOT, "synth.db")
SCHEMA_PATH = os.path.join(ROOT, "sql", "schema.sql")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local(ts: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Render a stored timestamp in local time.

    Everything is stored in UTC — SQLite's datetime('now') is UTC, and now() is explicit
    about it — but every human-facing surface must show local time. On a system whose whole
    job is deadlines, showing a UTC timestamp as if it were local is a real error, not a
    cosmetic one.
    """
    if not ts:
        return ""
    text = ts.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime(fmt)


def text_hash(s: str) -> str:
    """Hash of normalised text: whitespace-collapsed, so trivial reflow is not an edit."""
    normalised = " ".join(s.split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def connect(path: str = DB_PATH, timeout: float = 120.0) -> sqlite3.Connection:
    # A long busy timeout matters: an OCR pass running alongside an enrichment pass held the
    # write lock past 30s and killed the enrichment outright. Heavy writers should not be
    # run concurrently, but the timeout should survive it if they are.
    conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(path: str = DB_PATH) -> sqlite3.Connection:
    conn = connect(path)
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()
    return conn


# ---------------------------------------------------------------- provenance


def upsert_source(conn, kind: str, native_id: str, detail: str | None = None,
                  content_hash: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO source (kind, native_id, detail, content_hash) VALUES (?,?,?,?) "
        "ON CONFLICT (kind, native_id) DO UPDATE SET "
        "  detail = COALESCE(excluded.detail, source.detail), "
        "  content_hash = COALESCE(excluded.content_hash, source.content_hash) "
        "RETURNING id",
        (kind, native_id, detail, content_hash),
    )
    return cur.fetchone()[0]


# ---------------------------------------------------------------- audit


@contextmanager
def run(conn, job: str, trigger: str | None = None):
    """Wrap a job so every execution lands in run_log whether or not it changed anything."""
    cur = conn.execute(
        "INSERT INTO run_log (job, trigger) VALUES (?,?) RETURNING id", (job, trigger)
    )
    run_id = cur.fetchone()[0]
    conn.commit()
    try:
        yield run_id
    except Exception as e:
        conn.execute(
            "UPDATE run_log SET finished_at=?, status='error', detail=? WHERE id=?",
            (now(), f"{type(e).__name__}: {e}", run_id),
        )
        conn.commit()
        raise
    else:
        conn.execute(
            "UPDATE run_log SET finished_at=?, status='ok' WHERE id=?", (now(), run_id)
        )
        conn.commit()


def log_action(conn, action: str, target_kind: str, reason: str, *,
               run_id: int | None = None, target_id: str | None = None,
               evidence_id: int | None = None, before=None, after=None) -> int:
    """Record a mutation. `reason` is required — an unexplained write is a bug."""
    cur = conn.execute(
        "INSERT INTO action_log (run_id, action, target_kind, target_id, reason, "
        "evidence_id, before_json, after_json) VALUES (?,?,?,?,?,?,?,?) RETURNING id",
        (run_id, action, target_kind, target_id, reason, evidence_id,
         json.dumps(before) if before is not None else None,
         json.dumps(after) if after is not None else None),
    )
    action_id = cur.fetchone()[0]
    conn.commit()
    return action_id


# ---------------------------------------------------------------- undo

# Each entry maps an action to the synthd call that reverses it.
REVERSALS = {
    "complete_reminder": ("uncomplete_reminder", lambda before: {"id": before["id"]}),
    "uncomplete_reminder": ("complete_reminder", lambda before: {"id": before["id"]}),
    "update_reminder": ("update_reminder", lambda before: {
        "id": before["id"], "title": before["title"], "notes": before["notes"],
        **({"due": before["due"], "hasTime": before["hasTime"]} if before.get("due") else {}),
    }),
    "update_event": ("update_event", lambda before: {
        "id": before["id"], "title": before["title"], "start": before["start"],
        "end": before["end"], "location": before["location"], "notes": before["notes"],
    }),
    "notes_update": ("notes_update", lambda before: {
        "id": before["id"], "body": before["body"],
    }),
}


def undo(conn, action_id: int) -> str:
    """Reverse one logged action. Creations are not reversed — Synth never deletes."""
    from synth.applekit import call

    row = conn.execute(
        "SELECT * FROM action_log WHERE id = ?", (action_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no action {action_id}")
    if row["undone_at"]:
        return f"action {action_id} was already undone at {row['undone_at']}"
    if row["action"] not in REVERSALS:
        if row["action"].startswith("create_"):
            return (f"action {action_id} created a {row['target_kind']}; Synth never deletes. "
                    f"Remove it by hand if you want it gone: {row['target_id']}")
        return f"action {action_id} ({row['action']}) has no defined reversal"
    if not row["before_json"]:
        return f"action {action_id} has no recorded prior state, so it cannot be reversed"

    cmd, build = REVERSALS[row["action"]]
    call(cmd, **build(json.loads(row["before_json"])))
    conn.execute("UPDATE action_log SET undone_at = ? WHERE id = ?", (now(), action_id))
    conn.commit()
    return f"reversed action {action_id}: {row['action']} on {row['target_kind']}"
