"""A local model, the tool layer, and a loop with six ways to stop.

## Why this calls the tools directly instead of shelling out

Headless Claude runs reach the tool layer through `Bash(bin/synth:*)` -- `synth call <name>
<json>` -- because the CLI can only reach tools through Bash, and twenty-odd MCP schemas
re-sent every turn cost more than one Bash definition. Neither constraint applies here: this
runs in-process with a live connection, and it buys three things that matter.

* Each `synth call` is a fresh interpreter, a fresh `db.connect()` and a fresh import of
  tools.py, which pulls markitdown transitively. That is a second or two per call, and twenty
  turns of it is most of a minute spent starting Python.
* Exceptions arrive as objects rather than as a nonzero exit and some JSON on stderr. The
  tools raise *good sentences* -- `_require_reason`, `docwrite.resolve`, `_refuse_duplicate`
  -- and those sentences are the best steering available for a small model.
* **Every action_log row gets a real run_id.** `cmd_call` opens its own connection, so
  everything a headless run wrote today is logged with `run_id = NULL`. "What did this
  reaction actually do" being a single query is the entire premise of the runaway guard.

## Why the caps are structural

The tool set is built from `registry.AUTONOMOUS`, which does not contain the delete names, and
the dispatcher refuses any name that is not in the set it was built with. A model cannot reach
a delete by naming one, whether it inferred the name, read it in an email, or was told to.

## What a small model does that a large one does not

It loops. It will call the same tool with the same arguments repeatedly, and it will keep
going after the answer is already in front of it. Repeat-call suppression is four lines here
and is the single most valuable guard in the file: without it, a retried `create_reminder` is
a duplicate in Arun's list rather than a wasted turn.
"""
from __future__ import annotations

import inspect
import json
import os
import time

from synth import ollama, registry

HALT = os.path.expanduser("~/Developer/synth/.state/HALT")

# Supplied by the loop, never offered to the model. A model that can pass run_id can forge
# provenance -- it could attribute its own write to a different run, or to none.
INJECTED = {"conn", "run_id", "evidence_id"}

MAX_TURNS = 12
MAX_WRITES = 4
WALL_SECONDS = 600
MAX_CONSECUTIVE_ERRORS = 3

# A small model narrates a plan and treats having said it as having done it. Watching the
# reactor decide about an email headed "Action Required - RSVP ... for TAMU Dell Night 2026":
# it read the body, correctly judged it actionable, called already_scheduled, got back no
# matches at all -- and then stopped, with a closing message that read "I need to check if
# this is already scheduled. I will check for an event titled ...". It had already checked.
# The answer was sitting in front of it and it described the intention instead of using it.
#
# So a run that ends having only LOOKED, while saying it meant to act, is asked once. Once,
# not repeatedly: a model that declines twice is declining, and pushing further would be
# arguing with it until it writes something to make the question stop.
INTENT = ("i will ", "i'll ", "i need to ", "i should ", "next, i", "i am going to ",
          "i plan to ", "let me ")

NUDGE = ("You described what you were going to do rather than doing it. The tool results "
         "above are the answer to whatever you were checking — read them and finish. If the "
         "answer means no action is warranted, say that plainly instead. Do not restate the "
         "plan.")

