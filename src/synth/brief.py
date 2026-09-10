"""The twice-daily brief: the one thing Synth produces that Arun reads end to end.

## Why this is the one job that still spends money

Everything else moved to a model on Arun's own network. The brief did not, for two reasons
that both matter.

It is the output he actually reads, so it is the one worth the quality. And `prompts/brief.md`
is built around going and finding things out -- "Something needs doing somewhere? Find WHERE,
and give the link and the hours" -- which needs WebSearch and WebFetch. Ollama has no web
access at all, so a local brief would keep the shape and lose most of the substance.

If the budget refuses, it falls back to a local brief and says so in the first line. A brief
that did not happen is worse than a local one; a local one that pretends to be the researched
one is worse than either.

## Delivery

Into Notes, and then a reminder pointing at it. iCloud makes the note durable and reachable
from his phone; the reminder is what actually surfaces. A note nobody opens is a brief that
did not happen.

Two traps are avoided deliberately, both of which produce a feedback loop rather than an
error:

* The brief does NOT go in the `Synth` folder. That is the mirror, rendered from the database
  and rewritten every sweep, so anything else written there is either overwritten or read as
  an unread correction -- which stops the mirror. `create_note` refuses it outright.
* The reminder is NOT made with `tools.create_reminder`. That files an obligation for any list
  outside LIST_STYLE_LISTS, so "Read the morning brief" would become an open obligation, show
  up in the obligations mirror, and be reported in tomorrow's brief as outstanding work. The
  brief would slowly fill with instructions to read previous briefs.
"""
from __future__ import annotations

import json

from synth import actions, budget, db, runner

# Not config.NOTES_FOLDER. See the module docstring: that one is the mirror.
BRIEF_FOLDER = "Synth Briefs"
BRIEF_LIST = "Personal"

DEPTH_DEEP = """This is the researched brief of the day. Where a deadline, an eligibility rule
or a claim about an organisation is unverified, check it against the official source with
WebSearch or WebFetch and say what you found. Arun asked for findings, not hedged commentary:
establish the fact or leave the item out."""

DEPTH_LIGHT = """This is the short evening brief. The day's facts were already established
this morning — do not re-research them, and you have no web tools here. Report what actually
changed since the last brief, what is due tomorrow morning, and nothing else. If the day was
quiet, a four-line brief is the correct brief."""

DEPTH_LOCAL = """This brief is being written by a model on Arun's own network because the
metered one was unavailable. You have NO web access: you cannot verify a deadline, look up an
organisation or resolve a link. Report what the database and his calendar already hold, and
where the researched brief would have gone and checked something, say plainly that it is
unverified rather than guessing. A short honest brief is the right outcome here."""

MAX_TURNS_DEEP = 45
MAX_TURNS_LIGHT = 18


def digest_lines(conn, limit: int = 60) -> str:
    """Mail that was filtered before any model saw it, with its links already extracted.

    Hard filtering is only acceptable because this exists. Arun's standing rule is that
    nothing flies under the radar, so everything the free pass set aside is named here -- and
    named with its destination URL, because the whole point is that he never has to reopen an
    email to act on it.
    """
    from synth import triage as tri

    rows = tri.unreported(conn, limit=limit)
    if not rows:
        return "(nothing filtered since the last brief)"
    try:
        tri.fill_links(conn, rows)
    except Exception:
        pass  # a brief without links beats no brief
    out = []
    for r in rows:
        when = db.local(r["received_at"], "%a %H:%M") if r["received_at"] else ""
        line = f"- [{r['account']}] {when} {r['sender']} — {r['subject']}  ({r['decided_by']})"
        try:
            urls = json.loads(r["links"] or "[]")
        except ValueError:
            urls = []
        if urls:
            line += "\n  links: " + "  ".join(urls[:3])
        out.append(line)
    return "\n".join(out)


def title_for(when: str) -> str:
    return f"Synth — {when.title()} Brief, {db.local(db.now(), '%a %-d %b')}"


