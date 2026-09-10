"""The queue between "something arrived" and "something was done about it".

These invariants are the ones that decide whether work is lost or repeated. The reactor's
judgement can be wrong and a person will notice; a queue that drops an event is silent, and a
queue that hands the same event out twice creates a duplicate in Arun's reminders.
"""
from __future__ import annotations

import json

import pytest

from synth import watcher, worker


def _mail(mid, index=1, account="Work"):
    return {"kind": "mail_new", "messageId": mid, "account": account, "index": index,
            "subject": "s", "sender": "f@x", "receivedAt": "2026-09-10T00:00:00Z"}


@pytest.fixture(autouse=True)
def now_enough(monkeypatch):
    """The debounce holds a row back for a few seconds; tests should not wait it out."""
    monkeypatch.setattr(worker, "DEBOUNCE_SECONDS", 0)


# ---------------------------------------------------------------- identity

def test_the_same_message_is_never_enqueued_twice(conn):
    watcher.enqueue(conn, [_mail("<a@x>")])
    watcher.enqueue(conn, [_mail("<a@x>", index=9)])
    assert conn.execute("SELECT count(*) FROM reaction_queue").fetchone()[0] == 1


def test_a_second_different_edit_of_a_note_is_new_work(conn):
    """A note's identity is the note AND its content: re-detecting one edit every ninety
    seconds is not new work, but a second, different edit is."""
    first = {"kind": "note_edited", "noteId": "n1", "doc": "d", "hash": "h1"}
    second = {"kind": "note_edited", "noteId": "n1", "doc": "d", "hash": "h2"}
    watcher.enqueue(conn, [first])
    watcher.enqueue(conn, [first])
    watcher.enqueue(conn, [second])
    assert conn.execute("SELECT count(*) FROM reaction_queue").fetchone()[0] == 2


def test_eventkit_collapses_to_one_pending_row(conn):
    """The work is a diff, so five notifications and one notification find the same thing.
    A constant key against a UNIQUE column would also mean EventKit could be queued once in
    the lifetime of the database and never again."""
    watcher.enqueue(conn, [{"kind": "eventkit_changed"}] * 5)
    assert conn.execute("SELECT count(*) FROM reaction_queue "
                        "WHERE kind='eventkit_changed'").fetchone()[0] == 1


def test_eventkit_can_be_queued_again_once_the_last_one_is_done(conn):
    watcher.enqueue(conn, [{"kind": "eventkit_changed"}])
    conn.execute("UPDATE reaction_queue SET done_at = datetime('now')")
    conn.commit()
    watcher.enqueue(conn, [{"kind": "eventkit_changed"}])
    assert conn.execute("SELECT count(*) FROM reaction_queue WHERE done_at IS NULL"
                        ).fetchone()[0] == 1


def test_fsevents_noise_is_never_queued(conn):
    """~/Library/Mail fired 9,355 times in the period documents fired 1,307, because Mail
    rewrites its store constantly. Treating those as work drove 800 reactor runs that
    produced 13 reminders between them."""
    watcher.enqueue(conn, [{"kind": "mail_changed", "detail": "/some/path"},
                           {"kind": "notes_changed", "detail": "/other"},
                           {"kind": "documents_changed", "detail": "/third"}])
    assert conn.execute("SELECT count(*) FROM reaction_queue").fetchone()[0] == 0


# ---------------------------------------------------------------- claiming and releasing

def test_a_claimed_row_is_not_handed_out_again(conn):
    """The sweep and the worker are separate processes. Without a claim, the same message is
    reacted to twice -- which is a duplicate in Arun's list, not a wasted run."""
    watcher.enqueue(conn, [_mail("<a@x>")])
    assert len(worker.claim(conn)) == 1
    assert worker.claim(conn) == []


