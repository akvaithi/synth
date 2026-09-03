"""UTC in the database, local time on every human-facing surface.

On 2026-08-25 a brief reported a 1:50 PM class at 6:50 PM, a 4:10 PM class at 9:10 PM and a
10:00 AM lab visit at 3:00 PM, while a reminder in the same brief converted correctly. That
mix is the signature of arithmetic done by hand, and db.localize exists so no model is ever
asked to do it. The case that actually failed is the one that crosses a day boundary.

The zone cannot be pinned here: this Python build has no `time.tzset`, so setting TZ would not
reach `datetime.astimezone()`. Everything that can be asserted without a zone is asserted
unconditionally; the literal renderings that caused the incident run on a machine in the zone
they were recorded in, which is this VM.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from synth import db

CHICAGO = datetime.now().astimezone().tzname() in ("CDT", "CST")
in_chicago = pytest.mark.skipif(not CHICAGO, reason="literal renderings are zone-specific")


# ---------------------------------------------------------------- zone-independent

def test_a_naive_timestamp_is_read_as_utc_not_as_local():
    """SQLite's datetime('now') has no offset and is UTC. Reading it as local would shift
    every row by the offset and look entirely plausible while doing it."""
    assert db.local("2026-08-27 00:15:00", db.LOCAL_FMT) == \
        db.local("2026-08-27T00:15:00Z", db.LOCAL_FMT)


def test_the_conversion_runs_in_the_right_direction():
    """A wrong-direction conversion is what turned 1:50 PM into 6:50 PM. Rendering an instant
    and reading it back through the local offset has to land on the instant we started from."""
    instant = datetime(2026, 8, 25, 18, 50, tzinfo=timezone.utc)
    rendered = db.local(instant.isoformat(), "%Y-%m-%d %H:%M")
    back = datetime.strptime(rendered, "%Y-%m-%d %H:%M").astimezone()
    assert back == instant


def test_the_rendering_carries_the_date_and_the_weekday():
    """The date is not decoration: converting from UTC moves the day for anything late in the
    evening, which is how a Wednesday reminder was reported as Thursday."""
    rendered = db.local("2026-08-27T00:15:00Z", db.LOCAL_FMT)
    assert rendered.split()[0] in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    assert "2026-08-2" in rendered
    assert rendered.endswith(("AM", "PM"))


def test_a_day_apart_renders_a_day_apart():
    a = datetime(2026, 8, 26, 23, 15, tzinfo=timezone.utc)
    b = a + timedelta(days=1)
    assert db.local(a.isoformat(), "%Y-%m-%d") != db.local(b.isoformat(), "%Y-%m-%d")


def test_an_unparseable_timestamp_comes_back_unchanged_rather_than_crashing():
    assert db.local("not a time") == "not a time"
    assert db.local(None) == ""
    assert db.local("") == ""


def test_localize_adds_a_reading_beside_the_value_without_replacing_it():
    """The ISO value is what arithmetic and writes must keep using."""
    rows = [{"due": "2026-08-27T00:15:00Z", "title": "renew parking"}]
    db.localize(rows, "due")
    assert rows[0]["due"] == "2026-08-27T00:15:00Z"
    assert rows[0]["due_local"] == db.local("2026-08-27T00:15:00Z", db.LOCAL_FMT)


def test_localize_handles_a_single_row_and_several_keys():
    row = {"start": "2026-08-25T18:50:00Z", "end": "2026-08-25T19:40:00Z"}
    db.localize(row, "start", "end")
    assert row["start_local"] and row["end_local"]
    assert row["start_local"] != row["end_local"]


def test_localize_skips_missing_and_empty_values():
    rows = [{"due": None}, {"due": ""}, {}]
    db.localize(rows, "due")
    assert not any("due_local" in r for r in rows)


def test_localize_leaves_a_non_dict_row_alone():
    rows = ["not a row", {"due": "2026-08-27T00:15:00Z"}]
    db.localize(rows, "due")
    assert rows[0] == "not a row"


# ---------------------------------------------------------------- the recorded incident

@in_chicago
def test_a_utc_timestamp_after_7pm_local_belongs_to_the_previous_day():
    """A reminder due Wednesday 7:15 PM was stored as 2026-08-27T00:15:00Z and reported
    as Thursday."""
    assert db.local("2026-08-27T00:15:00Z", db.LOCAL_FMT) == "Wed 2026-08-26 7:15 PM"


@in_chicago
def test_the_class_times_that_were_reported_five_hours_late():
    assert db.local("2026-08-25T18:50:00Z", db.LOCAL_FMT) == "Tue 2026-08-25 1:50 PM"
    assert db.local("2026-08-25T21:10:00Z", db.LOCAL_FMT) == "Tue 2026-08-25 4:10 PM"
    assert db.local("2026-08-25T15:00:00Z", db.LOCAL_FMT) == "Tue 2026-08-25 10:00 AM"


@in_chicago
def test_tzname_reports_the_zone_the_renderings_are_in():
    assert db.tzname() in ("CDT", "CST")
