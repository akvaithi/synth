"""One place to look when something is wrong.

`synth status` is an inventory: row counts, the last few runs, what the budget has left. It
answers "what is Synth holding". It cannot answer "what is broken", and for a while nothing
could -- the two real failures on record, a BrokenPipeError and a run killed by a database
lock, existed only as rows in run_log that nobody queries by hand. Meanwhile .state/
sync.err.log had grown to 236 lines of CoreGraphics PDF warnings, 100% of it noise, and
tunnel.err.log to 287 KB of clean MCP session closes that cloudflared logs as errors. Both
channels were unreadable, so neither was read.

Every check here answers a question that has actually gone wrong at least once, and reports
nothing when the answer is fine. Silence is the healthy output.

The checks are data, not prints, so the same list can be surfaced in a brief or a reminder
rather than only on a terminal.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

from synth import db

OK, WARN, FAIL = "ok", "warn", "fail"

STATE = os.path.expanduser("~/Developer/synth/.state")

# What an error actually looks like. This is a positive filter rather than a list of things
# to ignore, because these files interleave startup chatter with failures and "not on the
# benign list" matched every "watching 3 paths" and "session manager started" in them -- which
# is the same way of being useless as printing everything, only harder to notice.
ERROR_SIGNAL = (
    "Traceback", "Error:", "error:", "Exception", "CRITICAL", "FATAL",
    "ERR ", "refused", "denied", "Permission", "failed to", "cannot",
)

# Lines that match ERROR_SIGNAL and still mean nothing is wrong.
BENIGN_LOG = (
    "CoreGraphics PDF has logged an error",   # a malformed PDF that PDFKit still reads
    "canceled by remote with error code 0",   # a client closing an SSE stream, i.e. normal
    "context canceled",                       # the same, from cloudflared's side
)

# launchd reports the last exit as a negative signal number for a job it stopped itself.
# SIGTERM is how a KeepAlive job is restarted, so it is the signature of a kickstart, not of
# a crash -- flagging it made every healthy restart look like a failure.
BENIGN_EXIT = {"-15"}

# A crash says something different from a non-zero return, and it is worth saying which.
# launchctl reports a signal death as the bare signal number here.
SIGNALS = {"11": "SIGSEGV (a crash)", "6": "SIGABRT (a crash)", "9": "SIGKILL",
           "-9": "SIGKILL", "-11": "SIGSEGV (a crash)", "-6": "SIGABRT (a crash)",
           "78": "a configuration problem — check the token file"}

# A sweep runs every 30 minutes. Twice that is late enough to mean something is wrong rather
# than that one pass ran long.
SWEEP_STALE_SECONDS = 3600


def _finding(level, check, detail, fix=""):
    return {"level": level, "check": check, "detail": detail, "fix": fix}


def daemon(conn=None) -> list[dict]:
    """Is synthd up, and does it still hold the grants it needs?

    Two separate questions with two separate answers, and only the first was ever asked.
    EventKit access is reported by `status`; whether the daemon can actually READ Mail and
    Notes is reported by `diag`, and that is the one that silently lapses. TCC attributes
    access to the responsible process, so a grant can be present for Calendar and absent for
    Full Disk Access at the same time -- which is exactly the state that leaves FSEvents
    detection blind while every AppleScript call still works, so nothing appears broken.
    """
    from synth.applekit import call

    out = []
    try:
        call("ping", timeout=15)
    except Exception as e:
        return [_finding(FAIL, "daemon", f"synthd is not answering: {e}",
                         "launchctl kickstart -k gui/$UID/page.akvaithi.synth.daemon")]

    try:
        grants = call("status", timeout=15)
        for kind, value in sorted(grants.items()):
            if value != "fullAccess":
                out.append(_finding(
                    FAIL, f"tcc/{kind}", f"EventKit {kind} access is {value!r}",
                    "System Settings > Privacy & Security > "
                    f"{kind.title()} > enable Synth"))
    except Exception as e:
        out.append(_finding(WARN, "tcc", f"could not read EventKit grants: {e}"))

    try:
        d = call("diag", timeout=30)
        for area in ("mail", "notes", "documents"):
            info = d.get(area) or {}
            if info.get("readable") is False:
                out.append(_finding(
                    FAIL, f"fda/{area}",
                    f"the daemon cannot read {area}: {info.get('error', 'unknown')}",
                    "System Settings > Privacy & Security > Full Disk Access > enable "
                    "Synth.app. Without it the FSEvents watcher is blind and mail is only "
                    "noticed by polling."))
    except Exception as e:
        out.append(_finding(WARN, "fda", f"could not read daemon diagnostics: {e}"))
    return out


def jobs(conn=None) -> list[dict]:
    """Every launchd job Synth ships, and whether launchd currently holds it."""
    root = os.path.expanduser("~/Developer/synth/launchd")
    try:
        listed = subprocess.run(["launchctl", "list"], capture_output=True, timeout=30)
        loaded = listed.stdout.decode("utf-8", errors="replace")
    except Exception as e:
        return [_finding(WARN, "launchd", f"could not list jobs: {e}")]

    out = []
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if not name.endswith(".plist"):
            continue
        label = name[:-len(".plist")]
        if label not in loaded:
            out.append(_finding(WARN, f"job/{label}", "is not loaded",
                                f"launchctl bootstrap gui/$UID {root}/{name}"))
            continue
        # launchctl list prints "PID\tstatus\tlabel". The status is the LAST exit code, which
        # says nothing about now -- a KeepAlive job that crashed and was restarted reports the
        # crash while running perfectly well. The PID column is what separates "died and came
        # back" from "died and stayed dead", and conflating them means either crying wolf over
        # every restart or missing a service that is actually gone.
        for line in loaded.splitlines():
            parts = line.split("\t")
            if len(parts) != 3 or parts[2] != label:
                continue
            pid, status = parts[0], parts[1]
            if status in ("0", "-") or status in BENIGN_EXIT:
                continue
            running = pid not in ("-", "0")
            how = SIGNALS.get(status, f"status {status}")
            if running:
                out.append(_finding(
                    WARN, f"job/{label}",
                    f"last exited on {how} and has since restarted (now pid {pid})",
                    f"tail {STATE}/{label.split('.')[-1]}.err.log"))
            else:
                out.append(_finding(
                    FAIL, f"job/{label}", f"exited on {how} and is not running",
                    f"launchctl kickstart -k gui/$UID/{label}"))
    return out


def runs(conn) -> list[dict]:
    """Failed and skipped runs, which exist only in the database until something reads them."""
    out = []
    rows = conn.execute(
        "SELECT job, status, started_at, summary FROM run_log "
        "WHERE status IN ('error','skipped') AND started_at > datetime('now','-7 days') "
        "ORDER BY id DESC LIMIT 10").fetchall()
    for r in rows:
        level = FAIL if r["status"] == "error" else WARN
        out.append(_finding(level, f"run/{r['job']}",
                            f"{db.local(r['started_at'])} {r['status']}: "
                            f"{(r['summary'] or '')[:160]}"))

    # A run that started and never finished is a crash, and it looks like nothing at all.
    stuck = conn.execute(
        "SELECT job, started_at FROM run_log WHERE status = 'running' "
        "AND started_at < datetime('now','-2 hours') ORDER BY id DESC LIMIT 5").fetchall()
    for r in stuck:
        out.append(_finding(FAIL, f"run/{r['job']}",
                            f"still marked running since {db.local(r['started_at'])} — "
                            f"the process died without recording an outcome"))
    return out


def sweep(conn) -> list[dict]:
    """Has the sweep actually run recently, and is anything holding its lock?"""
    out = []
    mark = os.path.join(STATE, "sync.json")
    try:
        with open(mark) as f:
            last = json.load(f).get("last_sweep")
    except (OSError, ValueError):
        last = None
    # sync.json records last_sweep as a float epoch, not an ISO string like the database uses.
    try:
        age = time.time() - float(last) if last is not None else None
    except (TypeError, ValueError):
        age = None
    if age is None:
        out.append(_finding(WARN, "sweep", "has never recorded a completed pass"))
    elif age > SWEEP_STALE_SECONDS:
        out.append(_finding(FAIL, "sweep",
                            f"last completed {int(age // 60)} min ago; it runs every 30",
                            "tail .state/sync.err.log, then launchctl kickstart -k "
                            "gui/$UID/page.akvaithi.synth.sync"))

    lock = os.path.join(STATE, "sweep.lock")
    if os.path.exists(lock):
        import fcntl
        try:
            fh = open(lock, "w")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                held = time.time() - os.path.getmtime(lock)
                if held > SWEEP_STALE_SECONDS:
                    out.append(_finding(
                        WARN, "sweep/lock",
                        f"held for {int(held // 60)} min — a pass may be wedged"))
            finally:
                fh.close()
        except OSError:
            pass
    return out


def backlogs(conn) -> list[dict]:
    """Work that is queued and not draining. Each of these is silent by design."""
    out = []
    ocr = conn.execute("SELECT count(*) FROM source "
                       "WHERE detail LIKE '%no text layer%'").fetchone()[0]
    if ocr > 50:
        out.append(_finding(WARN, "ocr", f"{ocr} scanned PDFs await OCR at "
                                         f"{os.environ.get('SYNTH_OCR_PER_SWEEP', '6')}/sweep"))
    unread = conn.execute("SELECT count(*) FROM mail_digest "
                          "WHERE reported_at IS NULL").fetchone()[0]
    if unread > 200:
        out.append(_finding(WARN, "mail", f"{unread} messages sorted but never surfaced"))
    return out


def inference(conn) -> list[dict]:
    """Local inference, and whether it is actually on the GPU.

    Two failures, and only the first is obvious. The tunnel can be down, which stops
    background reasoning outright. Or a model can be resident but largely on the CPU, which
    stops nothing and merely makes everything three times slower for no visible reason --
    qwen3.6 runs at 24% GPU offload on this box and benchmarks slower than an 8B model that
    fits. The probe records into service_health either way, so an outage has a start time
    rather than only a symptom.
    """
    from synth import ollama

    probe = ollama.health()
    state = ollama.record_health(conn, "ollama", probe)
    if not probe.get("up"):
        return [_finding(
            FAIL, "ollama",
            f"unreachable since {db.local(state['since'])} "
            f"({state['consecutive_bad']} consecutive failures): {probe.get('error')}",
            "launchctl kickstart -k gui/$UID/page.akvaithi.synth.ollama-tunnel")]

    out = []
    try:
        for m in ollama.loaded():
            if m["on_gpu"] < 90:
                out.append(_finding(
                    WARN, f"ollama/{m['name']}",
                    f"only {m['on_gpu']}% of it is on the GPU "
                    f"({m['vram'] / 1e9:.1f} of {m['size'] / 1e9:.1f} GB) — the rest runs on "
                    f"the CPU and is several times slower",
                    "use a model that fits in VRAM, or lower num_ctx"))
    except Exception as e:
        out.append(_finding(WARN, "ollama", f"could not read resident models: {e}"))
    return out


def reaction(conn) -> list[dict]:
    """The reaction queue: work waiting, work given up on, and whether writing is on.

    A queue that stops draining looks exactly like a quiet inbox, and a reactor left in
    dry-run looks exactly like a reactor that decided to do nothing. Both are worth saying
    out loud rather than inferring from silence.
    """
    from synth import worker

    out = []
    try:
        st = worker.status(conn)
    except Exception as e:
        return [_finding(WARN, "reactor", f"could not read the queue: {e}")]

    if st["pending"] > 25:
        out.append(_finding(WARN, "reactor/queue",
                            f"{st['pending']} events waiting — the worker may be stopped",
                            "synth work --status, then tail .state/worker.err.log"))
    if st["retired"]:
        out.append(_finding(WARN, "reactor/retired",
                            f"{st['retired']} event(s) were given up on after repeated "
                            f"failures and will not be retried"))
    if st["halted"]:
        out.append(_finding(WARN, "reactor", "halted: .state/HALT exists, so nothing is "
                                             "being written", "rm .state/HALT"))
    if st["writes_today"] >= st["cap"]:
        out.append(_finding(WARN, "reactor/cap",
                            f"{st['writes_today']} autonomous writes today has reached the "
                            f"daily cap of {st['cap']}; it is analysing but not writing"))
    if st.get("worker_dry_run") is None:
        out.append(_finding(WARN, "reactor", "no worker has recorded a pass — it may never "
                                             "have started",
                            "launchctl kickstart -k gui/$UID/page.akvaithi.synth.worker"))
    return out


def mirror(conn) -> list[dict]:
    """Mirror notes that have stopped updating.

    A note whose live hash differs from what Synth last wrote is read as a correction from
    Arun, and render() then refuses to touch it until the correction is accepted. That is the
    right behaviour for a real edit and a trap for a false one: Notes rewrites HTML on save,
    so a stored hash that drifts out of step holds the note for ever and nothing says so. The
    note simply stops being true, which is the worst way for a mirror to fail.

    Whether it is a real edit is decided by comparing the live text against a fresh render --
    equal means the divergence is formatting and `synth reconcile` is safe; different means
    Arun actually changed something and it should go through accept_correction.
    """
    from synth import notes_sync

    out = []
    try:
        held = notes_sync.pending_corrections(conn)
    except Exception as e:
        return [_finding(WARN, "mirror", f"could not read the mirror state: {e}")]
    # notes_mirror stores two hashes in two different spaces: last_written_hash/last_seen_hash
    # are over the raw HTML Notes returns, last_render_hash is over the extracted text. Only
    # the text comparison answers "did the content change", because Notes rewrites the HTML on
    # every save and the raw hashes therefore differ even when nothing did.
    bodies = {}
    try:
        bodies = {n["id"]: n.get("body", "")
                  for n in notes_sync.call("notes_dump",
                                           folder=notes_sync.config.NOTES_FOLDER, timeout=300)}
    except Exception:
        pass

    for row in held:
        doc = row["doc"]
        verdict = "unknown"
        if doc in notes_sync.DOCS and row["note_id"] in bodies:
            try:
                title, blocks = notes_sync.DOCS[doc](conn)
                fresh = db.text_hash(notes_sync.html_to_text(notes_sync.to_html(title, blocks)))
                live = db.text_hash(notes_sync.html_to_text(bodies[row["note_id"]]))
                verdict = "formatting only" if live == fresh else "a real edit"
            except Exception:
                verdict = "unknown"
        fix = ("synth reconcile" if verdict == "formatting only" else
               "read the note, then add_facts + accept_correction")
        out.append(_finding(
            WARN, f"mirror/{doc}",
            f"held since {db.local(row['last_edit_at'])} and no longer re-rendering "
            f"({verdict})", fix))
    return out


def budget_state(conn) -> list[dict]:
    from synth import budget
    b = budget.read_backoff()
    if b:
        return [_finding(WARN, "budget",
                         f"{b['kind']} limit in effect for another "
                         f"{b['seconds_left'] // 60} min (until {b['until']})")]
    return []


def logs(conn=None) -> list[dict]:
    """Anything in a .err.log that is not one of the lines these services always print."""
    out = []
    if not os.path.isdir(STATE):
        return out
    for name in sorted(os.listdir(STATE)):
        if not name.endswith(".err.log"):
            continue
        path = os.path.join(STATE, name)
        try:
            if os.path.getsize(path) == 0:
                continue
            with open(path, errors="replace") as f:
                # Only the tail matters: the reason to open one of these is always "what
                # happened just now".
                tail = f.readlines()[-400:]
        except OSError:
            continue
        real = [ln.rstrip() for ln in tail
                if any(sig in ln for sig in ERROR_SIGNAL)
                and not any(b in ln for b in BENIGN_LOG)]
        if real:
            out.append(_finding(WARN, f"log/{name}",
                                f"{len(real)} error line(s) in the last {len(tail)}: "
                                f"{real[-1][:140]}"))
    return out


CHECKS = (daemon, inference, jobs, runs, sweep, reaction, mirror, backlogs,
          budget_state, logs)


def run_all(conn) -> list[dict]:
    """Every check, with one refusing to take the others down with it."""
    out = []
    for check in CHECKS:
        try:
            out += check(conn) or []
        except Exception as e:
            out.append(_finding(WARN, check.__name__,
                                f"the check itself failed: {type(e).__name__}: {e}"))
    order = {FAIL: 0, WARN: 1, OK: 2}
    return sorted(out, key=lambda f: order.get(f["level"], 3))


def summary(findings: list[dict]) -> str:
    """One sentence, for a brief or a reminder where a table will not fit."""
    fails = [f for f in findings if f["level"] == FAIL]
    warns = [f for f in findings if f["level"] == WARN]
    if not fails and not warns:
        return "Everything Synth depends on is answering."
    parts = []
    if fails:
        parts.append(f"{len(fails)} broken ({', '.join(f['check'] for f in fails[:3])})")
    if warns:
        parts.append(f"{len(warns)} worth a look")
    return "; ".join(parts) + "."
