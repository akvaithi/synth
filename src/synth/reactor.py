"""Acting on what arrived, without being asked.

This is the thing that was removed on 2026-08-26 and the thing everything else in this rebuild
exists to make safe. What it does is unchanged in shape: read what the watcher detected, decide
whether anything needs doing, and do the small closed list of things it is allowed to do.

What changed is the price of being wrong.

* The reasoning is free and local, so nothing here is rationed by money -- which means money is
  no longer accidentally doing the rate limiting. On 2026-08-25 it made 1,372 runs and the
  budget refused 1,355 of them; that refusal was the only thing holding the line, and it is
  gone.
* Every run is capped by `agent.run`: four writes, twelve turns, ten minutes, and a repeated
  call is answered from the first result instead of executed again.
* Every write carries a run_id, so "what did this reaction do" is one query.
* `.state/HALT` stops it at the next write without unloading anything.

## The three kinds, and why they are not alike

**mail_new** is the one that justifies the whole thing. It reads the body -- which it could not
do until the mailbox index was carried on the event -- and files the deadline the message
states.

**note_edited** is a correction from Arun. It is read, never written back to; the reactor has
no note tools at all, because the activity note renders the action log and a reactor that could
write notes would be writing into its own input.

**eventkit_changed** never reaches a model. It schedules a free diff -- sync_obligations
against EventKit -- and only if that diff finds something a person would care about is the
result described to one. This is what makes Synth's own reminder writes cost nothing instead of
scheduling a run to look at them.
"""
from __future__ import annotations

import json
import os

from synth import agent, db, registry

# Beyond this a single run carries so much unrelated material that judgement degrades, and a
# run that stops half way leaves the work silently incomplete -- which is how a reply that had
# already been sent got a reminder telling Arun to send it.
MAX_EVENTS_PER_RUN = int(os.environ.get("SYNTH_MAX_EVENTS_PER_RUN", "12"))

# The most a single batch may act on. Asked to sort twelve subjects, the old triage called six
# actionable -- including a football ticket promotion. A ceiling turns a generous grader into
# one that has to rank, and the overflow is named in the brief rather than lost.
MAX_ACTS_PER_BATCH = int(os.environ.get("SYNTH_MAX_ACTS", "4"))

DRY_RUN = os.environ.get("SYNTH_REACTOR_DRY_RUN", "") not in ("", "0", "false")

# What the reactor may touch, per kind. Absent from every one of them, deliberately:
#   create_reminders   bulk; the doctrine says a bulk write is something Arun is asked about
#   draft_email        drafting on his behalf unprompted is not on the closed list
#   create_note/append_note/*_document   writing into its own input, and the highest-
#                      consequence writes it has
#   undo               reversing its own past decisions unattended
#   every delete       registry.AUTONOMOUS does not contain them at all
MAIL_TOOLS = ["search", "entity", "obligations", "already_scheduled", "agenda", "conflicts",
              "mail_read", "mail_links", "mail_attachments", "read_attachment",
              "read_invitation", "create_reminder", "complete_reminder", "create_event",
              "update_obligation", "add_facts"]
NOTE_TOOLS = ["read_note", "search", "entity", "add_facts", "accept_correction"]
SCHEDULE_TOOLS = ["obligations", "agenda", "already_scheduled", "entity",
                  "update_reminder", "update_obligation"]

TOOLSETS = {"mail": MAIL_TOOLS, "note_edited": NOTE_TOOLS, "schedule": SCHEDULE_TOOLS}


def summarise(events: list[dict], cap: int = 60) -> str:
    """Compact a batch into something worth a model's attention."""
    by_kind: dict[str, list[dict]] = {}
    for e in events:
        by_kind.setdefault(e.get("kind", "unknown"), []).append(e)
    lines = []
    for kind, group in sorted(by_kind.items()):
        if kind == "mail_new":
            lines.append(f"### {len(group)} new message(s)")
            for e in group[:cap]:
                lines.append(f"- [{e.get('account')}] {e.get('receivedAt', '')} "
                             f"from {e.get('sender', '')}\n"
                             f"  subject: {e.get('subject', '')}\n"
                             f"  messageId: {e.get('messageId')}  index: {e.get('index', '?')}")
        elif kind == "note_edited":
            lines.append(f"### {len(group)} mirror note(s) Arun edited")
            for e in group:
                lines.append(f"- doc `{e.get('doc')}` (note {e.get('noteId')}) — read it and "
                             f"work out what he corrected")
        else:
            lines.append(f"### {kind} ({len(group)})")
            for e in group[:5]:
                if e.get("detail"):
                    lines.append(f"- {str(e['detail'])[:200]}")
    return "\n".join(lines) if lines else "(nothing)"


def _obligation_lines(conn, limit: int = 25) -> str:
    from synth import mailsync
    return mailsync._obligation_lines(conn, limit)


