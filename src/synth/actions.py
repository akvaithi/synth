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


def _require_reason(reason: str) -> str:
    if not reason or not reason.strip():
        raise UnexplainedWrite("every Synth write must carry a reason")
    cleaned = reason.strip()
    if cleaned.lower().rstrip(".!") in PLACEHOLDER_REASONS or len(cleaned) < 12:
        raise UnexplainedWrite(
            f"{cleaned!r} is not a reason. Say why this write helps Arun, in words he would "
            f"understand months from now. Never create a real reminder to probe the API.")
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
                 location=None, run_id=None, evidence_id=None):
    params = {"title": title, "start": start}
    for k, v in (("calendar", calendar), ("end", end), ("notes", notes), ("location", location)):
        if v:
            params[k] = v
    return _do(conn, "create_event", "event", reason, params,
               run_id=run_id, evidence_id=evidence_id)


def update_event(conn, *, id, reason, run_id=None, evidence_id=None, **fields):
    return _do(conn, "update_event", "event", reason, {"id": id, **fields},
               run_id=run_id, evidence_id=evidence_id)


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
