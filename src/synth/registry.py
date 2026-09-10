"""One registry, three tiers.

The same functions are reached two ways: `synth call <name>` from a shell, and the MCP server,
which wraps them with schemas for the Claude app.

The tiers are what the split is for. READ and WRITE are what Synth may do on its own
initiative. DELETE is not -- removing something of Arun's happens because he asked for it, in
a session he is driving. The autonomous tier is assembled from AUTONOMOUS, which simply does
not contain the delete names, so a model running unattended cannot call one by guessing it
exists.
"""
from __future__ import annotations

from synth import tools


def _invites():
    from synth import invites
    return invites

READ = {
    "search": tools.search_context,
    "entity": tools.get_entity,
    "history": tools.fact_history,
    "obligations": tools.list_obligations,
    "document": tools.read_document,
    "today": tools.today,
    "activity": tools.activity,
    "why": tools.why,
    "sync_obligations": tools.sync_obligations,
    "agenda": tools.agenda,
    "already_scheduled": tools.already_scheduled,
    "conflicts": tools.conflicts,
    "free_slot": tools.find_free_slot,
    "free_slots": tools.free_slots,
    "mail": tools.mail_recent,
    "mail_read": tools.mail_read,
    "mail_links": tools.mail_links,
    "mail_attachments": tools.mail_attachments,
    # Both of these existed in tools.py and in the MCP server but never here, so anything
    # reaching the tool layer through this registry could see that a message had a PDF and
    # could not open it -- which is how an invitation or an offer letter gets summarised
    # from its subject line instead of read.
    "read_attachment": tools.read_attachment,
    "mail_digest": tools.mail_digest,
    "read_note": tools.read_note,
    "list_notes": tools.list_notes,
    "read_invitation": _invites().read_invitation,
}

WRITE = {
    "add_facts": tools.add_facts,
    "create_reminder": tools.create_reminder,
    "create_reminders": tools.create_reminders,
    "update_reminder": tools.update_reminder,
    "complete_reminder": tools.complete_reminder,
    "create_event": tools.create_event,
    "update_event": tools.update_event,
    "update_obligation": tools.update_obligation,
    "draft_email": tools.draft_email,
    "accept_correction": tools.accept_correction,
    "create_note": tools.create_note,
    "append_note": tools.append_note,
    "retract_reminder": tools.retract_reminder,
    "update_document": tools.update_document,
    "append_document": tools.append_document,
    "create_document": tools.create_document,
    "reindex_documents": tools.reindex_documents,
    "enrich_documents": tools.enrich_documents,
    "undo": tools.undo,
}

# Removing something of Arun's. Deliberately a third set rather than more entries in WRITE:
# the split is the whole point. Arun prompting Synth directly reaches all three; the
# autonomous tier is built from READ and WRITE and can never name one of these, because the
# name is not in the table it is built from.
#
# retract_reminder stays in WRITE. It is provenance-gated -- action_log must show Synth
# created that exact reminder -- so it is Synth cleaning up after itself rather than removing
# anything of his, which is a different act and belongs on the other side of the line.
DELETE = {
    "delete_reminder": tools.delete_reminder,
    "delete_event": tools.delete_event,
    "delete_note": tools.delete_note,
    "delete_document": tools.delete_document,
}

# What the autonomous tier may ever see. Anything absent here cannot be called by a model
# Synth is running on its own initiative, whatever it asks for.
AUTONOMOUS = {**READ, **WRITE}

ALL = {**READ, **WRITE, **DELETE}
