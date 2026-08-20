# Synth architecture notes

## TCC and the responsible process (load-bearing)

macOS attributes a TCC access request to the **responsible process** — the ancestor that
launched the requesting binary — not to the binary itself. Consequences, all verified on this
VM rather than assumed:

1. **PyObjC from a venv python cannot get EventKit access.** It returns `granted=False`,
   status `notDetermined`, with no dialog and no error. A binary carrying no Info.plist usage
   strings is denied silently. This is why the Apple layer is Swift, not PyObjC.
2. **A signed binary run from a shell still cannot get access.** Even inside `Synth.app` with
   correct usage strings, invoking `Synth.app/Contents/MacOS/synthkit` from a terminal yields
   `notDetermined`, because the responsible process is the shell's ancestor.
3. **Only LaunchServices (`open -a`) or launchd make it its own responsible process.** Then
   the grant is real and persists: `fullAccess` to both Calendar and Reminders.

Therefore the Apple layer runs as a **launchd-managed daemon** (`synthd`), and everything
else talks to it over a Unix domain socket at `.state/synthd.sock` (mode 0600). Callers need
no TCC standing of their own. The daemon also hosts the `EKEventStoreChanged` observer, so
change detection and access live in the one process that is allowed to have them.

```
  caller (python / claude -p / MCP server)
        │  JSON line over AF_UNIX, 0600
        ▼
  synthd  ← launchd (own responsible process, holds TCC grants)
        │
        ├── EventKit: calendars, reminders, events
        └── EKEventStoreChanged observer → .state/changes.jsonl
```

Rebuilding the binary changes its ad-hoc cdhash and re-prompts for permission. A self-signed
code-signing certificate would give a stable identity across rebuilds — still to do.

## Layout

- `applekit/synthkit.swift` — the Swift Apple layer; `build.sh` produces `bin/Synth.app`.
- `src/synth/applekit.py` — socket client. All Apple access from Python goes through `call()`.
- `launchd/` — agent plists, copied to `~/Library/LaunchAgents/`.
- `.state/` — socket, change queue, logs. Not in git.
