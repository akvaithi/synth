"""The process that turns "something arrived" into "something was done about it".

## Why a resident worker and not a launchd job per event

Three constraints, none of them about cost.

Ollama serves one model at a time, so two reactions racing each other thrash a model in and
out of memory. `synthd` is a single Unix socket and mail enumeration takes 15-600 seconds per
call, so concurrent callers queue behind each other and blow their timeouts. And FSEvents are
not messages -- `~/Library/Mail` fired 9,355 times in the period documents fired 1,307, because
Mail rewrites its store constantly.

So: one process, one queue, one lock. Reaction is near-instant without any of it being
parallel.

## The lock is the sweep's own

The worker takes `.state/sweep.lock`, blocking, exactly as the sweep and enrichment do. A
reaction and a sweep can then never both be inside synthd, inside Ollama, or inside a SQLite
write transaction. Waiting is right here where skipping is right for the sweep: the sweep runs
again in half an hour regardless, and a detected message does not go away.

## Detection lives here too

The sweep detects every thirty minutes, which is the wrong latency for mail. The worker runs
its own detection pass every ninety seconds; the expensive enumeration only happens when
`mail_probe` says a mailbox actually moved, so an idle machine costs four cheap probes a
minute and a half. The sweep keeps detecting as well, as the net that catches whatever the
worker missed while it was down.
"""
from __future__ import annotations

import json
import os
import time

from synth import agent, config, db, ollama, reactor, watcher
from synth.cli import LOCK, _exclusive

POLL_SECONDS = 5
DETECT_SECONDS = int(os.environ.get("SYNTH_DETECT_SECONDS", "90"))
DEBOUNCE_SECONDS = getattr(config, "DEBOUNCE_SECONDS", 15)
HEALTH_RETRY_SECONDS = 30
LOCK_WAIT_SECONDS = 900

# The ceiling across all runs in a day -- the number the 142-writes-a-day incident is about.
# **Zero means no limit**, which is how it is set: Arun asked for it unlimited on 2026-09-10,
# after watching it work a backlog of 89 messages and write almost nothing.
#
# What still bounds a runaway with this off: two acts per run, twelve turns, a ten-minute wall
# clock, repeat-call suppression, and .state/HALT. Those are per-run and structural. This one
# was the only ceiling counted across a day, so with it lifted the day is bounded by how much
# mail actually arrives.
MAX_WRITES_PER_DAY = int(os.environ.get("SYNTH_MAX_WRITES_PER_DAY", "0"))

MAX_ATTEMPTS = 3


# Reconciliation is not a write of Arun's. sync_obligations records what EventKit already
# says -- it creates nothing and changes nothing of his -- and it runs on every EventKit
# notification, so counting it would spend the daily cap on bookkeeping and leave nothing for
# the writes the cap exists to bound.
NOT_A_WRITE = ("sync_obligations",)


def writes_today(conn) -> int:
    """Actions a reactor run took since local midnight.

    Reminders, events and notes go through actions._do and leave an action_log row carrying
    the run that made them, so these are attributable and countable.

    Facts are NOT included, and the omission is deliberate rather than an oversight. add_facts
    calls facts.ingest_batch, which inserts assertions and commits without logging an action
    and without recording which run asked -- so an assertion cannot be attributed to the
    reactor rather than to the nightly enrichment pass, and counting all of today's would
    charge the reactor for enrichment's work. `facts_today` reports them separately instead of
    summing two things that are not the same and cannot be told apart.
    """
    placeholders = ",".join("?" * len(NOT_A_WRITE))
    return conn.execute(
        "SELECT count(*) FROM action_log a JOIN run_log r ON r.id = a.run_id "
        "WHERE r.job = 'reactor' AND a.at > datetime('now', 'start of day') "
        f"AND a.action NOT IN ({placeholders}) AND NOT {db.HOUSEKEEPING}",
        NOT_A_WRITE).fetchone()[0]


def facts_today(conn) -> int:
    """Assertions recorded today, by anything. See writes_today for why this is separate."""
    return conn.execute(
        "SELECT count(*) FROM assertion WHERE observed_at > datetime('now', 'start of day')"
    ).fetchone()[0]


def claim(conn, limit: int = 12) -> list[dict]:
    """Take the next batch of pending work, marking it in flight.

    Claiming is what a JSON file could not express, and the reason this is a table: the sweep
    and this process are separate, and without a claim the same message is reacted to twice.
    """
    rows = conn.execute(
        "SELECT id, kind, payload, attempts FROM reaction_queue "
        "WHERE done_at IS NULL AND claimed_at IS NULL AND attempts < ? "
        "AND enqueued_at <= datetime('now', ?) "
        "ORDER BY enqueued_at LIMIT ?",
        # A negative debounce would render as '--1 seconds', which SQLite treats as an invalid
        # modifier and quietly matches nothing -- a queue that never drains and never says why.
        (MAX_ATTEMPTS, f"-{max(0, DEBOUNCE_SECONDS)} seconds", limit)).fetchall()
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    conn.execute(f"UPDATE reaction_queue SET claimed_at = ?, claimed_by = ? "
                 f"WHERE id IN ({','.join('?' * len(ids))})",
                 (db.now(), os.getpid(), *ids))
    conn.commit()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except ValueError:
            payload = {"kind": r["kind"]}
        payload["_queue_id"] = r["id"]
        out.append(payload)
    return out


