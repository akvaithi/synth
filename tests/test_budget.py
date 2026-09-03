"""The ledger that decides whether Synth is allowed to spend anything.

db.now() is ISO 8601 with a T and an offset; run_log.started_at comes from SQLite's datetime()
and is 'YYYY-MM-DD HH:MM:SS'. Comparing them as strings is silently wrong -- a space sorts
below 'T', so an epoch written by db.now() excluded every row and the ledger reported $0.00 no
matter what had been spent. A budget that reads zero is worse than no budget at all.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from synth import budget


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    """Keep the epoch and backoff files out of the real .state directory."""
    monkeypatch.setattr(budget, "STATE", str(tmp_path))
    monkeypatch.setattr(budget, "BACKOFF", str(tmp_path / "backoff.json"))
    monkeypatch.setattr(budget, "EPOCH", str(tmp_path / "budget-epoch.json"))


def _run(conn, job, cost, hours_ago=1.0, output_tokens=100):
    started = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
               ).strftime("%Y-%m-%d %H:%M:%S")
    detail = json.dumps({"total_cost_usd": cost, "usage": {"output_tokens": output_tokens}})
    conn.execute("INSERT INTO run_log (job, started_at, status, detail) VALUES (?,?,?,?)",
                 (job, started, "ok", detail))
    conn.commit()


# ---------------------------------------------------------------- _sqlite_ts

def test_an_iso_timestamp_is_reshaped_to_what_started_at_actually_holds():
    assert budget._sqlite_ts("2026-09-03T16:00:00+00:00") == "2026-09-03 16:00:00"


def test_an_offset_is_converted_to_utc_not_merely_dropped():
    """Dropping the offset would move the epoch by hours and quietly change what it excludes."""
    assert budget._sqlite_ts("2026-09-03T11:00:00-05:00") == "2026-09-03 16:00:00"


def test_a_timestamp_already_in_sqlite_shape_survives_unchanged():
    assert budget._sqlite_ts("2026-09-03 16:00:00") == "2026-09-03 16:00:00"


def test_the_normalised_form_sorts_against_started_at_the_way_a_reader_expects():
    """The whole bug in one assertion: the raw ISO form sorts ABOVE a same-instant
    started_at, so an epoch written with it excluded rows it should have counted."""
    started_at = "2026-09-03 16:00:00"
    raw = "2026-09-03T16:00:00+00:00"
    assert raw > started_at
    assert budget._sqlite_ts(raw) <= started_at


def test_nothing_normalises_to_nothing():
    assert budget._sqlite_ts(None) is None
    assert budget._sqlite_ts("") is None
    assert budget._sqlite_ts("not a time") == "not a time"


# ---------------------------------------------------------------- the ledger

def test_spend_is_totalled_across_the_window(conn):
    _run(conn, "triage", 0.05)
    _run(conn, "triage", 0.05)
    _run(conn, "enrich", 0.30)
    s = budget._sum(budget._rows(conn, budget._ago(5)))
    assert s["spend"] == pytest.approx(0.40)
    assert s["by_job"] == {"triage": pytest.approx(0.10), "enrich": pytest.approx(0.30)}


def test_a_refused_call_costs_nothing_and_is_not_counted_as_a_run(conn):
    """Counting them is how a past incident poisoned the rolling window and blocked a
    legitimate day."""
    _run(conn, "triage", 0.05)
    for _ in range(20):
        _run(conn, "triage", 0.0)
    s = budget._sum(budget._rows(conn, budget._ago(5)))
    assert s["runs"] == 1
    assert s["spend"] == pytest.approx(0.05)


def test_a_run_outside_the_window_is_not_counted(conn):
    _run(conn, "enrich", 0.30, hours_ago=9)
    assert budget._sum(budget._rows(conn, budget._ago(5)))["spend"] == 0.0
    assert budget._sum(budget._rows(conn, budget._ago(24)))["spend"] == pytest.approx(0.30)


def test_a_run_with_no_usable_detail_is_skipped_rather_than_crashing(conn):
    conn.execute("INSERT INTO run_log (job, started_at, status, detail) VALUES (?,?,?,?)",
                 ("triage", "2026-09-03 16:00:00", "ok", "not json at all"))
    conn.commit()
    _run(conn, "triage", 0.05)
    assert budget._sum(budget._rows(conn, budget._ago(5)))["spend"] == pytest.approx(0.05)


def test_an_epoch_written_by_db_now_still_counts_the_rows_after_it(conn):
    """The regression this module's docstring is about: set_epoch stores db.now(), _rows
    compares it against started_at, and the shapes have to match or everything is excluded."""
    _run(conn, "triage", 0.05, hours_ago=2)
    budget.set_epoch(conn, "test")
    # Negative: a run started after the epoch was written. Stated as a future offset so the
    # ordering is exact rather than a race against the second the epoch landed in.
    _run(conn, "triage", 0.07, hours_ago=-0.05)

    s = budget._sum(budget._rows(conn, budget._ago(5)))
    assert s["spend"] == pytest.approx(0.07), "the epoch excluded a run that came after it"


def test_an_epoch_excludes_what_came_before_it(conn):
    _run(conn, "enrich", 5.00, hours_ago=2)
    budget.set_epoch(conn, "architecture changed")
    assert budget._sum(budget._rows(conn, budget._ago(5)))["spend"] == 0.0


def test_setting_an_epoch_is_logged_like_any_other_write(conn):
    budget.set_epoch(conn, "retuned after the autonomy removal")
    row = conn.execute("SELECT action, reason FROM action_log").fetchone()
    assert row["action"] == "budget_epoch"
    assert "retuned after the autonomy removal" in row["reason"]


# ---------------------------------------------------------------- backoff

def test_an_expired_backoff_is_treated_as_absent(tmp_path):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with open(budget.BACKOFF, "w") as f:
        json.dump({"until": past, "kind": "session"}, f)
    assert budget.read_backoff() is None


def test_a_live_backoff_reports_how_long_is_left(tmp_path):
    future = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    with open(budget.BACKOFF, "w") as f:
        json.dump({"until": future, "kind": "session"}, f)
    b = budget.read_backoff()
    assert b["kind"] == "session"
    assert 0 < b["seconds_left"] <= 1800


def test_a_missing_or_malformed_backoff_file_is_not_a_backoff(tmp_path):
    assert budget.read_backoff() is None
    with open(budget.BACKOFF, "w") as f:
        f.write("{not json")
    assert budget.read_backoff() is None
