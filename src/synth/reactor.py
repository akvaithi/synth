"""Runs a Claude session over pending changes.

Detection is model-free and cheap; this is the only expensive part, so it runs on a debounce
and only when there is genuinely something to consider. Each run is recorded in run_log
whether or not it changed anything.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone

from synth import budget, config, db

ROOT = os.path.expanduser("~/Developer/synth")
CLAUDE = os.path.expanduser("~/.local/bin/claude")
PROMPTS = os.path.join(ROOT, "prompts")

# The VM side calls the tool layer directly through `synth call`. Twenty-odd MCP schemas
# were being re-sent on every turn for no benefit -- MCP earns its keep on the Claude app
# side, where schemas are the interface, not here.
SYNTH = os.path.join(ROOT, "bin", "synth")
# Allow every spelling of the same command. Runs happen with cwd=ROOT, so the model reaches
# for the relative form; an absolute-only allowlist silently failed to match and the run
# stalled asking for approval it could never get.
DIRECT_TOOLS = [
    "Bash(bin/synth:*)", "Bash(./bin/synth:*)", f"Bash({SYNTH}:*)",
    "WebSearch", "WebFetch",
]

# Reads only, for briefs.
DIRECT_READ_TOOLS = list(DIRECT_TOOLS)
# Triage judges subject lines it was handed. Giving it tools invites it to go
# reading bodies, which is the cost this tier exists to avoid -- and the built-ins have to be
# named to be gone, because an empty allowlist still leaves them offered.
NO_TOOLS = []
DENY_ALL = ["Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep",
            "WebSearch", "WebFetch", "Task", "TodoWrite", "SlashCommand", "KillShell",
            "BashOutput"]

# Two tiers. Triage reads subject lines and needs no tools, so it runs on the cheapest
# model available; only what triage promotes is worth Sonnet and a full tool loop.
TRIAGE_MODEL = os.environ.get("SYNTH_TRIAGE_MODEL", "haiku")
REACTOR_MODEL = os.environ.get("SYNTH_REACTOR_MODEL", "sonnet")
# The morning brief researches and is the one Arun acts on, so it gets the stronger model.
# The evening brief recaps a day whose facts were already established; measured side by side,
# Haiku produced a tighter version of it for a fifth of the cost.
BRIEF_MODEL = os.environ.get("SYNTH_BRIEF_MODEL", "sonnet")
BRIEF_LIGHT_MODEL = os.environ.get("SYNTH_BRIEF_LIGHT_MODEL", "haiku")
BRIEF_MAX_TURNS = int(os.environ.get("SYNTH_BRIEF_MAX_TURNS", "45"))
BRIEF_LIGHT_TURNS = int(os.environ.get("SYNTH_BRIEF_LIGHT_TURNS", "18"))

DEPTH_DEEP = """This is the researched brief of the day. Where a deadline, an eligibility rule
or a claim about an organisation is unverified, check it against the official source with
WebSearch or WebFetch and say what you found. Arun asked for findings, not hedged commentary:
establish the fact or leave the item out."""

DEPTH_LIGHT = """This is the short evening brief. The day's facts were already established
this morning — do not re-research them, and you have no web tools here. Report what actually
changed since the last brief, what is due tomorrow morning, and nothing else. If the day was
quiet, a four-line brief is the correct brief."""

def _prompt(name: str, core_only: bool = False, **fmt) -> str:
    """Assemble a prompt. `core_only` sends the short doctrine.

    The full doctrine is 8KB and was prepended to every run including ones that only look at
    subject lines. The cheap tier gets the non-negotiables and nothing else.
    """
    doc = "doctrine-core.md" if core_only else "doctrine.md"
    with open(os.path.join(PROMPTS, doc)) as f:
        doctrine = f.read()
    with open(os.path.join(PROMPTS, f"{name}.md")) as f:
        body = f.read()
    for k, v in fmt.items():
        body = body.replace("{" + k + "}", v)
    return f"{doctrine}\n\n---\n\n{body}"


def run_claude(prompt: str, allowed: list[str], max_turns: int = 40,
               timeout: int = 1800, model: str | None = None,
               thinking: int | None = None, denied: list[str] | None = None) -> dict:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # never bill the API; this must ride the subscription
    if thinking is not None:
        # Triage produced 4,584 output tokens to return twelve one-line verdicts, and output
        # is the expensive half on Haiku. Sorting subject lines needs no deliberation.
        env["MAX_THINKING_TOKENS"] = str(thinking)
    cmd = [
        CLAUDE, "-p", prompt,
        "--output-format", "json",
        # No MCP on the VM side. The tool layer is reached through `synth call`, so loading
        # the server here would re-add two dozen schemas on every turn -- the exact cost this
        # was changed to avoid. An empty strict config guarantees none are loaded.
        # --strict-mcp-config with no --mcp-config loads no servers at all.
        "--strict-mcp-config",
        "--allowed-tools", ",".join(allowed),
        "--permission-mode", "acceptEdits",
        "--max-turns", str(max_turns),
    ]
    if denied:
        # An empty --allowed-tools does not remove the built-ins: the model reached for one,
        # was refused, and burned both its turns doing it. They have to be denied by name.
        cmd += ["--disallowed-tools", ",".join(denied)]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, timeout=timeout)
    out = proc.stdout.decode("utf-8", errors="replace")
    try:
        result = json.loads(out)
    except ValueError:
        result = {"is_error": proc.returncode != 0, "result": out[-4000:],
                  "stderr": proc.stderr.decode("utf-8", errors="replace")[-2000:]}
    # A refusal states when the limit resets. Reading it is the difference between one quiet
    # wait and the 574 rejected calls that filled two silent days.
    b = budget.note_failure(result)
    if b:
        result["backoff"] = b
    return result


def summarise_events(events: list[dict], cap: int = 60) -> str:
    """Compact the queue into something worth a model's attention."""
    by_kind: dict[str, list[dict]] = {}
    for e in events:
        by_kind.setdefault(e.get("kind", "unknown"), []).append(e)
    lines = []
    for kind, group in sorted(by_kind.items()):
        if kind == "mail_new":
            lines.append(f"### {len(group)} new message(s)")
            for e in group[:cap]:
                lines.append(f"- [{e.get('account')}] {e.get('receivedAt','')} "
                             f"from {e.get('sender','')}\n  subject: {e.get('subject','')}\n"
                             f"  messageId: {e.get('messageId')}  index: {e.get('index','?')}")
        elif kind == "note_edited":
            lines.append(f"### {len(group)} note(s) you rendered were edited by Arun")
            for e in group:
                lines.append(f"- doc `{e.get('doc')}` (note {e.get('noteId')}) — read it and "
                             f"work out what he corrected")
        elif kind == "eventkit_changed":
            lines.append("### Reminders or Calendar changed "
                         f"({len(group)} notification(s)) — compare against known obligations")
        elif kind.endswith("_error"):
            lines.append(f"### {len(group)} detection error(s)")
            for e in group[:5]:
                lines.append(f"- {e.get('detail','')[:160]}")
        else:
            lines.append(f"### {kind} ({len(group)})")
    return "\n".join(lines) if lines else "(nothing)"


