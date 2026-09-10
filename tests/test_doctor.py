"""The check that tells Arun what is broken, and stays quiet otherwise.

Every assertion here is a false positive or a false negative that this module produced on its
first run against the live machine. The two that matter most are opposite failures: reporting
a healthy restart as a fault, and failing to report a lapsed permission at all.
"""
from __future__ import annotations

import json
import os

import pytest

from synth import doctor


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    """Keep every check off the real .state directory."""
    monkeypatch.setattr(doctor, "STATE", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------- log filtering

def test_a_startup_line_is_not_an_error(state):
    """'not on the benign list' matched every 'watching 3 paths' the daemon prints at boot.

    The first version reported 39 non-routine lines in connector.err.log and quoted
    "StreamableHTTP session manager started" as the evidence, which is a service announcing
    that it started correctly.
    """
    (state / "connector.err.log").write_text(
        "StreamableHTTP session manager started\n"
        "INFO:     Uvicorn running on http://127.0.0.1:8787\n"
        "watching 3 paths\n")
    assert doctor.logs() == []


def test_a_real_traceback_is_an_error(state):
    (state / "sync.err.log").write_text(
        "watching 3 paths\n"
        "Traceback (most recent call last):\n"
        "  File \"x.py\", line 1\n"
        "sqlite3.OperationalError: database is locked\n")
    found = doctor.logs()
    assert len(found) == 1
    assert found[0]["level"] == doctor.WARN
    assert "sync.err.log" in found[0]["check"]


def test_coregraphics_noise_is_never_reported(state):
    """236 of 236 lines in sync.err.log were this, which is why nobody read the file."""
    (state / "sync.err.log").write_text(
        "CoreGraphics PDF has logged an error. "
        "Set environment variable \"CG_PDF_VERBOSE\" to learn more.\n" * 50)
    assert doctor.logs() == []


def test_a_client_closing_a_stream_is_not_a_tunnel_error(state):
    """736 of these in tunnel.err.log; error code 0 is a clean cancel, not a failure."""
    (state / "tunnel.err.log").write_text(
        'ERR Request failed error="stream 113 canceled by remote with error code 0"\n' * 20)
    assert doctor.logs() == []


def test_an_empty_log_is_not_a_finding(state):
    (state / "enrich.err.log").write_text("")
    assert doctor.logs() == []


# ---------------------------------------------------------------- launchd exit codes

def test_sigterm_is_how_a_keepalive_job_restarts_not_how_it_fails():
    """-15 is what launchctl kickstart leaves behind. Flagging it made every healthy
    restart of the daemon and the connector look like a fault, which is two thirds of the
    output on a machine where nothing at all is wrong."""
    assert "-15" in doctor.BENIGN_EXIT


def _launchctl(monkeypatch, tmp_path, line):
    """Stand in for `launchctl list`, with one plist on disk to look for."""
    import subprocess as sp
    (tmp_path / "launchd").mkdir(exist_ok=True)
    (tmp_path / "launchd" / "page.akvaithi.synth.tunnel.plist").write_text("<plist/>")
    monkeypatch.setattr(doctor.os.path, "expanduser",
                        lambda p: str(tmp_path / "launchd")
                        if p.endswith("launchd") else os.path.expanduser(p))

    class R:
        stdout = line.encode()
    monkeypatch.setattr(sp, "run", lambda *a, **k: R())


def test_a_job_that_crashed_and_came_back_is_not_reported_as_down(monkeypatch, tmp_path):
    """The status column is the LAST exit code and says nothing about now: a KeepAlive job
    that segfaulted and was restarted reports the crash while running perfectly well. Without
    the PID column, that is indistinguishable from a service that is actually gone."""
    _launchctl(monkeypatch, tmp_path, "74568\t11\tpage.akvaithi.synth.tunnel\n")
    found = doctor.jobs()
    assert len(found) == 1
    assert found[0]["level"] == doctor.WARN
    assert "restarted" in found[0]["detail"]
    assert "SIGSEGV" in found[0]["detail"]


def test_a_job_that_crashed_and_stayed_down_is_a_failure(monkeypatch, tmp_path):
    _launchctl(monkeypatch, tmp_path, "-\t11\tpage.akvaithi.synth.tunnel\n")
    found = doctor.jobs()
    assert len(found) == 1
    assert found[0]["level"] == doctor.FAIL
    assert "not running" in found[0]["detail"]


def test_a_healthy_job_is_silent(monkeypatch, tmp_path):
    _launchctl(monkeypatch, tmp_path, "74568\t0\tpage.akvaithi.synth.tunnel\n")
    assert doctor.jobs() == []


# ---------------------------------------------------------------- the sweep watermark

def test_a_stale_sweep_is_reported(state, conn):
    """last_sweep is a float epoch, not the ISO string the database uses. Parsing it as ISO
    raised, the exception was swallowed, and the check silently never fired."""
    (state / "sync.json").write_text(json.dumps({"last_sweep": 0.0}))
    found = [f for f in doctor.sweep(conn) if f["check"] == "sweep"]
    assert found and found[0]["level"] == doctor.FAIL


def test_a_recent_sweep_is_not_reported(state, conn):
    import time
    (state / "sync.json").write_text(json.dumps({"last_sweep": time.time()}))
    assert [f for f in doctor.sweep(conn) if f["check"] == "sweep"] == []


def test_a_missing_watermark_is_a_warning_not_a_crash(state, conn):
    assert any(f["check"] == "sweep" for f in doctor.sweep(conn))


# ---------------------------------------------------------------- run_log

def test_a_run_that_never_finished_is_reported(conn):
    """A crashed run leaves status 'running' for ever and otherwise looks like nothing."""
    conn.execute("INSERT INTO run_log (job, started_at, status) "
                 "VALUES ('enrich', datetime('now','-5 hours'), 'running')")
    conn.commit()
    found = doctor.runs(conn)
    assert any(f["level"] == doctor.FAIL and "enrich" in f["check"] for f in found)


def test_a_run_still_going_is_left_alone(conn):
    conn.execute("INSERT INTO run_log (job, started_at, status) "
                 "VALUES ('enrich', datetime('now'), 'running')")
    conn.commit()
    assert doctor.runs(conn) == []


# ---------------------------------------------------------------- aggregation

def test_a_failing_check_does_not_take_the_others_down(conn, monkeypatch):
    def explode(_conn=None):
        raise RuntimeError("boom")
    monkeypatch.setattr(doctor, "CHECKS", (explode, doctor.runs))
    found = doctor.run_all(conn)
    assert any("boom" in f["detail"] for f in found)


def test_failures_sort_above_warnings(conn, monkeypatch):
    monkeypatch.setattr(doctor, "CHECKS", (
        lambda _c=None: [doctor._finding(doctor.WARN, "w", "a warning")],
        lambda _c=None: [doctor._finding(doctor.FAIL, "f", "a failure")],
    ))
    assert [f["level"] for f in doctor.run_all(conn)] == [doctor.FAIL, doctor.WARN]


def test_a_clean_machine_says_so():
    assert "answering" in doctor.summary([])


# ---------------------------------------------------------------- the stuck mirror

def _hold_a_note(conn, doc="programs_active", note_id="x://n1"):
    """A mirror row whose stored hash no longer matches what Notes holds."""
    conn.execute(
        "INSERT INTO notes_mirror (doc, note_id, note_name, last_written_hash, "
        "last_seen_hash, last_edit_at) VALUES (?,?,?,?,?,datetime('now'))",
        (doc, note_id, "Synth — Programs, Active", "written-hash", "seen-hash"))
    conn.commit()


def _mirror_fixture(monkeypatch, rendered_text, live_text=None):
    """Point the mirror check at a note whose live body we control.

    The live body is built through the real to_html so the comparison exercises the same
    rendering path the mirror uses; `live_text` differing from `rendered_text` is what makes
    it a genuine edit rather than a reflow.
    """
    from synth import notes_sync

    title = "Synth — Programs, Active"
    blocks = [("p", rendered_text)]
    monkeypatch.setitem(notes_sync.DOCS, "programs_active", lambda _conn: (title, blocks))
    body = notes_sync.to_html(title, [("p", live_text or rendered_text)])
    # Notes reflows what it stores: extra blank lines and runs of spaces come back that were
    # never sent. text_hash collapses whitespace precisely so that this is not an edit.
    body = body.replace("<p>", "\n\n  <p>  ")
    monkeypatch.setattr(notes_sync, "call",
                        lambda cmd, **kw: [{"id": "x://n1", "body": body}])


def test_a_note_held_only_by_reformatting_is_named_as_such(conn, monkeypatch):
    """Notes rewrites HTML on save, so a stored hash drifts out of step and render() then
    refuses to touch the note for ever. The note stops being true and nothing says so --
    which is the worst way for a mirror to fail. Live text equal to a fresh render is the
    proof that no one actually edited it."""
    _hold_a_note(conn)
    _mirror_fixture(monkeypatch, "the same words")
    found = doctor.mirror(conn)
    assert len(found) == 1
    assert "formatting only" in found[0]["detail"]
    assert found[0]["fix"] == "synth reconcile"


def test_a_note_arun_really_edited_is_not_offered_to_reconcile(conn, monkeypatch):
    """reconcile() overwrites the stored hash with whatever Notes holds, so calling it on a
    genuine correction discards the correction silently."""
    _hold_a_note(conn)
    _mirror_fixture(monkeypatch, "the original words",
                    live_text="Arun added this line himself")
    found = doctor.mirror(conn)
    assert len(found) == 1
    assert "a real edit" in found[0]["detail"]
    assert "accept_correction" in found[0]["fix"]


def test_a_mirror_in_step_is_not_reported(conn, monkeypatch):
    _mirror_fixture(monkeypatch, "x")
    assert doctor.mirror(conn) == []
