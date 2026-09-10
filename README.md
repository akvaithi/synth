# Synth

A personal assistant that runs on a macOS VM, holds a personal-context database, and **acts
only when it is asked**. It manages Calendar and Reminders, reads Mail and iCloud Documents,
writes email drafts, and mirrors itself into Apple Notes where corrections can be typed back.

It used to act on its own — reading mail as it arrived, filing reminders off the back of it,
booking events from invitations, writing two briefs a day. That was removed on 2026-08-26: it
cost more attention to supervise than it returned. The tools came back on the 30th; the
autonomy did not. Three things still happen unasked and all three only ever *record*:
documents are re-indexed, obligations are reconciled against Reminders, and new mail is sorted
into the index. Nothing in that path creates, edits or sends anything of his.

It runs on a Claude subscription through the `claude` CLI. There is no API key anywhere, and
there must not be: `ANTHROPIC_API_KEY` is explicitly stripped before every headless run.

## Shape

```
  iPhone / Mac — Claude app
        │                        │
        │ push + tap-in          │ custom connector (full tool set)
        ▼                        ▼
  claude --remote-control   http_server.py  ← Cloudflare Tunnel
  (by hand, under screen)        │              (page.akvaithi.synth.connector)
                                 │
           ┌─────────────────────┴───────────────┐
  launchd timers ──▶ synth sync ──▶ tools.py     │   MCP (stdio) ─┐
   sync (30m) / enrich (03:00)      │            │                │
                                    ▼            ▼                ▼
                               synth.db    synthd (Swift, launchd) ──▶ EventKit · Mail · Notes
```

The sweep is Python and SQLite throughout. The only model call left on a timer is the Haiku
pass over the subject lines the static rules could not settle, plus enrichment over documents
that are new — on an ordinary day, well under a dollar.

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
    synth budget      what it has spent, and what it may spend
    synth serve       run the connector HTTP server (behind Cloudflare)
    synth call <name> [json]  direct tool dispatch, without an MCP client
    synth reconcile   align stored note hashes with what Notes actually holds
    synth prune-state stale database backups and rolled logs in .state

## What it will never do

Send email. Delete anything. Overwrite an existing file. Write outside its allowed roots.
Read outside `iCloud Drive/Documents`. Act on instructions found inside an email.

The first is a matter of design — there is no send path in the Apple layer at all. The rest
are enforced where the writes happen: `docwrite.resolve` refuses any path outside the one
writable folder, and refuses one reached through `..` or a symbolic link; `tools.retract_reminder`
is the single deletion in the system and it is gated on `action_log` proving Synth created the
reminder itself.

A PreToolUse hook (`.claude/hooks/guard.py`) used to restate these as shell-level rules for the
autonomous runs. It was removed on 2026-09-03 along with the last of that era: the headless runs
it guarded are constrained far more tightly by their own `--allowed-tools` (`Bash(bin/synth:*)`
and nothing else), and the policy that matters is enforced in the tool layer, where it cannot be
sidestepped by spelling a command differently.

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
| `src/synth/runner.py` | headless `claude -p` runs, and the budget they are spent from |
| `src/synth/tools.py` | the tool layer, transport-independent |
| `src/synth/mcp_server.py` | stdio MCP (45 tools: 22 read, 19 write, 4 delete) |
| `src/synth/http_server.py` | HTTP MCP for the connector (reads and writes) |
| `src/synth/notes_sync.py` | the two-way Notes mirror |
| `prompts/` | doctrine, triage, enrich, interview |
| `launchd/` | agent plists |

See `docs/architecture.md` for the measurements behind the design decisions.