GROUPS = {
    "mail": {"mail_new"},
    "schedule": {"eventkit_changed"},
    "notes": {"note_edited"},
}

# A ceiling on how often the expensive stage may run, whatever the watcher thinks.
# Arun asked for ~2-minute reaction, and with FS noise triaged out the watcher rarely has
# anything. This floor is a backstop against a pathological loop, not the normal cadence.
# A floor on how often the expensive stage may run, whatever the watcher thinks. The real
# protection is now budget.allowed(), which counts money rather than runs; this only stops a
# pathological loop from spinning.
MIN_SECONDS_BETWEEN_RUNS = int(os.environ.get("SYNTH_MIN_RUN_GAP", "180"))

# Observed runs averaged 13 turns. Forty was room for a run to wander.
REACTOR_MAX_TURNS = int(os.environ.get("SYNTH_REACTOR_MAX_TURNS", "24"))


# Beyond this, a single run carries so much unrelated material that judgement degrades and
# the budget can run out midway -- which is how an already-sent reply got a reminder telling
# Arun to send it.
MAX_EVENTS_PER_RUN = int(os.environ.get("SYNTH_MAX_EVENTS_PER_RUN", "12"))

# The most a single batch may promote to a full run. Asked to sort twelve subjects, triage
# called six of them actionable -- including a football ticket promotion. A ceiling turns a
# generous grader into one that has to rank, and the overflow is named in the brief rather
# than lost.
MAX_ACTS_PER_BATCH = int(os.environ.get("SYNTH_MAX_ACTS", "4"))


