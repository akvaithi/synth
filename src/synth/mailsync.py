"""Keeping the mail index current, and stopping there.

The old path had three tiers: a free static filter, a Haiku pass over subject lines, and a
Sonnet run with every tool that acted on what the first two promoted. That third tier is what
made Synth autonomous, and it is what Arun asked to remove -- it filed reminders, booked
events and drafted replies off the back of mail he had not read yet.

Tiers one and two remain, because they are what makes the index worth having. Every message
is recorded with a verdict and the reasoning behind it, so when he asks "anything urgent?" the
answer is already sorted rather than assembled on the spot. What used to be promoted to a full
run is now simply marked urgent and left for him.

Cost: nothing, in the ordinary case. The static tier is free and settles most of it. What it
cannot settle goes to a model on Arun's own network, which is also free. Metered Haiku is now
only the escalation for local output that cannot be read at all, and the free static rules are
the floor beneath that -- so three things have to fail before a message is sorted badly, and
none of them can drop it.
"""
from __future__ import annotations

import json
import re

from synth import db, ollama, runner, triage as tri


def _obligation_lines(conn, limit: int = 25) -> str:
    rows = conn.execute(
        "SELECT title, due FROM obligation WHERE status IN ('open','waiting') "
        "ORDER BY due IS NULL, due LIMIT ?", (limit,)).fetchall()
    if not rows:
        return "(nothing open)"
    return "\n".join(f"- {r['title']}" + (f" — due {db.local(r['due'])}" if r["due"] else "")
                     for r in rows)


def _message_table(messages: list[dict]) -> str:
    return "\n".join(
        f"- messageId: {m.get('messageId')}\n"
        f"  from: {m.get('sender','')}\n"
        f"  subject: {m.get('subject','')}\n"
        f"  received: {m.get('receivedAt','')}  account: {m.get('account','')}"
        for m in messages)


# The free classifier's four verdicts mapped onto the three the model returns, for when the
# model's answer cannot be read. `consider` is what the static rules say when they recognise
# nothing either way, and that is the case that has to stay visible -- so it maps to `act`.
STATIC_TO_VERDICT = {"urgent": "act", "consider": "act", "digest": "digest", "ignore": "digest"}


def _extract_verdicts(text: str) -> dict:
    """Pull the verdict object out of whatever the model wrapped it in.

    Haiku narrates before answering however firmly it is told not to, and a greedy brace match
    spanning that prose would swallow the wrong thing. Candidates are tried newest first: the
    answer is at the end, the reasoning is in front of it.
    """
    out: dict = {}
    for i in reversed([m.start() for m in re.finditer(r"\{", text or "")]):
        depth = 0
        for j in range(i, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[i:j + 1])
                    except ValueError:
                        break
                    if isinstance(obj, dict) and isinstance(obj.get("verdicts"), list):
                        for v in obj["verdicts"]:
                            if isinstance(v, dict) and v.get("messageId"):
                                out[v["messageId"]] = (v.get("verdict", "digest"),
                                                       v.get("why", ""))
                        return out
                    break
    return out


# Subject-line triage as a schema, so the answer cannot arrive as prose that has to be found.
# The old path scraped braces out of whatever Haiku wrapped its answer in; a constrained
# decode makes that the fallback rather than the mechanism.
TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "messageId": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["act", "digest", "ignore"]},
                    "why": {"type": "string"},
                },
                "required": ["messageId", "verdict"],
            },
        },
    },
    "required": ["verdicts"],
}


