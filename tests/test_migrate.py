"""db.migrate, which every step of claims to be safe to run twice and none of proved.

It runs on a schema that is already current here, so what is under test is mostly the
idempotency: a second pass must find nothing to do, and in particular must not log a second
one-time predicate merge or reopen a run it already closed.
"""
from __future__ import annotations

from synth import db, predicates


def test_migrate_on_a_current_schema_does_the_one_time_work_and_stops(conn):
    first = db.migrate(conn)
    second = db.migrate(conn)

    # The one-time merge is marked by its action_log row, so it appears once and never again.
    assert any("merged" in step for step in first)
    assert not any("merged" in step for step in second)


def test_the_one_time_merge_is_logged_exactly_once(conn):
    db.migrate(conn)
    db.migrate(conn)
    db.migrate(conn)

    n = conn.execute("SELECT count(*) FROM action_log WHERE action = 'dedupe_predicates'"
                     ).fetchone()[0]
    assert n == 1


def test_an_abandoned_run_is_closed_rather_than_left_saying_running(conn):
    """A run killed mid-flight leaves its row saying 'running' for ever, which makes both
    `status` and the ledger lie."""
    conn.execute("INSERT INTO run_log (job, started_at, status) VALUES (?,?,?)",
                 ("enrich", "2020-01-01 00:00:00", "running"))
    conn.commit()

    steps = db.migrate(conn)
    assert any("abandoned" in s for s in steps)
    assert conn.execute("SELECT status FROM run_log").fetchone()[0] == "error"


def test_a_recent_running_run_is_left_alone(conn):
    """Only runs older than two hours are presumed dead; killing a live one would be worse."""
    conn.execute("INSERT INTO run_log (job, started_at, status) VALUES "
                 "(?, datetime('now'), ?)", ("sync", "running"))
    conn.commit()

    db.migrate(conn)
    assert conn.execute("SELECT status FROM run_log").fetchone()[0] == "running"


def test_run_log_accepts_the_skipped_status(conn):
    """Added after the fact; SQLite cannot alter a CHECK constraint, so migrate rebuilds
    the table rather than dropping the troubleshooting record."""
    db.migrate(conn)
    conn.execute("INSERT INTO run_log (job, status) VALUES (?,?)", ("triage", "skipped"))
    conn.commit()
    assert conn.execute("SELECT status FROM run_log").fetchone()[0] == "skipped"


def test_migrate_backfills_predicate_key_for_rows_that_predate_the_column(conn):
    eid = conn.execute("INSERT INTO entity (kind, name) VALUES ('person','X') RETURNING id"
                       ).fetchone()[0]
    conn.execute("INSERT INTO assertion (entity_id, predicate, value_text) VALUES (?,?,?)",
                 (eid, "overall GPA on the audit", "3.9"))
    conn.commit()

    db.migrate(conn)
    key = conn.execute("SELECT predicate_key FROM assertion").fetchone()[0]
    assert key == predicates.key("overall GPA on the audit")


def test_migrate_collapses_same_key_duplicates_it_finds(conn):
    eid = conn.execute("INSERT INTO entity (kind, name) VALUES ('person','X') RETURNING id"
                       ).fetchone()[0]
    for spelling, when in (("overall GPA on the audit", "2026-01-01 00:00:00"),
                           ("overall GPA per the audit", "2026-02-01 00:00:00")):
        conn.execute("INSERT INTO assertion (entity_id, predicate, value_text, observed_at) "
                     "VALUES (?,?,?,?)", (eid, spelling, "3.9", when))
    conn.commit()

    db.migrate(conn)
    live = conn.execute("SELECT predicate FROM assertion WHERE superseded_by IS NULL"
                        ).fetchall()
    assert [r[0] for r in live] == ["overall GPA per the audit"]