def chunk(events: list[dict]) -> list[tuple[str, list[dict]]]:
    """Split a batch by kind.

    One long thread carrying mail, calendar changes and note edits at once burns context on
    material irrelevant to each decision, and a run that exhausts its budget half way through
    leaves the work silently incomplete -- which is how a reply that had already been sent
    got a reminder telling Arun to send it.
    """
    out = []

    def add(name, group):
        for i in range(0, len(group), MAX_EVENTS_PER_RUN):
            part = group[i:i + MAX_EVENTS_PER_RUN]
            label = name if len(group) <= MAX_EVENTS_PER_RUN else \
                f"{name} {i // MAX_EVENTS_PER_RUN + 1}/{-(-len(group) // MAX_EVENTS_PER_RUN)}"
            out.append((label, part))

    for name, kinds in GROUPS.items():
        group = [e for e in events if e.get("kind") in kinds]
        if group:
            add(name, group)
    known = {k for ks in GROUPS.values() for k in ks}
    rest = [e for e in events if e.get("kind") not in known]
    if rest:
        add("other", rest)
    return out


USAGE_FIELDS = ("total_cost_usd", "num_turns", "duration_api_ms", "is_error",
                "stop_reason", "session_id", "subtype", "terminal_reason")
USAGE_TOKENS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens")


def usage_record(result: dict, **extra) -> str:
    """A small, bounded summary of what a run cost.

    The whole result was being dumped and cut at 4000 characters, which produced invalid JSON
    whenever it ran long -- and the ledger silently read those runs as free. A brief that cost
    real money showed up as $0.00, which is precisely the blindness this was meant to end. So
    only the fields the ledger needs are kept, and they cannot overflow.
    """
    rec = {k: result[k] for k in USAGE_FIELDS if k in result}
    u = result.get("usage") or {}
    rec["usage"] = {k: u.get(k, 0) for k in USAGE_TOKENS}
    if result.get("backoff"):
        rec["backoff"] = result["backoff"].get("kind")
    for k, v in extra.items():
        if v:
            rec[k] = v
    return json.dumps(rec, default=str)[:4000]


def _record(conn, run_id, result) -> None:
    conn.execute("UPDATE run_log SET summary = ?, detail = ? WHERE id = ?",
                 (str(result.get("result", ""))[:2000], usage_record(result), run_id))
    conn.commit()


def _spend(conn, job: str, trigger: str, prompt: str, tools: list[str],
           model: str, max_turns: int, thinking: int | None = None,
           denied: list[str] | None = None) -> dict:
    """One model call, budget-checked immediately before it happens and logged either way.

    The check has to be here rather than once per batch. The old code asked permission once
    and then launched a run per chunk, so a single yes bought three or four runs.
    """
    ok, why = budget.allowed(conn, job)
    if not ok:
        budget.log_skip(conn, job, trigger, why)
        return {"skipped": True, "is_error": False, "result": f"skipped: {why}"}
    with db.run(conn, job, trigger=trigger) as run_id:
        result = run_claude(prompt, tools, model=model, max_turns=max_turns,
                            thinking=thinking, denied=denied)
        result["run_id"] = run_id
        _record(conn, run_id, result)
    return result


def _obligation_lines(conn, limit: int = 25) -> str:
    rows = conn.execute(
        "SELECT title, due FROM obligation WHERE status IN ('open','waiting') "
        "ORDER BY due IS NULL, due LIMIT ?", (limit,)).fetchall()
    if not rows:
        return "(nothing open)"
    return "\n".join(f"- {r['title']}" + (f" — due {db.local(r['due'])}" if r["due"] else "")
                      for r in rows)


def _message_table(messages: list[dict]) -> str:
    out = []
    for m in messages:
        out.append(f"- messageId: {m.get('messageId')}\n"
                   f"  from: {m.get('sender','')}\n"
                   f"  subject: {m.get('subject','')}\n"
                   f"  received: {m.get('receivedAt','')}  account: {m.get('account','')}")
    return "\n".join(out)


def _extract_verdicts(text: str) -> dict:
    """Pull the verdict object out of whatever the model wrapped it in.

    Haiku narrates before answering however firmly it is told not to, and a greedy brace
    match spanning that prose would swallow the wrong thing. Candidates are tried newest
    first: the answer is at the end, the reasoning is in front of it.
    """
    out: dict = {}
    starts = [m.start() for m in re.finditer(r"\{", text or "")]
    for i in reversed(starts):
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


