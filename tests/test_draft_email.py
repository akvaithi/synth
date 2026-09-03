"""Drafting mail, and what may be attached to it.

A draft is the one write that leaves the machine once Arun presses send, so the attachment
path is a real boundary and not a convenience check: what can be attached is exactly what he
can already read.

The bug these came out of was in the daemon's AppleScript rather than here -- `first email
address of ...` does not compile, because "email address" is not a class in Mail's dictionary
-- so it failed at compile time, identically, five times, and looked like a permissions
problem. Nothing in Python could have caught that. What Python can hold is everything around
it, which is what is pinned below.
"""
from __future__ import annotations

import pytest

from synth import tools


@pytest.fixture
def documents_root(tmp_path, monkeypatch):
    from synth import docwrite

    root = tmp_path / "Documents"
    (root / "Career").mkdir(parents=True)
    (root / "Career" / "Resume.pdf").write_bytes(b"%PDF-1.4 resume")
    monkeypatch.setattr(docwrite, "DOCUMENTS", str(root))
    monkeypatch.setattr("synth.extract.materialise", lambda p, timeout=60: True)
    return root


@pytest.fixture
def drafted(monkeypatch):
    sent = []

    def record(cmd, **kw):
        sent.append((cmd, kw))
        return {"drafted": True}

    monkeypatch.setattr(tools, "call", record)
    return sent


def _draft(conn, **kw):
    return tools.draft_email(conn, to=["someone@example.com"], subject="s", body="b",
                             reason="he asked for it", **kw)


def test_a_plain_draft_reaches_the_daemon(conn, documents_root, drafted):
    _draft(conn)
    cmd, kw = drafted[0]
    assert cmd == "mail_draft"
    assert kw["to"] == ["someone@example.com"]
    assert kw["attachments"] == []


def test_an_attachment_under_documents_is_resolved_and_passed(conn, documents_root, drafted):
    _draft(conn, attachments=["Career/Resume.pdf"])
    _, kw = drafted[0]
    assert kw["attachments"] == [str(documents_root / "Career" / "Resume.pdf")]


def test_an_unknown_account_is_refused_before_the_daemon_is_touched(conn, documents_root,
                                                                   drafted):
    with pytest.raises(ValueError, match="unknown account"):
        _draft(conn, account="NotAnAccount")
    assert drafted == []


@pytest.mark.parametrize("path", ["/etc/passwd", "~/Downloads/x.pdf"])
def test_an_absolute_or_home_path_is_refused(conn, documents_root, drafted, path):
    with pytest.raises(ValueError, match="relative to Documents"):
        _draft(conn, attachments=[path])
    assert drafted == []


def test_a_path_reaching_outside_documents_is_refused(conn, documents_root, drafted):
    with pytest.raises(PermissionError, match="outside Documents"):
        _draft(conn, attachments=["../../../.ssh/id_rsa"])
    assert drafted == []


def test_a_symlink_pointing_out_of_documents_is_refused(conn, documents_root, drafted,
                                                        tmp_path):
    import os

    secret = tmp_path / "secret.pdf"
    secret.write_bytes(b"private")
    os.symlink(secret, documents_root / "Career" / "link.pdf")
    with pytest.raises(PermissionError, match="outside Documents"):
        _draft(conn, attachments=["Career/link.pdf"])
    assert drafted == []


def test_a_missing_attachment_is_refused(conn, documents_root, drafted):
    with pytest.raises(FileNotFoundError, match="not a file"):
        _draft(conn, attachments=["Career/nope.pdf"])
    assert drafted == []


def test_a_folder_is_not_an_attachment(conn, documents_root, drafted):
    with pytest.raises(FileNotFoundError, match="not a file"):
        _draft(conn, attachments=["Career"])
    assert drafted == []


def test_an_evicted_attachment_is_refused_rather_than_sent_empty(conn, documents_root,
                                                                 drafted, monkeypatch):
    """A draft carrying a 0-byte placeholder is worse than no draft."""
    monkeypatch.setattr("synth.extract.materialise", lambda p, timeout=60: False)
    with pytest.raises(FileNotFoundError, match="evicted"):
        _draft(conn, attachments=["Career/Resume.pdf"])
    assert drafted == []


def test_the_draft_is_logged_with_its_reason_and_what_was_attached(conn, documents_root,
                                                                   drafted):
    out = _draft(conn, attachments=["Career/Resume.pdf"])
    row = conn.execute("SELECT action, reason, after_json, args_json FROM action_log "
                       "WHERE id = ?", (out["action_id"],)).fetchone()
    assert row["action"] == "mail_draft"
    assert row["reason"] == "he asked for it"
    assert "Resume.pdf" in row["after_json"]
    # The body is recorded too: a draft that went out wrong has to be explicable afterwards.
    assert "args_json" in row.keys() and row["args_json"]


def test_there_is_still_no_send_path(conn, documents_root, drafted):
    """The one rule this tool exists under. If a `send` ever appears, it is a bug."""
    _draft(conn)
    assert all("send" not in cmd for cmd, _ in drafted)
