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


def now_minus(seconds: int) -> str:
    """An ISO timestamp `seconds` in the past, in the same shape now() produces.

    Comparable as a string against anything now() wrote, which is what makes an age check on
    claimed_at work without parsing every row.
    """
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(
        timespec="seconds")


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


# Long enough to be unambiguous on its own. The date is not decoration: converting from UTC
# moves the day for anything after 7pm local, which is how a reminder due Wednesday 7:15 PM
# was read off a "2026-08-27T00:15:00Z" and reported as Thursday.
LOCAL_FMT = "%a %Y-%m-%d %-I:%M %p"


def tzname() -> str:
    """The zone the _local renderings are in, so a reader never has to assume."""
    return datetime.now().astimezone().tzname()


def localize(rows, *keys):
    """Add a readable local rendering beside each UTC timestamp handed to a model.

    The daemon speaks UTC with a Z suffix, which is right for comparing and storing and wrong
    for reading. Anything that hands a time to a model now carries both: the ISO value, which
    is what arithmetic and writes must keep using, and a `_local` string to quote to Arun.

    Asking a model to do the conversion itself is not a small ask made once -- it is the same
    ask on every row of every brief, and it only has to be forgotten once. On 2026-08-25 it
    was: a 1:50 PM class was reported at 6:50 PM, a 4:10 PM class at 9:10 PM, and a 10:00 AM
    lab visit at 3:00 PM, while a reminder in the same brief converted correctly. That mix is
    the signature of arithmetic done by hand.
    """
    for it in (rows if isinstance(rows, list) else [rows]):
        if not isinstance(it, dict):
            continue
        for k in keys:
            v = it.get(k)
            if isinstance(v, str) and v:
                it[f"{k}_local"] = local(v, LOCAL_FMT)
    return rows


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
    migrate(conn)
    return conn


