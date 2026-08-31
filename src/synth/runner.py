"""Running one Claude session, and paying for it out of a ledger.

All that survives of the reactor. Synth no longer decides on its own to act on Arun's behalf
-- there is no autonomous reasoning tier, no brief, and nothing that files a reminder because
it read an email. Two things here still cost money, both bounded and both his choice: the
Haiku pass that sorts subject lines for the mail index, and enrichment over new documents.

Everything else Synth does is free: daemon calls and SQLite.
"""
from __future__ import annotations

import json
import os
import subprocess

from synth import budget, db

ROOT = os.path.expanduser("~/Developer/synth")
CLAUDE = os.path.expanduser("~/.local/bin/claude")
PROMPTS = os.path.join(ROOT, "prompts")
SYNTH = os.path.join(ROOT, "bin", "synth")

# The tool layer is reached through `synth call` rather than MCP. Loading the server here
# would re-send a dozen schemas every turn, which is the cost this arrangement avoids. Every
# spelling is allowed: runs happen with cwd=ROOT, so the model reaches for the relative form
# and an absolute-only allowlist silently fails to match.
DIRECT_TOOLS = [
    "Bash(bin/synth:*)", "Bash(./bin/synth:*)", f"Bash({SYNTH}:*)",
]

# Triage judges subject lines it was handed. Giving it tools invites it to go reading bodies,
# which is the cost that tier exists to avoid -- and the built-ins have to be named to be
# gone, because an empty allowlist still leaves them offered.
NO_TOOLS: list[str] = []
DENY_ALL = ["Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep",
            "WebSearch", "WebFetch", "Task", "TodoWrite", "SlashCommand", "KillShell",
            "BashOutput"]

TRIAGE_MODEL = os.environ.get("SYNTH_TRIAGE_MODEL", "haiku")

USAGE_FIELDS = ("total_cost_usd", "num_turns", "duration_api_ms", "is_error",
                "stop_reason", "session_id", "subtype", "terminal_reason")
USAGE_TOKENS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens")


def prompt(name: str, core_only: bool = False, **fmt) -> str:
    """Assemble a prompt from the doctrine plus one task file."""
    doc = "doctrine-core.md" if core_only else "doctrine.md"
    with open(os.path.join(PROMPTS, doc)) as f:
        doctrine = f.read()
    with open(os.path.join(PROMPTS, f"{name}.md")) as f:
        body = f.read()
    for k, v in fmt.items():
        body = body.replace("{" + k + "}", v)
    return f"{doctrine}\n\n---\n\n{body}"


def run_claude(prompt_text: str, allowed: list[str], max_turns: int = 40,
               timeout: int = 1800, model: str | None = None,
               thinking: int | None = None, denied: list[str] | None = None) -> dict:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # never bill the API; this must ride the subscription
    if thinking is not None:
        # Triage produced 4,584 output tokens to return twelve one-line verdicts, and output
        # is the expensive half on Haiku. Sorting subject lines needs no deliberation.
        env["MAX_THINKING_TOKENS"] = str(thinking)
    cmd = [
        CLAUDE, "-p", prompt_text,
        "--output-format", "json",
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
    # wait and the 574 rejected calls that once filled two silent days.
    b = budget.note_failure(result)
    if b:
        result["backoff"] = b
    return result


def usage_record(result: dict, **extra) -> str:
    """A small, bounded summary of what a run cost.

    The whole result was once dumped and cut at 4000 characters, which produced invalid JSON
    whenever it ran long -- and the ledger silently read those runs as free. Only the fields
    the ledger needs are kept, and they cannot overflow.
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


def spend(conn, job: str, trigger: str, prompt_text: str, tools: list[str],
          model: str | None = None, max_turns: int = 40, thinking: int | None = None,
          denied: list[str] | None = None, timeout: int = 1800) -> dict:
    """One model call, budget-checked immediately before it happens and logged either way.

    The check is here rather than once per batch: asking permission once and then launching a
    run per chunk is how a single yes bought four runs.
    """
    ok, why = budget.allowed(conn, job)
    if not ok:
        budget.log_skip(conn, job, trigger, why)
        return {"skipped": True, "is_error": False, "result": f"skipped: {why}"}
    with db.run(conn, job, trigger=trigger) as run_id:
        result = run_claude(prompt_text, tools, model=model, max_turns=max_turns,
                            thinking=thinking, denied=denied, timeout=timeout)
        result["run_id"] = run_id
        conn.execute("UPDATE run_log SET summary = ?, detail = ? WHERE id = ?",
                     (str(result.get("result", ""))[:2000], usage_record(result), run_id))
        conn.commit()
    return result
