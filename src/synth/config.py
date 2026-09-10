"""Synth configuration. Deliberately plain data — no logic, easy to audit."""
import os as _os

# Mail accounts Synth may read, by Mail.app account name.
MAIL_ACCOUNTS = {
    "Work": "akvaithi.tech@gmail.com",
    "College": "akvaithi@tamu.edu",
    "Personal": "akvaithi@gmail.com",
    "iCloud": "akvaithi.msg@icloud.com",
}

# Reaction latency. Both of these were sized when a reactor run cost about thirty cents on
# Sonnet, so batching mail for half an hour was how a burst of newsletters avoided paying for
# itself one message at a time. Background reasoning is local and free now, so the only thing
# a window still buys is collapsing a burst into one run -- which needs seconds, not minutes.
DEBOUNCE_SECONDS = int(_os.environ.get("SYNTH_DEBOUNCE", "15"))

# The Notes folder that mirrors the context DB, and is read back for corrections.
NOTES_FOLDER = "Synth"

# Notes folders Synth may never write into, whatever it is asked. The mirror folder is owned
# by notes_sync.render, which holds off re-rendering whenever a note carries an unread
# correction; a note written into it by hand either gets rendered over or blocks the mirror.
# Everything else is fair game -- Synth writes to any folder that exists and creates none,
# the same rule Reminders lists follow.
PROTECTED_NOTE_FOLDERS = [NOTES_FOLDER, "Recently Deleted"]

# Where a note goes when the caller does not name a folder.
DEFAULT_NOTE_FOLDER = "Notes"

# Reads are confined to this subtree. The other iCloud Drive folders are media.
DOCUMENTS_ROOT = "~/Library/Mobile Documents/com~apple~CloudDocs/Documents"

# How many recent inbox messages to examine per account per sweep.
MAIL_SCAN_LIMIT = 25

# Reminder lists Synth may write to: any list that already exists.
#
# This was an allowlist of four -- Personal, Academics, Career, Research -- and nothing
# domestic fitted in it. A grocery list, a chores list, anything household was refused before
# EventKit was reached. The allowlist is gone rather than widened, because what it guarded
# against (a mis-read email filing into the wrong list) is now guarded by "act only when
# asked". The half that is structural remains: synthd's findReminderCalendar throws for a name
# it cannot find and never creates a list, so Synth can write to every list Arun has and
# cannot invent one.

# Lists where an item is a line on a list, not an appointment. create_reminder's near-time
# refusal assumes a commitment with a place in the day -- two things a few hours apart sharing
# a distinctive word are usually one thing under two names. On a grocery list that assumption
# is exactly wrong: 29 untimed items filed in one go are 29 different things, and the refusal
# would fight every one of them. These lists get an exact-title check within the list instead.
LIST_STYLE_LISTS = ["Grocery List", "Shopping"]

# Most items one create_reminders call may file. A grocery run is twenty or thirty; past fifty
# something has gone wrong with the caller rather than with the shopping.
MAX_BATCH_REMINDERS = 50

# Calendars Synth may create events in. Anything not named here is refused before EventKit is
# touched, so a mis-read invitation cannot land on a shared or subscribed calendar. Unlike the
# reminder lists this stays an allowlist: there is no way to delete an event, so a wrongly
# created one has to be removed by Arun himself.
#
# Also read by agenda._collect. When the same event arrives twice -- once from the Google
# calendar, once as its own Zoom event -- the copy that survives deduplication is the one on a
# calendar named here, because that is the copy update_event could act on.
MANAGED_CALENDARS = ["Personal", "Semester Calendar", "College Events", "Meetings"]

# ---------------------------------------------------------------- the agenda
#
# An all-day event covering at least this many local days is reported once, in the agenda's
# `spanning` field, rather than inside every day it touches. "New Member Applications Open!"
# runs 26 August to 18 September and turned up in all seven responses of a week-long read; it
# is never the answer to "what is on Tuesday". Two, so that a single-day all-day event -- a
# holiday like Krishna Jayanti, which is a real constraint on that day -- still shows up in
# its day, where it reads as one.
SPANNING_ALL_DAY_DAYS = 2

# Longest span one agenda or free_slots call may cover. The daemon reads the whole range in a
# single EventKit predicate, so this bounds the response rather than the cost.
MAX_AGENDA_DAYS = 31

# Where an event goes when the caller does not name a calendar.
DEFAULT_CALENDAR = "Personal"

# ---------------------------------------------------------------- document writes
#
# The one folder Synth may write files in, relative to DOCUMENTS_ROOT. Everything else under
# Documents is read-only, the same way MANAGED_CALENDARS works for Calendar.
WRITABLE_DOCUMENTS = "Archive/Synth/markdown"

