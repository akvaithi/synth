"""Getting a brief onto Arun's phone.

Briefs run as headless `claude -p` processes. Those have no Remote Control connection, so
they can write to the database and the Notes mirror but cannot push — which is why the first
briefs landed nowhere Arun would see them.

The Remote Control session is the only process holding a push channel. It runs under launchd
inside a detached `screen`, so the way to reach it is to type into it: inject a short prompt
telling it the brief is ready. It reads the brief itself and pushes, which keeps the message
short and avoids trying to shell-quote a page of markdown.
"""
from __future__ import annotations

import subprocess
import time

SCREEN = "/usr/bin/screen"
SESSION = "synth"


def session_running() -> bool:
    r = subprocess.run([SCREEN, "-ls"], capture_output=True, text=True)
    return f".{SESSION}\t" in r.stdout or f".{SESSION} " in r.stdout


def send(prompt: str, timeout: int = 20) -> dict:
    """Type a prompt into the Remote Control session and submit it."""
    if not session_running():
        return {"delivered": False, "reason": "no Remote Control session is running"}
    # One line only: stuff sends raw keystrokes, and a newline mid-prompt would submit half
    # a message.
    single_line = " ".join(prompt.split())
    r = subprocess.run(
        [SCREEN, "-S", SESSION, "-p", "0", "-X", "stuff", single_line],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return {"delivered": False, "reason": r.stderr.strip()[:200] or "screen stuff failed"}

    # Submit as a separate keystroke. Appending \r to the same stuff call leaves the text
    # sitting unsent in the prompt box -- the TUI needs the return on its own.
    time.sleep(1.0)
    r = subprocess.run(
        [SCREEN, "-S", SESSION, "-p", "0", "-X", "stuff", "\r"],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return {"delivered": False, "reason": "text typed but submit failed"}
    return {"delivered": True, "chars": len(single_line)}


BRIEF_PROMPT = (
    "Your {when} brief has finished running. Retrieve it with "
    "`bin/synth call latest_brief \'{{}}\'` and relay it to me in full, exactly as written, "
    "with no summarising and nothing added. Then send me a push notification saying the "
    "{when} brief is ready and naming the single most time-sensitive item in it."
)


def deliver_brief(when: str) -> dict:
    return send(BRIEF_PROMPT.format(when=when))
