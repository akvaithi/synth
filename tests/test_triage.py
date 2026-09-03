"""The free pass that decides what never costs money.

The reactor spent four days paying Sonnet about thirty cents a time to read marketing email
in full and then say it was marketing email. These rules are what replaced that, and the
order they run in is the whole design: the urgent rules run first because
chen-studentservices@tamu.edu looks like a no-reply address and is in fact one of the people
Arun most needs to hear from.
"""
from __future__ import annotations

import pytest

from synth import triage


# ---------------------------------------------------------------- parsing

@pytest.mark.parametrize("sender,expected", [
    ('"Texas A&M Rec Sports" <recsports@rec.tamu.edu>', "recsports@rec.tamu.edu"),
    ("plain@example.com", "plain@example.com"),
    ("<UPPER@Example.COM>", "upper@example.com"),
    ("no address here", ""),
    ("", ""),
])
def test_the_address_comes_out_of_the_display_form(sender, expected):
    assert triage.bare_address(sender) == expected


def test_the_display_name_drops_the_bracketed_address():
    assert triage.display_name('"Texas A&M Rec Sports" <recsports@rec.tamu.edu>') \
        == "Texas A&M Rec Sports"


def test_the_domain_is_the_half_after_the_at():
    assert triage.domain("a@sub.example.com") == "sub.example.com"
    assert triage.domain("not an address") == ""


# ---------------------------------------------------------------- classify

EMPTY = {"addresses": set(), "people": set(), "obligations": [], "policy": {}}


def _ctx(**over):
    return {**EMPTY, **over}


def _msg(sender="someone@example.com", subject="hello"):
    return {"sender": sender, "subject": subject}


def test_a_person_the_database_knows_by_name_is_urgent():
    verdict, why = triage.classify(
        _msg(sender='"Jane Chen" <chen-studentservices@tamu.edu>'),
        _ctx(people={"jane chen"}))
    assert verdict == "urgent"
    assert "jane chen" in why


def test_a_known_person_outranks_looking_like_a_no_reply_mailbox():
    """The ordering that the docstring calls out as load-bearing."""
    verdict, _ = triage.classify(
        _msg(sender='"Jane Chen" <no-reply@tamu.edu>'), _ctx(people={"jane chen"}))
    assert verdict == "urgent"


@pytest.mark.parametrize("domain", ["e2ma.net", "sendgrid.net", "mail.mailchimp.com"])
def test_a_bulk_sender_domain_is_filed_not_read(domain):
    verdict, _ = triage.classify(_msg(sender=f"news@{domain}"), _ctx())
    assert verdict == "digest"


@pytest.mark.parametrize("subject", [
    "Your order has shipped", "50% off flash sale", "Your statement is now available",
])
def test_a_self_explanatory_subject_is_ignored(subject):
    verdict, _ = triage.classify(_msg(subject=subject), _ctx())
    assert verdict == "ignore"


def test_a_routine_security_notice_is_recorded_never_run():
    verdict, _ = triage.classify(_msg(subject="New sign-in from Chrome"), _ctx())
    assert verdict == "digest"


def test_a_no_reply_mailbox_cannot_be_waiting_for_an_answer():
    verdict, why = triage.classify(_msg(sender="do-not-reply@example.com"), _ctx())
    assert verdict == "digest"
    assert "no-reply" in why


def test_a_calendar_invitation_earns_a_look_whoever_sent_it():
    """A sender demoted for three dull messages can still send a fourth that matters."""
    verdict, _ = triage.classify(
        _msg(sender="careers@e2ma.net", subject="Invitation: interview Thursday"),
        _ctx(policy={"careers@e2ma.net": {"policy": "ignore", "decided_by": "learned"}}))
    assert verdict == "consider"


def test_content_matching_an_open_obligation_outranks_a_learned_demotion():
    verdict, why = triage.classify(
        _msg(sender="noreply@shell.com", subject="Shell Houston internship decision"),
        _ctx(obligations=[{"shell", "houston", "internship"}],
             policy={"noreply@shell.com": {"policy": "digest", "decided_by": "learned"}}))
    assert verdict == "consider"
    assert "obligation" in why


def test_a_single_shared_word_is_not_an_obligation_match():
    """Two shared tokens are required. One is how everything matches everything -- the
    message still earns a look as an unknown sender, but not on the obligation's account."""
    _, why = triage.classify(
        _msg(subject="Shell station discount"), _ctx(obligations=[{"shell", "internship"}]))
    assert "obligation" not in why


def test_a_hand_made_decision_outranks_the_static_rules():
    verdict, why = triage.classify(
        _msg(sender="news@e2ma.net"),
        _ctx(policy={"news@e2ma.net": {"policy": "consider", "decided_by": "manual"}}))
    assert verdict == "consider"
    assert "manual" in why


def test_an_unknown_sender_earns_a_cheap_subject_line_look_not_a_full_read():
    verdict, why = triage.classify(_msg(subject="Question about your research"), _ctx())
    assert verdict == "consider"
    assert "unknown sender" in why


def test_an_address_the_database_holds_earns_a_look_never_a_bypass():
    """seniordev88590@gmail.com is in the database precisely because Synth investigated it
    and found a recruiter whose story did not check out. A mention is not a vouch."""
    verdict, _ = triage.classify(
        _msg(sender="seniordev88590@gmail.com"),
        _ctx(addresses={"seniordev88590@gmail.com"}))
    assert verdict == "consider"


# ---------------------------------------------------------------- the digest queue

def test_holding_a_message_makes_it_readable_and_unreported(conn):
    triage.hold(conn, [{"messageId": "a@b", "account": "Work", "sender": "x@y.com",
                        "subject": "hello", "receivedAt": "2026-09-01T10:00:00Z",
                        "verdict": "digest", "decided_by": "bulk sender domain"}])
    rows = triage.unreported(conn)
    assert len(rows) == 1
    assert rows[0]["message_id"] == "a@b"
    assert rows[0]["verdict"] == "digest"


def test_the_same_message_is_not_held_twice(conn):
    msg = {"messageId": "a@b", "account": "Work", "sender": "x@y.com", "subject": "hello",
           "receivedAt": "2026-09-01T10:00:00Z", "verdict": "digest", "decided_by": "rule"}
    triage.hold(conn, [msg])
    triage.hold(conn, [msg])
    assert len(triage.unreported(conn)) == 1


def test_marking_reported_takes_a_message_out_of_the_queue(conn):
    triage.hold(conn, [{"messageId": f"{i}@b", "account": "Work", "sender": "x@y.com",
                        "subject": "s", "receivedAt": "2026-09-01T10:00:00Z",
                        "verdict": "digest", "decided_by": "rule"} for i in range(3)])
    rows = triage.unreported(conn)
    assert triage.mark_reported(conn, [rows[0]["id"]]) == 1
    assert len(triage.unreported(conn)) == 2

    assert triage.mark_reported(conn) == 2
    assert triage.unreported(conn) == []
