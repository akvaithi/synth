"""Synth configuration. Deliberately plain data — no logic, easy to audit."""
import os as _os

# Mail accounts Synth may read, by Mail.app account name.
MAIL_ACCOUNTS = {
    "Work": "akvaithi.tech@gmail.com",
    "College": "akvaithi@tamu.edu",
    "Personal": "akvaithi@gmail.com",
    "iCloud": "akvaithi.msg@icloud.com",
}

# Reaction latency. Detection is free (no model calls); only a run that has something to
# chew on costs quota, so this can be short.
DEBOUNCE_SECONDS = 120

# Briefs, local time.
BRIEF_MORNING = (6, 50)
BRIEF_EVENING = (20, 20)

# The Notes folder that mirrors the context DB, and is read back for corrections.
NOTES_FOLDER = "Synth"

# Reads are confined to this subtree. The other iCloud Drive folders are media.
DOCUMENTS_ROOT = "~/Library/Mobile Documents/com~apple~CloudDocs/Documents"

# How many recent inbox messages to examine per account per sweep.
MAIL_SCAN_LIMIT = 25

# Reminder lists Synth may write to. Others are read-only to Synth.
MANAGED_LISTS = ["Personal", "Academics", "Career", "Research"]

# Calendars Synth may create events in. Others are read-only to Synth, the same way
# MANAGED_LISTS works for Reminders. Anything not named here is refused before EventKit
# is touched, so a mis-read invitation cannot land on a shared or subscribed calendar.
MANAGED_CALENDARS = ["Personal", "Semester Calendar", "College Events", "Meetings"]

# Where an event goes when the caller does not name a calendar.
DEFAULT_CALENDAR = "Personal"

# ---------------------------------------------------------------- document writes
#
# The one folder Synth may write files in, relative to DOCUMENTS_ROOT. Everything else under
# Documents is read-only, the same way MANAGED_LISTS works for Reminders.
WRITABLE_DOCUMENTS = "Archive/Consort/markdown"

# Files inside that folder that stay read-only. These are what enrich.py feeds to a model to
# extract facts from -- PRIORITY_PATTERNS[0] is exactly 'Archive/Consort/markdown/%' -- and
# they are what Synth is told about itself. A system that can edit its own instructions and
# then read them back as evidence is one that can talk itself into anything.
PROTECTED_DOCUMENTS = ["self.md", "corrections.md", "patterns.md", "CLAUDE.md", "CONTEXT.md"]

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
BUDGET_5H = _money("SYNTH_BUDGET_5H", 1.50)
# Held back from everything except enrichment, so a noisy inbox cannot spend the ceiling on
# five-cent triage batches before the 03:00 enrichment run and leave it nothing. Triage is
# chatty and cheap; enrichment is rare and is what actually builds the context.
BUDGET_5H_RESERVE = _money("SYNTH_BUDGET_5H_RESERVE", 0.50)
BUDGET_DAY_RESERVE = _money("SYNTH_BUDGET_DAY_RESERVE", 1.00)
BUDGET_MONTH_RESERVE = _money("SYNTH_BUDGET_MONTH_RESERVE", 5.00)
BUDGET_DAY = _money("SYNTH_BUDGET_DAY", 3.00)
BUDGET_MONTH = _money("SYNTH_BUDGET_MONTH", 30.00)

# Ordinary mail waits this long so a batch is judged in one run instead of one run per
# message. The log was full of "mail: 1 event(s)" runs costing $0.30 to read a newsletter;
# a batch of twelve costs barely more than a batch of one. Urgent mail bypasses this.
MAIL_BATCH_SECONDS = int(_os.environ.get("SYNTH_MAIL_BATCH", "1800"))

# A sender seen this many times without ever producing an action is demoted to digest:
# recorded in the mail index, never given a model run.
DEMOTE_AFTER_SIGHTINGS = int(_os.environ.get("SYNTH_DEMOTE_AFTER", "3"))
