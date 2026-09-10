"""Logged mutations.

Every write goes through here, never through applekit.call() directly, so that nothing
Synth does can escape action_log. A write with no stated reason is refused.
"""
from __future__ import annotations

from synth import db
from synth.applekit import call


class UnexplainedWrite(ValueError):
    pass


# Reasons that explain nothing. The model created real reminders in Arun's list titled
# "test" while probing the API schema, then had to retract them. A reason is for him to read
# later, not a placeholder to satisfy a required field.
PLACEHOLDER_REASONS = {"test", "testing", "probe", "probing", "check", "checking", "debug",
                       "example", "sample", "foo", "bar", "tmp", "temp", "n/a", "none"}


# Markers of a write made to explore the tool rather than to help Arun. "test" alone is in
# PLACEHOLDER_REASONS above rather than here, because a student legitimately has reasons like
# "test results due Friday" -- these phrases do not appear in a real one.
#
# The second row was added after a real document write got through with the reason "Write
# test plus a dated marker recording that the 2026-08-25 rebuild exists". PLACEHOLDER_REASONS
# only matches a reason that is nothing but a placeholder, so a probe wrapped in a sentence
# passed. These phrases name the tool as the object of the test, which is what separates them
# from a chemical engineer's "test" -- "bench test", "tensile test" and "test results" all
# survive.
PROBE_MARKERS = ("schema", "no-op", "noop", "probing", "probe the", "placeholder",
                 "dummy", "debug", "scratch", "ignore this",
                 "write test", "test write", "tool test", "testing the tool",
                 "test of the tool", "test the tool", "smoke test")


def _require_reason(reason: str) -> str:
    if not reason or not reason.strip():
        raise UnexplainedWrite("every Synth write must carry a reason")
    cleaned = reason.strip()
    lowered = cleaned.lower()
    if lowered.rstrip(".!") in PLACEHOLDER_REASONS or len(cleaned) < 12:
        raise UnexplainedWrite(
            f"{cleaned!r} is not a reason. Say why this write helps Arun, in words he would "
            f"understand months from now.")
    if any(m in lowered for m in PROBE_MARKERS):
        raise UnexplainedWrite(
            f"{cleaned!r} reads as a write made to explore the tool, not to help Arun. "
            f"Never write to his calendar, reminders or mail to check how a tool behaves — "
            f"read the tool description, or exercise a read tool instead.")
    return cleaned


def _do(conn, action: str, target_kind: str, reason: str, params: dict, *,
        run_id=None, evidence_id=None):
    _require_reason(reason)
    result = call(action, **params)
    before = result.get("before")
    after = result.get("after") or result.get("created")
    target_id = (after or {}).get("id") if isinstance(after, dict) else None
    action_id = db.log_action(
        conn, action, target_kind, reason, run_id=run_id, target_id=target_id,
        evidence_id=evidence_id, before=before, after=after,
    )
    return action_id, result


def create_reminder(conn, *, title, reason, list=None, due=None, notes=None,
                    run_id=None, evidence_id=None):
    params = {"title": title}
    if list:
        params["list"] = list
    if due:
        params["due"] = due
    if notes:
        params["notes"] = notes
    return _do(conn, "create_reminder", "reminder", reason, params,
               run_id=run_id, evidence_id=evidence_id)


def complete_reminder(conn, *, id, reason, run_id=None, evidence_id=None):
    """Completion, never deletion. evidence_id is the mail that justified it."""
    return _do(conn, "complete_reminder", "reminder", reason, {"id": id},
               run_id=run_id, evidence_id=evidence_id)


def update_reminder(conn, *, id, reason, run_id=None, evidence_id=None, **fields):
    return _do(conn, "update_reminder", "reminder", reason, {"id": id, **fields},
               run_id=run_id, evidence_id=evidence_id)


def create_event(conn, *, title, start, reason, calendar=None, end=None, notes=None,
                 location=None, allDay=False, run_id=None, evidence_id=None):
    params = {"title": title, "start": start}
    for k, v in (("calendar", calendar), ("end", end), ("notes", notes), ("location", location)):
        if v:
            params[k] = v
    # Sent unconditionally: False is meaningful, and the loop above drops falsy values.
    params["allDay"] = bool(allDay)
    return _do(conn, "create_event", "event", reason, params,
               run_id=run_id, evidence_id=evidence_id)


def update_event(conn, *, id, reason, run_id=None, evidence_id=None, **fields):
    return _do(conn, "update_event", "event", reason, {"id": id, **fields},
               run_id=run_id, evidence_id=evidence_id)


