"""Knowing what is already scheduled, before scheduling anything else.

Synth created duplicate reminders for events already on the calendar — Dell Night, a career
fair Zoom, two lab visit invites. In every case the item was already there and the reminder
was noise. The fix is not smarter title matching; it is looking at the day before writing to
it. Time is the reliable key, titles are not: "Dell Night 2026" and "Information Session with
Dell Technologies" are the same commitment and share one word.

Everything here used to be shaped around one day, because that is the question
`already_scheduled` asks. Reading a week therefore cost seven agenda calls, and each of those
made its own events call AND its own reminders call — fourteen daemon round trips for what
two cover. So the day is no longer the unit of collection: `_collect` reads a whole span at
once and everything else, the single day included, is a view over it.
"""
from __future__ import annotations

import re
from datetime import date as _date
from datetime import datetime, timedelta, timezone

from synth import config, db
from synth.applekit import call

STOP = {"the", "a", "an", "with", "and", "for", "of", "to", "at", "on", "in", "session",
        "meeting", "info", "information", "event", "night", "2026", "2027", "reminder"}

# Minutes of air left either side of anything already scheduled when looking for a free slot.
# Ten because the gap between two back-to-back classes across campus is not a free slot, and a
# slot reported as starting the exact minute a class ends is arithmetically true and useless.
DEFAULT_BUFFER = 10


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


def _norm(title: str) -> str:
    """A title reduced to what two copies of the same commitment agree on."""
    return " ".join((title or "").split()).casefold()


def _midnight(d: _date) -> datetime:
    """Local midnight opening the given day.

    Built from the calendar date every time rather than by adding 24 hours to the previous
    midnight: an aware datetime plus timedelta(days=1) adds exactly 24 hours, which is an hour
    wrong on each of the two days a year the offset changes.
    """
    return datetime(d.year, d.month, d.day).astimezone()


