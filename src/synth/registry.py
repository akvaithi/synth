"""One registry, two callers.

The reactor runs on the VM and reaches these directly through `synth call`, which costs a
single Bash tool definition instead of twenty-odd MCP schemas re-sent every turn. The MCP
server wraps the same functions for the Claude app, where schemas are the right interface.
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

ALL = {**READ, **WRITE}
