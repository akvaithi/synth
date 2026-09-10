"""synth — command line entry point.

    synth sync           one free sweep: documents, obligations, mail index, Notes mirror
    synth migrate        bring the database up to the current schema
    synth ingest         read Documents, extract text, cache it
    synth index          load extracted document text into the searchable index
    synth ocr            OCR the scanned PDFs that had no text layer
    synth enrich [--limit=N|--dry-run|--claude|--no-gate]  extract facts from documents
    synth notes          re-render the Notes mirror
    synth reconcile      align stored note hashes with what Notes actually holds
    synth call <name> [json]  direct tool dispatch (see: synth call)
    synth serve          run the connector HTTP server (behind Cloudflare)
    synth log            what Synth has done
    synth why <id>       why it did one thing
    synth undo <id>      reverse one action
    synth work [--once|--status|--dry-run]  drain the reaction queue
    synth work --backfill [--days=N|--limit=N|--dry-run]  queue mail indexed before the reactor
    synth brief [morning|evening]  write and deliver the brief
    synth embed [--backfill|--stats]  build the semantic index
    synth status         health of every moving part
    synth doctor [--json]  what is broken right now, and nothing else
    synth budget [clear|reset <why>]  what it has spent, and what it may spend
    synth dedupe-predicates [--merge <keep_id> <id>...]  one fact under several names
    synth suspect-sources    live facts sourced from documents enrichment now excludes
    synth prune-state [--yes] stale database backups and rolled logs in .state
"""
from __future__ import annotations

import json
import os
import sys

from synth import db


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


def _exclusive(wait: float = 0.0):
    """Hold a lock for the whole reaction, or bail out.

    A run regularly outlasts the 120s launchd interval, so two passes overlapped and both
    created the same reminder -- one run's own summary says "a reminder already existed from
    a concurrent Synth process".

    `wait` is for the jobs that must not simply skip. The sweep is periodic, so bailing out
    costs it nothing -- it runs again in half an hour. Enrichment is a nightly appointment
    and a typed command is a request, so both wait instead. They did neither: only cmd_sync
    ever took this lock, and the 03:00 enrich job overlapped the 03:00 sweep every night it
    landed together. run_log still holds the result -- "killed by database lock during
    concurrent OCR pass".
    """
    import fcntl
    import time as _time
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    fh = open(LOCK, "w")
    deadline = _time.time() + wait
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if _time.time() >= deadline:
                fh.close()
                return None
            _time.sleep(0.5)


def _locked_or_exit(what: str, wait: float = 1800.0):
    """Take the sweep lock for a command that writes, or explain why it did not run.

    Every command that writes to the database takes this. SQLite's own busy timeout does not
    help here: the sweep holds its write transactions across OCR and AppleScript calls that
    run for minutes, so a second writer does not queue politely, it fails partway through
    whatever it had already begun.
    """
    held = _exclusive(wait=wait)
    if held is None:
        print(json.dumps({
            "skipped": f"{what} did not run: another Synth process held the lock for "
                       f"{int(wait)}s. It is probably a long sweep; try again after it."}))
    return held


# launchd appends to StandardOutPath for ever and rotates nothing. These four files only
# grow: sync.out.log was 288 KB and index.out.log 324 KB after two weeks, which is small and
# also monotonic. One generation is kept, because the reason to open one of these is always
# "what happened just now".
LOG_MAX_BYTES = 10 * 1024 * 1024

# Scanned PDFs OCRed per sweep. Vision is free and offline, and slow enough that the whole
# backlog would outlast the timer that starts it.
OCR_PER_SWEEP = int(os.environ.get("SYNTH_OCR_PER_SWEEP", "6"))


