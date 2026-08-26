"""Running one Claude session, for the one thing that still needs a model.

All that survives of the reactor. Synth no longer watches mail, files reminders or writes
briefs, so nothing spends tokens on a schedule any more. Enrichment is the exception, and it
only ever runs because Arun asked for it -- from the CLI or through the connector.

The budget ceilings went with the reactor. They existed to stop an unattended loop from
making 574 refused calls across two silent days; there is no unattended loop left, and a
refusal now comes back in the result where the person who typed the command reads it.
"""
from __future__ import annotations

import json
import os
import subprocess

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
               denied: list[str] | None = None) -> dict:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # never bill the API; this must ride the subscription
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
        return json.loads(out)
    except ValueError:
        return {"is_error": proc.returncode != 0, "result": out[-4000:],
                "stderr": proc.stderr.decode("utf-8", errors="replace")[-2000:]}