# Files inside that folder that stay read-only. These are what enrich.py feeds to a model to
# extract facts from -- PRIORITY_PATTERNS[0] is exactly 'Archive/Synth/markdown/%' -- and
# they are what Synth is told about itself. A system that can edit its own instructions and
# then read them back as evidence is one that can talk itself into anything.
# self.md, patterns.md and corrections.md were removed on 2026-09-01. All three were empty
# scaffolds announcing beliefs they did not hold: the first two were read-only to Synth so only
# a sweep could fill them and no sweep did, and corrections.md assumed hand-editing that never
# happens -- corrections arrive through Claude, add_facts and the Notes mirror. A file that
# claims to hold binding beliefs and holds nothing is worse than no file.
PROTECTED_DOCUMENTS = ["CLAUDE.md", "CONTEXT.md"]

# Extensions Synth may write. Only extract.PLAIN round-trips losslessly; a .docx or .pdf is
# extracted through MarkItDown or PDFKit and writing one back would destroy its formatting.
WRITABLE_EXTENSIONS = [".md", ".markdown", ".txt"]

# The largest file Synth will rewrite in one piece. Deliberately equal to extract.MAX_CHARS:
# above it, extract() truncates, so the indexed copy would be a partial view of a whole file
# and an anchored replace could not be checked against the text that is actually on disk.
MAX_DOCUMENT_BYTES = 200_000

# ---------------------------------------------------------------- cost control
#
# Ceilings on model spend, in the API-equivalent dollars the CLI reports per run. The true
# safety net is the backoff parsed out of a refusal message; these exist to stop Synth
# reaching it.
#
# Measured on this machine:
#   subject-line triage of a batch (Haiku, no tools) ~$0.05
#   one enrichment batch (60k chars of documents)    ~$0.30
# and both are demand-driven -- triage only when mail arrives, enrichment only when documents
# do. An ordinary day spends nothing at all.
#
# Every one is overridable by environment variable, so a bad guess is a restart, not an edit.
def _money(name: str, default: float) -> float:
    try:
        return float(_os.environ.get(name, default))
    except ValueError:
        return default

# Retuned 2026-08-30, after the autonomous tiers were removed. What spends now is the Haiku
# pass over subject lines the static rules could not settle (~$0.05 a batch, only when mail
# arrives) and enrichment over new documents (nightly, usually nothing to do). That is roughly
# $0.30-0.80 on an ordinary day against the $1.50-1.80 of the reactor era and the $8-14 before
# that. These ceilings are a circuit breaker, not a throttle: they should never be reached in
# normal use, and reaching one means something is wrong rather than busy.
# Sized on 2026-08-30, when the only metered job was five-cent triage batches and the brief
# did not exist. The brief came back on 2026-09-10 and the first researched one cost $2.99 in
# 46 turns -- twice the whole 5-hour ceiling on its own, which then refused everything else
# for the rest of the day to protect a reserve it had already spent.
#
# Two morning briefs and two evening ones is the realistic worst case, and the evening brief
# is much cheaper: no web tools, 18 turns instead of 45. Everything else Synth does now runs
# on local inference and costs nothing, so this ceiling is the brief's alone.
BUDGET_5H = _money("SYNTH_BUDGET_5H", 4.00)
# Held back from everything except enrichment, so a noisy inbox cannot spend the ceiling on
# five-cent triage batches before the 03:00 enrichment run and leave it nothing. Triage is
# chatty and cheap; enrichment is rare and is what actually builds the context.
BUDGET_5H_RESERVE = _money("SYNTH_BUDGET_5H_RESERVE", 1.50)
BUDGET_DAY_RESERVE = _money("SYNTH_BUDGET_DAY_RESERVE", 3.00)
BUDGET_MONTH_RESERVE = _money("SYNTH_BUDGET_MONTH_RESERVE", 5.00)
BUDGET_DAY = _money("SYNTH_BUDGET_DAY", 8.00)
BUDGET_MONTH = _money("SYNTH_BUDGET_MONTH", 60.00)

# Ordinary mail waits this long so a batch is judged in one run instead of one run per
# message. The log was full of "mail: 1 event(s)" runs costing $0.30 to read a newsletter;
# a batch of twelve costs barely more than a batch of one. Urgent mail bypasses this.
MAIL_BATCH_SECONDS = int(_os.environ.get("SYNTH_MAIL_BATCH", "120"))

# A sender seen this many times without ever producing an action is demoted to digest:
# recorded in the mail index, never given a model run.
DEMOTE_AFTER_SIGHTINGS = int(_os.environ.get("SYNTH_DEMOTE_AFTER", "3"))