def triage_mail(conn, messages: list[dict]) -> dict:
    """Tier 1. Subject lines only, cheapest model, no tools.

    Arun's idea, and the right one: read the subject first and let that decide whether the
    body is worth the money. The refinement is that subjects lie selectively -- they lie for
    people who matter, not for no-reply@e2ma.net -- which is why the model-free pass has
    already sent anyone the database vouches for straight past this tier.
    """
    prompt = _prompt("triage", core_only=True,
                     messages=_message_table(messages),
                     obligations=_obligation_lines(conn))
    result = _spend(conn, "triage", f"triage: {len(messages)} subject(s)",
                    prompt, NO_TOOLS, TRIAGE_MODEL, max_turns=4, thinking=0,
                    denied=DENY_ALL)
    if result.get("skipped") or result.get("is_error"):
        return {"verdicts": {}, "result": result}
    verdicts = _extract_verdicts(str(result.get("result", "")))
    if not verdicts:
        # Unparseable output must not silently drop mail. Treat the batch as worth a look
        # rather than assuming it was noise.
        return {"verdicts": {m.get("messageId"): ("act", "triage output unreadable")
                             for m in messages}, "result": result, "degraded": True}
    return {"verdicts": verdicts, "result": result}


def act_on_mail(conn, messages: list[dict], label: str = "mail") -> dict:
    """Tier 2. Full doctrine, full tools, only for what earned it."""
    prompt = _prompt("reactor", events=summarise_events(
        [dict(m, kind="mail_new") for m in messages]))
    return _spend(conn, "reactor", f"{label}: {len(messages)} message(s)",
                  prompt, DIRECT_TOOLS, REACTOR_MODEL, max_turns=REACTOR_MAX_TURNS)


def react_mail(conn, messages: list[dict]) -> dict:
    """The whole mail path: filter free, triage cheap, act expensively and rarely."""
    from synth import triage as tri

    split = tri.split(conn, messages)
    tri.hold(conn, split["digest"] + split["ignore"])
    filtered = len(split["digest"]) + len(split["ignore"])

    to_act = list(split["urgent"])
    notes = [f"{len(messages)} message(s): {filtered} filtered without a model, "
             f"{len(split['urgent'])} urgent, {len(split['consider'])} to triage"]

    if split["consider"]:
        t = triage_mail(conn, split["consider"])
        if t.get("result", {}).get("skipped"):
            # No budget for even the cheap pass. Hold them; they are not lost.
            return {"result": "\n".join(notes + ["triage skipped, messages held"]),
                    "is_error": False, "skipped": True, "handled": [], "held": messages}
        by_id = {m.get("messageId"): m for m in split["consider"]}
        promoted, held = [], []
        for mid, (verdict, why) in t["verdicts"].items():
            m = by_id.get(mid)
            if not m:
                continue
            if verdict == "act":
                promoted.append(dict(m, decided_by=f"triage: {why}"))
            else:
                held.append(dict(m, verdict=verdict, decided_by=f"triage: {why}"))
        overflow = promoted[MAX_ACTS_PER_BATCH:]
        promoted = promoted[:MAX_ACTS_PER_BATCH]
        for m in promoted:
            tri.credit_action(conn, m.get("sender", ""))
        to_act += promoted
        tri.hold(conn, held + [dict(m, verdict="digest",
                                    decided_by=f"{m['decided_by']} — over the per-batch limit")
                               for m in overflow])
        notes.append(f"triage promoted {len(promoted)} of {len(split['consider'])}"
                     + (f", {len(overflow)} over the limit went to the digest" if overflow else ""))

    if not to_act:
        notes.append("nothing warranted a full run")
        return {"result": "\n".join(notes), "is_error": False, "handled": messages}

    results = []
    for i in range(0, len(to_act), MAX_EVENTS_PER_RUN):
        part = to_act[i:i + MAX_EVENTS_PER_RUN]
        r = act_on_mail(conn, part)
        results.append(r)
        if r.get("skipped") or r.get("backoff"):
            # Out of budget mid-way. Everything not yet examined stays queued.
            done = [m for m in messages if m not in to_act[i:]]
            return {"result": "\n".join(notes + [str(r.get("result", ""))]),
                    "is_error": False, "skipped": True,
                    "handled": done, "held": to_act[i:]}
    notes += [str(r.get("result", "")) for r in results]
    return {"result": "\n".join(notes), "is_error": any(r.get("is_error") for r in results),
            "handled": messages}