def deliver(conn, when: str, text: str, run_id=None) -> dict:
    """Put the brief in Notes and file a reminder pointing at it.

    Returns what happened rather than raising: a brief that was written and not delivered is
    worth knowing about precisely, and it is still worth recording that it exists.
    """
    from synth import tools

    out: dict = {"note": None, "reminder": None, "problems": []}
    title = title_for(when)

    # The one folder Synth may make for itself. The rule that it cannot create folders exists
    # so a typo can never leave a "Recipies" beside Arun's "Recipes" -- it is about not
    # littering his Notes with near-misses of his own names. This is Synth's own output
    # location, created once under a fixed name, and the alternative is dropping a brief a day
    # into whatever folder happens to be the default.
    folder = BRIEF_FOLDER
    try:
        from synth.applekit import call
        call("notes_ensure_folder", folder=BRIEF_FOLDER, timeout=60)
    except Exception as e:
        from synth import config
        folder = config.DEFAULT_NOTE_FOLDER
        out["problems"].append(
            f"could not ensure the {BRIEF_FOLDER!r} folder ({e}); delivering to "
            f"{folder!r} instead")

    try:
        created = tools.create_note(conn, name=title, body=text, folder=folder,
                                    reason=f"deliver the {when} brief", run_id=run_id)
        out["note"] = created
    except FileExistsError:
        # One note per brief, never appended to yesterday's. A name clash means this brief was
        # already delivered, which makes redelivery idempotent for free.
        out["problems"].append("already delivered: a note with this title exists")
        return out
    except Exception as e:
        out["problems"].append(f"Notes delivery failed: {e}")
        return out

    # A reminder always surfaces; a note waits to be opened. The date is in the title so
    # _refuse_duplicate's window can never conflate the morning brief with the evening one.
    try:
        action_id, result = actions.create_reminder(
            conn, title=f"Read the {when} brief — {db.local(db.now(), '%a %-d %b')}",
            list=BRIEF_LIST,
            notes=f"In Notes › {folder} › {title}",
            reason=f"point at the {when} brief, which is in Notes and easy to miss",
            run_id=run_id)
        out["reminder"] = {"action_id": action_id,
                           "id": (result.get("created") or {}).get("id")}
    except Exception as e:
        out["problems"].append(f"reminder failed: {e}")
    return out


def write(conn, when: str = "morning") -> dict:
    """Produce and deliver one brief. The product, and the last thing to be starved."""
    from synth import tools, triage as tri

    with db.run(conn, "brief", trigger=when) as run_id:
        problems: list[str] = []
        # Reconcile first, or the brief reports work Arun has already finished as overdue.
        try:
            tools.sync_obligations(conn, run_id=run_id)
        except Exception as e:
            problems.append(f"obligation sync failed: {e}")
        try:
            tri.learn(conn)
        except Exception as e:
            problems.append(f"sender learning failed: {e}")

        deep = when == "morning"
        digest = digest_lines(conn)
        allowed, why = budget.allowed(conn, "brief")

        if allowed:
            prompt = runner.prompt("brief", when=when, digest=digest,
                                   budget=budget.summary(conn),
                                   depth=DEPTH_DEEP if deep else DEPTH_LIGHT)
            allow = list(runner.DIRECT_TOOLS) + (["WebSearch", "WebFetch"] if deep else [])
            turns = MAX_TURNS_DEEP if deep else MAX_TURNS_LIGHT
            result = runner.run_claude(prompt, allow, max_turns=turns)
            text = str(result.get("result", ""))
            if result.get("is_error"):
                problems.append(f"metered brief failed: {text[:200]}")
                text = ""
            source = "claude"
        else:
            result, text, source = {}, "", "local"
            problems.append(f"metered brief unavailable ({why}); wrote a local one")

        if not text:
            text = _local_brief(conn, when, digest, run_id=run_id)
            source = "local"
            if text:
                text = (f"_Written locally — the researched brief was unavailable "
                        f"({why or 'the metered run failed'}), so nothing here has been "
                        f"checked against a source._\n\n{text}")

        if not text:
            problems.append("no brief was produced at all")
            conn.execute("UPDATE run_log SET summary = ?, detail = ? WHERE id = ?",
                         ("no brief produced", json.dumps({"problems": problems}), run_id))
            conn.commit()
            return {"delivered": False, "problems": problems}

        delivery = deliver(conn, when, text, run_id=run_id)
        problems += delivery["problems"]
        if delivery["note"]:
            # Only once it is actually in front of him is the digest considered reported.
            try:
                tri.mark_reported(conn)
            except Exception as e:
                problems.append(f"digest marking failed: {e}")

        conn.execute("UPDATE run_log SET summary = ?, detail = ? WHERE id = ?",
                     (text[:4000],
                      runner.usage_record(result, source=source, problems=problems), run_id))
        conn.commit()
    return {"delivered": bool(delivery["note"]), "source": source,
            "problems": problems, "note": delivery["note"]}


def _local_brief(conn, when: str, digest: str, run_id=None) -> str:
    """The fallback. Same shape, no web, and honest about it."""
    from synth import agent

    try:
        result = agent.run(
            conn, job="brief", trigger=when,
            system=runner.prompt("brief", core_only=True, when=when, digest=digest,
                                 budget="(not applicable — this brief is local and free)",
                                 depth=DEPTH_LOCAL),
            user=f"Write Arun's {when} brief now, using the tools to check what is actually "
                 f"due and what changed. Then stop.",
            tool_names=["today", "agenda", "obligations", "activity", "search", "entity"],
            tier="reason", max_turns=10, max_writes=0, wall_seconds=420, run_id=run_id)
        return (result.get("text") or "").strip()
    except Exception:
        return ""
