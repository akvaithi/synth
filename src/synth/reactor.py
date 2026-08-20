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

REACTOR_TOOLS = [
    "mcp__synth__search_context", "mcp__synth__get_entity", "mcp__synth__fact_history",
    "mcp__synth__list_obligations", "mcp__synth__read_document", "mcp__synth__today",
    "mcp__synth__activity", "mcp__synth__why", "mcp__synth__mail_recent",
    "mcp__synth__mail_read", "mcp__synth__mail_attachments",
    "mcp__synth__add_facts", "mcp__synth__create_reminder",
    "mcp__synth__complete_reminder", "mcp__synth__draft_email",
]
BRIEF_TOOLS = [t for t in REACTOR_TOOLS if not t.endswith(
    ("create_reminder", "complete_reminder", "draft_email", "add_facts"))]


def _prompt(name: str, **fmt) -> str:
    with open(os.path.join(PROMPTS, "doctrine.md")) as f:
        doctrine = f.read()
    with open(os.path.join(PROMPTS, f"{name}.md")) as f:
        body = f.read()
    for k, v in fmt.items():
        body = body.replace("{" + k + "}", v)
    return f"{doctrine}\n\n---\n\n{body}"


def run_claude(prompt: str, allowed: list[str], max_turns: int = 40,
               timeout: int = 1800) -> dict:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # never bill the API; this must ride the subscription
    cmd = [
        CLAUDE, "-p", prompt,
        "--output-format", "json",
        # Passing the MCP config explicitly avoids the interactive "pending approval"
        # state that a project-scoped .mcp.json sits in, which no headless run can clear.
        "--mcp-config", os.path.join(ROOT, ".mcp.json"),
        "--strict-mcp-config",
        "--allowed-tools", ",".join(allowed),
        "--permission-mode", "acceptEdits",
        "--max-turns", str(max_turns),
    ]
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


def react(conn, events: list[dict]) -> dict:
    with db.run(conn, "reactor", trigger=f"{len(events)} change event(s)") as run_id:
        prompt = _prompt("reactor", events=summarise_events(events))
        result = run_claude(prompt, REACTOR_TOOLS)
        conn.execute("UPDATE run_log SET summary = ?, detail = ? WHERE id = ?",
                     (str(result.get("result", ""))[:2000],
                      json.dumps({k: v for k, v in result.items() if k != "result"},
                                 default=str)[:4000], run_id))
        conn.commit()
    return result


def brief(conn, when: str = "morning") -> dict:
    with db.run(conn, "brief", trigger=when) as run_id:
        prompt = _prompt("brief", when=when)
        result = run_claude(prompt, BRIEF_TOOLS, max_turns=30)
        conn.execute("UPDATE run_log SET summary = ? WHERE id = ?",
                     (str(result.get("result", ""))[:4000], run_id))
        conn.commit()
    return result
