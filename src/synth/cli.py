"""synth — command line entry point.

    synth sync           one free sweep: documents, obligations, mail index, Notes mirror
    synth ingest         read Documents, extract text, cache it
    synth index          load extracted document text into the searchable index
    synth ocr            OCR the scanned PDFs that had no text layer
    synth enrich         extract structured facts from documents (the one model pass)
    synth notes          re-render the Notes mirror
    synth reconcile      align stored note hashes with what Notes actually holds
    synth call <name> [json]  direct tool dispatch (see: synth call)
    synth serve          run the connector HTTP server (behind Cloudflare)
    synth log            what Synth has done
    synth why <id>       why it did one thing
    synth undo <id>      reverse one action
    synth status         health of every moving part
    synth budget [clear|reset <why>]  what it has spent, and what it may spend
"""
from __future__ import annotations

import json
import os
import sys

from synth import config, db


LOCK = os.path.expanduser("~/Developer/synth/.state/sweep.lock")
SYNC_MARK = os.path.expanduser("~/Developer/synth/.state/sync.json")


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def _exclusive():
    """Hold a lock for the whole reaction, or bail out.

    A run regularly outlasts the 120s launchd interval, so two passes overlapped and both
    created the same reminder -- one run's own summary says "a reminder already existed from
    a concurrent Synth process".
    """
    import fcntl
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def cmd_budget(args):
    from synth import budget
    conn = db.connect()
    if args and args[0] == "clear":
        budget.clear_backoff()
        print("backoff cleared")
    if args and args[0] == "reset":
        reason = " ".join(args[1:]) or "reset by hand"
        e = budget.set_epoch(conn, reason)
        print(f"ledger now counts from {e['since']} — {reason}")
    print(budget.summary(conn))
    return 0


def cmd_ingest(args):
    """Read Documents, extract text, cache it. Free: no model is involved."""
    from synth import ingest
    conn = db.connect()
    limit = None
    for a in args:
        if a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
    print(json.dumps(ingest.scan(conn, limit=limit), indent=2))
    return 0


def cmd_sync(args):
    """Keep every index current. The only thing that runs on its own.

    Nothing here reasons or acts on Arun's behalf. It notices what changed, records it, and
    stops -- documents re-indexed, obligations reconciled against Reminders, mail sorted into
    the digest, the Notes mirror re-rendered. The one part that touches a model is the Haiku
    pass over subject lines the static rules could not settle.
    """
    import time
    from synth import indexer, ingest, mailsync, notes_sync, tools, triage as tri, watcher

    held = _exclusive()
    if held is None:
        print(json.dumps({"skipped": "another sweep is already running"}))
        return 0
    conn = db.connect()
    out = {}
    # Watermark, taken before any work so a file written mid-sweep is caught next time
    # rather than missed. The minute of slack covers clock skew and iCloud writing an mtime
    # slightly behind the moment it finished.
    mark = _load_json(SYNC_MARK, {})
    since = mark.get("last_sweep")
    started = time.time() - 60
    full = "--full" in args

    events = []
    try:
        events = watcher.collect(conn)
    except Exception as e:
        out["detect"] = f"{type(e).__name__}: {e}"

    # Documents. Free. Only files touched since the last sweep, unless --full.
    try:
        out["ingest"] = ingest.scan(conn, since=None if full else since)
        out["index"] = indexer.build(conn)
        _save_json(SYNC_MARK, {"last_sweep": started})
    except Exception as e:
        out["documents"] = f"{type(e).__name__}: {e}"

    # Calendar and reminders. Free -- EventKit through the daemon, then SQLite.
    try:
        out["obligations"] = tools.sync_obligations(conn)
    except Exception as e:
        out["obligations"] = f"{type(e).__name__}: {e}"

    # Mail. Static rules free; Haiku only for what they could not settle.
    mail = [e for e in events if e.get("kind") == "mail_new"]
    try:
        out["mail"] = mailsync.index_mail(conn, mail)
        tri.learn(conn)
    except Exception as e:
        out["mail"] = f"{type(e).__name__}: {e}"

    # Notes mirror. Free. The brief document is gone; these four are what remain.
    notes = {}
    for doc in ("obligations", "programs", "people", "activity"):
        try:
            notes[doc] = notes_sync.render(conn, doc)
        except Exception as e:
            notes[doc] = f"{type(e).__name__}: {e}"
    out["notes"] = notes

    watcher.clear_pending()
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_index(args):
    from synth import indexer
    conn = db.connect()
    print(json.dumps(indexer.build(conn), indent=2))
    return 0