# Tools whose signature does not describe them well enough to call.
#
# Two take **fields, which a signature cannot describe at all. The third is worse and was only
# found by watching it fail: `add_facts(payload: dict)` derives to a bare {"type": "object"},
# so the model is told a dict goes here and nothing about its shape. gemma4 duly sent
# something plausible, ingest_batch accepted it, and nothing was recorded -- two "successful"
# writes, no assertions, no action_log rows. A nested payload has to be spelled out or the
# most important tool in the enrichment pass silently does nothing.
SCHEMA_OVERRIDES = {
    "add_facts": {
        "payload": {
            "type": "object",
            "description": "the facts to record, grouped by entity",
            "properties": {
                "entities": {
                    "type": "array",
                    "description": "one entry per person, program, course, project or award",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string",
                                     "enum": ["person", "org", "program", "course",
                                              "application", "project", "award", "topic"]},
                            "name": {"type": "string",
                                     "description": "canonical and reusable: 'Goldwater "
                                                    "Scholarship', never 'the Goldwater'"},
                            "description": {"type": "string"},
                            "status": {"type": "string"},
                            "facts": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "predicate": {
                                            "type": "string",
                                            "description": "what this states, e.g. "
                                                           "'deadline' or 'credit hours'"},
                                        "text": {"type": "string"},
                                        "num": {"type": "number"},
                                        "date": {"type": "string",
                                                 "description": "ISO 8601, YYYY-MM-DD"},
                                        "confidence": {"type": "number"},
                                    },
                                    "required": ["predicate"],
                                },
                            },
                        },
                        "required": ["kind", "name", "facts"],
                    },
                },
            },
            "required": ["entities"],
        },
        "document_id": {"type": "integer",
                        "description": "the document these facts came from"},
    },
    "update_reminder": {
        "ek_identifier": {"type": "string", "description": "the reminder's stored identifier"},
        "reason": {"type": "string", "description": "why this helps Arun, in plain words"},
        "title": {"type": "string"},
        "due": {"type": "string", "description": "ISO 8601, with a time"},
        "notes": {"type": "string"},
    },
    "update_event": {
        "ek_identifier": {"type": "string", "description": "the event's stored identifier"},
        "reason": {"type": "string", "description": "why this helps Arun, in plain words"},
        "title": {"type": "string"},
        "start": {"type": "string", "description": "ISO 8601"},
        "end": {"type": "string", "description": "ISO 8601"},
        "location": {"type": "string"},
        "notes": {"type": "string"},
    },
}
REQUIRED_OVERRIDES = {
    "update_reminder": ["ek_identifier", "reason"],
    "update_event": ["ek_identifier", "reason"],
    "add_facts": ["payload"],
}

# Descriptions for parameter names whose meaning a model cannot infer from the name alone.
# A derived schema gives every string the same shape, so `account` and `mailbox` arrive
# indistinguishable and gemma4 filled both with "College" -- ten wasted calls in the first
# live hour, each burning a turn and pushing the run toward the tool it did understand.
#
# Keyed by name rather than by tool because these mean the same thing everywhere they appear,
# and the ones that do not need explaining are left out: `title`, `reason` and `due` say what
# they are.
PARAM_DESCRIPTIONS = {
    "account": "which of Arun's four mail accounts: Work, College, Personal or iCloud",
    "mailbox": "the folder WITHIN that account, e.g. 'INBOX' or 'Sent Mail'. This is NOT the "
               "account name — leave it out unless you mean a folder other than the inbox.",
    "index": "the message's position in the mailbox, from the event or from `mail`",
    "messageId": "the RFC822 Message-ID, which is what actually identifies the message",
    "ek_identifier": "the stored EventKit identifier, never the title",
    "when": "ISO 8601, e.g. 2026-09-15T18:00:00",
    "due": "ISO 8601 with a time. An untimed reminder never appears in Calendar, which is "
           "where Arun reads his day.",
    "start": "ISO 8601, with a time",
    "end": "ISO 8601, with a time",
    "calendar": "one of the writable calendars: Personal, Semester Calendar, College Events, "
                "Meetings. Anything else is refused.",
    "list": "an existing Reminders list. Synth cannot create one; an unknown name is refused.",
    "reason": "why this helps Arun, in plain words he would understand months from now",
    "evidence_source": "the Message-ID of the mail that justifies this",
}

_JSON_TYPES = {
    str: "string", int: "integer", float: "number", bool: "boolean",
    list: "array", dict: "object",
}


def _json_type(annotation) -> dict:
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}
    if annotation in _JSON_TYPES:
        return {"type": _JSON_TYPES[annotation]}
    text = str(annotation)
    for py, js in (("str", "string"), ("int", "integer"), ("float", "number"),
                   ("bool", "boolean"), ("list", "array"), ("dict", "object")):
        if text.startswith(py) or text.startswith(f"{py} |") or f"[{py}" in text:
            return {"type": js}
    return {"type": "string"}


def _describe(fn) -> str:
    """The first paragraph of the docstring. The rest is for a human reading the source."""
    doc = inspect.getdoc(fn) or ""
    first = doc.split("\n\n")[0].strip().replace("\n", " ")
    return first[:400] or fn.__name__


