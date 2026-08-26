"""One registry, two callers.

Enrichment runs on the VM and reaches these through `synth call`, which costs a single Bash
tool definition instead of a dozen MCP schemas re-sent every turn. The MCP server wraps the
same functions for the connector, where schemas are the right interface.
"""
from __future__ import annotations

from synth import tools

READ = {
    "search": tools.search_context,
    "entity": tools.get_entity,
    "history": tools.fact_history,
    "document": tools.read_document,
    "activity": tools.activity,
    "why": tools.why,
}

WRITE = {
    "add_facts": tools.add_facts,
    "update_document": tools.update_document,
    "append_document": tools.append_document,
    "create_document": tools.create_document,
    "reindex_documents": tools.reindex_documents,
    "enrich_documents": tools.enrich_documents,
    "undo": tools.undo,
}

ALL = {**READ, **WRITE}