def triage_batch(conn, messages: list[dict]) -> dict:
    """Subject lines only. Local first, metered Claude only if local cannot answer.

    This was the last recurring thing Synth paid for besides the brief, at about five cents a
    batch -- which was cheap until the brief came back at three dollars a run and the daily
    ceiling started refusing triage to protect it. Sorting subject lines is exactly what an 8B
    model on Arun's own network is for, and moving it here means the whole budget belongs to
    the one job that actually needs a metered model.

    Escalation is mechanical, never a judgement: local output that cannot be read, or local
    inference that is not there at all. A local run that answers is trusted.
    """
    prompt = runner.prompt("triage", core_only=True,
                           messages=_message_table(messages),
                           obligations=_obligation_lines(conn))
    result: dict = {}
    verdicts: dict = {}
    try:
        answer = ollama.generate_json(prompt, TRIAGE_SCHEMA, tier="fast")
        for v in answer.get("verdicts", []):
            if isinstance(v, dict) and v.get("messageId"):
                verdicts[v["messageId"]] = (v.get("verdict", "digest"), v.get("why", ""))
        result = {"source": "local", "count": len(verdicts)}
    except ollama.OllamaError as e:
        result = {"source": "local", "error": f"{type(e).__name__}: {e}"}

    if not verdicts:
        # Local could not answer. This is one of the two mechanical escalation triggers, and
        # it is still budget-gated -- a broken tunnel must not be able to spend the month.
        result = runner.spend(conn, "triage", f"triage: {len(messages)} subject(s)",
                              prompt, runner.NO_TOOLS, model=runner.TRIAGE_MODEL,
                              max_turns=4, thinking=0, denied=runner.DENY_ALL)
        if result.get("skipped") or result.get("is_error"):
            return {"verdicts": {}, "result": result}
        verdicts = _extract_verdicts(str(result.get("result", "")))
    if not verdicts:
        # Unparseable output must not silently drop mail -- but it must not cry wolf either.
        # This used to mark every message in the batch "act", so one unreadable reply turned
        # a quiet inbox into a full one. Fall back to the free tier instead: static rules and
        # learned sender policy already settle most mail for nothing, and they are a strictly
        # better guess than a blanket flag. Anything they cannot settle still comes back
        # `consider`, so nothing is dropped and the noise is bounded by what is genuinely
        # unrecognised. `degraded` stays set: the caller records that this batch was not
        # actually judged by a model.
        ctx = tri.context(conn)
        fallback = {}
        for m in messages:
            if not m.get("messageId"):
                continue
            try:
                verdict = tri.classify(m, ctx)[0]
            except Exception:
                # A classifier that cannot judge is not a reason to hide a message.
                verdict = "consider"
            fallback[m["messageId"]] = (
                STATIC_TO_VERDICT.get(verdict, "act"),
                "triage output unreadable; sorted by static rules instead")
        return {"verdicts": fallback, "result": result, "degraded": True}
    return {"verdicts": verdicts, "result": result}


def index_mail(conn, messages: list[dict]) -> dict:
    """Sort a batch of mail into the index. Never acts on any of it."""
    if not messages:
        return {"messages": 0, "note": "nothing new"}

    split = tri.split(conn, messages)
    tri.hold(conn, split["digest"] + split["ignore"])
    filtered = len(split["digest"]) + len(split["ignore"])
    urgent = [dict(m, verdict="urgent", decided_by=m.get("decided_by", "static rules"))
              for m in split["urgent"]]
    stats = {"messages": len(messages), "filtered_free": filtered,
             "urgent": len(split["urgent"]), "triaged": 0, "flagged": 0}

    if split["consider"]:
        t = triage_batch(conn, split["consider"])
        if t.get("result", {}).get("skipped"):
            # No budget even for the cheap pass. Hold them unsorted; they are not lost, and
            # the next sweep will sort them.
            stats["note"] = "triage skipped for budget; messages held unsorted"
            tri.hold(conn, split["consider"])
            tri.hold(conn, urgent)
            return stats
        stats["triaged"] = len(split["consider"])
        by_id = {m.get("messageId"): m for m in split["consider"]}
        rest = []
        for mid, (verdict, why) in t["verdicts"].items():
            m = by_id.get(mid)
            if not m:
                continue
            if verdict == "act":
                # What the old system would have handed to Sonnet. It is recorded as urgent
                # and waits for him instead.
                urgent.append(dict(m, verdict="urgent", decided_by=f"triage: {why}"))
                tri.credit_action(conn, m.get("sender", ""))
            else:
                rest.append(dict(m, verdict=verdict, decided_by=f"triage: {why}"))
        tri.hold(conn, rest)

    tri.hold(conn, urgent)
    stats["flagged"] = len(urgent)
    return stats


def unread_urgent(conn, limit: int = 20) -> list[dict]:
    """What the index has flagged and he has not been told about yet."""
    return [dict(r) for r in conn.execute(
        "SELECT account, sender, subject, received_at, decided_by, links "
        "FROM mail_digest WHERE verdict = 'urgent' AND reported_at IS NULL "
        "ORDER BY received_at DESC LIMIT ?", (limit,))]
