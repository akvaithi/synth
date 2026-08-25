"""synth — command line entry point.

    synth watch          run one detection pass, react if the debounce window has closed
    synth react          force a reaction over whatever is pending
    synth brief morning  write a brief
    synth index          load extracted document text into the searchable index
    synth ocr            OCR the scanned PDFs that had no text layer
    synth enrich         extract structured facts from the curated documents
    synth notes          re-render the Notes mirror
    synth reconcile      align stored note hashes with what Notes actually holds
    synth log            what Synth has done
    synth why <id>       why it did one thing
    synth undo <id>      reverse one action
    synth call <name> [json]  direct tool dispatch (see: synth call)
    synth serve          run the connector HTTP server (behind Cloudflare)
    synth status         health of every moving part
    synth budget [clear|reset <why>]  what it has spent, and what it may spend
    synth triage [n]     how the free filter sorts the inbox, costing nothing
"""
from __future__ import annotations

import json
import os
import sys

from synth import config, db


LOCK = os.path.expanduser("~/Developer/synth/.state/reactor.lock")


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


def cmd_watch(args):
    from synth import budget, reactor, watcher
    conn = db.connect()

    # Detection is free and always runs: the queue must keep filling even while the model is
    # unaffordable, or a quiet budget day becomes a day of lost mail.
    try:
        from synth import tools
        tools.sync_obligations(conn)
    except Exception:
        pass  # detection must not fail because reconciliation did

    events = watcher.actionable(conn, watcher.collect(conn))
    pending = watcher.accumulate(events)
    if not watcher.due(pending):
        n = len(pending.get("events", []))
        print(f"{len(events)} new, {n} pending, debounce not closed")
        return 0

    b = budget.read_backoff()
    if b:
        print(f"holding {len(pending['events'])} event(s): {b['kind']} limit for another "
              f"{b['seconds_left'] // 60} min")
        return 0

    lock = _exclusive()
    if lock is None:
        print("another reaction is already running; holding the queue")
        return 0
    try:
        print(f"reacting to {len(pending['events'])} pending event(s)")
        result = reactor.react(conn, pending["events"])
        # Clear only what was actually finished. Clearing the lot on any return is how two
        # days of detected mail was thrown away when the API refused every call.
        watcher.keep_only(result.get("handled", []), pending["events"])
        print(str(result.get("result", ""))[:2000])
    finally:
        lock.close()
    return 0


def cmd_react(args):
    from synth import reactor, watcher
    conn = db.connect()
    pending = watcher._load(watcher.PENDING, {"events": []})
    events = pending.get("events") or watcher.actionable(conn, watcher.collect(conn))
    if not events:
        print("nothing pending")
        return 0
    lock = _exclusive()
    if lock is None:
        print("another reaction is already running")
        return 0
    try:
        result = reactor.react(conn, events, force="--force" in args)
        watcher.keep_only(result.get("handled", []), events)
        print(str(result.get("result", ""))[:4000])
    finally:
        lock.close()
    return 0


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


def cmd_triage(args):
    """Show how the free filter would sort the current inbox, without spending anything."""
    import collections
    from synth import triage as tri
    from synth.applekit import call
    conn = db.connect()
    ctx = tri.context(conn)
    limit = int(args[0]) if args and args[0].isdigit() else config.MAIL_SCAN_LIMIT
    counts = collections.Counter()
    for account in config.MAIL_ACCOUNTS:
        try:
            msgs = call("mail_recent", account=account, limit=limit, timeout=300)
        except Exception as e:
            print(f"{account}: {e}")
            continue
        for m in msgs:
            v, why = tri.classify(m, ctx)
            counts[v] += 1
            if v in ("urgent", "consider") or "-v" in args:
                print(f"  {v:9} [{why[:40]:42}] {(m.get('subject') or '')[:56]}")
    total = sum(counts.values()) or 1
    free = counts["digest"] + counts["ignore"]
    print(f"\n{total} messages: urgent {counts['urgent']}, consider {counts['consider']}, "
          f"digest {counts['digest']}, ignore {counts['ignore']}")
    print(f"{free / total * 100:.0f}% never reaches a model; "
          f"{counts['urgent'] / total * 100:.0f}% goes straight to the expensive one")
    return 0


def cmd_brief(args):
    from synth import reactor
    when = args[0] if args else "morning"
    conn = db.connect()
    result = reactor.brief(conn, when)
    print(str(result.get("result", "")))
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
    print(f"    digest queue  {d} message(s) awaiting the next brief")
    print(f"    senders       {pol}")
    print("runs")
    for r in conn.execute("SELECT job, status, started_at, summary FROM run_log "
                          "ORDER BY id DESC LIMIT 5"):
        print(f"    {db.local(r['started_at'])}  {r['job']:<12} {r['status']:<6} "
              f"{(r['summary'] or '')[:60]}")
    return 0


COMMANDS = {
    "watch": cmd_watch, "react": cmd_react, "brief": cmd_brief, "index": cmd_index,
    "ocr": cmd_ocr, "enrich": cmd_enrich, "notes": cmd_notes, "reconcile": cmd_reconcile, "call": cmd_call, "serve": cmd_serve, "log": cmd_log, "why": cmd_why,
    "undo": cmd_undo, "status": cmd_status, "budget": cmd_budget,
    "triage": cmd_triage,
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
