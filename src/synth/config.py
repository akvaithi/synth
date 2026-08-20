"""Synth configuration. Deliberately plain data — no logic, easy to audit."""

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
