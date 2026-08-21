#!/usr/bin/env python3
"""PreToolUse guard for autonomous Synth runs.

The write policy is enforced here rather than asked for in a prompt, because a prompt is a
request and this is a rule. Blocks:

  * Edit / NotebookEdit anywhere        — Synth writes new files, it does not modify existing ones
  * rm / mv / rmdir / shred / trash     — Synth never deletes; removal is a human action
  * writes outside the allowed roots    — no scribbling across the disk
  * reads outside Documents in iCloud   — the other iCloud folders are media and out of scope
  * any attempt to send mail            — drafting is the maximum write that is ever acceptable

Exit 0 allows, exit 2 blocks with the message on stderr.
"""
import json
import os
import re
import sys

HOME = os.path.expanduser("~")
ICLOUD = f"{HOME}/Library/Mobile Documents/com~apple~CloudDocs"
ALLOWED_WRITE = [f"{HOME}/Developer/synth/.state", f"{HOME}/Developer/synth/exports"]
ALLOWED_READ_ICLOUD = f"{ICLOUD}/Documents"

DESTRUCTIVE = re.compile(
    r"(?:^|[;&|]\s*|\$\(|`)\s*(?:sudo\s+)?(rm|rmdir|shred|srm|trash|unlink)\b")
SEND_MAIL = re.compile(r"\bsend\b[^\n]{0,40}\b(message|mail)\b", re.I)
MV_OVER = re.compile(r"(?:^|[;&|]\s*)\s*(?:sudo\s+)?mv\b")


def block(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool = event.get("tool_name", "")
    args = event.get("tool_input", {}) or {}

    if tool in ("Edit", "NotebookEdit", "MultiEdit"):
        block("Synth does not modify existing files. Write a new file instead, "
              "or ask Arun to make the change himself.")

    if tool == "Write":
        path = os.path.abspath(os.path.expanduser(args.get("file_path", "")))
        if not any(path.startswith(root) for root in ALLOWED_WRITE):
            block(f"Write refused: {path} is outside the allowed roots "
                  f"({', '.join(ALLOWED_WRITE)}).")
        if os.path.exists(path):
            block(f"Write refused: {path} already exists. Synth creates new files, "
                  "it does not overwrite.")

    if tool == "Read":
        path = os.path.abspath(os.path.expanduser(args.get("file_path", "")))
        if path.startswith(ICLOUD) and not path.startswith(ALLOWED_READ_ICLOUD):
            block(f"Read refused: only {ALLOWED_READ_ICLOUD} is in scope. "
                  "The other iCloud Drive folders are media.")

    if tool == "Bash":
        cmd = args.get("command", "") or ""
        if DESTRUCTIVE.search(cmd):
            block("Synth never deletes. Removal is a human action.")
        if MV_OVER.search(cmd):
            block("Synth does not move or rename files; that can destroy data silently.")
        if SEND_MAIL.search(cmd) and "mail" in cmd.lower():
            block("There is no send path. Synth writes drafts only.")
        if "osascript" in cmd:
            block("Do not drive AppleScript directly — it attributes the request to the "
                  "wrong process and will fail. Use the synth MCP tools.")

    sys.exit(0)


if __name__ == "__main__":
    main()
