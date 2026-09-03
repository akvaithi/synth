"""Reading calendar invitations, and checking whether they are already accepted.

Synth created a reminder telling Arun to "get the ExxonMobil visit on the calendar" when the
event was already there. It could see that an attachment named Mail Attachment.ics existed
but never opened it, so it never learned the date, and never checked the calendar for it.

This closes that loop: pull the .ics, read the real time out of it, and look at the calendar.
The answer to "is this already handled?" is almost always yes.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from synth.applekit import call

ICS_DIR = os.path.expanduser("~/Developer/synth/.state/attachments")


def _unfold(text: str) -> str:
    """RFC 5545 folds long lines with a leading space on the continuation."""
    return re.sub(r"\r?\n[ \t]", "", text)


def _dt(value: str, params: str = "") -> str | None:
    value = value.strip()
    try:
        if value.endswith("Z"):
            d = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        elif "T" in value:
            d = datetime.strptime(value, "%Y%m%dT%H%M%S")
            d = d.astimezone() if "TZID" in params else d.replace(tzinfo=timezone.utc)
        else:
            d = datetime.strptime(value, "%Y%m%d").astimezone()
    except ValueError:
        return None
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ics(text: str) -> list[dict]:
    """Every VEVENT in an .ics, reduced to what matters for cross-checking."""
    events, current = [], None
    for line in _unfold(text).splitlines():
        line = line.strip()
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        head, _, value = line.partition(":")
        name, _, params = head.partition(";")
        name = name.upper()
        if name == "SUMMARY":
            current["summary"] = value
        elif name == "DTSTART":
            current["start"] = _dt(value, params)
        elif name == "DTEND":
            current["end"] = _dt(value, params)
        elif name == "LOCATION":
            current["location"] = value
        elif name == "UID":
            current["uid"] = value
        elif name == "ORGANIZER":
            current["organizer"] = value.replace("mailto:", "")
        elif name == "STATUS":
            current["status"] = value
    return events


def read_invitation(conn, account: str, index: int, messageId: str,
                    mailbox: str = "INBOX") -> dict:
    """Open any .ics on a message, read the real event, and check the calendar for it.

    Returns a verdict per event. `already_on_calendar` means do nothing — that is the common
    case, including when the email claims the event was not added automatically.
    """
    from synth import agenda as _agenda

    atts = call("mail_attachments", account=account, index=index,
                messageId=messageId, mailbox=mailbox, timeout=300)
    if not atts.get("matched"):
        return {"found": False, "reason": "index no longer points at that message"}
    ics = [a for a in atts.get("attachments", []) if a["name"].lower().endswith(".ics")]
    if not ics:
        return {"found": False, "reason": "no .ics attachment on this message"}

    out = []
    for att in ics:
        saved = call("mail_save_attachment", account=account, index=index,
                     name=att["name"], directory=ICS_DIR, mailbox=mailbox, timeout=600)
        with open(saved["path"], encoding="utf-8", errors="replace") as f:
            text = f.read()
        for ev in parse_ics(text):
            entry = dict(ev)
            if ev.get("start") and ev.get("summary"):
                check = _agenda.already_scheduled(ev["summary"], ev["start"])
                entry["calendar_matches"] = check.get("matches", [])
                entry["verdict"] = ("already_on_calendar" if check.get("matches")
                                    else "not on calendar")
            else:
                entry["verdict"] = "incomplete invitation"
            out.append(entry)
    return {"found": True, "attachment": ics[0]["name"], "events": out}