def cmd_ocr(args):
    from synth import indexer
    conn = db.connect()
    limit = int(args[0]) if args else None
    print(json.dumps(indexer.ocr_pass(conn, limit=limit), indent=2))
    return 0


def cmd_enrich(args):
    from synth import enrich
    conn = db.connect()
    dry = "--dry-run" in args
    extra = 0
    for a in args:
        if a.startswith("--extra="):
            extra = int(a.split("=", 1)[1])
    docs = enrich.select(conn, extra_limit=extra)
    print(f"{len(docs)} document(s) selected for extraction")
    results = enrich.run(conn, docs, dry_run=dry)
    print(json.dumps({"batches": len(results),
                      "errors": sum(1 for r in results if r.get("error"))}, indent=2))
    return 0


def cmd_notes(args):
    from synth import notes_sync
    conn = db.connect()
    with db.run(conn, "notes_sync", trigger="manual") as run_id:
        print(json.dumps(notes_sync.sync_all(conn, run_id=run_id), indent=2))
    return 0


def cmd_serve(args):
    """synth serve — run the connector HTTP server."""
    from synth.http_server import main
    main()
    return 0


def cmd_call(args):
    """synth call <name> [json] — direct dispatch, no MCP round trip."""
    from synth.registry import ALL
    if not args:
        print(" ".join(sorted(ALL)))
        return 0
    name = args[0]
    if name not in ALL:
        print(json.dumps({"error": f"unknown call {name!r}",
                          "available": sorted(ALL)}), file=sys.stderr)
        return 2
    payload = {}
    if len(args) > 1:
        raw = args[1]
        if raw == "-":
            raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
        except ValueError as e:
            print(json.dumps({"error": f"bad json: {e}"}), file=sys.stderr)
            return 2
    conn = db.connect()
    try:
        result = ALL[name](conn, **payload)
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(json.dumps(result, default=str, separators=(",", ":")))
    return 0


def cmd_reconcile(args):
    from synth import notes_sync
    conn = db.connect()
    print(json.dumps(notes_sync.reconcile(conn), indent=2))
    print("pending after:", notes_sync.pending_corrections(conn))
    return 0


def cmd_log(args):
    from synth import tools
    conn = db.connect()
    for r in tools.activity(conn, int(args[0]) if args else 30):
        flag = "  [UNDONE]" if r["undone_at"] else ""
        print(f"{r['id']:>5}  {db.local(r['at'])}  {r['action']:<20} {r['reason'][:70]}{flag}")
    return 0


def cmd_why(args):
    from synth import tools
    conn = db.connect()
    print(json.dumps(tools.why(conn, int(args[0])), indent=2, default=str))
    return 0


def cmd_undo(args):
    conn = db.connect()
    print(db.undo(conn, int(args[0])))
    return 0


def cmd_status(args):
    from synth.applekit import call, SynthdError
    conn = db.connect()
    print("database")
    for t in ("source", "document", "entity", "assertion", "obligation", "link",
              "action_log", "run_log"):
        try:
            n = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        except Exception:
            n = "-"
        print(f"    {t:<12} {n}")
    print("daemon")
    try:
        print(f"    ping       {call('ping', timeout=15)}")
        print(f"    tcc        {call('status', timeout=15)}")
    except SynthdError as e:
        print(f"    UNREACHABLE {e}")
    print("budget")
    from synth import budget
    for line in budget.summary(conn).splitlines():
        print(f"    {line}")
    print("mail filter")
    d = conn.execute("SELECT COUNT(*) FROM mail_digest WHERE reported_at IS NULL").fetchone()[0]
    pol = dict(conn.execute("SELECT policy, COUNT(*) FROM sender_policy GROUP BY policy"))
    print(f"    digest queue  {d} message(s) not yet surfaced to you")
    print(f"    senders       {pol}")
    print("runs")
    for r in conn.execute("SELECT job, status, started_at, summary FROM run_log "
                          "ORDER BY id DESC LIMIT 5"):
        print(f"    {db.local(r['started_at'])}  {r['job']:<12} {r['status']:<6} "
              f"{(r['summary'] or '')[:60]}")
    return 0


COMMANDS = {
    "sync": cmd_sync, "ingest": cmd_ingest, "index": cmd_index, "ocr": cmd_ocr,
    "enrich": cmd_enrich, "notes": cmd_notes, "reconcile": cmd_reconcile,
    "call": cmd_call, "serve": cmd_serve, "log": cmd_log, "why": cmd_why,
    "undo": cmd_undo, "status": cmd_status, "budget": cmd_budget,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print(f"unknown command {cmd!r}\n{__doc__}", file=sys.stderr)
        return 2
    return COMMANDS[cmd](sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