def rotate_logs(state_dir: str = None) -> list[str]:
    """Roll any oversized .log in .state to .log.1. Never raises: this is housekeeping."""
    state_dir = state_dir or os.path.dirname(LOCK)
    rotated = []
    try:
        names = os.listdir(state_dir)
    except OSError:
        return rotated
    for name in names:
        if not name.endswith(".log"):
            continue
        path = os.path.join(state_dir, name)
        try:
            if os.path.getsize(path) <= LOG_MAX_BYTES:
                continue
            os.replace(path, path + ".1")
            # launchd holds the original descriptor open and keeps writing to the renamed
            # inode until the job restarts. Truncating in place would be worse -- it would
            # leave a sparse file the size of the old one -- so the new file appears here and
            # the tail of the previous generation is what carries on filling for now.
            with open(path, "w"):
                pass
            rotated.append(name)
        except OSError:
            continue
    return rotated


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
    held = _locked_or_exit("ingest")
    if held is None:
        return 0
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

    # OCR, bounded. About a fifth of the PDFs under Documents are scans -- transcripts,
    # letters, forms -- and they are often the ones carrying the hard facts. ocr_pass and
    # `synth ocr` have existed all along, but nothing ever called them: there is no launchd
    # job for OCR and the sweep did not do it, so 107 scanned documents sat permanently
    # textless while ingest re-attempted them every thirty minutes and reported them as
    # failures. That is what "the index is stale" actually was.
    #
    # A handful per sweep rather than the backlog at once: Vision is free and offline but
    # costs seconds a page, and this runs inside a thirty-minute timer. At this rate the
    # backlog drains in a day and a half, and afterwards there is rarely anything to do.
    try:
        if not full:
            out["ocr"] = indexer.ocr_pass(conn, limit=OCR_PER_SWEEP)
        else:
            out["ocr"] = indexer.ocr_pass(conn)
        # Newly OCRed text is only in the cache until the index is rebuilt over it.
        if out["ocr"].get("ocred"):
            out["index_after_ocr"] = indexer.build(conn)
    except Exception as e:
        out["ocr"] = f"{type(e).__name__}: {e}"

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

    # Semantic index. Bounded, because this runs inside the half-hour timer and embedding
    # goes over a tunnel to another machine: it takes whatever slice it can finish and leaves
    # the rest queued rather than making the sweep's length depend on a remote service. Costs
    # nothing when nothing changed, since docwrite only queues a document whose text moved.
    try:
        from synth import embed
        result = embed.drain(conn, budget_seconds=90)
        if result.get("embedded") or result.get("stopped"):
            out["embed"] = result
    except Exception as e:
        out["embed"] = f"{type(e).__name__}: {e}"

    # Free, and the only place that runs often enough to keep the state directory bounded.
    rotated = rotate_logs()
    if rotated:
        out["rotated_logs"] = rotated
    # Lets SQLite refresh the statistics its query planner uses, using whatever budget it
    # thinks is warranted. Cheap when there is nothing to do, which is most sweeps.
    try:
        conn.execute("PRAGMA optimize")
    except Exception as e:
        out["optimize"] = f"{type(e).__name__}: {e}"

    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_migrate(args):
    """Bring the database up to the current schema, and tidy it. Safe to run twice."""
    # ANALYZE and a WAL checkpoint both want the database to themselves, and migrate may be
    # creating tables the sweep is about to read.
    held = _locked_or_exit("migrate")
    if held is None:
        return 0
    conn = db.connect()
    steps = db.migrate(conn)
    print("\n".join(f"  {s}" for s in steps) if steps else "  already current")

    # This is already the "run it occasionally" command, and the database had never had
    # either of these run against it: no ANALYZE since it was created, and a WAL that only
    # ever checkpoints when SQLite decides to. Both are safe and both are quick at this size.
    if "--no-tidy" not in args:
        conn.execute("ANALYZE")
        pages, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[1:]
        conn.commit()
        print(f"  analysed; WAL checkpointed ({pages} page(s))")
    return 0