def schema_for(name: str) -> dict:
    """One Ollama tool definition, derived from the function's own signature."""
    fn = registry.ALL[name]
    if name in SCHEMA_OVERRIDES:
        props = dict(SCHEMA_OVERRIDES[name])
        required = list(REQUIRED_OVERRIDES.get(name, []))
    else:
        props, required = {}, []
        for p in inspect.signature(fn).parameters.values():
            if p.name in INJECTED or p.kind is inspect.Parameter.VAR_KEYWORD:
                continue
            spec = _json_type(p.annotation)
            if p.name in PARAM_DESCRIPTIONS:
                spec["description"] = PARAM_DESCRIPTIONS[p.name]
            props[p.name] = spec
            if p.default is inspect.Parameter.empty:
                required.append(p.name)
    return {"type": "function", "function": {
        "name": name, "description": _describe(fn),
        "parameters": {"type": "object", "properties": props, "required": required}}}


def schemas(names) -> list[dict]:
    return [schema_for(n) for n in names]


def halted() -> bool:
    return os.path.exists(HALT)


def _changed_something(result) -> bool:
    """Did this write actually do anything?

    Nothing here is clever: a tool that records an action reports an action_id, and a tool
    that ingests a batch reports counts. A result with neither, or with all-zero counts, ran
    without effect.
    """
    if not isinstance(result, dict):
        return True
    if result.get("action_id") is not None:
        return True
    if any(k in result for k in ("created", "updated", "removed", "deleted", "would_have")):
        return True
    # ingest_batch returns its tallies at the top level -- {"entities": n, "facts": n, ...} --
    # rather than nested, so the counts have to be read rather than merely looked for. Testing
    # for the KEYS said "nothing happened" every time facts were actually recorded.
    tallies = [result[k] for k in ("entities", "facts", "edges", "links") if k in result]
    if tallies:
        return any(isinstance(v, int) and v > 0 for v in tallies)
    # An unrecognised shape counts as a real write: undercounting is the dangerous direction,
    # because it is the one that lets a run quietly exceed its cap.
    return True


def _canonical(args: dict) -> str:
    try:
        return json.dumps(args, sort_keys=True, default=str)
    except Exception:
        return repr(sorted(args.items()))


