"""Reversing one logged action.

Two reversal tables, one dispatch. REVERSALS maps an action to the synthd call that undoes it;
PY_REVERSALS covers what is undone in Python instead -- a file write is reversed by putting
the previous bytes back and reindexing, which is not AppleScript. Both are driven off the same
before_json contract, and neither is exercised anywhere else in the suite.
"""
from __future__ import annotations

import os

import pytest

from synth import config, db, docwrite, ingest


@pytest.fixture(autouse=True)
def text_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "TEXT_CACHE", str(tmp_path / "text"))


@pytest.fixture
def synthd(monkeypatch):
    """Record what would have gone to the daemon instead of sending it there."""
    calls = []

    def fake_call(cmd, **params):
        calls.append((cmd, params))
        return {"ok": True}

    monkeypatch.setattr("synth.applekit.call", fake_call)
    return calls


# ---------------------------------------------------------------- the daemon-backed half

def test_an_updated_reminder_is_restored_to_its_prior_state(conn, synthd):
    action = db.log_action(
        conn, "update_reminder", "reminder", "moved the due date at his request",
        target_id="EK-1",
        before={"id": "EK-1", "title": "renew parking", "notes": None,
                "due": "2026-09-04T14:00:00Z", "hasTime": True})

    assert "reversed" in db.undo(conn, action)
    cmd, params = synthd[0]
    assert cmd == "update_reminder"
    assert params["title"] == "renew parking"
    assert params["due"] == "2026-09-04T14:00:00Z"


def test_a_completed_reminder_is_uncompleted(conn, synthd):
    action = db.log_action(conn, "complete_reminder", "reminder", "he said it was done",
                           target_id="EK-2", before={"id": "EK-2"})
    db.undo(conn, action)
    assert synthd[0][0] == "uncomplete_reminder"


def test_an_action_is_marked_undone_and_cannot_be_undone_twice(conn, synthd):
    action = db.log_action(conn, "complete_reminder", "reminder", "done",
                           target_id="EK-2", before={"id": "EK-2"})
    db.undo(conn, action)
    assert conn.execute("SELECT undone_at FROM action_log WHERE id = ?",
                        (action,)).fetchone()[0]

    assert "already undone" in db.undo(conn, action)
    assert len(synthd) == 1, "the second undo reached the daemon anyway"


def test_a_creation_is_not_reversed_by_undo(conn, synthd):
    """Synth can delete now, for the tier Arun drives himself -- and undo still will not.

    Those are separate acts. Letting undo remove a reminder would mean one call could delete
    something of his as a side effect of tidying up, and for reminders, events and notes the
    removal cannot be taken back the way an edit can. The message names the tool that does it
    instead of claiming, as it used to, that no such tool exists.
    """
    action = db.log_action(conn, "create_reminder", "reminder", "he asked for it",
                           target_id="EK-3", after={"id": "EK-3"})
    message = db.undo(conn, action)

    assert "undo does not delete" in message
    assert "delete_reminder" in message, "the caller is not told which tool does it"
    assert "EK-3" in message, "the caller is not told what to remove"
    assert synthd == [], "undo must not have reached the daemon at all"


def test_an_action_with_no_recorded_prior_state_is_refused(conn, synthd):
    action = db.log_action(conn, "update_reminder", "reminder", "no before recorded",
                           target_id="EK-4")
    assert "cannot be reversed" in db.undo(conn, action)
    assert synthd == []


def test_an_action_with_no_defined_reversal_says_so(conn, synthd):
    action = db.log_action(conn, "mail_draft", "draft", "drafted a reply",
                           before={"id": "x"})
    assert "no defined reversal" in db.undo(conn, action)
    assert synthd == []


def test_an_unknown_action_id_is_an_error(conn):
    with pytest.raises(KeyError):
        db.undo(conn, 999)


# ---------------------------------------------------------------- the Python half

def _write(documents, name, text):
    (documents / config.WRITABLE_DOCUMENTS / name).write_text(text)
    return f"{config.WRITABLE_DOCUMENTS}/{name}"


def test_a_document_write_is_reversed_by_putting_the_bytes_back(conn, documents, synthd):
    rel = _write(documents, "notes.md", "the original passage\n")
    abs_path = docwrite.resolve(rel)
    before = docwrite.snapshot(rel, abs_path)
    docwrite.reindex(conn, rel, abs_path)

    docwrite.write_atomic(abs_path, "a replacement that was wrong\n")
    docwrite.reindex(conn, rel, abs_path)
    action = db.log_action(conn, "update_document", "document", "replaced a passage",
                           target_id=rel, before=before)

    assert "restored" in db.undo(conn, action)
    assert open(abs_path).read() == "the original passage\n"
    assert synthd == [], "a file restore does not go through the daemon"


def test_restoring_also_brings_the_search_index_back_in_step(conn, documents, synthd):
    rel = _write(documents, "notes.md", "the peregrine falcon\n")
    abs_path = docwrite.resolve(rel)
    before = docwrite.snapshot(rel, abs_path)
    docwrite.reindex(conn, rel, abs_path)

    docwrite.write_atomic(abs_path, "the kestrel instead\n")
    docwrite.reindex(conn, rel, abs_path)
    action = db.log_action(conn, "update_document", "document", "replaced", before=before)
    db.undo(conn, action)

    hits = [r[0] for r in conn.execute(
        "SELECT d.path FROM document_fts f JOIN document d ON d.id = f.rowid "
        "WHERE document_fts MATCH 'peregrine'")]
    assert hits == [rel]


def test_undoing_an_undo_is_possible_because_no_version_is_ever_lost(conn, documents, synthd):
    rel = _write(documents, "notes.md", "version one\n")
    abs_path = docwrite.resolve(rel)
    before = docwrite.snapshot(rel, abs_path)
    docwrite.reindex(conn, rel, abs_path)
    docwrite.write_atomic(abs_path, "version two\n")
    docwrite.reindex(conn, rel, abs_path)

    action = db.log_action(conn, "update_document", "document", "replaced", before=before)
    db.undo(conn, action)

    assert len(os.listdir(docwrite.VERSIONS)) == 2, \
        "the bytes replaced by the restore were not kept"


def test_a_restore_is_refused_when_the_saved_version_no_longer_hashes_the_same(
        conn, documents, synthd):
    rel = _write(documents, "notes.md", "original\n")
    abs_path = docwrite.resolve(rel)
    before = docwrite.snapshot(rel, abs_path)

    with open(before["backup"], "w") as f:
        f.write("something else entirely\n")

    action = db.log_action(conn, "update_document", "document", "replaced", before=before)
    with pytest.raises(ValueError, match="no longer hashes"):
        db.undo(conn, action)


def test_a_restore_is_refused_when_the_saved_version_is_gone(conn, documents, synthd):
    rel = _write(documents, "notes.md", "original\n")
    before = docwrite.snapshot(rel, docwrite.resolve(rel))
    os.unlink(before["backup"])

    action = db.log_action(conn, "update_document", "document", "replaced", before=before)
    with pytest.raises(FileNotFoundError, match="Recover it from iCloud"):
        db.undo(conn, action)


def test_the_write_guard_applies_to_undo_too(conn, documents, synthd):
    """resolve() runs on the restore path as well, so a before_json naming a path outside
    the writable folder cannot be used to write there."""
    action = db.log_action(conn, "update_document", "document", "replaced",
                           before={"path": "../../escaped.md", "backup": "/tmp/x",
                                   "content_hash": "0" * 32})
    with pytest.raises(PermissionError):
        db.undo(conn, action)
