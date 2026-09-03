"""synth — command line entry point.

    synth sync           one free sweep: documents, obligations, mail index, Notes mirror
    synth migrate        bring the database up to the current schema
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
    synth dedupe-predicates [--merge <keep_id> <id>...]  one fact under several names
    synth suspect-sources    live facts sourced from documents enrichment now excludes
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

    # Documents. Free. Only files touched since the last sweep, unless --full -- plus
    # anything that failed last time, which the mtime filter would otherwise skip forever.
    try:
        retry = [] if full else [p for p in mark.get("retry", []) if os.path.exists(p)]
        res = ingest.scan(conn, since=None if full else since, extra=retry)
        out["ingest"] = {k: v for k, v in res.items() if k != "failed_paths"}
        if retry:
            out["ingest"]["retried"] = len(retry)
        out["index"] = indexer.build(conn)
        _save_json(SYNC_MARK, {"last_sweep": started,
                               "retry": res.get("failed_paths", [])})
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

    # Notes mirror. Free. Programs is split three ways by status: rendered whole it was 88,565
    # characters, re-composed every sweep and unreadable on a phone, which is the one thing the
    # mirror exists for.
    notes = {}
    for doc in notes_sync.DOCS:
        try:
            notes[doc] = notes_sync.render(conn, doc)
        except Exception as e:
            notes[doc] = f"{type(e).__name__}: {e}"
    out["notes"] = notes

    watcher.clear_pending()
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_migrate(args):
    """Bring the database up to the current schema. Safe to run twice."""
    conn = db.connect()
    steps = db.migrate(conn)
    print("\n".join(f"  {s}" for s in steps) if steps else "  already current")
    return 0


def cmd_dedupe_predicates(args):
    """One fact recorded under several predicate names.

    With no arguments this reports. Supersession keys on the normalised predicate, so the
    clusters the automatic pass could prove are already merged; what is left here needs a
    person, because the spellings that matter most read as obviously-one-fact and score far
    too low for a threshold that is safe to run unattended.
    """
    from synth import predicates
    conn = db.connect()
    if args and args[0] == "--merge":
        ids = [int(a) for a in args[1:]]
        if len(ids) < 2:
            print("usage: synth dedupe-predicates --merge <keep_id> <id> [<id>...]",
                  file=sys.stderr)
            return 2
        moved = predicates.merge_ids(conn, ids[0], ids[1:])
        if moved:
            db.log_action(conn, "dedupe_predicates", "db",
                          "merged by hand: " + "; ".join(moved),
                          target_id=str(ids[0]), after={"keep": ids[0], "superseded": ids[1:]})
        print(f"superseded {len(moved)} assertion(s) onto {ids[0]}")
        for m in moved:
            print(f"  {m}")
        return 0

    mergeable, reported = predicates.clusters(conn)
    if mergeable:
        print(f"{len(mergeable)} cluster(s) the automatic pass has not yet taken:")
        for c in mergeable:
            print(f"  [{c['entity']}] keep {c['keep']['predicate']!r}")
        print()
    live = [r for r in reported if not r["series"]]
    series = [r for r in reported if r["series"]]
    print(f"{len(live)} cluster(s) share a value and need a person to judge them:\n")
    for c in live:
        m0 = c["members"][0]
        val = m0["value_text"] or m0["value_date"] or m0["value_num"]
        print(f"  [{c['entity']}]  value = {str(val)[:70]!r}")
        for m in c["members"]:
            print(f"      {m['id']:>6}  {m['predicate']}")
        keep = max(c["members"], key=lambda m: (m["observed_at"], m["id"]))
        rest = [str(m["id"]) for m in c["members"] if m["id"] != keep["id"]]
        print(f"      -> synth dedupe-predicates --merge {keep['id']} {' '.join(rest)}\n")
    if series:
        print(f"{len(series)} cluster(s) share a value but are a SERIES, not duplicates — "
              f"left alone:")
        for c in series:
            print(f"  [{c['entity']}] "
                  + " | ".join(m["predicate"] for m in c["members"]))
    return 0


def cmd_suspect_sources(args):
    """Live facts whose source document is one enrichment now refuses to read.

    Superseded resumes were being read with today's observed_at, so an old resume entered the
    database as the most recent evidence -- facts about the math minor and a 3.918 GPA sit at
    confidence 1.0 alongside the corrections that replaced them. Excluding those paths stops it
    happening again; it does not touch what is already recorded. This is the list to read
    before deciding what to do about that, and it changes nothing by itself.
    """
    from synth import enrich
    conn = db.connect()
    rows = conn.execute(
        "SELECT a.id, a.predicate, a.value_text, a.value_num, a.value_date, a.confidence, "
        "  e.name AS entity, d.path FROM assertion a "
        "JOIN entity e ON e.id = a.entity_id "
        "JOIN source s ON s.id = a.source_id "
        "JOIN document d ON d.source_id = s.id "
        "WHERE a.superseded_by IS NULL ORDER BY d.path, a.predicate").fetchall()
    hits = [(r, enrich.excluded(r["path"])) for r in rows]
    hits = [(r, why) for r, why in hits if why]
    print(f"{len(hits)} live fact(s) sourced from documents enrichment now excludes\n")
    path = None
    for r, why in hits:
        if r["path"] != path:
            path = r["path"]
            print(f"  {path}\n    ({why})")
        val = r["value_text"] or r["value_date"] or r["value_num"]
        print(f"    {r['id']:>6}  [{r['entity']}] {r['predicate']}: {str(val)[:60]}"
              + ("" if r["confidence"] >= 0.99 else f"  (confidence {r['confidence']:.0%})"))
    if hits:
        print("\nNothing was changed. These are facts, not errors — read them and decide.")
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
    docs, skipped = enrich.select_with_skipped(conn, extra_limit=extra)
    print(f"{len(docs)} document(s) selected for extraction, {len(skipped)} excluded")
    for s in skipped:
        print(f"    excluded  {s['path']}  ({s['why']})")
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
    "migrate": cmd_migrate, "dedupe-predicates": cmd_dedupe_predicates,
    "suspect-sources": cmd_suspect_sources,
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
