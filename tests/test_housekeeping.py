"""Keeping .state bounded.

None of this is clever. It is here because every one of these files only ever grew: an
append-only FSEvents queue read by byte offset, four launchd logs that rotate nothing, and
database backups that accumulate one per migration at 60 MB each on a disk with 16 GiB free.
"""
from __future__ import annotations

import json
import os

import pytest

from synth import cli, watcher


# ---------------------------------------------------------------- the FSEvents queue

@pytest.fixture
def queue(tmp_path, monkeypatch):
    path = tmp_path / "changes.jsonl"
    monkeypatch.setattr(watcher, "QUEUE", str(path))
    return path


def _fill(path, n):
    with open(path, "w") as f:
        for i in range(n):
            f.write(json.dumps({"kind": "file", "detail": f"x{i}" + "p" * 200}) + "\n")


def test_events_are_read_from_where_the_last_read_stopped(queue):
    _fill(queue, 3)
    state = {}
    assert len(watcher.drain_fsevents(state)) == 3
    assert watcher.drain_fsevents(state) == []

    with open(queue, "a") as f:
        f.write(json.dumps({"kind": "file", "detail": "new"}) + "\n")
    assert len(watcher.drain_fsevents(state)) == 1


def test_a_small_queue_is_left_alone(queue):
    _fill(queue, 3)
    state = {}
    watcher.drain_fsevents(state)
    assert os.path.getsize(queue) > 0, "a queue under the ceiling was truncated"


def test_a_large_queue_is_truncated_once_it_has_been_read(queue, monkeypatch):
    monkeypatch.setattr(watcher, "QUEUE_MAX_BYTES", 100)
    _fill(queue, 20)
    state = {}

    assert len(watcher.drain_fsevents(state)) == 20
    assert os.path.getsize(queue) == 0
    assert state["queue_offset"] == 0


def test_an_event_appended_between_the_read_and_the_truncate_is_not_lost(queue, monkeypatch):
    """The daemon writes to this file continuously. Anything landing after the read finished
    but before the truncate would be discarded unread, so the size is checked again first."""
    monkeypatch.setattr(watcher, "QUEUE_MAX_BYTES", 100)
    _fill(queue, 20)
    state = {}

    real_getsize = os.path.getsize
    appended = []

    def getsize_then_append(path):
        size = real_getsize(path)
        # Fire once, at the re-check: stand in for the daemon appending in the window.
        if not appended and str(path) == str(queue) and state.get("queue_offset"):
            appended.append(True)
            with open(queue, "a") as f:
                f.write(json.dumps({"kind": "file", "detail": "arrived late"}) + "\n")
            return real_getsize(path)
        return size

    monkeypatch.setattr(watcher.os.path, "getsize", getsize_then_append)
    watcher.drain_fsevents(state)

    assert os.path.getsize(queue) > 0, "the late event was truncated away unread"
    assert "arrived late" in open(queue).read()


def test_a_truncated_queue_is_read_from_the_start_again(queue):
    _fill(queue, 3)
    state = {"queue_offset": 10_000}
    assert len(watcher.drain_fsevents(state)) == 3


def test_a_missing_queue_is_not_an_error(queue):
    assert watcher.drain_fsevents({}) == []


# ---------------------------------------------------------------- log rotation

def test_an_oversized_log_is_rolled_to_a_single_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "LOG_MAX_BYTES", 100)
    big = tmp_path / "sync.out.log"
    big.write_text("x" * 500)

    assert cli.rotate_logs(str(tmp_path)) == ["sync.out.log"]
    assert (tmp_path / "sync.out.log.1").read_text() == "x" * 500
    assert big.read_text() == ""


def test_a_small_log_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "LOG_MAX_BYTES", 10_000)
    (tmp_path / "sync.out.log").write_text("recent output")

    assert cli.rotate_logs(str(tmp_path)) == []
    assert not (tmp_path / "sync.out.log.1").exists()


def test_rotation_ignores_everything_that_is_not_a_log(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "LOG_MAX_BYTES", 10)
    (tmp_path / "synth.db").write_text("x" * 500)
    (tmp_path / "changes.jsonl").write_text("x" * 500)

    assert cli.rotate_logs(str(tmp_path)) == []


def test_rotation_never_raises(tmp_path):
    assert cli.rotate_logs(str(tmp_path / "does-not-exist")) == []


# ---------------------------------------------------------------- prune-state

def test_prune_keeps_the_newest_backup_by_time_not_by_name(tmp_path, monkeypatch, capsys):
    """The names carry the reason they were taken, so sorting them sorts by reason. Sorted
    that way this offered to remove a backup taken twenty minutes earlier and keep one from
    two days before."""
    monkeypatch.setattr(cli, "LOCK", str(tmp_path / "sweep.lock"))
    older = tmp_path / "synth.db.pre-rename-20260901T162739Z"
    newer = tmp_path / "synth.db.pre-oauth-hash-20260903T181755Z"
    older.write_text("old")
    newer.write_text("new")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    cli.cmd_prune_state([])
    out = capsys.readouterr().out
    assert "keeping synth.db.pre-oauth-hash-20260903T181755Z" in out
    assert "synth.db.pre-rename-20260901T162739Z" in out


def test_prune_reports_without_removing_anything_by_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "LOCK", str(tmp_path / "sweep.lock"))
    (tmp_path / "synth.db.pre-a").write_text("a")
    (tmp_path / "synth.db.pre-b").write_text("b")
    (tmp_path / "sync.out.log.1").write_text("rolled")

    cli.cmd_prune_state([])
    assert "nothing removed" in capsys.readouterr().out
    assert len(list(tmp_path.iterdir())) == 3


def test_prune_removes_only_with_an_explicit_yes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "LOCK", str(tmp_path / "sweep.lock"))
    (tmp_path / "synth.db.pre-a").write_text("a")
    (tmp_path / "synth.db.pre-b").write_text("b")
    (tmp_path / "sync.out.log.1").write_text("rolled")

    cli.cmd_prune_state(["--yes"])
    left = sorted(f.name for f in tmp_path.iterdir())
    assert len(left) == 1 and left[0].startswith("synth.db.pre-")


def test_prune_never_touches_the_live_database_or_a_current_log(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "LOCK", str(tmp_path / "sweep.lock"))
    (tmp_path / "synth.db").write_text("live")
    (tmp_path / "sync.out.log").write_text("current")
    (tmp_path / "synth.db.pre-a").write_text("a")
    (tmp_path / "synth.db.pre-b").write_text("b")

    cli.cmd_prune_state(["--yes"])
    assert (tmp_path / "synth.db").exists()
    assert (tmp_path / "sync.out.log").exists()


def test_prune_says_so_when_there_is_nothing_to_do(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "LOCK", str(tmp_path / "sweep.lock"))
    cli.cmd_prune_state([])
    assert "nothing to prune" in capsys.readouterr().out
