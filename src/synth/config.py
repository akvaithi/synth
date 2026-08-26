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
# Ceilings on model spend, in the API-equivalent dollars the CLI reports per run. These are
# calibration starting points, not physics: four days of running reported ~$30 and twice hit
# a real limit, so these are set roughly 5x below the observed burn and are meant to be tuned
# against `synth budget` once a week of real data exists. The backoff parsed from the refusal
# message is the true safety net; these ceilings exist to stop Synth reaching it.
#
# Measured on this machine, 2026-08-25, after the rebuild:
#   morning brief (Sonnet, researching, web tools)   ~$0.96
#   evening brief (Haiku, recap, no web tools)       ~$0.12
#   subject-line triage of a batch (Haiku, no tools) ~$0.05
#   one acting run over a batch (Sonnet, all tools)  ~$0.30
# which is roughly $1.50-1.80 on an ordinary day against the $8-14 the old design burned.
# The reserve is a whole morning brief, so the run Arun actually reads can never be the one
# that finds the window empty.
#
# Every one is overridable by environment variable, so a bad guess is a restart, not an edit.
def _money(name: str, default: float) -> float:
    try:
        return float(_os.environ.get(name, default))
    except ValueError:
        return default

# Retuned 2026-08-25 against the first real week, which is what these were always waiting
# for. At the old ceilings the arithmetic left no room: both briefs cost about $1.08 and the
# reactor's share after the day reserve was $1.10, against a $2.20 day ceiling -- roughly two
# cents of slack. On 2026-08-25 that produced 1,355 skipped runs out of 1,372, the reactor
# spent its whole day's share in four runs before noon, and the day ceiling then refused the
# evening brief at 20:20 ("last 24h spend $2.35 has reached $2.20"). The reserve protects the
# briefs from the reactor but not from the total, and the evening brief, being last, is
# always what the total squeezes out.
#
# The rolling 5-hour window is the limit that actually bites day to day. At $1.40 with $1.00
# reserved the reactor got $0.40 -- one run per five hours.
BUDGET_5H = _money("SYNTH_BUDGET_5H", 2.00)
# Held back from everything except the brief, so the 06:50 brief is never the run that
# discovers the window is empty. The brief is what Arun reads; the reactor is the expense.
# Each reserve is sized to the briefs that window still owes him -- one in five hours, both
# in a day -- rather than derived from a multiple, which had the day reserve swallowing all
# but twenty cents of the daily budget.
BUDGET_5H_RESERVE = _money("SYNTH_BUDGET_5H_RESERVE", 1.00)
BUDGET_DAY_RESERVE = _money("SYNTH_BUDGET_DAY_RESERVE", 1.10)
BUDGET_MONTH_RESERVE = _money("SYNTH_BUDGET_MONTH_RESERVE", 8.00)
BUDGET_DAY = _money("SYNTH_BUDGET_DAY", 4.00)
BUDGET_MONTH = _money("SYNTH_BUDGET_MONTH", 55.00)

# Ordinary mail waits this long so a batch is judged in one run instead of one run per
# message. The log was full of "mail: 1 event(s)" runs costing $0.30 to read a newsletter;
# a batch of twelve costs barely more than a batch of one. Urgent mail bypasses this.
MAIL_BATCH_SECONDS = int(_os.environ.get("SYNTH_MAIL_BATCH", "1800"))

# A sender seen this many times without ever producing an action is demoted to digest:
# named in the brief, never given a model run.
DEMOTE_AFTER_SIGHTINGS = int(_os.environ.get("SYNTH_DEMOTE_AFTER", "3"))