def cmd_prune_state(args):
    """What in .state is safe to remove, and -- with --yes -- removing it.

    Reports by default. Nothing here is Arun's: these are Synth's own database backups and
    rolled log generations, and the never-delete rule is about his files. The confirmation
    step is kept anyway, because a command that removes things on sight is one nobody reads
    the output of.
    """
    state = os.path.dirname(LOCK)
    keep_newest = 1
    # By mtime, not by name. The backups carry the reason they were taken rather than only a
    # timestamp -- pre-rename, pre-audit-fixes, pre-oauth-hash -- so sorting the names sorts
    # by reason and keeps whichever one happens to sort last. Sorted that way this offered to
    # remove a backup taken twenty minutes earlier and keep one from two days before.
    backups = sorted((f for f in os.listdir(state) if f.startswith("synth.db.pre-")),
                     key=lambda f: os.path.getmtime(os.path.join(state, f)), reverse=True)
    stale = [f for f in os.listdir(state) if f.endswith(".log.1")]
    stale += backups[keep_newest:]

    if not stale:
        print("nothing to prune")
        return 0
    total = 0
    for name in stale:
        size = os.path.getsize(os.path.join(state, name))
        total += size
        print(f"  {size / 1e6:8.1f} MB  {name}")
    print(f"  {'-' * 8}")
    kept = f"; keeping {backups[0]}" if backups else ""
    print(f"  {total / 1e6:8.1f} MB in {len(stale)} file(s){kept}")

    if "--yes" not in args:
        print("\nnothing removed. Re-run with --yes to remove these.")
        return 0
    for name in stale:
        os.remove(os.path.join(state, name))
    print(f"\nremoved {len(stale)} file(s), {total / 1e6:.1f} MB")
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
    held = _locked_or_exit("index")
    if held is None:
        return 0
    conn = db.connect()
    print(json.dumps(indexer.build(conn), indent=2))
    return 0


def cmd_ocr(args):
    from synth import indexer
    held = _locked_or_exit("ocr")
    if held is None:
        return 0
    conn = db.connect()
    limit = int(args[0]) if args else None
    print(json.dumps(indexer.ocr_pass(conn, limit=limit), indent=2))
    return 0


def cmd_enrich(args):
    from synth import enrich
    # The nightly job is at 03:00 and so is a sweep; whichever lost the race was killed
    # partway through. It waits now rather than skipping, because enrichment that silently
    # does not happen looks exactly like enrichment that found nothing to do.
    held = _locked_or_exit("enrich")
    if held is None:
        return 0
    conn = db.connect()
    dry = "--dry-run" in args
    limit = 0
    extra = 0
    for a in args:
        if a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
        if a.startswith("--extra="):
            extra = int(a.split("=", 1)[1])

    # Metered Claude over the curated set is still here, because it is a better extractor and
    # a large backlog is a reasonable thing to spend money on deliberately. It is no longer
    # the default: the local pass costs nothing and can therefore consider the whole corpus.
    if "--claude" in args:
        docs, skipped = enrich.select_with_skipped(conn, extra_limit=extra)
        print(f"{len(docs)} document(s) selected, {len(skipped)} excluded  [metered Claude]")
        for sk in skipped:
            print(f"    excluded  {sk['path']}  ({sk['why']})")
        results = enrich.run(conn, docs, dry_run=dry)
        print(json.dumps({"batches": len(results),
                          "errors": sum(1 for r in results if r.get("error"))}, indent=2))
        return 0

    docs, skipped = enrich.select_all(conn, limit=limit)
    print(f"{len(docs)} document(s) to consider, {len(skipped)} excluded by rule  [local]")
    seconds = enrich.NIGHTLY_SECONDS
    for a in args:
        if a.startswith("--seconds="):
            seconds = float(a.split("=", 1)[1])
    results = enrich.run_local(conn, docs, dry_run=dry,
                               use_gate="--no-gate" not in args, budget_seconds=seconds)
    gated = [r for r in results if r.get("gated")]
    ran = [r for r in results if "stop" in r]
    print(json.dumps({
        "considered": len(results),
        "gated_out": len(gated),
        "extracted": len(ran),
        "facts_written": sum(r.get("writes", 0) for r in ran),
        "stopped_early": [r["stop"] for r in ran if r["stop"] not in ("done", "writes")],
        "budget_exhausted": any(r.get("stopped") == "budget" for r in results),
    }, indent=2))
    return 0


def cmd_notes(args):
    from synth import notes_sync
    held = _locked_or_exit("notes")
    if held is None:
        return 0
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
    # Reconciling while a sweep is re-rendering the mirror would record the hash of a note
    # that is about to be rewritten, which is the phantom-correction bug from the other side.
    held = _locked_or_exit("reconcile")
    if held is None:
        return 0
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
    print("inference")
    from synth import ollama
    probe = ollama.health()
    if probe.get("up"):
        print(f"    ollama     up, version {probe['version']}")
        try:
            for m in ollama.loaded():
                print(f"    resident   {m['name']}  {m['on_gpu']}% on GPU, ctx {m['context']}")
        except Exception:
            pass
    else:
        print(f"    ollama     DOWN — {probe.get('error')}")
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