def migrate(conn) -> list[str]:
    """Bring an existing database up to the current schema.

    `init` is all CREATE TABLE IF NOT EXISTS, so it silently does nothing to a table whose
    definition changed. Anything that alters an existing table belongs here, and every step
    must be safe to run twice.
    """
    done = []
    # Every statement in schema.sql is CREATE ... IF NOT EXISTS, so replaying the whole file
    # is safe and is what brings a NEW table into an existing database. Without this, a new
    # table had to be written twice -- once in schema.sql for a fresh install and once here
    # for the live one -- and the two definitions drift, which is the failure this avoids.
    # Only ALTERs to existing tables need their own step below.
    before = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()
    added = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")} - before
    if added:
        done.append(f"created {', '.join(sorted(added))}")

    # A run killed mid-flight leaves its row saying 'running' forever, which makes `status`
    # and the ledger both lie. Anything still marked running from a previous process is over.
    stale = conn.execute(
        "UPDATE run_log SET status = 'error', finished_at = ?, "
        "summary = COALESCE(summary, 'process ended before the run finished') "
        "WHERE status = 'running' AND started_at < datetime('now', '-2 hours')",
        (now(),)).rowcount
    if stale:
        conn.commit()
        done.append(f"run_log: closed {stale} abandoned run(s)")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(mail_digest)")}
    for col, decl in (("mail_index", "INTEGER"), ("links", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE mail_digest ADD COLUMN {col} {decl}")
            done.append(f"mail_digest: added {col}")
    # before/after record what the file looked like, which is enough to undo a write but not
    # enough to explain one. A document write logged as an append that behaved like a
    # replacement could not be diagnosed after the fact, because nothing recorded the
    # arguments the tool was actually handed.
    # Notes rewrites HTML on save, so the hash of what Synth sends never equals the hash of
    # what Notes stores. render() compared those two to decide whether anything had changed,
    # which could never be true: all five mirror notes were rewritten every sweep regardless
    # of their content, 142 writes a day, and the action log held nothing else. The composed
    # hash is now kept separately and compared against itself.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(notes_mirror)")}
    if "last_render_hash" not in cols:
        conn.execute("ALTER TABLE notes_mirror ADD COLUMN last_render_hash TEXT")
        done.append("notes_mirror: added last_render_hash")

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(action_log)")}
    if "args_json" not in cols:
        conn.execute("ALTER TABLE action_log ADD COLUMN args_json TEXT")
        done.append("action_log: added args_json")
    if done:
        conn.commit()

    # Supersession keyed on the exact predicate string, so a fact rewritten under a slightly
    # different name superseded nothing: the UIN was live three times over, the degree-audit
    # GPA twice. facts.live() reads predicate_key instead, which is why the column has to
    # exist and be filled before anything writes a fact.
    from synth import predicates
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(assertion)")}
    if "predicate_key" not in cols:
        conn.execute("ALTER TABLE assertion ADD COLUMN predicate_key TEXT")
        conn.commit()
    filled = conn.execute(
        "SELECT id, predicate FROM assertion WHERE predicate_key IS NULL").fetchall()
    if filled:
        conn.executemany("UPDATE assertion SET predicate_key = ? WHERE id = ?",
                         [(predicates.key(r["predicate"]), r["id"]) for r in filled])
        conn.execute("CREATE INDEX IF NOT EXISTS idx_assertion_live_key "
                     "ON assertion (entity_id, predicate_key) WHERE superseded_by IS NULL")
        conn.commit()
        done.append(f"assertion: added predicate_key, backfilled {len(filled)} row(s)")

    # Rows that now normalise to the same predicate. Safe, idempotent, and run every time:
    # nothing new should ever appear here, and if something does it is worth knowing.
    collapsed = predicates.collapse_same_key(conn)
    if collapsed:
        log_action(conn, "dedupe_predicates", "db",
                   f"collapsed {len(collapsed)} assertion(s) whose predicate normalises to one "
                   f"already live on the same entity — " + "; ".join(c[:60] for c in collapsed[:8]),
                   after={"collapsed": collapsed})
        done.append(f"assertion: collapsed {len(collapsed)} same-key duplicate(s)")

    # The duplicates already in the table. Once only -- the action_log row is the marker, and
    # it is also the audit trail, so the merge is as visible as any other write.
    if not conn.execute(
        "SELECT 1 FROM action_log WHERE action = 'dedupe_predicates' LIMIT 1"
    ).fetchone():
        mergeable, reported = predicates.clusters(conn)
        moved = predicates.merge(conn, mergeable)
        named = "; ".join(
            f"{c['keep']['predicate'][:40]} <- {len(c['supersede'])}" for c in mergeable[:8])
        log_action(conn, "dedupe_predicates", "db",
                   f"one-time predicate merge: {moved} assertion(s) in {len(mergeable)} "
                   f"cluster(s) superseded onto their newest spelling; {len(reported)} "
                   f"same-value cluster(s) left alone as too dissimilar to merge safely"
                   + (f" — {named}" if named else ""),
                   after={"merged": [
                       {"entity": c["entity"], "keep": c["keep"]["predicate"],
                        "superseded": [o["predicate"] for o in c["supersede"]]}
                       for c in mergeable],
                       "reported": [
                       {"entity": r["entity"],
                        "predicates": [m["predicate"] for m in r["members"]]}
                       for r in reported]})
        done.append(f"assertion: merged {moved} duplicate predicate spelling(s), "
                    f"{len(reported)} cluster(s) reported for review")
    # OAuth secrets were stored in the clear: the access token, the code and the client
    # secret were all replayable by anything that could read synth.db, which also holds the
    # UIN, the date of birth and the home address. They are digests now.
    #
    # The existing values are hashed IN PLACE rather than revoked, so the live connector keeps
    # working: the client still holds the plaintext, valid_token hashes what it presents, and
    # the two match. Guarded by its own action_log row -- hashing a digest a second time would
    # lock the connector out for good.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(oauth_token)")}
    for col in ("refresh_token", "refresh_expires_at"):
        if col not in cols:
            conn.execute(f"ALTER TABLE oauth_token ADD COLUMN {col} TEXT")
            done.append(f"oauth_token: added {col}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oauth_refresh "
                 "ON oauth_token (refresh_token) WHERE refresh_token IS NOT NULL")
    conn.commit()

    if not conn.execute(
        "SELECT 1 FROM action_log WHERE action = 'hash_oauth_secrets' LIMIT 1"
    ).fetchone():
        import hashlib

        def _digest(v):
            return hashlib.sha256(v.encode()).hexdigest()

        # A code is single-use and lives ten minutes; there is nothing worth carrying over,
        # and rewriting them would only risk mangling one mid-flight.
        dropped = conn.execute("DELETE FROM oauth_code").rowcount
        tokens = conn.execute("SELECT token FROM oauth_token").fetchall()
        for r in tokens:
            conn.execute("UPDATE oauth_token SET token = ? WHERE token = ?",
                         (_digest(r["token"]), r["token"]))
        clients = conn.execute(
            "SELECT client_id, client_secret FROM oauth_client "
            "WHERE client_secret IS NOT NULL").fetchall()
        for r in clients:
            conn.execute("UPDATE oauth_client SET client_secret = ? WHERE client_id = ?",
                         (_digest(r["client_secret"]), r["client_id"]))
        conn.commit()
        log_action(conn, "hash_oauth_secrets", "db",
                   f"one-time: {len(tokens)} access token(s) and {len(clients)} client "
                   f"secret(s) replaced by their SHA-256 digests, and {dropped} pending "
                   f"authorization code(s) cleared. Existing tokens keep working -- the "
                   f"client holds the plaintext and valid_token hashes what it presents.",
                   after={"tokens": len(tokens), "clients": len(clients),
                          "codes_cleared": dropped})
        done.append(f"oauth: hashed {len(tokens)} token(s) and {len(clients)} client secret(s)")

    # run_log.status gained 'skipped'. SQLite cannot alter a CHECK constraint, so the table
    # is rebuilt -- the log is the troubleshooting record and must not be dropped.
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='run_log'").fetchone()
    if sql and "'skipped'" not in sql["sql"]:
        conn.executescript("""
            PRAGMA foreign_keys=off;
            BEGIN;
            CREATE TABLE run_log_new (
                id            INTEGER PRIMARY KEY,
                job           TEXT NOT NULL,
                trigger       TEXT,
                started_at    TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at   TEXT,
                status        TEXT NOT NULL DEFAULT 'running'
                                CHECK (status IN ('running','ok','error','skipped')),
                summary       TEXT,
                detail        TEXT
            );
            INSERT INTO run_log_new SELECT id, job, trigger, started_at, finished_at,
                                           status, summary, detail FROM run_log;
            DROP TABLE run_log;
            ALTER TABLE run_log_new RENAME TO run_log;
            COMMIT;
            PRAGMA foreign_keys=on;
        """)
        done.append("run_log: status allows 'skipped'")
    return done


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


# The mirror re-rendering itself. Every write Synth makes is logged and that does not change
# -- but these are housekeeping, not decisions, and they are logged three times a sweep. Left
# in the default view they buried everything else: 142 of 142 actions in a day, so `activity`
# showed eight hours of mirror churn and not one thing Synth had decided. Filtered out of the
# views by default, still in the table for anyone who asks.
HOUSEKEEPING = "(action = 'notes_update' AND reason LIKE 're-render % from the database')"


ARG_VALUE_LIMIT = 4000


def _trim_args(args: dict) -> dict:
    """Record what a tool was asked to do without keeping a second copy of the file.

    A whole-file append would otherwise put its entire text in the audit log. The head of a
    string is what identifies an edit; the truncation is marked so a reader never mistakes a
    shortened value for the real argument.
    """
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > ARG_VALUE_LIMIT:
            out[k] = v[:ARG_VALUE_LIMIT] + f"... [{len(v) - ARG_VALUE_LIMIT} more chars]"
        else:
            out[k] = v
    return out


def log_action(conn, action: str, target_kind: str, reason: str, *,
               run_id: int | None = None, target_id: str | None = None,
               evidence_id: int | None = None, before=None, after=None,
               args: dict | None = None) -> int:
    """Record a mutation. `reason` is required — an unexplained write is a bug."""
    cur = conn.execute(
        "INSERT INTO action_log (run_id, action, target_kind, target_id, reason, "
        "evidence_id, before_json, after_json, args_json) VALUES (?,?,?,?,?,?,?,?,?) "
        "RETURNING id",
        (run_id, action, target_kind, target_id, reason, evidence_id,
         json.dumps(before) if before is not None else None,
         json.dumps(after) if after is not None else None,
         json.dumps(_trim_args(args)) if args is not None else None),
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
    # Deletes are reversed by building the thing again, which is not the same as putting it
    # back: EventKit and Notes both issue a new identifier on save, so anything that referred
    # to the old one no longer resolves. The tools say so in as many words rather than letting
    # the existence of an undo imply more than it does.
    "delete_reminder": ("create_reminder", lambda before: {
        "title": before["title"], "list": before.get("list"),
        "notes": before.get("notes"),
        **({"due": before["due"], "hasTime": before.get("hasTime", True)}
           if before.get("due") else {}),
    }),
    "delete_event": ("create_event", lambda before: {
        "title": before["title"], "start": before["start"], "end": before.get("end"),
        "calendar": before.get("calendar"), "location": before.get("location"),
        "notes": before.get("notes"), "allDay": before.get("allDay", False),
    }),
    "delete_note": ("notes_create", lambda before: {
        "name": before.get("name"), "body": before.get("body", ""),
    }),
}


# Not everything is reversed by a synthd call. A file write is undone by putting the previous
# bytes back and reindexing, which is Python, not AppleScript. Rather than widen REVERSALS
# into a two-shaped table, these get their own: same dispatch, same before_json contract, a
# different executor. Each takes (conn, before) and returns a sentence for the caller.
def _restore_document(conn, before):
    # Local import: docwrite imports db, so this cycle is real. Mirrors the local applekit
    # import in undo() below.
    from synth import docwrite

    return docwrite.restore(conn, before)


PY_REVERSALS = {
    "update_document": _restore_document,
    "append_document": _restore_document,
    # The one delete that genuinely restores rather than recreates: the bytes are in
    # .state/docversions and the path does not change, so undoing it is indistinguishable
    # from the file never having been deleted.
    "delete_document": _restore_document,
}


def undo(conn, action_id: int) -> str:
    """Reverse one logged action.

    A creation is still not reversed here, and that is now a choice rather than a limitation:
    delete_reminder, delete_event and delete_note exist for the tier Arun drives directly. But
    they are separate acts that he asks for by name. Turning `undo` into a delete would mean a
    single call could remove something of his as a side effect of tidying, and for two of the
    three that removal cannot be taken back the way an edit can.
    """
    from synth.applekit import call

    row = conn.execute(
        "SELECT * FROM action_log WHERE id = ?", (action_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no action {action_id}")
    if row["undone_at"]:
        return f"action {action_id} was already undone at {row['undone_at']}"
    if row["action"] in PY_REVERSALS:
        if not row["before_json"]:
            return f"action {action_id} has no recorded prior state, so it cannot be reversed"
        detail = PY_REVERSALS[row["action"]](conn, json.loads(row["before_json"]))
        conn.execute("UPDATE action_log SET undone_at = ? WHERE id = ?", (now(), action_id))
        conn.commit()
        return f"reversed action {action_id}: {detail}"
    if row["action"] not in REVERSALS:
        if row["action"].startswith("create_"):
            tool = {"reminder": "delete_reminder", "event": "delete_event",
                    "note": "delete_note", "document": "delete_document"
                    }.get(row["target_kind"])
            how = (f"Use {tool} if you want it gone" if tool
                   else "Remove it by hand if you want it gone")
            return (f"action {action_id} created a {row['target_kind']}, and undo does not "
                    f"delete. {how}: {row['target_id']}")
        return f"action {action_id} ({row['action']}) has no defined reversal"
    if not row["before_json"]:
        return f"action {action_id} has no recorded prior state, so it cannot be reversed"

    cmd, build = REVERSALS[row["action"]]
    call(cmd, **build(json.loads(row["before_json"])))
    conn.execute("UPDATE action_log SET undone_at = ? WHERE id = ?", (now(), action_id))
    conn.commit()
    return f"reversed action {action_id}: {row['action']} on {row['target_kind']}"
