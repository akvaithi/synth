"""What Synth is allowed to spend, and what to do when it has spent it.

Four days of running cost about $30 and produced 82 logged actions, 19 of which were Synth
retracting its own mistakes. Twice it exhausted a limit and then spent two full days calling
the API 574 times, being refused every time, doing nothing. Nothing in the system knew it had
been cut off, and nothing counted what it was spending.

This module is the part that knows. It reads the ledger that `run_log` has been keeping all
along -- every run already records `total_cost_usd` and its full `usage` block -- and it reads
the refusal message, which states plainly when the limit resets.

Two limits exist and they behave differently:

  * "You've hit your session limit · resets 6:40pm (America/Chicago)" -- the rolling 5-hour
    window. Recoverable, and it tells us exactly when.
  * "You've hit your monthly spend limit · raise it at claude.ai/..." -- extra-usage billing
    past the subscription. Arun's target is never to see this one again.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

from synth import config, db

STATE = os.path.expanduser("~/Developer/synth/.state")
BACKOFF = os.path.join(STATE, "backoff.json")
EPOCH = os.path.join(STATE, "budget-epoch.json")


# ---------------------------------------------------------------- reading the ledger


def _sqlite_ts(ts: str | None) -> str | None:
    """Normalise a timestamp to the shape run_log.started_at actually has.

    db.now() is ISO 8601 with a T and an offset; started_at comes from SQLite's datetime()
    and is 'YYYY-MM-DD HH:MM:SS'. Comparing them as strings is silently wrong -- a space
    sorts below 'T', so an epoch written by db.now() excluded every row and reported a ledger
    of $0.00 no matter what had been spent. A budget that reads zero is worse than none.
    """
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d.strftime("%Y-%m-%d %H:%M:%S")


def epoch() -> str | None:
    """Spend before this moment is not counted against the ceilings.

    Set once, deliberately, when the thing being measured changes -- the ledger exists to
    govern the current system, and carrying a previous architecture's burn into its budget
    would block the new one for a day to punish it for something it did not do. Nothing is
    deleted: run_log keeps every row, and `synth budget` says plainly that an epoch is in
    force and what it excludes.
    """
    try:
        with open(EPOCH) as f:
            return _sqlite_ts(json.load(f).get("since"))
    except (OSError, ValueError):
        return None


def set_epoch(conn, reason: str) -> dict:
    e = {"since": _sqlite_ts(db.now()), "reason": reason, "set_at": db.now()}
    os.makedirs(STATE, exist_ok=True)
    with open(EPOCH, "w") as f:
        json.dump(e, f, indent=2)
    db.log_action(conn, "budget_epoch", "db",
                  f"ledger epoch set — spend before {e['since']} no longer counts "
                  f"against the ceilings: {reason}",
                  after=e)
    return e


def _rows(conn, since: str):
    ep = epoch()
    if ep and ep > since:
        since = ep
    return conn.execute(
        "SELECT job, detail FROM run_log WHERE started_at >= ? AND detail LIKE '{%'",
        (since,)).fetchall()


def _sum(rows) -> dict:
    spend, runs, out_tokens = 0.0, 0, 0
    by_job: dict[str, float] = {}
    for r in rows:
        try:
            d = json.loads(r["detail"])
        except ValueError:
            continue
        cost = d.get("total_cost_usd") or 0.0
        spend += cost
        by_job[r["job"]] = by_job.get(r["job"], 0.0) + cost
        # A refused call costs nothing and is not a run for budgeting purposes. Counting them
        # is how a past incident poisoned the rolling window and blocked a legitimate day.
        if cost > 0:
            runs += 1
            out_tokens += (d.get("usage") or {}).get("output_tokens") or 0
    return {"spend": round(spend, 4), "runs": runs, "output_tokens": out_tokens,
            "by_job": {k: round(v, 4) for k, v in by_job.items()}}


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def state(conn) -> dict:
    """Spend across each window that matters, plus any active backoff."""
    return {
        "window_5h": _sum(_rows(conn, _ago(5))),
        "day": _sum(_rows(conn, _ago(24))),
        "week": _sum(_rows(conn, _ago(24 * 7))),
        "month": _sum(_rows(conn, _ago(24 * 30))),
        "backoff": read_backoff(),
    }


# ---------------------------------------------------------------- backoff


def read_backoff() -> dict | None:
    """The active backoff, or None. Expired backoffs are treated as absent."""
    try:
        with open(BACKOFF) as f:
            b = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        until = datetime.fromisoformat(b["until"])
    except (KeyError, ValueError):
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if until <= datetime.now(timezone.utc):
        return None
    b["seconds_left"] = int((until - datetime.now(timezone.utc)).total_seconds())
    return b


def clear_backoff() -> None:
    try:
        os.remove(BACKOFF)
    except OSError:
        pass


def _write_backoff(until: datetime, kind: str, message: str) -> dict:
    b = {"until": until.astimezone(timezone.utc).isoformat(), "kind": kind,
         "message": message[:300], "set_at": db.now()}
    os.makedirs(STATE, exist_ok=True)
    tmp = BACKOFF + ".tmp"
    with open(tmp, "w") as f:
        json.dump(b, f, indent=2)
    os.replace(tmp, BACKOFF)
    return b


# "You've hit your session limit · resets 6:40pm (America/Chicago)"
_SESSION = re.compile(
    r"session limit.*?resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap])m(?:\s*\(([^)]+)\))?",
    re.IGNORECASE | re.DOTALL)
_MONTHLY = re.compile(r"monthly spend limit|usage limit reached|credit balance", re.IGNORECASE)


def _next_occurrence(hour: int, minute: int, tzname: str | None) -> datetime:
    """The next time the clock reads hour:minute, in the stated zone."""
    tz = None
    if tzname:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tzname.strip())
        except Exception:
            tz = None
    now = datetime.now(tz) if tz else datetime.now().astimezone()
    reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    return reset


def note_failure(result: dict) -> dict | None:
    """Inspect a finished run. If a limit refused it, set the backoff and say so.

    The refusal message carries the reset time. Reading it is the difference between one
    quiet wait and 285 rejected calls in a day.
    """
    if not result.get("is_error"):
        return None
    text = str(result.get("result") or "") + " " + str(result.get("stderr") or "")
    m = _SESSION.search(text)
    if m:
        hour = int(m.group(1)) % 12
        minute = int(m.group(2) or 0)
        if m.group(3).lower() == "p":
            hour += 12
        until = _next_occurrence(hour, minute, m.group(4)) + timedelta(minutes=1)
        # The session window is five hours, so a reset can never be further off than that.
        # Without this cap a stale or misparsed message silences Synth for a whole day, which
        # is the failure this module exists to prevent, arrived at from the other direction.
        cap = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        return _write_backoff(min(until, cap), "session", text.strip())
    if _MONTHLY.search(text):
        # No stated reset. Check back hourly rather than hammering, and stay degraded.
        return _write_backoff(datetime.now(timezone.utc) + timedelta(hours=1),
                              "monthly_spend", text.strip())
    return None


# ---------------------------------------------------------------- the decision

# Priority order. The brief is the product Arun actually reads; the reactor is the expense.
# When the budget is tight the reactor yields first and the brief is the last thing to stop.
PRIORITY = {"brief": 0, "urgent": 1, "triage": 2, "reactor": 3, "enrich": 4}


def allowed(conn, job: str) -> tuple[bool, str]:
    """May `job` call the model right now? Returns (allowed, why not).

    Every caller must consult this immediately before spending, not once per batch. The old
    code checked once and then launched a run per chunk, so a single permission bought three
    or four runs.

    The reserve applies in every window, not just the 5-hour one. Reserving only there let a
    heavy reactor afternoon exhaust the daily ceiling and starve the morning brief, which is
    the one output Arun actually reads.
    """
    b = read_backoff()
    if b:
        mins = b["seconds_left"] // 60
        return False, (f"{b['kind']} limit in effect for another {mins} min "
                       f"(until {b['until']})")

    s = state(conn)
    is_brief = PRIORITY.get(job, 3) == 0

    # (window label, spent, ceiling, how much of it briefs keep to themselves)
    for label, spent, ceiling, held in (
        ("month-to-date", s["month"]["spend"], config.BUDGET_MONTH, config.BUDGET_MONTH_RESERVE),
        ("last 24h", s["day"]["spend"], config.BUDGET_DAY, config.BUDGET_DAY_RESERVE),
        ("5-hour window", s["window_5h"]["spend"], config.BUDGET_5H, config.BUDGET_5H_RESERVE),
    ):
        limit = ceiling if is_brief else ceiling - held
        if spent >= limit:
            note = "" if is_brief else f" (${held:.2f} of it is reserved for briefs)"
            return False, (f"{label} spend ${spent:.2f} has reached "
                           f"${limit:.2f}{note}")
    return True, ""


def log_skip(conn, job: str, trigger: str, why: str) -> int:
    """Record a run that did not happen. A quiet day must be explainable."""
    cur = conn.execute(
        "INSERT INTO run_log (job, trigger, finished_at, status, summary) "
        "VALUES (?,?,?,'skipped',?) RETURNING id", (job, trigger, db.now(), why))
    run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def summary(conn) -> str:
    """One human-readable paragraph, for `synth budget` and for the brief."""
    s = state(conn)
    lines = [
        f"5-hour window : ${s['window_5h']['spend']:>7.2f} of ${config.BUDGET_5H:.2f}"
        f"   ({s['window_5h']['runs']} runs)",
        f"last 24 hours : ${s['day']['spend']:>7.2f} of ${config.BUDGET_DAY:.2f}"
        f"   ({s['day']['runs']} runs)  {s['day']['by_job']}",
        f"last 7 days   : ${s['week']['spend']:>7.2f}   ({s['week']['runs']} runs)",
        f"last 30 days  : ${s['month']['spend']:>7.2f} of ${config.BUDGET_MONTH:.2f}"
        f"   ({s['month']['runs']} runs)",
    ]
    ep = epoch()
    if ep:
        lines.append(f"EPOCH         : counting from {db.local(ep)} "
                     f"(earlier spend is in run_log but not charged)")
    b = s["backoff"]
    if b:
        lines.append(f"BACKOFF       : {b['kind']} for another {b['seconds_left'] // 60} min "
                     f"— {b['message'][:120]}")
    else:
        lines.append("BACKOFF       : none")
    for job in ("brief", "reactor", "triage"):
        ok, why = allowed(conn, job)
        lines.append(f"{job:14}: {'allowed' if ok else 'BLOCKED — ' + why}")
    return "\n".join(lines)