def test_an_event_a_run_did_not_handle_stays_pending(conn):
    """The old code cleared the queue whenever the reactor returned, including when every
    call had been refused. Two days of detected mail was discarded that way and never came
    back."""
    watcher.enqueue(conn, [_mail("<a@x>"), _mail("<b@x>")])
    claimed = worker.claim(conn)
    worker.release(conn, claimed, handled=[claimed[0]], error="ran out of turns")

    assert conn.execute("SELECT count(*) FROM reaction_queue WHERE done_at IS NOT NULL"
                        ).fetchone()[0] == 1
    still = conn.execute("SELECT attempts, claimed_at FROM reaction_queue "
                         "WHERE done_at IS NULL").fetchone()
    assert still["attempts"] == 1
    assert still["claimed_at"] is None, "a requeued row must be claimable again"


def test_a_row_that_has_failed_three_times_stops_being_claimed(conn):
    """A poison event must not spin the worker for ever."""
    watcher.enqueue(conn, [_mail("<a@x>")])
    for _ in range(worker.MAX_ATTEMPTS):
        claimed = worker.claim(conn)
        if not claimed:
            break
        worker.release(conn, claimed, handled=[], error="it keeps failing")
    assert worker.claim(conn) == []
    row = conn.execute("SELECT done_at, last_error FROM reaction_queue").fetchone()
    assert row["done_at"] is not None
    assert row["last_error"], "a retired row must say why it was given up on"


def test_nothing_is_ever_deleted_from_the_queue(conn):
    """`was this message ever reacted to` has to stay answerable."""
    watcher.enqueue(conn, [_mail("<a@x>")])
    claimed = worker.claim(conn)
    worker.release(conn, claimed, handled=claimed)
    assert conn.execute("SELECT count(*) FROM reaction_queue").fetchone()[0] == 1


# ---------------------------------------------------------------- the daily cap

def test_reconciliation_does_not_count_against_the_write_cap(conn):
    """sync_obligations records what EventKit already says: it creates nothing and changes
    nothing of Arun's, and it runs on every EventKit notification. Counting it spends the
    daily cap on bookkeeping and leaves nothing for the writes the cap exists to bound."""
    from synth import db

    with db.run(conn, "reactor", trigger="schedule") as run_id:
        db.log_action(conn, "sync_obligations", "obligation", "reconciled against Reminders",
                      run_id=run_id)
    assert worker.writes_today(conn) == 0


def test_a_real_autonomous_write_does_count(conn):
    from synth import db

    with db.run(conn, "reactor", trigger="mail") as run_id:
        db.log_action(conn, "create_reminder", "reminder",
                      "the message states a deadline he is not tracking", run_id=run_id)
    assert worker.writes_today(conn) == 1


def test_a_write_from_a_brief_is_not_a_reactor_write(conn):
    from synth import db

    with db.run(conn, "brief", trigger="morning") as run_id:
        db.log_action(conn, "create_reminder", "reminder", "point at the brief", run_id=run_id)
    assert worker.writes_today(conn) == 0


# ---------------------------------------------------------------- the mode file

def test_status_reports_the_workers_mode_not_this_shells(conn, tmp_path, monkeypatch):
    """`synth work --status` runs in a different process from the worker. Reading
    reactor.DRY_RUN there reports the SHELL's environment -- and the worker gets
    SYNTH_REACTOR_DRY_RUN=1 from its plist while the shell does not, so status said
    "dry_run: false" about a worker that was writing nothing."""
    monkeypatch.setattr(worker, "MODE_FILE", str(tmp_path / "worker.json"))
    (tmp_path / "worker.json").write_text(json.dumps({"pid": 42, "dry_run": True,
                                                      "at": "2026-09-10T00:00:00+00:00"}))
    assert worker.status(conn)["worker_dry_run"] is True
    assert worker.status(conn)["worker_pid"] == 42


def test_status_says_nothing_rather_than_guessing_when_no_worker_has_run(conn, tmp_path,
                                                                        monkeypatch):
    monkeypatch.setattr(worker, "MODE_FILE", str(tmp_path / "absent.json"))
    assert worker.status(conn)["worker_dry_run"] is None