def cmd_embed(args):
    """Build and maintain the semantic index.

        synth embed                drain whatever is queued
        synth embed --backfill     queue every document, then drain it
        synth embed --stats        what the index holds
    """
    from synth import embed
    held = _locked_or_exit("embed")
    if held is None:
        return 0
    conn = db.connect()
    if "--stats" in args:
        print(json.dumps(embed.stats(conn), indent=2))
        return 0
    if "--backfill" in args:
        marked = embed.enqueue_all(conn, "document")
        print(f"queued {marked} document(s)")
    seconds = 120.0
    for a in args:
        if a.startswith("--seconds="):
            seconds = float(a.split("=", 1)[1])
    if "--backfill" in args and not any(a.startswith("--seconds=") for a in args):
        # A backfill is a deliberate one-off, not a slice of a sweep.
        seconds = 86400.0
    with db.run(conn, "embed", trigger="manual") as run_id:
        result = embed.drain(conn, budget_seconds=seconds)
        conn.execute("UPDATE run_log SET summary = ? WHERE id = ?",
                     (json.dumps(result)[:2000], run_id))
        conn.commit()
    print(json.dumps(result, indent=2))
    print(json.dumps(embed.stats(conn), indent=2))
    return 0


def cmd_brief(args):
    """Write and deliver one brief. `synth brief morning` or `synth brief evening`."""
    from synth import brief
    when = "evening" if "evening" in args else "morning"
    held = _locked_or_exit("brief")
    if held is None:
        return 0
    conn = db.connect()
    result = brief.write(conn, when)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("delivered") else 1


def cmd_work(args):
    """Drain the reaction queue as work appears. The resident reactor.

        synth work            run forever (this is what launchd starts)
        synth work --once     one pass, for looking at what it would do
        synth work --status   what is queued, what has been written today
        synth work --dry-run  analyse and write nothing, whatever the halt file says
    """
    from synth import reactor, watcher, worker
    conn = db.connect()
    if "--status" in args:
        print(json.dumps(worker.status(conn), indent=2))
        return 0
    if "--backfill" in args:
        days = next((int(a.split("=", 1)[1]) for a in args if a.startswith("--days=")), 14)
        limit = next((int(a.split("=", 1)[1]) for a in args if a.startswith("--limit=")), 0)
        result = watcher.backfill_mail(conn, days=days, limit=limit,
                                       dry_run="--dry-run" in args)
        if "--dry-run" in args:
            for m in result.pop("messages", []):
                print(f"  {m['received']}  [{m['account']}] {(m['sender'] or '')[:32]:<32} "
                      f"{(m['subject'] or '')[:56]}")
        print(json.dumps(result, indent=2))
        return 0
    dry = True if "--dry-run" in args else (True if reactor.DRY_RUN else None)
    result = worker.loop(conn, dry_run=dry, forever="--once" not in args)
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_doctor(args):
    """What is broken right now, and nothing else.

    Exits non-zero when something is actually failing, so it can be the thing a monitor or a
    launchd job runs rather than something only a person reads.
    """
    from synth import doctor
    conn = db.connect()
    findings = doctor.run_all(conn)
    if "--json" in args:
        print(json.dumps(findings, indent=2))
    else:
        if not findings:
            print("ok — everything Synth depends on is answering.")
        for f in findings:
            mark = {"fail": "FAIL", "warn": "warn", "ok": "ok  "}.get(f["level"], "?")
            print(f"{mark}  {f['check']:<22} {f['detail']}")
            if f["fix"]:
                print(f"      fix: {f['fix']}")
    return 1 if any(f["level"] == "fail" for f in findings) else 0


COMMANDS = {
    "sync": cmd_sync, "ingest": cmd_ingest, "index": cmd_index, "ocr": cmd_ocr,
    "doctor": cmd_doctor, "embed": cmd_embed, "brief": cmd_brief,
    "work": cmd_work,
    "enrich": cmd_enrich, "notes": cmd_notes, "reconcile": cmd_reconcile,
    "call": cmd_call, "serve": cmd_serve, "log": cmd_log, "why": cmd_why,
    "undo": cmd_undo, "status": cmd_status, "budget": cmd_budget,
    "migrate": cmd_migrate, "dedupe-predicates": cmd_dedupe_predicates,
    "suspect-sources": cmd_suspect_sources, "prune-state": cmd_prune_state,
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
