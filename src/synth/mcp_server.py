"""Synth MCP server.

The whole of Synth that Arun still talks to: his personal context, the documents indexed out
of iCloud Drive, and the tools that edit the markdown masters. Everything the mail, calendar,
reminder and brief machinery used to expose is gone.

Read tools and write tools are registered separately so the public transport can serve reads
alone — a leaked endpoint should be able to embarrass, not to act.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from synth import db, tools  # noqa: E402


def _j(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


def _with_conn(fn):
    conn = db.connect()
    try:
        return fn(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------- read tools


def search_context(query: str, limit: int = 20) -> str:
    """Search everything Synth knows — entities, facts, extracted documents and saved links.
    Start here when answering any question about Arun."""
    return _j(_with_conn(lambda c: tools.search_context(c, query, limit)))


def get_entity(name: str) -> str:
    """Everything known about one program, person, course, application, project or award:
    current facts with provenance, related entities, obligations and links."""
    return _j(_with_conn(lambda c: tools.get_entity(c, name)))


def fact_history(name: str, predicate: str) -> str:
    """Every value a fact has held over time, newest first, with what told us and when.
    Use when a date or requirement may have changed."""
    return _j(_with_conn(lambda c: tools.fact_history(c, name, predicate)))


def read_document(doc_id: int = 0, path: str = "", max_chars: int = 20000) -> str:
    """Full extracted text of one ingested file, by id or by path relative to Documents.

    When `truncated` comes back true you have not seen the whole file — you may still edit a
    passage you did see, but you do not know what is at the end of it."""
    return _j(_with_conn(lambda c: tools.read_document(c, doc_id or None,
                                                       path or None, max_chars)))


def activity(limit: int = 50) -> str:
    """What Synth has done recently, newest first, with the reason for each action."""
    return _j(_with_conn(lambda c: tools.activity(c, limit)))


def why(action_id: int) -> str:
    """Why Synth took one specific action: its reason, its evidence, and the prior state."""
    return _j(_with_conn(lambda c: tools.why(c, action_id)))


def add_facts(payload: dict, source_ref: str = "interview", document_id: int = 0) -> str:
    """Record facts in the context database.

    payload: {"entities":[{"kind","name","description","status",
                           "facts":[{"predicate","text"|"num"|"date","confidence"}]}],
              "edges":[{"src","src_kind","dst","dst_kind","relation"}],
              "links":[{"url","title","kind","entity"}]}

    kind is one of person, org, program, course, application, project, award, topic.
    Facts are never overwritten — a changed value supersedes the old one and the history is
    kept. Set confidence below 1.0 for anything inferred rather than stated outright.

    When extracting from a document, ALWAYS pass document_id — the id you were given for
    that document. It links every fact back to the file that stated it, which is what makes
    provenance answerable later. One add_facts call per document."""
    return _j(_with_conn(lambda c: tools.add_facts(
        c, payload, source_ref=source_ref, document_id=document_id or None)))


def update_document(path: str, old: str, new: str, reason: str) -> str:
    """Replace one exact passage in one of Arun's markdown files. This writes the FILE, on
    disk, in iCloud — it syncs to his phone. It is a real edit, not a database note.

    Only Archive/Consort/markdown/ is writable. Inside it, self.md, corrections.md,
    patterns.md, CLAUDE.md and CONTEXT.md are read-only.

    `old` is the exact text to replace and MUST appear exactly once. Copy it verbatim from
    read_document, including line breaks and punctuation. Zero matches and two matches are
    both refused and nothing is written — extend `old` with the line above it until it is
    unique. `new` replaces it; pass "" to remove the passage, and only when Arun asked for
    that in those words.

    There is deliberately no way to replace a whole document. read_document truncates, and a
    tool that accepted a whole file would let a partial read silently destroy the rest.

    `reason` is required and recorded. The previous version of the file is kept; undo with
    the returned action_id puts it back."""
    return _j(_with_conn(lambda c: tools.update_document(
        c, path=path, old=old, new=new, reason=reason)))


def append_document(path: str, text: str, reason: str) -> str:
    """Add text to the end of one of Arun's markdown files, after a blank line. Nothing
    already in the file is touched, so this is the safe way to add an entry, a row or a new
    section. Same writable folder and same read-only files as update_document.

    `reason` is required and recorded; undo removes the addition."""
    return _j(_with_conn(lambda c: tools.append_document(
        c, path=path, text=text, reason=reason)))


def create_document(path: str, text: str, reason: str) -> str:
    """Create a NEW file under Archive/Consort/markdown/. Refused if the path already exists
    — use update_document to change a passage, or append_document to add to the end.

    `path` is relative to Documents and must end in .md, .markdown or .txt. The file is
    indexed immediately, so search_context finds it in the same conversation, and it syncs
    to his phone.

    A file Synth created cannot be undone away — Synth never deletes. undo will say so and
    name the file for Arun to remove himself. Be correspondingly deliberate."""
    return _j(_with_conn(lambda c: tools.create_document(
        c, path=path, text=text, reason=reason)))


def reindex_documents(path: str = "") -> str:
    """Pick up documents changed outside Synth, or added to Documents since the last pass.

    Costs nothing — extraction is Python and the index is SQLite, with no model anywhere in
    it. Edits made through update_document, append_document and create_document already
    refresh the index as they go, so this is for files changed on his phone, in an editor, or
    dropped into the folder.

    Pass `path` to do one file, relative to Documents. Omit it to sweep everything, which
    takes a couple of seconds over roughly 1,700 documents."""
    return _j(_with_conn(lambda c: tools.reindex_documents(c, path=path)))


def enrich_documents(extra_limit: int = 0, dry_run: bool = False) -> str:
    """Read documents with a model and record the facts they state into the context database.

    The ONLY tool here that spends tokens, and the only reason a model still runs on Arun's
    machine. Nothing triggers it but him — there is no schedule and no watcher behind it.

    Each document is enriched once and `enrichment` remembers which, so repeated calls only
    pick up what is new. Start with dry_run=true to see what it would read before committing
    to the run; a batch is up to 60,000 characters and can take several minutes.

    Facts land with the document id that stated them, so provenance stays answerable."""
    return _j(_with_conn(lambda c: tools.enrich_documents(
        c, extra_limit=extra_limit, dry_run=dry_run)))


def undo(action_id: int) -> str:
    """Reverse one logged action by its id, restoring the prior state."""
    return _j(_with_conn(lambda c: tools.undo(c, action_id)))


READ_TOOLS = [search_context, get_entity, fact_history, read_document, activity, why]
WRITE_TOOLS = [add_facts, update_document, append_document, create_document,
               reindex_documents, enrich_documents, undo]


DOCTRINE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "prompts", "doctrine.md")


def _instructions(writable: bool) -> str:
    """Ship the operating doctrine with the server.

    Enrichment gets these rules in its prompt; a client reaching in over MCP would not, and
    the rules about how his documents may be written are exactly the ones a remote caller
    most needs. The tool-calling section is stripped -- an MCP client calls tools directly
    rather than through `synth call`.
    """
    try:
        with open(DOCTRINE_PATH, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return "Synth: personal assistant over Arun Vaithianathan's context database."
    out, skipping = [], False
    for line in text.splitlines():
        if line.startswith("## How you call things"):
            skipping = True
            continue
        if skipping and line.startswith("## "):
            skipping = False
        if not skipping:
            out.append(line)
    body = "\n".join(out)
    if not writable:
        body += ("\n\n## This connection is read-only\n\n"
                 "Only the read tools are available here. Do not promise to create, change "
                 "or draft anything.")
    return body


def build(name: str = "synth", writable: bool = True) -> MCPServer:
    server = MCPServer(name, instructions=_instructions(writable))
    for fn in READ_TOOLS + (WRITE_TOOLS if writable else []):
        server.add_tool(fn)
    return server


if __name__ == "__main__":
    build().run("stdio")