def release(conn, claimed: list[dict], handled: list[dict], error: str | None = None) -> dict:
    """Mark what was finished; put the rest back.

    An event not in `handled` was not dealt with, whatever else happened, and it goes back on
    the queue rather than being dropped. The old code cleared the queue whenever the reactor
    returned -- including when every call had been refused -- and two days of detected mail
    was discarded that way and never came back.
    """
    # Identity, not equality: reactor.react passes the very dicts it was given through to
    # `handled`, and two different messages can compare equal after a failed parse.
    done_ids = {e.get("_queue_id") for e in handled if e.get("_queue_id") is not None}
    finished, requeued, retired = 0, 0, 0
    for e in claimed:
        qid = e.get("_queue_id")
        if qid is None:
            continue
        if qid in done_ids:
            conn.execute("UPDATE reaction_queue SET done_at = ?, claimed_at = NULL "
                         "WHERE id = ?", (db.now(), qid))
            finished += 1
            continue
        row = conn.execute("SELECT attempts FROM reaction_queue WHERE id = ?",
                           (qid,)).fetchone()
        attempts = (row["attempts"] if row else 0) + 1
        if attempts >= MAX_ATTEMPTS:
            # A poison event must not spin the worker for ever. It is retired with its reason
            # recorded, not deleted, so `why` can still answer for it.
            conn.execute("UPDATE reaction_queue SET attempts = ?, claimed_at = NULL, "
                         "done_at = ?, last_error = ? WHERE id = ?",
                         (attempts, db.now(), error or "gave up after repeated attempts", qid))
            retired += 1
        else:
            conn.execute("UPDATE reaction_queue SET attempts = ?, claimed_at = NULL, "
                         "last_error = ? WHERE id = ?", (attempts, error, qid))
            requeued += 1
    conn.commit()
    return {"finished": finished, "requeued": requeued, "retired": retired}


def once(conn, dry_run: bool | None = None) -> dict:
    """One pass: detect if due, claim, react, release. Returns what happened."""
    claimed = claim(conn)
    if not claimed:
        return {"claimed": 0}

    capped = MAX_WRITES_PER_DAY > 0 and writes_today(conn) >= MAX_WRITES_PER_DAY
    if capped:
        dry_run = True

    error = None
    handled: list[dict] = []
    try:
        result = reactor.react(conn, claimed, dry_run=dry_run)
        handled = result["handled"]
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        result = {"runs": [], "writes": 0}
    outcome = release(conn, claimed, handled, error=error)
    return {"claimed": len(claimed), "writes": result.get("writes", 0),
            "capped": capped, "error": error, **outcome}


def loop(conn=None, dry_run: bool | None = None, forever: bool = True) -> dict:
    """Drain the queue as work appears. This is `synth work`."""
    conn = conn or db.connect()
    last_detect = 0.0
    passes = 0

    while True:
        passes += 1
        record_mode()
        if agent.halted():
            # The kill switch stops the reactor without unloading anything, so detection keeps
            # running and nothing is lost while it is down.
            time.sleep(30)
            if not forever:
                return {"halted": True}
            continue

        if time.time() - last_detect > DETECT_SECONDS:
            last_detect = time.time()
            try:
                watcher.enqueue(conn, watcher.collect(conn))
            except Exception as e:
                print(f"detection failed: {type(e).__name__}: {e}", flush=True)

        probe = ollama.health()
        if not probe.get("up"):
            # Do NOT claim while local inference is away: a claimed row is in flight, and an
            # outage that claimed and failed the whole queue would burn every row's retry
            # budget in an afternoon.
            ollama.record_health(conn, "ollama", probe)
            time.sleep(HEALTH_RETRY_SECONDS)
            if not forever:
                return {"waiting_on": "ollama"}
            continue
        ollama.record_health(conn, "ollama", probe)

        held = _exclusive(wait=LOCK_WAIT_SECONDS)
        if held is None:
            time.sleep(POLL_SECONDS)
            if not forever:
                return {"waiting_on": "lock"}
            continue
        try:
            result = once(conn, dry_run=dry_run)
        finally:
            held.close()

        if result.get("claimed"):
            print(json.dumps({"at": db.local(db.now()), **result}), flush=True)
        if not forever:
            return result
        time.sleep(POLL_SECONDS if result.get("claimed") else POLL_SECONDS * 3)


MODE_FILE = os.path.join(os.path.dirname(LOCK), "worker.json")


def record_mode() -> None:
    """Leave the worker's actual mode where another process can read it.

    `synth work --status` runs in a different process from the worker, so reading
    reactor.DRY_RUN there reports the SHELL's environment. The worker has
    SYNTH_REACTOR_DRY_RUN=1 from its plist and the shell does not, so status cheerfully said
    "dry_run: false" about a worker that was writing nothing -- which is exactly the fact a
    person checks this for.
    """
    tmp = MODE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"pid": os.getpid(), "dry_run": bool(reactor.DRY_RUN),
                   "at": db.now()}, f)
    os.replace(tmp, MODE_FILE)


def status(conn) -> dict:
    pending = conn.execute("SELECT count(*) FROM reaction_queue "
                           "WHERE done_at IS NULL").fetchone()[0]
    retired = conn.execute("SELECT count(*) FROM reaction_queue "
                           "WHERE done_at IS NOT NULL AND last_error IS NOT NULL"
                           ).fetchone()[0]
    try:
        with open(MODE_FILE) as f:
            mode = json.load(f)
    except (OSError, ValueError):
        mode = {"dry_run": None, "pid": None, "at": None}
    return {"pending": pending, "retired": retired, "writes_today": writes_today(conn),
            "facts_today": facts_today(conn),
            "cap": MAX_WRITES_PER_DAY or "unlimited", "halted": agent.halted(),
            "worker_dry_run": mode.get("dry_run"), "worker_pid": mode.get("pid"),
            "worker_last_pass": mode.get("at"), "lock": LOCK}