def run(conn, *, job: str, trigger: str, system: str, user: str,
        tool_names, tier: str = "fast", max_turns: int = MAX_TURNS,
        max_writes: int = MAX_WRITES, wall_seconds: int = WALL_SECONDS,
        run_id=None, evidence_id=None, dry_run: bool = False,
        allow: dict | None = None) -> dict:
    """Let a local model work with tools until one of six things stops it.

    Returns what happened rather than raising: `stop` says which of the six ended it, `calls`
    is every tool call with its result, and `writes` is how many of them changed something of
    Arun's. A run that finishes having done nothing is a normal and frequent outcome, not a
    failure -- treating it as one is how a reactor learns to act for the sake of acting.
    """
    allow = allow if allow is not None else registry.AUTONOMOUS
    unknown = [n for n in tool_names if n not in allow]
    if unknown:
        raise ValueError(f"{unknown} is not available to this tier; "
                         f"the tool set is built from {len(allow)} names and does not "
                         f"include it")

    tools = schemas(tool_names)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    started = time.time()
    calls: list[dict] = []
    seen: dict[str, dict] = {}
    writes = 0
    consecutive_errors = 0
    nudged = False
    text = ""
    stop = "done"

    for turn in range(max_turns):
        if halted():
            stop = "halt"
            break
        if time.time() - started > wall_seconds:
            stop = "wall"
            break
        try:
            response = ollama.chat(messages, tier=tier, tools=tools)
        except ollama.OllamaError as e:
            return {"stop": "error", "error": f"{type(e).__name__}: {e}", "turns": turn,
                    "writes": writes, "calls": calls, "text": text}

        message = response.get("message") or {}
        # A thinking block is not content and must never reach the caller's parsing.
        text = (message.get("content") or "").strip() or text
        requested = message.get("tool_calls") or []
        messages.append({"role": "assistant", "content": message.get("content") or "",
                         **({"tool_calls": requested} if requested else {})})
        if not requested:
            lower = text.lower()
            if (not nudged and writes == 0 and calls
                    and any(p in lower for p in INTENT)):
                nudged = True
                messages.append({"role": "user", "content": NUDGE})
                continue
            stop = "done"
            break

        for call in requested:
            fn = (call.get("function") or {})
            name = fn.get("name") or ""
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            args = args or {}

            if name not in allow or name not in tool_names:
                result = {"error": f"{name!r} is not a tool you have. Available: "
                                   f"{', '.join(tool_names)}"}
                consecutive_errors += 1
            else:
                key = f"{name}:{_canonical(args)}"
                if key in seen:
                    # The most valuable four lines here. A repeated create_reminder is a
                    # duplicate in Arun's list, not a wasted turn.
                    result = {**seen[key],
                              "note": "you already made this exact call; its result is "
                                      "repeated here. Do not call it again — say what you "
                                      "concluded and stop."}
                elif name in registry.WRITE and writes >= max_writes:
                    result = {"error": f"this run has already made {writes} writes, which is "
                                       f"its limit. Report what is left undone and stop."}
                    stop = "writes"
                elif name in registry.WRITE and halted():
                    result = {"error": "writing is halted (.state/HALT exists). Say what you "
                                       "would have done."}
                    stop = "halt"
                elif name in registry.WRITE and dry_run:
                    result = {"would_have": name, "args": args,
                              "note": "dry run — nothing was written"}
                    writes += 1
                    seen[key] = result
                else:
                    try:
                        extra = {}
                        sig = inspect.signature(allow[name])
                        if "run_id" in sig.parameters:
                            extra["run_id"] = run_id
                        if "evidence_id" in sig.parameters and evidence_id is not None:
                            extra["evidence_id"] = evidence_id
                        result = allow[name](conn, **args, **extra)
                        # A call that succeeded and changed nothing is not a write. It cost
                        # two of this run's four before this was checked: gemma4 sent an
                        # add_facts payload of the wrong shape, ingest_batch accepted it and
                        # recorded nothing, and the cap counted both attempts anyway -- so the
                        # run ended having written nothing and believing it was finished.
                        if name in registry.WRITE and _changed_something(result):
                            writes += 1
                        consecutive_errors = 0
                        seen[key] = result if isinstance(result, dict) else {"result": result}
                    except Exception as e:
                        # The tools raise good sentences. Handing one back is better steering
                        # than any instruction in the prompt.
                        result = {"error": f"{type(e).__name__}: {e}"}
                        consecutive_errors += 1

            calls.append({"turn": turn, "name": name, "args": args, "result": result})
            messages.append({"role": "tool", "content": json.dumps(result, default=str)[:4000]})

        if stop in ("writes", "halt"):
            break
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            stop = "error"
            break
    else:
        stop = "turns"

    return {"stop": stop, "turns": len(calls) and calls[-1]["turn"] + 1 or 0,
            "writes": writes, "calls": calls, "text": text,
            "seconds": round(time.time() - started, 1)}


def record(conn, run_id: int, result: dict) -> None:
    """Put a bounded summary of the run into run_log, the way runner.spend does for Claude."""
    summary = (result.get("text") or "")[:2000]
    # Failed calls carry their arguments. Names alone say a tool failed and not what it was
    # asked, which is the difference between "mail_read failed three times" and "it passed a
    # College message with the Work account". A dry-run week is read through this field.
    failures = [{"tool": c["name"], "args": c.get("args"),
                 "error": c["result"]["error"][:200]}
                for c in result.get("calls", [])
                if isinstance(c.get("result"), dict) and "error" in c["result"]]
    detail = json.dumps({
        "stop": result.get("stop"), "turns": result.get("turns"),
        "writes": result.get("writes"), "seconds": result.get("seconds"),
        "tools": [c["name"] for c in result.get("calls", [])],
        # A suppressed repeat carries the FIRST call's result back to the model, so it has no
        # error and looks exactly like a second write. Listing it as one made a dry run report
        # four writes where the loop had correctly performed two, which is the opposite of
        # what a dry-run log is for.
        "wrote": [{"tool": c["name"], "args": c.get("args")}
                  for c in result.get("calls", [])
                  if c["name"] in registry.WRITE
                  and isinstance(c.get("result"), dict)
                  and "error" not in c["result"]
                  and "already made this exact call" not in str(c["result"].get("note", ""))],
        "failures": failures[:5],
    }, default=str)[:4000]
    conn.execute("UPDATE run_log SET summary = ?, detail = ? WHERE id = ?",
                 (summary, detail, run_id))
    conn.commit()
