"""Runs a Claude session over pending changes.

Detection is model-free and cheap; this is the only expensive part, so it runs on a debounce
and only when there is genuinely something to consider. Each run is recorded in run_log
whether or not it changed anything.
"""
from __future__ import annotations

import json
import os
import subprocess

from synth import db

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

REACTOR_MODEL = os.environ.get("SYNTH_REACTOR_MODEL", "sonnet")
BRIEF_MODEL = os.environ.get("SYNTH_BRIEF_MODEL", "sonnet")

_LEGACY_MCP_TOOLS = [
    "mcp__synth__search_context", "mcp__synth__get_entity", "mcp__synth__fact_history",
    "mcp__synth__list_obligations", "mcp__synth__read_document", "mcp__synth__today",
    "mcp__synth__activity", "mcp__synth__why", "mcp__synth__mail_recent",
    "mcp__synth__mail_read", "mcp__synth__mail_attachments",
    "mcp__synth__mail_links", "mcp__synth__read_note", "mcp__synth__accept_correction",
    "mcp__synth__add_facts", "mcp__synth__create_reminder",
    "mcp__synth__complete_reminder", "mcp__synth__update_reminder",
    "mcp__synth__update_obligation", "mcp__synth__draft_email",
    # Arun asked for certainty over speed, and four recorded deadlines are explicitly
    # unverified. Without these, "verify against the source" is not a thing Synth can do.
    "WebSearch", "WebFetch",
]
_LEGACY_BRIEF_TOOLS = [t for t in _LEGACY_MCP_TOOLS if not t.endswith(
    ("create_reminder", "complete_reminder", "update_reminder", "update_obligation",
     "draft_email", "add_facts", "accept_correction"))]


def _prompt(name: str, **fmt) -> str:
    with open(os.path.join(PROMPTS, "doctrine.md")) as f:
        doctrine = f.read()
    with open(os.path.join(PROMPTS, f"{name}.md")) as f:
        body = f.read()
    for k, v in fmt.items():
        body = body.replace("{" + k + "}", v)
    return f"{doctrine}\n\n---\n\n{body}"


def run_claude(prompt: str, allowed: list[str], max_turns: int = 40,
               timeout: int = 1800, model: str | None = None) -> dict:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # never bill the API; this must ride the subscription
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
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, timeout=timeout)
    out = proc.stdout.decode("utf-8", errors="replace")
    try:
        return json.loads(out)
    except ValueError:
        return {"is_error": proc.returncode != 0, "result": out[-4000:],
                "stderr": proc.stderr.decode("utf-8", errors="replace")[-2000:]}


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
    "mail": {"mail_new", "mail_error"},
    "schedule": {"eventkit_changed"},
    "notes": {"note_edited", "notes_error"},
    "files": {"documents_changed", "mail_changed", "notes_changed"},
}


def chunk(events: list[dict]) -> list[tuple[str, list[dict]]]:
    """Split a batch by kind.

    One long thread carrying mail, calendar changes and note edits at once burns context on
    material irrelevant to each decision, and a run that exhausts its budget half way through
    leaves the work silently incomplete -- which is how a reply that had already been sent
    got a reminder telling Arun to send it.
    """
    out = []
    for name, kinds in GROUPS.items():
        group = [e for e in events if e.get("kind") in kinds]
        if group:
            out.append((name, group))
    known = {k for ks in GROUPS.values() for k in ks}
    rest = [e for e in events if e.get("kind") not in known]
    if rest:
        out.append(("other", rest))
    return out


def react(conn, events: list[dict]) -> dict:
    """Run one focused pass per kind of change."""
    results = []
    for name, group in chunk(events):
        with db.run(conn, "reactor", trigger=f"{name}: {len(group)} event(s)") as run_id:
            prompt = _prompt("reactor", events=summarise_events(group))
            result = run_claude(prompt, DIRECT_TOOLS, model=REACTOR_MODEL)
            conn.execute("UPDATE run_log SET summary = ?, detail = ? WHERE id = ?",
                         (str(result.get("result", ""))[:2000],
                          json.dumps({k: v for k, v in result.items() if k != "result"},
                                     default=str)[:4000], run_id))
            conn.commit()
            results.append({"group": name, "events": len(group),
                            "error": result.get("is_error"),
                            "result": str(result.get("result", ""))})
    if not results:
        return {"result": "nothing to react to", "is_error": False}
    return {"result": "\n\n".join(f"## {r['group']}\n{r['result']}" for r in results),
            "is_error": any(r["error"] for r in results), "groups": results}


def brief(conn, when: str = "morning") -> dict:
    with db.run(conn, "brief", trigger=when) as run_id:
        prompt = _prompt("brief", when=when)
        result = run_claude(prompt, DIRECT_READ_TOOLS, max_turns=30, model=BRIEF_MODEL)
        conn.execute("UPDATE run_log SET summary = ? WHERE id = ?",
                     (str(result.get("result", ""))[:4000], run_id))
        conn.commit()
        if not result.get("is_error"):
            from synth import notes_sync
            try:
                notes_sync.render(conn, "brief", run_id=run_id)
            except Exception as e:  # a delivery failure must not lose the brief
                conn.execute("UPDATE run_log SET detail = ? WHERE id = ?",
                             (f"brief written but Notes delivery failed: {e}", run_id))
                conn.commit()
    return result