def refresh_indexes(conn, events: list[dict]) -> list[dict]:
    """Replace each mail event's recorded index with where the message actually is now.

    A mailbox index moves every time mail arrives, so the one captured at detection is stale
    by the time anything acts on it. The daemon verifies the Message-ID and refuses a
    mismatch, so a stale index is safe rather than wrong -- it simply returns nothing, and an
    empty body is the quiet failure that makes the reactor decide from the subject line alone.
    """
    from synth import triage as tri

    mail = [e for e in events if e.get("kind") == "mail_new"]
    if not mail:
        return events
    try:
        index = tri.resolve_indexes({e.get("account") for e in mail})
    except Exception:
        return events
    for e in mail:
        fresh = index.get(e.get("account"), {}).get(e.get("messageId"))
        if fresh:
            e["index"] = fresh
    return events


def reconcile_schedule(conn, run_id=None) -> dict:
    """The free half of an EventKit notification.

    Returns what actually moved. `completed` alone does not count as something a person needs
    told about -- Arun ticking a reminder off is the system working, not news.
    """
    from synth import tools

    diff = tools.sync_obligations(conn, run_id=run_id)
    interesting = {k: v for k, v in diff.items()
                   if k in ("adopted", "redated", "vanished", "reopened") and v}
    return {"diff": diff, "interesting": interesting}


def react(conn, events: list[dict], dry_run: bool | None = None,
          max_acts: int = MAX_ACTS_PER_BATCH) -> dict:
    """React to one batch. Returns the events it actually finished with.

    The `handled` contract is the important half of the return. Anything not in it stays
    queued: the old code cleared the queue whenever react() returned, including when every
    call had been refused, and two days of detected mail was discarded that way and never
    came back. Nothing here may throw away an event it did not actually deal with.
    """
    dry_run = DRY_RUN if dry_run is None else dry_run
    handled: list[dict] = []
    runs: list[dict] = []

    # EventKit first, and for free. If the diff found nothing, the batch is finished without a
    # model ever being asked -- which is the case every time Synth's own write caused it.
    schedule = [e for e in events if e.get("kind") == "eventkit_changed"]
    if schedule:
        with db.run(conn, "reactor", trigger="schedule") as run_id:
            try:
                result = reconcile_schedule(conn, run_id=run_id)
            except Exception as e:
                conn.execute("UPDATE run_log SET summary = ? WHERE id = ?",
                             (f"reconcile failed: {e}", run_id))
                conn.commit()
                result = None
            if result is not None:
                conn.execute("UPDATE run_log SET summary = ?, detail = ? WHERE id = ?",
                             (json.dumps(result["interesting"])[:2000],
                              json.dumps(result["diff"], default=str)[:4000], run_id))
                conn.commit()
                handled += schedule
                if result["interesting"]:
                    runs.append(_run_one(conn, "schedule",
                                         [{"kind": "eventkit_changed",
                                           "detail": json.dumps(result["interesting"],
                                                                default=str)}],
                                         conn_obligations=_obligation_lines(conn),
                                         dry_run=dry_run, max_acts=max_acts))

    rest = [e for e in events if e.get("kind") != "eventkit_changed"]
    rest = refresh_indexes(conn, rest)
    for kind in ("mail_new", "note_edited"):
        group = [e for e in rest if e.get("kind") == kind]
        for i in range(0, len(group), MAX_EVENTS_PER_RUN):
            part = group[i:i + MAX_EVENTS_PER_RUN]
            label = "mail" if kind == "mail_new" else kind
            outcome = _run_one(conn, label, part,
                               conn_obligations=_obligation_lines(conn),
                               dry_run=dry_run, max_acts=max_acts)
            runs.append(outcome)
            # Only a run that reached a conclusion has dealt with its events. One that ran out
            # of turns, time or writes left work behind by definition.
            if outcome.get("stop") in ("done", "writes"):
                handled += part

    return {"handled": handled, "runs": runs,
            "writes": sum(r.get("writes", 0) for r in runs)}


def _run_one(conn, label: str, events: list[dict], conn_obligations: str,
             dry_run: bool, max_acts: int) -> dict:
    from synth import runner

    tools = TOOLSETS.get(label if label in TOOLSETS else "mail")
    with db.run(conn, "reactor", trigger=label) as run_id:
        system = runner.prompt("reactor", core_only=True,
                               events=summarise(events),
                               obligations=conn_obligations)
        result = agent.run(
            conn, job="reactor", trigger=label,
            system=system,
            user=("Decide what, if anything, needs doing about the events above. Doing "
                  "nothing is a valid and frequent answer. When you have decided, say what "
                  "you concluded in a few lines and stop."),
            tool_names=[t for t in tools if t in registry.AUTONOMOUS],
            tier="reason", max_turns=12, max_writes=max_acts, wall_seconds=420,
            run_id=run_id, dry_run=dry_run)
        agent.record(conn, run_id, result)
    result["label"] = label
    result["events"] = len(events)
    return result
