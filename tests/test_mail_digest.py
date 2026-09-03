"""The read half of the mail-triage contract.

triage has been filing every filtered message into `mail_digest` since the sender rules went
in; the brief that read the table was removed with the rest of the autonomy on 2026-08-26 and
nothing has read it since. These tests pin the two properties that make the tool worth having:
it answers from the index rather than from Mail, and reading it does not empty it.
"""
from __future__ import annotations

import json

import pytest

from synth import tools, triage


@pytest.fixture
def no_daemon(monkeypatch):
    """fill_links reaches Mail through the daemon. Nothing in this suite may."""
    called = []
    monkeypatch.setattr(triage, "fill_links",
                        lambda conn, rows, cap=12: called.append(len(rows)))
    return called


def _hold(conn, n=3, verdict="digest"):
    triage.hold(conn, [
        {"messageId": f"m{i}@example.com", "account": "Work", "sender": "news@e2ma.net",
         "subject": f"subject {i}", "receivedAt": f"2026-09-0{i + 1}T10:00:00Z",
         "verdict": verdict, "decided_by": "bulk sender domain e2ma.net", "index": i}
        for i in range(n)])


def test_an_empty_queue_reads_as_empty(conn):
    out = tools.mail_digest(conn)
    assert out == {"waiting": 0, "shown": 0, "by_verdict": {}, "messages": [],
                   "marked_reported": 0}


def test_it_returns_what_was_filed_with_the_rule_that_filed_it(conn):
    _hold(conn, 2)
    out = tools.mail_digest(conn)

    assert out["waiting"] == 2
    assert out["shown"] == 2
    assert out["by_verdict"] == {"digest": 2}
    assert out["messages"][0]["decided_by"] == "bulk sender domain e2ma.net"


def test_times_carry_a_local_rendering(conn):
    """Every surface that hands a time to a model carries both forms."""
    _hold(conn, 1)
    row = tools.mail_digest(conn)["messages"][0]
    assert row["received_at"] == "2026-09-01T10:00:00Z"
    assert row["received_at_local"]


def test_reading_does_not_clear_the_queue(conn):
    """A question answered is not a queue emptied."""
    _hold(conn, 3)
    assert tools.mail_digest(conn)["marked_reported"] == 0
    assert tools.mail_digest(conn)["waiting"] == 3


def test_marking_is_the_deliberate_act_that_clears_it(conn):
    _hold(conn, 3)
    out = tools.mail_digest(conn, mark=True)
    assert out["marked_reported"] == 3
    assert tools.mail_digest(conn)["waiting"] == 0


def test_marking_only_clears_what_was_actually_shown(conn):
    """Clearing the whole table off the back of a bounded read would lose the remainder
    without anyone having seen it."""
    _hold(conn, 5)
    out = tools.mail_digest(conn, limit=2, mark=True)

    assert out["marked_reported"] == 2
    assert tools.mail_digest(conn)["waiting"] == 3


def test_a_bounded_answer_is_never_mistakable_for_a_complete_one(conn):
    _hold(conn, 5)
    out = tools.mail_digest(conn, limit=2)

    assert out["waiting"] == 5
    assert out["shown"] == 2
    assert "3 more message(s) not shown" in out["note"]


def test_the_default_read_never_reaches_mail(conn, no_daemon):
    """The whole point of answering from the index: filling links re-resolves mailbox
    indexes against live Mail and took 99 seconds on the first call against a backlog."""
    _hold(conn, 3)
    tools.mail_digest(conn)
    assert no_daemon == [], "the default read went to Mail anyway"


def test_asking_for_links_is_what_reaches_mail(conn, no_daemon):
    _hold(conn, 3)
    tools.mail_digest(conn, links=True)
    assert no_daemon == [3]


def test_links_come_back_as_a_list_not_a_json_string(conn):
    _hold(conn, 1)
    conn.execute("UPDATE mail_digest SET links = ?",
                 (json.dumps(["https://example.com/a"]),))
    conn.commit()
    assert tools.mail_digest(conn)["messages"][0]["links"] == ["https://example.com/a"]


def test_malformed_stored_links_do_not_break_the_answer(conn):
    _hold(conn, 1)
    conn.execute("UPDATE mail_digest SET links = ?", ("not json",))
    conn.commit()
    assert tools.mail_digest(conn)["messages"][0]["links"] == []


def test_the_verdicts_are_counted_separately(conn):
    _hold(conn, 2, verdict="digest")
    triage.hold(conn, [{"messageId": "z@example.com", "account": "Work",
                        "sender": "sales@example.com", "subject": "50% off",
                        "receivedAt": "2026-09-05T10:00:00Z", "verdict": "ignore",
                        "decided_by": "self-explanatory marketing"}])
    assert tools.mail_digest(conn)["by_verdict"] == {"ignore": 1, "digest": 2}


def test_a_failure_reaching_mail_costs_the_links_not_the_answer(conn, monkeypatch):
    """A mailbox index moves whenever mail arrives, and the daemon is allowed to be down."""
    def boom(conn, rows, cap=12):
        raise RuntimeError("cannot reach synthd")

    monkeypatch.setattr(triage, "fill_links", boom)
    _hold(conn, 2)
    out = tools.mail_digest(conn, links=True)

    assert out["shown"] == 2
    assert "cannot reach synthd" in out["messages"][0]["links_error"]
