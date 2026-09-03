"""Reading a file that arrived by email.

Every engine this needs already existed and none of it was pointed at mail: extract.py reads
PDFs and Office files, ocr.py runs Vision over scans, and the daemon has been able to save an
attachment to disk since the beginning. The chain simply ran over the iCloud Documents tree
only, so the honest answer to "what does this PDF say" was "drag it into Documents first".
"""
from __future__ import annotations

import pytest

from synth import tools


@pytest.fixture
def mail(tmp_path, monkeypatch):
    """Stand in for the daemon: list the attachments, then save one to a real file."""
    saved = tmp_path / "attachments"
    saved.mkdir()
    state = {"attachments": [{"name": "Proposal.pdf", "bytes": 4_200_000}],
             "content": "the proposal text", "calls": []}

    def fake_call(cmd, **kw):
        state["calls"].append((cmd, kw))
        if cmd == "mail_attachments":
            return {"attachments": state["attachments"]}
        if cmd == "mail_save_attachment":
            path = saved / kw["name"]
            path.write_text(state["content"])
            return {"path": str(path), "bytes": path.stat().st_size, "name": kw["name"]}
        raise AssertionError(f"unexpected daemon call {cmd}")

    monkeypatch.setattr(tools, "call", fake_call)
    monkeypatch.setattr(tools, "ATTACHMENTS", str(saved))
    return state


def _read(conn, name="Proposal.pdf", **kw):
    return tools.read_attachment(conn, account="Work", index=3, messageId="m@example.com",
                                 name=name, **kw)


def test_it_returns_the_text_inside_the_attachment(conn, mail, monkeypatch):
    monkeypatch.setattr("synth.extract.extract", lambda p: "the proposal text")
    out = _read(conn)
    assert out["found"] is True
    assert out["text"] == "the proposal text"
    assert out["chars"] == len("the proposal text")


def test_the_message_id_is_verified_before_anything_is_saved(conn, mail, monkeypatch):
    """mail_save_attachment does not check the id and a mailbox index moves whenever mail
    arrives, so listing first is what stops the wrong message's file being written."""
    monkeypatch.setattr("synth.extract.extract", lambda p: "text")
    _read(conn)
    order = [c[0] for c in mail["calls"]]
    assert order == ["mail_attachments", "mail_save_attachment"]
    assert mail["calls"][0][1]["messageId"] == "m@example.com"


def test_asking_for_an_attachment_that_is_not_there_names_the_ones_that_are(conn, mail):
    out = _read(conn, name="Announcement.pdf")
    assert out["found"] is False
    assert out["attachments"] == ["Proposal.pdf"]
    assert [c[0] for c in mail["calls"]] == ["mail_attachments"], "it saved anyway"


def test_a_scan_falls_through_to_ocr(conn, mail, monkeypatch):
    """The case the document path already handles: no text layer is not a failure, it is
    what OCR exists for."""
    from synth import extract as _extract

    def no_text(path):
        raise _extract.ExtractionError("no text layer (4 pages, likely scanned)")

    monkeypatch.setattr("synth.extract.extract", no_text)
    monkeypatch.setattr("synth.ocr.ocr_pdf", lambda p, **k: "RECRUITMENT ANNOUNCEMENT")

    out = _read(conn)
    assert out["ocr"] is True
    assert out["text"] == "RECRUITMENT ANNOUNCEMENT"


def test_a_scan_ocr_cannot_read_says_so_and_keeps_the_file(conn, mail, monkeypatch):
    from synth import extract as _extract

    monkeypatch.setattr("synth.extract.extract", lambda p: (_ for _ in ()).throw(
        _extract.ExtractionError("no text layer (4 pages, likely scanned)")))
    monkeypatch.setattr("synth.ocr.ocr_pdf", lambda p, **k: "   ")

    out = _read(conn)
    assert out["text"] == ""
    assert "OCR found no readable text" in out["note"]
    assert out["path"], "the file has to stay reachable by eye"


def test_an_unextractable_type_is_reported_not_raised(conn, mail, monkeypatch):
    from synth import extract as _extract

    monkeypatch.setattr("synth.extract.extract", lambda p: (_ for _ in ()).throw(
        _extract.ExtractionError("unsupported type .dmg")))
    out = _read(conn)
    assert out["text"] == ""
    assert "no text could be extracted" in out["note"]


def test_a_long_attachment_is_truncated_and_says_so(conn, mail, monkeypatch):
    monkeypatch.setattr("synth.extract.extract", lambda p: "x" * 5000)
    out = _read(conn, max_chars=1000)
    assert len(out["text"]) == 1000
    assert "4000 more characters" in out["truncated"]
    assert out["chars"] == 5000, "the real length is still reported"


def test_the_attachment_is_not_added_to_the_document_index(conn, mail, monkeypatch):
    """The index mirrors Documents. Filing mail attachments into it would make search answer
    out of a folder Arun cannot see."""
    monkeypatch.setattr("synth.extract.extract", lambda p: "the proposal text")
    _read(conn)
    assert conn.execute("SELECT count(*) FROM document").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM source").fetchone()[0] == 0