def _local_day(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d")


def day_bounds(date: str) -> tuple[str, str]:
    """Local-day bounds for a YYYY-MM-DD date, expressed in UTC."""
    d = datetime.fromisoformat(date).date()
    return _iso(_midnight(d)), _iso(_midnight(d + timedelta(days=1)))


def span_bounds(first: str, last: str) -> tuple[str, str]:
    """UTC bounds covering whole local days from `first` to `last` inclusive."""
    a = datetime.fromisoformat(first).date()
    b = datetime.fromisoformat(last).date()
    return _iso(_midnight(a)), _iso(_midnight(b + timedelta(days=1)))


def dates_between(first: str, last: str) -> list[str]:
    a = datetime.fromisoformat(first).date()
    b = datetime.fromisoformat(last).date()
    if b < a:
        raise ValueError(f"end {last!r} is before start {first!r}")
    count = (b - a).days + 1
    if count > config.MAX_AGENDA_DAYS:
        raise ValueError(
            f"{first} to {last} is {count} days, over the {config.MAX_AGENDA_DAYS}-day limit "
            f"for one agenda. Ask for a shorter span.")
    return [(a + timedelta(days=i)).isoformat() for i in range(count)]


# ---------------------------------------------------------------- collection


def _collapse(items: list[dict], keyfn, label: str, prefer=None) -> tuple[list[dict], int]:
    """Fold copies of one commitment into one entry, remembering where the copies were.

    The same workshop arrives twice — once from the Google calendar, once as a Zoom event
    with its own identifier — and it is one commitment, so it should be read once. Only an
    exact match on title and both times collapses, which is what keeps two genuinely
    different things from ever merging.

    `prefer` picks which copy survives. For events that is the one on a calendar Synth can
    write to, because the id handed back is the id `update_event` would have to act on.

    The copies that lose are never dropped silently. The survivor lists their identifiers in
    `duplicate_ids` and, when they were somewhere else, names it in `also_on`; the caller
    counts every collapse. Often they are on the same calendar and differ only by identifier —
    one grad school workshop arrived as ...@google.com and ...@zoom.us on College Events —
    which is why the id is the field that always appears and the calendar is not.
    """
    groups: dict = {}
    order: list = []
    for it in items:
        k = keyfn(it)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(it)

    out, dropped = [], 0
    for k in order:
        copies = groups[k]
        if len(copies) == 1:
            out.append(copies[0])
            continue
        best = next((c for c in copies if prefer and prefer(c)), copies[0])
        rest = [c for c in copies if c is not best]
        dropped += len(rest)
        kept = dict(best)
        # The ids always differ and are the useful trail -- one copy of the grad school
        # workshop carried a zoom.us identifier. The calendar often does not differ at all,
        # both copies having landed on the same one, so name it only when it says something.
        kept["duplicate_ids"] = [c.get("id") for c in rest if c.get("id")]
        elsewhere = sorted({c.get(label) or "" for c in rest
                            if c.get(label) and c.get(label) != best.get(label)})
        if elsewhere:
            kept["also_on"] = elsewhere
        out.append(kept)
    return out, dropped


def _collect(first: str, last: str) -> dict:
    """Everything committed across a span of local days, in one events call and one reminders
    call however many days the span is.

    Deliberately unfiltered beyond deduplication — the judgement about whether something is a
    duplicate of what the caller is about to create belongs to the caller, which can weigh
    titles, times and context in a way a similarity score cannot.
    """
    start, end = span_bounds(first, last)
    lo, hi = _parse(start), _parse(end)

    events = [{"title": e["title"], "start": e["start"], "end": e["end"],
               "allDay": e["allDay"], "calendar": e["calendar"], "id": e["id"]}
              for e in call("events", start=start, end=end, timeout=240)]
    reminders = []
    for r in call("reminders", timeout=240):
        d = _parse(r.get("due", ""))
        if d and lo <= d < hi:
            reminders.append({"title": r["title"], "due": r["due"], "list": r["list"],
                              "id": r["id"]})

    events, ev_dropped = _collapse(
        events,
        lambda e: (_norm(e["title"]), e.get("start", ""), e.get("end", ""),
                   bool(e.get("allDay"))),
        "calendar",
        prefer=lambda e: e.get("calendar") in config.MANAGED_CALENDARS)
    reminders, rm_dropped = _collapse(
        reminders,
        lambda r: (_norm(r["title"]), r.get("due", "")),
        "list")

    return {"events": db.localize(events, "start", "end"),
            "reminders": db.localize(reminders, "due"),
            "duplicates_collapsed": ev_dropped + rm_dropped}


def _split_spanning(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separate the all-day events that run over several days from the rest.

    A banner like "New Member Applications Open!", 26 August to 18 September, is true of every
    day in a week-long read and the answer to none of them. Returned once, in its own field,
    with the span spelled out.

    The threshold is two days, so a single-day all-day event stays in its day: a holiday is a
    real constraint on that day and reads as one.
    """
    inline, spanning = [], []
    for e in events:
        if not e.get("allDay"):
            inline.append(e)
            continue
        a, b = _parse(e.get("start", "")), _parse(e.get("end", ""))
        if a is None or b is None:
            inline.append(e)
            continue
        # EventKit ends an all-day event at the midnight *after* its last day, so the last day
        # it actually covers is a second before the end.
        first = a.astimezone().date()
        last = (b - timedelta(seconds=1)).astimezone().date()
        days = (last - first).days + 1
        if days < config.SPANNING_ALL_DAY_DAYS:
            inline.append(e)
            continue
        spanning.append({**e, "from": first.isoformat(), "to": last.isoformat(),
                         "days": days})
    return inline, spanning


def _bucket(items: list[dict], key: str, dates: list[str]) -> dict[str, list[dict]]:
    """Group by LOCAL day. Bucketing on the UTC date puts anything after 7pm on tomorrow."""
    out: dict[str, list[dict]] = {d: [] for d in dates}
    for it in items:
        t = _parse(it.get(key, ""))
        if t is None:
            continue
        d = _local_day(t)
        if d in out:
            out[d].append(it)
    for v in out.values():
        v.sort(key=lambda i: i.get(key) or "")
    return out


# ---------------------------------------------------------------- the views


def agenda_range(start: str, end: str | None = None) -> dict:
    """Everything committed between two local days, read in one pass.

    This is what a week-shaped question should cost: one call, not one per day.
    """
    dates = dates_between(start, end or start)
    got = _collect(dates[0], dates[-1])
    inline, spanning = _split_spanning(got["events"])
    events = _bucket(inline, "start", dates)
    reminders = _bucket(got["reminders"], "due", dates)
    return {
        "start": dates[0],
        "end": dates[-1],
        "timezone": db.tzname(),
        "spanning": spanning,
        "days": [{"date": d,
                  "weekday": datetime.fromisoformat(d).strftime("%a"),
                  "events": events[d],
                  "reminders": reminders[d]} for d in dates],
        "counts": {"events": sum(len(v) for v in events.values()),
                   "reminders": sum(len(v) for v in reminders.values()),
                   "spanning": len(spanning),
                   "duplicates_collapsed": got["duplicates_collapsed"]},
    }


def day(date: str) -> dict:
    """One local day, flat: the shape already_scheduled, conflicts and free_slot read."""
    span = agenda_range(date, date)
    only = span["days"][0]
    return {"date": date, "timezone": span["timezone"], "events": only["events"],
            "reminders": only["reminders"], "spanning": span["spanning"]}


def already_scheduled(title: str, when: str, window_minutes: int = 240) -> dict:
    """Is this commitment already on the calendar or in reminders?

    Two different answers, deliberately kept apart, because conflating them made a busy day
    unwritable. A same-day item that shares a distinctive word is almost always the same thing
    under another name -- "Dell Night 2026" and "Information Session with Dell Technologies"
    share exactly one -- and that belongs in `matches`, which create_reminder refuses on.

    Something merely close in time and sharing NOTHING is not a duplicate. It used to land in
    the same list and carry the same verdict: asking about "Call Mom" at 5:20 PM returned
    "Donuts and Discussion" ten minutes away with shared_words empty, and the write was
    refused. On a Tuesday holding eight events almost any proposed time is within thirty
    minutes of something, so the guard against duplicates became a guard against writing at
    all. Those go in `time_conflicts` now: worth reporting, never worth blocking.
    """
    target = _parse(when)
    if target is None:
        return {"checked": False, "reason": "unparseable time"}
    date = target.astimezone().strftime("%Y-%m-%d")
    sched = day(date)
    want = tokens(title)
    duplicates, overlaps = [], []
    for kind, items, key in (("event", sched["events"], "start"),
                             ("reminder", sched["reminders"], "due")):
        for it in items:
            t = _parse(it.get(key, ""))
            if t is None:
                continue
            delta = abs((t - target).total_seconds()) / 60
            if delta > window_minutes:
                continue
            shared = want & tokens(it["title"])
            hit = {"kind": kind, "title": it["title"], "when": it.get(key),
                   "minutes_apart": round(delta), "shared_words": sorted(shared),
                   "id": it["id"]}
            if shared:
                duplicates.append(hit)
            elif delta <= 30:
                overlaps.append(hit)
    for group in (duplicates, overlaps):
        group.sort(key=lambda h: h["minutes_apart"])
        db.localize(group, "when")
    return {
        "checked": True, "date": date,
        "matches": duplicates,
        "time_conflicts": overlaps,
        "verdict": ("likely already scheduled" if duplicates
                    else "time conflict only" if overlaps
                    else "nothing similar found"),
        "note": ("`matches` is the blocking answer: same commitment, different name. "
                 "`time_conflicts` share the hour and nothing else — report them, never "
                 "suppress a write over them."),
    }


def conflicts(start: str, minutes: int = 30) -> dict:
    """What overlaps a proposed slot. A reminder that fires mid-class is a bad reminder."""
    s = _parse(start)
    if s is None:
        return {"checked": False}
    e = s + timedelta(minutes=minutes)
    sched = day(s.astimezone().strftime("%Y-%m-%d"))
    out = []
    for ev in sched["events"]:
        if ev["allDay"]:
            continue
        a, b = _parse(ev["start"]), _parse(ev["end"])
        if a and b and a < e and s < b:
            out.append({"kind": "event", "title": ev["title"], "start": ev["start"],
                        "end": ev["end"]})
    for r in sched["reminders"]:
        d = _parse(r["due"])
        if d and abs((d - s).total_seconds()) < 15 * 60:
            out.append({"kind": "reminder", "title": r["title"], "due": r["due"]})
    db.localize(out, "start", "end", "due")
    return db.localize(
        {"checked": True, "proposed": start, "minutes": minutes, "conflicts": out},
        "proposed")


# ---------------------------------------------------------------- free time


def _busy(sched: dict, buffer_minutes: int = 0) -> list[tuple[datetime, datetime]]:
    """Merged busy intervals for one day: timed events, and a quarter hour either side of
    each timed reminder.

    `buffer_minutes` widens each event so a "free" slot is not flush against one. Without it
    a gap was reported starting at 2:40 PM, the exact minute CHEN 481 ends, and running to
    4:10 PM, the exact minute CHEN 354 starts -- true of the calendar and useless to a person
    who has to walk between them. Reminders keep their own quarter hour rather than stacking
    the buffer on top; they are already padded for the same reason.

    The merge is what is new. Walking a cursor over unmerged intervals is enough to find the
    first opening, and wrong for enumerating every gap — two overlapping classes would leave
    an imaginary slot between the start of the second and the end of the first.
    """
    spans: list[tuple[datetime, datetime]] = []
    for ev in sched["events"]:
        if ev.get("allDay"):
            continue
        a, b = _parse(ev.get("start", "")), _parse(ev.get("end", ""))
        if a and b and b > a:
            pad = timedelta(minutes=buffer_minutes)
            spans.append((a - pad, b + pad))
    for r in sched["reminders"]:
        d = _parse(r.get("due", ""))
        if d:
            spans.append((d - timedelta(minutes=15), d + timedelta(minutes=15)))
    spans.sort()

    merged: list[tuple[datetime, datetime]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _slot(a: datetime, b: datetime) -> dict:
    """One opening, rendered both ways.

    astimezone() before strftime, always. A gap's edges come from two different places -- the
    cursor is built local, but the end of a busy interval comes back from _parse carrying the
    daemon's +00:00 -- and strftime on the second prints UTC while claiming to be local. That
    put a 9:00 AM gap end at 2:00 PM, which is the same five-hour slip db.localize exists to
    stop.
    """
    a, b = a.astimezone(), b.astimezone()
    return {"start": _iso(a), "start_local": a.strftime(db.LOCAL_FMT),
            "end": _iso(b), "end_local": b.strftime(db.LOCAL_FMT),
            "minutes": round((b - a).total_seconds() / 60)}


def _gaps(date: str, busy: list[tuple[datetime, datetime]], minutes: int,
          earliest_hour: int, latest_hour: int) -> list[dict]:
    """Every opening of at least `minutes` on one day, in local waking hours."""
    base = datetime.fromisoformat(date).date()
    cursor = datetime(base.year, base.month, base.day, earliest_hour).astimezone()
    limit = datetime(base.year, base.month, base.day, latest_hour).astimezone()
    need = timedelta(minutes=minutes)
    out = []
    for a, b in busy:
        edge = min(a, limit)
        if edge - cursor >= need:
            out.append(_slot(cursor, edge))
        if b > cursor:
            cursor = b
        if cursor >= limit:
            break
    if limit - cursor >= need:
        out.append(_slot(cursor, limit))
    return out


def find_free_slot(date: str, minutes: int = 30, earliest_hour: int = 8,
                   latest_hour: int = 21, buffer_minutes: int = DEFAULT_BUFFER) -> dict:
    """First slot on a day with no event or reminder against it, in local waking hours.

    `buffer_minutes` keeps the slot off the edges of what surrounds it; pass 0 for the old
    flush-against-the-class behaviour.
    """
    slots = _gaps(date, _busy(day(date), buffer_minutes), minutes,
                  earliest_hour, latest_hour)
    if slots:
        return {"date": date, "slot": slots[0]["start"],
                "slot_local": slots[0]["start_local"],
                "timezone": db.tzname(), "minutes": minutes,
                "buffer_minutes": buffer_minutes}
    return {"date": date, "slot": None, "buffer_minutes": buffer_minutes,
            "reason": f"no free {minutes}-minute window between "
                      f"{earliest_hour}:00 and {latest_hour}:00"
                      + (f", allowing {buffer_minutes} minutes either side of anything "
                         f"scheduled" if buffer_minutes else "")}


def free_slots(start: str, end: str | None = None, minutes: int = 45,
               earliest_hour: int = 8, latest_hour: int = 21,
               buffer_minutes: int = DEFAULT_BUFFER) -> dict:
    """Every opening of at least `minutes` across a span of days.

    find_free_slot answers "when could this go today". The question behind anything recurring
    — a standing block for cooking, study, a lab shift — is "where are all the gaps this
    week", and building that by hand out of seven days of agenda is the work this saves.
    """
    dates = dates_between(start, end or start)
    got = _collect(dates[0], dates[-1])
    inline, _spanning = _split_spanning(got["events"])
    events = _bucket(inline, "start", dates)
    reminders = _bucket(got["reminders"], "due", dates)

    days, total = [], 0
    for d in dates:
        slots = _gaps(d, _busy({"events": events[d], "reminders": reminders[d]},
                               buffer_minutes),
                      minutes, earliest_hour, latest_hour)
        total += len(slots)
        days.append({"date": d, "weekday": datetime.fromisoformat(d).strftime("%a"),
                     "slots": slots})
    return {"start": dates[0], "end": dates[-1], "timezone": db.tzname(),
            "minutes": minutes, "earliest_hour": earliest_hour, "latest_hour": latest_hour,
            "buffer_minutes": buffer_minutes, "days": days, "total_slots": total}
