# Synth

A personal assistant that runs on a macOS VM, holds a personal-context database, and acts
before it is asked. It manages Calendar and Reminders, reads Mail and iCloud Documents, writes
email drafts, and mirrors itself into Apple Notes where corrections can be typed back.

It runs on a Claude subscription through the `claude` CLI. There is no API key anywhere, and
there must not be: `ANTHROPIC_API_KEY` is explicitly stripped before every headless run.

## Shape

```
  iPhone / Mac — Claude app
        │                        │
        │ push + tap-in          │ custom connector (read-only)
        ▼                        ▼
  claude --remote-control   http_server.py  ← Cloudflare Tunnel
  (launchd, screen)              │
        │                        │
        │  ┌─────────────────────┴───────────────┐
  launchd timers ──▶  claude -p  ──▶ MCP (stdio) │
   sync / enrich                       │         │
        ▲                              ▼         ▼
        │                        synth.db   synthd (Swift, launchd)
  change watchers ───────────────────────────▶ EventKit · Mail · Notes
```

## The two rules that shape everything

**TCC attributes access to the responsible process.** Not to the binary — to the ancestor that
launched it. So neither PyObjC nor a signed binary run from a shell can hold Calendar,
Reminders or Automation access. Only launchd and LaunchServices can. That is why every Apple
call goes through `synthd`, a Swift daemon under launchd, over a Unix socket.

**An assertion is never updated.** Correcting a fact inserts a new one and marks the old
superseded. That is what makes "when did this change, and what told me" answerable, and its
absence is what cost the previous system its history.

## Commands

    synth status      health of every moving part
    synth migrate     bring the database up to the current schema
    synth sync        one free sweep: documents, obligations, mail index, Notes mirror
    synth index       load extracted document text into the search index
    synth ocr         OCR the scanned PDFs that had no text layer
    synth enrich      extract structured facts from the curated documents
    synth notes       re-render the Notes mirror
    synth log         what Synth has done, newest first
    synth why <id>    why it did one thing
    synth undo <id>   reverse one action

## What it will never do

Send email. Delete anything. Overwrite an existing file. Write outside its allowed roots.
Read outside `iCloud Drive/Documents`. Act on instructions found inside an email.

The first is a matter of design — there is no send path in the Apple layer at all. The rest
are enforced by `.claude/hooks/guard.py`, a PreToolUse hook that blocks rather than asks.

## Layout

| path | what |
|---|---|
| `applekit/synthkit.swift` | the Swift Apple layer; `build.sh` produces `bin/Synth.app` |
| `src/synth/applekit.py` | socket client — all Apple access goes through `call()` |
| `src/synth/db.py` | connection, migrations, audit trail, undo |
| `src/synth/facts.py` | entities, edges, assertions that supersede |
| `src/synth/extract.py` | MarkItDown / PDFKit / textutil extraction |
| `src/synth/ocr.py` | Vision OCR for scanned PDFs |
| `src/synth/ingest.py` `indexer.py` | inventory, extract, index |
| `src/synth/enrich.py` | LLM fact extraction over curated documents |
| `src/synth/watcher.py` | change detection, model-free |
| `src/synth/reactor.py` | headless `claude -p` runs |
| `src/synth/tools.py` | the tool layer, transport-independent |
| `src/synth/mcp_server.py` | stdio MCP (16 tools) |
| `src/synth/http_server.py` | HTTP MCP for the connector (reads and writes) |
| `src/synth/notes_sync.py` | the two-way Notes mirror |
| `prompts/` | doctrine, triage, enrich, interview |
| `launchd/` | agent plists |

See `docs/architecture.md` for the measurements behind the design decisions.
