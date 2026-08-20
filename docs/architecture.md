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

## Mail: what is cheap and what is not

Measured on this install, not assumed:

| Operation | Cost |
|---|---|
| `mail_probe` (newest id + count) | ~2s per account — the steady-state sentinel |
| `mail_recent` (bulk headers, 25) | 15–48s per account — only when the sentinel moves |
| `mail_get_at` (body, index-addressed) | ~3.6s |
| body fetch, already cached | ~0.5s/msg |
| body fetch, cold from server | ~5s/msg |
| finding a message by `whose message id is …` | **minutes** — never do this |

Consequences baked into the code:

- **Address messages by (account, mailbox, index) and verify the Message-ID.** Searching a
  23,000-message inbox by id takes minutes; index addressing takes milliseconds. Indices shift
  as mail arrives, so the id is the correctness check and a mismatch is reported, not guessed.
- **Detection never touches `content`.** Reading a body can force a download from the server;
  an early version did this during detection and took over ten minutes per pass.
- **Apple Events need an explicit `with timeout of`.** The 2-minute default expires while Mail
  fetches uncached history, and the failure looks like an empty body rather than an error.
- **Bulk range access works; per-message loops do not.** `content of (messages a thru b of box)`
  returns real bodies, while `content of message i of box` returns empty instantly.
- **Attachment properties must each be guarded.** Mail raises -10000 on some parts rather than
  returning a value, and one bad attachment should not lose the message.

A full-archive warm is ~42,000 unique messages at ~5s/msg cold — roughly 60 hours. Not worth
it. Recent mail is already cached, including attachments, and that is what Synth reasons over.
Older messages are fetched on demand, which also caches them.

Known gap: Mail's AppleScript reports every attachment as `application/octet-stream`, so type
must be inferred from the filename extension. Text extraction from PDF and .docx is not built.
