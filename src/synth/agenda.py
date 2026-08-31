"""Knowing what is already scheduled, before scheduling anything else.

Synth created duplicate reminders for events already on the calendar — Dell Night, a career
fair Zoom, two lab visit invites. In every case the item was already there and the reminder
was noise. The fix is not smarter title matching; it is looking at the day before writing to
it. Time is the reliable key, titles are not: "Dell Night 2026" and "Information Session with
Dell Technologies" are the same commitment and share one word.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from synth import db
from synth.applekit import call

STOP = {"the", "a", "an", "with", "and", "for", "of", "to", "at", "on", "in", "session",
        "meeting", "info", "information", "event", "night", "2026", "2027", "reminder"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return {w for w in words if w not in STOP and len(w) > 2}


def day_bounds(date: str) -> tuple[str, str]:
    """Local-day bounds for a YYYY-MM-DD date, expressed in UTC."""
    d = datetime.fromisoformat(date).replace(tzinfo=None)
    start = d.astimezone() if d.tzinfo else d.replace(hour=0, minute=0).astimezone()
    local_start = datetime(d.year, d.month, d.day).astimezone()
    return _iso(local_start), _iso(local_start + timedelta(days=1))


def agenda(date: str) -> dict:
    """Everything already committed on one local day: events and reminders together.

    This is what must be consulted before creating anything. It is deliberately unfiltered —
    the judgement about whether something is a duplicate belongs to the caller, which can
    weigh titles, times and context in a way a similarity score cannot.
    """
    start, end = day_bounds(date)
    events = call("events", start=start, end=end, timeout=180)
    lo, hi = _parse(start), _parse(end)
    reminders = []
    for r in call("reminders", timeout=240):
        d = _parse(r.get("due", ""))
        if d and lo <= d < hi:
            reminders.append(r)
    return {
        "date": date,
        "timezone": db.tzname(),
        "events": db.localize(
            [{"title": e["title"], "start": e["start"], "end": e["end"],
              "allDay": e["allDay"], "calendar": e["calendar"], "id": e["id"]}
             for e in events], "start", "end"),
        "reminders": db.localize(
            [{"title": r["title"], "due": r["due"], "list": r["list"], "id": r["id"]}
             for r in reminders], "due"),
    }


def already_scheduled(title: str, when: str, window_minutes: int = 240) -> dict:
    """Is this commitment already on the calendar or in reminders?

    Matches on proximity in time first, then on any meaningful shared word. A same-day item
    within a few hours that shares a distinctive token is almost always the same thing under
    a different name.
    """
    target = _parse(when)
    if target is None:
        return {"checked": False, "reason": "unparseable time"}
    date = target.astimezone().strftime("%Y-%m-%d")
    day = agenda(date)
    want = tokens(title)
    hits = []
    for kind, items, key in (("event", day["events"], "start"),
                             ("reminder", day["reminders"], "due")):
        for it in items:
            t = _parse(it.get(key, ""))
            if t is None:
                continue
            delta = abs((t - target).total_seconds()) / 60
            shared = want & tokens(it["title"])
            if delta <= window_minutes and (shared or delta <= 30):
                hits.append({"kind": kind, "title": it["title"], "when": it.get(key),
                             "minutes_apart": round(delta), "shared_words": sorted(shared),
                             "id": it["id"]})
    hits.sort(key=lambda h: h["minutes_apart"])
    db.localize(hits, "when")
    return {"checked": True, "date": date, "matches": hits,
            "verdict": "likely already scheduled" if hits else "nothing similar found"}


def conflicts(start: str, minutes: int = 30) -> dict:
    """What overlaps a proposed slot. A reminder that fires mid-class is a bad reminder."""
    s = _parse(start)
    if s is None:
        return {"checked": False}
    e = s + timedelta(minutes=minutes)
    day = agenda(s.astimezone().strftime("%Y-%m-%d"))
    out = []
    for ev in day["events"]:
        if ev["allDay"]:
            continue
        a, b = _parse(ev["start"]), _parse(ev["end"])
        if a and b and a < e and s < b:
            out.append({"kind": "event", "title": ev["title"], "start": ev["start"],
                        "end": ev["end"]})
    for r in day["reminders"]:
        d = _parse(r["due"])
        if d and abs((d - s).total_seconds()) < 15 * 60:
            out.append({"kind": "reminder", "title": r["title"], "due": r["due"]})
    db.localize(out, "start", "end", "due")
    return db.localize(
        {"checked": True, "proposed": start, "minutes": minutes, "conflicts": out},
        "proposed")


def find_free_slot(date: str, minutes: int = 30, earliest_hour: int = 8,
                   latest_hour: int = 21) -> dict:
    """First slot on a day with no event or reminder against it, in local waking hours."""
    day = agenda(date)
    busy = []
    for ev in day["events"]:
        if ev["allDay"]:
            continue
        a, b = _parse(ev["start"]), _parse(ev["end"])
        if a and b:
            busy.append((a, b))
    for r in day["reminders"]:
        d = _parse(r["due"])
        if d:
            busy.append((d - timedelta(minutes=15), d + timedelta(minutes=15)))
    busy.sort()

    base = datetime.fromisoformat(date)
    cursor = datetime(base.year, base.month, base.day, earliest_hour).astimezone()
    limit = datetime(base.year, base.month, base.day, latest_hour).astimezone()
    need = timedelta(minutes=minutes)
    while cursor + need <= limit:
        end = cursor + need
        clash = next((b for b in busy if b[0] < end and cursor < b[1]), None)
        if clash is None:
            return {"date": date, "slot": _iso(cursor),
                    "slot_local": cursor.strftime(db.LOCAL_FMT),
                    "timezone": db.tzname(), "minutes": minutes}
        cursor = clash[1].astimezone()
    return {"date": date, "slot": None,
            "reason": f"no free {minutes}-minute window between "
                      f"{earliest_hour}:00 and {latest_hour}:00"}
