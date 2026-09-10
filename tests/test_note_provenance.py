"""A note Synth wrote is not a correction from Arun.

The pair of tests at the top is the whole file. Getting the first one wrong files a phantom
edit against a note nobody touched and holds the mirror; getting the second one wrong throws
away a real correction, silently, which is worse.

Scope worth being exact about, because it changes what is a live bug and what is insurance.
poll_notes only dumps config.NOTES_FOLDER, so a note anywhere else -- a brief in its own
folder, anything create_note wrote elsewhere -- was never visible to this machinery and never
could have produced a phantom correction. The live bug is inside the Synth folder:
notes_mirror holds a row for every note the watcher has merely SEEN there, append_note is
guarded only against the six rendered documents, so appending to any other note in that folder
went through and came back on the next poll as an edit Arun never made.
"""
from __future__ import annotations

import pytest

from synth import config, db, notes_sync, watcher


@pytest.fixture
def notes(monkeypatch):
    """A stand-in Notes folder. Nothing here reaches synthd."""
    store = {}

    def fake_call(cmd, **kw):
        if cmd == "notes_dump":
            folder = kw.get("folder")
            return [n for n in store.values() if folder is None or n["folder"] == folder]
        if cmd == "notes_folders":
            return sorted({n["folder"] for n in store.values()}) or ["Notes"]
        raise AssertionError(f"unexpected daemon call {cmd!r}")

    monkeypatch.setattr(notes_sync, "call", fake_call)
    monkeypatch.setattr(watcher, "call", fake_call)
    return store


def _put(store, note_id, body, folder=None, name="a note"):
    folder = folder or config.NOTES_FOLDER   # what poll_notes actually watches
    store[note_id] = {"id": note_id, "name": name, "body": body, "folder": folder}


# ---------------------------------------------------------------- the pair

def test_a_note_synth_wrote_is_not_read_back_as_a_correction(conn, notes):
    """poll_notes compares a note's live hash against what Synth last wrote. Anything written
    outside notes_mirror's bookkeeping -- create_note, append_note, a brief -- matched nothing
    and came back as an edit of Arun's that he never made."""
    _put(notes, "x://appended", "<p>a note Synth appended to</p>")
    notes_sync.record_write(conn, "x://appended", config.NOTES_FOLDER, "a kept note",
                            purpose="note")

    events = watcher.poll_notes(conn, {})
    assert [e for e in events if e.get("kind") == "note_edited"] == []


def test_a_real_edit_after_a_synth_write_is_still_detected(conn, notes):
    """The other half. A provenance check that swallows genuine corrections would be worse
    than the bug it replaced -- accept_correction is how Arun tells Synth it is wrong."""
    _put(notes, "x://n", "<p>what Synth wrote</p>")
    notes_sync.record_write(conn, "x://n", config.NOTES_FOLDER, "a note")
    # notes_mirror is what turns a divergence into an event; synth_note only suppresses.
    stored = conn.execute("SELECT written_hash FROM synth_note").fetchone()["written_hash"]
    conn.execute("INSERT INTO notes_mirror (doc, note_id, note_name, last_written_hash, "
                 "last_seen_hash) VALUES (?,?,?,?,?)",
                 ("a note", "x://n", "a note", stored, stored))
    conn.commit()

    notes["x://n"]["body"] = "<p>what Synth wrote, plus a line Arun added</p>"
    events = watcher.poll_notes(conn, {})
    assert [e["kind"] for e in events] == ["note_edited"]


# ---------------------------------------------------------------- the hash itself

def test_the_hash_recorded_is_what_notes_stored_not_what_was_composed(conn, notes):
    """Notes rewrites HTML on save, so the two are never equal. render() reads back for
    exactly this reason and record_write has to do the same or it stores a hash that will
    never match anything again."""
    _put(notes, "x://n", "<p>  what   Notes  actually kept </p>")
    stored = notes_sync.record_write(conn, "x://n", config.NOTES_FOLDER, "n")
    assert stored == db.text_hash("<p>  what   Notes  actually kept </p>")


def test_reflow_alone_is_not_an_edit(conn, notes):
    """text_hash collapses whitespace precisely so Notes' own reformatting is not a change."""
    _put(notes, "x://n", "<p>one two three</p>")
    notes_sync.record_write(conn, "x://n", config.NOTES_FOLDER, "n")
    notes["x://n"]["body"] = "<p>one   two\n\n  three</p>"
    live = db.text_hash(notes["x://n"]["body"])
    assert notes_sync.written_by_synth(conn, "x://n", live)


def test_a_note_synth_never_wrote_is_not_claimed(conn, notes):
    assert not notes_sync.written_by_synth(conn, "x://never-seen", "any-hash")


def test_writing_the_same_note_twice_keeps_only_the_latest_hash(conn, notes):
    _put(notes, "x://n", "<p>first</p>")
    notes_sync.record_write(conn, "x://n", config.NOTES_FOLDER, "n")
    notes["x://n"]["body"] = "<p>second</p>"
    notes_sync.record_write(conn, "x://n", config.NOTES_FOLDER, "n")
    rows = conn.execute("SELECT written_hash FROM synth_note WHERE note_id = ?",
                        ("x://n",)).fetchall()
    assert len(rows) == 1
    assert rows[0]["written_hash"] == db.text_hash("<p>second</p>")


def test_a_note_that_cannot_be_read_back_does_not_fail_the_write(conn, monkeypatch):
    """The write already happened. Failing here would turn a delivered brief into an error."""
    monkeypatch.setattr(notes_sync, "call",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthd is away")))
    assert notes_sync.record_write(conn, "x://n", config.NOTES_FOLDER, "n") is None


# ---------------------------------------------------------------- the brief's own trap

def test_the_brief_does_not_go_in_the_mirror_folder():
    """The Synth folder is re-rendered every sweep. A brief written there is either
    overwritten within half an hour or read as an unread correction that stops the mirror
    entirely -- and create_note refuses it, so the brief would simply never be delivered."""
    from synth import brief, config

    assert brief.BRIEF_FOLDER != config.NOTES_FOLDER
    assert brief.BRIEF_FOLDER not in config.PROTECTED_NOTE_FOLDERS


def test_the_brief_reminder_does_not_become_an_obligation():
    """tools.create_reminder files an obligation for any list outside LIST_STYLE_LISTS, so
    'Read the morning brief' would appear in the obligations mirror and be reported in
    tomorrow's brief as outstanding work. The brief would fill with instructions to read
    previous briefs."""
    import inspect

    from synth import brief

    source = inspect.getsource(brief.deliver)
    assert "actions.create_reminder" in source
    assert "tools.create_reminder" not in source