# ---------------------------------------------------------------- deletes
#
# These exist because Arun prompting Synth directly should not be told "there is no way to do
# that" about his own calendar. They are not offered to the autonomous tier at all: the
# capability sets in mcp_server and agent decide who may reach them, and this module is only
# where the write and its log entry become inseparable.
#
# `before` is captured in full on every one of them, because for two of the three it is the
# only thing that could ever rebuild the item -- and rebuilding is not undoing. Each tool
# docstring says which it is rather than letting "undo" imply more than it can do.


def delete_reminder(conn, *, id, reason, run_id=None, evidence_id=None):
    """Remove a reminder. Reversible only as a recreation: EventKit issues a new identifier,
    so anything holding the old one -- an obligation row, a link -- is pointing at nothing."""
    return _do(conn, "delete_reminder", "reminder", reason, {"id": id},
               run_id=run_id, evidence_id=evidence_id)


def delete_event(conn, *, id, reason, run_id=None, evidence_id=None):
    """Remove a calendar event. Recreation, not restoration, for the same reason."""
    return _do(conn, "delete_event", "event", reason, {"id": id},
               run_id=run_id, evidence_id=evidence_id)


def delete_note(conn, *, id, reason, run_id=None):
    """Move a note to Recently Deleted, where Notes keeps it for thirty days.

    That is a better guarantee than anything here could offer and it is left as it is. The
    body is still recorded, because thirty days is not forever and because `why` should be
    able to show what was removed without asking Notes.
    """
    _require_reason(reason)
    result = call("notes_delete", id=id)
    before = result.get("before")
    action_id = db.log_action(conn, "delete_note", "note", reason, run_id=run_id,
                              target_id=id, before=before, after=None)
    return action_id, result


def notes_update(conn, *, id, body, reason, name=None, run_id=None):
    params = {"id": id, "body": body}
    if name:
        params["name"] = name
    _require_reason(reason)
    result = call("notes_update", **params)
    before = result.get("before")
    if isinstance(before, dict):
        before = {"id": id, **before}
    action_id = db.log_action(conn, "notes_update", "note", reason, run_id=run_id,
                              target_id=id, before=before, after={"id": id})
    return action_id, result


def write_document(conn, *, action, rel_path, abs_path, text, reason, expect_hash=None,
                   args=None, run_id=None, evidence_id=None):
    """Write one of Arun's files, keep its prior bytes, refresh the index, log all three.

    The one write that touches no Apple app, so it cannot go through _do() — but it belongs
    here all the same, because "the mutation and its log entry, inseparably" is what this
    module is for.

    The file is written before the index because the file is the source of truth: it is what
    syncs to his phone and what the next ingest re-reads. An index refreshed against a write
    that never landed would be a lie.
    """
    from synth import docwrite

    _require_reason(reason)
    before = docwrite.snapshot(rel_path, abs_path)      # None when the file is new

    # Optimistic concurrency. The snapshot is taken immediately before the write, so its
    # hash IS the just-before-write hash — one hash, not two. iCloud syncing an edit he made
    # on his phone into the middle of a read-modify-write is not hypothetical on this folder.
    if expect_hash and before and before["content_hash"] != expect_hash:
        raise ValueError(
            f"{rel_path!r} changed on disk between reading it and writing it — most likely "
            f"iCloud syncing an edit Arun made on his phone. Nothing was written. Read it "
            f"again with read_document and redo the edit against the new text.")

    docwrite.write_atomic(abs_path, text)

    # Read the file back before indexing or logging anything. write_atomic cannot half-write
    # -- it replaces -- but this folder is iCloud-synced, so another device or an open editor
    # can land on top of the bytes just written. Without this, reindex would hash whatever
    # won that race and the log would attribute it to this action, which is how an append
    # came to be recorded as having replaced two lines it never touched.
    landed = docwrite.read_back(abs_path)
    if landed != text:
        kept = (before or {}).get("backup")
        raise ValueError(
            f"{rel_path!r} does not contain what Synth just wrote — {len(text):,} characters "
            f"were written and {len(landed):,} are on disk. Something else wrote to the file "
            f"in the same moment, most likely iCloud syncing another device or an editor with "
            f"it open. Nothing was indexed and nothing was logged; the file as it stood before "
            f"this write is kept at {kept}. Close the file elsewhere, read it again, and redo "
            f"the edit against what is actually there.")

    index = docwrite.reindex(conn, rel_path, abs_path)
    action_id = db.log_action(
        conn, action, "document", reason, run_id=run_id, target_id=rel_path,
        evidence_id=evidence_id, before=before, args=args,
        after={"path": rel_path, "chars": index["chars"],
               "content_hash": index["content_hash"], "document_id": index["document_id"]})
    return action_id, index
