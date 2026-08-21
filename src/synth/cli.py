"""synth — command line entry point.

    synth watch          run one detection pass, react if the debounce window has closed
    synth react          force a reaction over whatever is pending
    synth brief morning  write a brief
    synth index          load extracted document text into the searchable index
    synth ocr            OCR the scanned PDFs that had no text layer
    synth enrich         extract structured facts from the curated documents
    synth notes          re-render the Notes mirror
    synth log            what Synth has done
    synth why <id>       why it did one thing
    synth undo <id>      reverse one action
    synth status         health of every moving part
"""
from __future__ import annotations

import json
import os
import sys

from synth import config, db


def cmd_watch(args):
    from synth import reactor, watcher
    conn = db.connect()
    events = watcher.collect(conn)
    pending = watcher.accumulate(events)
    if not watcher.due(pending):
        n = len(pending.get("events", []))
        print(f"{len(events)} new, {n} pending, debounce not closed")
        return 0
    print(f"reacting to {len(pending['events'])} pending event(s)")
    result = reactor.react(conn, pending["events"])
    watcher.clear_pending()
    print(str(result.get("result", ""))[:2000])
    return 0


def cmd_react(args):
    from synth import reactor, watcher
    conn = db.connect()
    pending = watcher._load(watcher.PENDING, {"events": []})
    events = pending.get("events") or watcher.collect(conn)
    if not events:
        print("nothing pending")
        return 0
    result = reactor.react(conn, events)
    watcher.clear_pending()
    print(str(result.get("result", ""))[:4000])
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
    print("runs")
    for r in conn.execute("SELECT job, status, started_at, summary FROM run_log "
                          "ORDER BY id DESC LIMIT 5"):
        print(f"    {db.local(r['started_at'])}  {r['job']:<12} {r['status']:<6} "
              f"{(r['summary'] or '')[:60]}")
    return 0


COMMANDS = {
    "watch": cmd_watch, "react": cmd_react, "brief": cmd_brief, "index": cmd_index,
    "ocr": cmd_ocr, "enrich": cmd_enrich, "notes": cmd_notes, "log": cmd_log, "why": cmd_why,
    "undo": cmd_undo, "status": cmd_status,
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
