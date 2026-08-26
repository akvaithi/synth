"""synth — command line entry point.

    synth ingest         read Documents, extract text, cache it
    synth index          load extracted document text into the searchable index
    synth ocr            OCR the scanned PDFs that had no text layer
    synth enrich         extract structured facts from the curated documents (uses a model)
    synth call <name> [json]  direct tool dispatch (see: synth call)
    synth serve          run the connector HTTP server (behind Cloudflare)
    synth log            what Synth has done
    synth why <id>       why it did one thing
    synth undo <id>      reverse one action
    synth status         health of every moving part
"""
from __future__ import annotations

import json
import os
import sys

from synth import db




SWEEP_LOCK = os.path.expanduser("~/Developer/synth/.state/sweep.lock")


def _exclusive():
    """Hold a lock for a whole ingest/index sweep, or bail out.

    Inherited from the reactor, where a run regularly outlasted the 120s launchd interval and
    two passes overlapped. The sweep has the same shape: a full pass over ~1,700 documents can
    outlast its own timer, and two of them would fight over the SQLite write lock.
    """
    import fcntl
    os.makedirs(os.path.dirname(SWEEP_LOCK), exist_ok=True)
    fh = open(SWEEP_LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def cmd_ingest(args):
    """Read Documents, extract text, cache it. Free: no model is involved at any point."""
    from synth import ingest
    held = _exclusive()
    if held is None:
        print(json.dumps({"skipped": "another sweep is already running"}))
        return 0
    conn = db.connect()
    limit = None
    for a in args:
        if a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
    print(json.dumps(ingest.scan(conn, limit=limit), indent=2))
    return 0


def cmd_index(args):
    from synth import indexer
    held = _exclusive()
    if held is None:
        print(json.dumps({"skipped": "another sweep is already running"}))
        return 0
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
    conn = db.connect()
    print("database")
    for t in ("source", "document", "entity", "assertion", "obligation", "link",
              "action_log", "run_log"):
        try:
            n = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        except Exception:
            n = "-"
        print(f"    {t:<12} {n}")
    print("documents")
    enriched = conn.execute("SELECT count(*) FROM enrichment").fetchone()[0]
    docs = conn.execute("SELECT count(*) FROM document").fetchone()[0]
    stale = conn.execute(
        "SELECT count(*) FROM source s JOIN document d ON d.source_id = s.id "
        "WHERE s.kind = 'file' AND s.content_hash IS NOT NULL").fetchone()[0]
    print(f"    indexed      {docs}")
    print(f"    enriched     {enriched} (the rest are searchable but not fact-extracted)")
    print(f"    from files   {stale}")
    print("runs")
    for r in conn.execute("SELECT job, status, started_at, summary FROM run_log "
                          "ORDER BY id DESC LIMIT 5"):
        print(f"    {db.local(r['started_at'])}  {r['job']:<12} {r['status']:<6} "
              f"{(r['summary'] or '')[:60]}")
    return 0


COMMANDS = {
    "ingest": cmd_ingest, "index": cmd_index, "ocr": cmd_ocr, "enrich": cmd_enrich,
    "call": cmd_call, "serve": cmd_serve, "log": cmd_log, "why": cmd_why,
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