def react(conn, events: list[dict], force: bool = False) -> dict:
    """Run one focused pass per kind of change.

    Returns `handled` -- the events it actually finished with. Anything not in that list stays
    queued. The old code cleared the whole queue whenever a run returned, including when the
    API had refused it outright, which is how two days of detected mail was thrown away.
    """
    mail = [e for e in events if e.get("kind") == "mail_new"]
    other = [e for e in events if e.get("kind") != "mail_new"]
    handled: list[dict] = []
    parts: list[str] = []
    skipped = False

    if mail:
        r = react_mail(conn, mail)
        parts.append(str(r.get("result", "")))
        handled += r.get("handled", [])
        skipped = skipped or bool(r.get("skipped"))

    for name, group in chunk(other):
        r = _spend(conn, "reactor", f"{name}: {len(group)} event(s)",
                   _prompt("reactor", events=summarise_events(group)),
                   DIRECT_TOOLS, REACTOR_MODEL, REACTOR_MAX_TURNS)
        parts.append(f"## {name}\n{r.get('result', '')}")
        if r.get("skipped") or r.get("backoff"):
            skipped = True
            break
        handled += group

    if not parts:
        return {"result": "nothing to react to", "is_error": False, "handled": events}
    return {"result": "\n\n".join(parts), "is_error": False,
            "skipped": skipped, "handled": handled}


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
        line = (f"- [{r['account']}] {when} {r['sender']} — {r['subject']}  "
                f"({r['decided_by']})")
        try:
            urls = json.loads(r["links"] or "[]")
        except ValueError:
            urls = []
        if urls:
            line += "\n  links: " + "  ".join(urls[:3])
        out.append(line)
    return "\n".join(out)


def brief(conn, when: str = "morning") -> dict:
    """The brief is the product. It is the last thing to be starved when budget is tight."""
    ok, why = budget.allowed(conn, "brief")
    if not ok:
        budget.log_skip(conn, "brief", when, why)
        return {"skipped": True, "is_error": False, "result": f"brief skipped: {why}"}

    with db.run(conn, "brief", trigger=when) as run_id:
        problems = []
        # Reconcile first, or the brief reports work Arun has already finished as overdue.
        try:
            from synth import tools
            tools.sync_obligations(conn, run_id=run_id)
        except Exception as e:
            problems.append(f"obligation sync failed: {e}")

        from synth import triage as tri
        try:
            tri.learn(conn)
        except Exception as e:
            problems.append(f"sender learning failed: {e}")

        digest = digest_lines(conn)
        # The morning brief is the one he acts on, and Arun asked for research rather than
        # hedging, so it keeps the web tools and a generous turn ceiling. The evening brief is
        # a status check over a day whose facts were already established, and paying twice for
        # the same research is how a fair cost becomes an unaffordable one.
        deep = when == "morning"
        prompt = _prompt("brief", when=when, digest=digest, budget=budget.summary(conn),
                         depth=DEPTH_DEEP if deep else DEPTH_LIGHT)
        tools = DIRECT_READ_TOOLS if deep else [t for t in DIRECT_READ_TOOLS
                                                if not t.startswith("Web")]
        turns = BRIEF_MAX_TURNS if deep else BRIEF_LIGHT_TURNS
        model = BRIEF_MODEL if deep else BRIEF_LIGHT_MODEL
        # Thirty was not enough once the digest arrived and the model started fetching a
        # link per message. The links are handed over ready-made now, but the ceiling stays
        # generous: a brief that runs out of turns produces nothing at all and still bills.
        result = run_claude(prompt, tools, max_turns=turns, model=model)
        if result.get("is_error") and result.get("num_turns", 0) >= turns:
            problems.append(f"ran out of turns after {result['num_turns']} and produced "
                            f"nothing — the prompt is asking for more work than it affords")
        conn.execute("UPDATE run_log SET summary = ? WHERE id = ?",
                     (str(result.get("result", ""))[:4000], run_id))
        conn.commit()

        if not result.get("is_error"):
            from synth import deliver, notes_sync
            try:
                notes_sync.render(conn, "brief", run_id=run_id)
            except Exception as e:
                problems.append(f"Notes delivery failed: {e}")
            # The headless run cannot push; the Remote Control session can. Hand it over.
            try:
                d = deliver.deliver_brief(when)
                if not d.get("delivered"):
                    problems.append(f"push delivery failed: {d.get('reason')}")
            except Exception as e:
                problems.append(f"push delivery failed: {e}")
            # Only once it has been delivered are the digest entries considered reported.
            try:
                tri.mark_reported(conn)
            except Exception as e:
                problems.append(f"digest marking failed: {e}")

        # detail carries the usage block: it is the ledger budget.py reads, so delivery
        # problems ride inside it rather than replacing it. Writing plain text here is why
        # brief cost was invisible and the governor could not see its own most important job.
        conn.execute("UPDATE run_log SET detail = ? WHERE id = ?",
                     (usage_record(result, problems=problems), run_id))
        conn.commit()
    return result
