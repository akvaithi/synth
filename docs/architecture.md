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

## Notes as the readable mirror (bidirectional)

The mirror lives in an Apple Notes folder called `Synth`, not in iCloud Drive: it syncs
everywhere, renders on the phone, and can be edited in place.

Notes is driven through the daemon, because Apple Events carry the same responsible-process
rule as EventKit. Only *named* Notes operations are exposed on the socket — there is
deliberately no arbitrary-AppleScript passthrough, so the write policy stays mechanical.

Two quirks that shape the renderer:

- Notes treats the **first line of the body as the title**. Emitting an `<h1>` as well
  duplicates it. The renderer writes the title as the first line and sets `name` to match.
- Bodies round-trip as Notes' own HTML, not as what you wrote. So edits are compared on a
  **whitespace-normalised text hash**, and a mismatch is treated as a correction *signal* for
  the model to interpret — never as a literal diff to replay.

`notes_mirror` holds, per document, the note id, the hash Synth last wrote, and the hash last
observed. `last_written_hash != last_seen_hash` means you edited it.

## Verified end to end

- Calendar and Reminders: `fullAccess`, 12 calendars and 6 reminder lists.
- Writes: create, partial update (unnamed fields untouched), complete, uncomplete, create
  event — each returning prior state.
- `action_log` + `db.undo()`: a real update reversed and the prior value restored.
- A write with no stated reason is refused by `actions.UnexplainedWrite`.
