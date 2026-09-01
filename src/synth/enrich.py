"""LLM extraction of structured facts from ingested documents.

The mechanical scan answered "what exists and what does it say". This answers "what does it
mean", which is the expensive part, so it runs over a curated selection rather than over
every file.
"""
from __future__ import annotations

import json
import os

from synth import db, runner

# Documents worth a model's attention, most authoritative first. Everything else is left to
# full-text search, which is enough for "what did I write about X".
PRIORITY_PATTERNS = [
    ("curated", "Archive/Synth/markdown/%"),
    ("degree", "%DEGREE%"),
    ("course-plan", "%COURSE-PLAN%"),
    ("record", "%RECORD%"),
    ("resume", "%RESUME%"),
    ("tracker", "%TRACKER%"),
    ("projects", "%PROJECTS%"),
    ("context", "%CONTEXT%"),
    ("transcript", "%ranscript%"),
    ("degree-eval", "%DEGREE-EVAL%"),
]

# Paths that match a priority pattern but are not *about* Arun -- generic workshop decks,
# bookmarks, other people's application material kept as writing samples.
EXCLUDE_SUBSTRINGS = ["Workshop", "Career Center", ".url", "other students",
                      "Goldwater Examples", "Sample"]

# Reached through `synth call`, not MCP. These were mcp__synth__* names until the VM side
# stopped loading MCP servers on 2026-08-23 (--strict-mcp-config with no --mcp-config loads
# none at all). Enrichment asked for tools that no longer existed from that day until this
# one; it last ran successfully on 2026-08-21 and nothing noticed, because nothing ran it.
ENRICH_TOOLS = list(runner.DIRECT_TOOLS)


def select(conn, extra_limit: int = 0) -> list[dict]:
    """The curated set: the distilled context documents first, then degree and record
    material. The folder was called Archive/Consort until 2026-09-01, after the system that
    exported it; the documents are the same ones."""
    done = {r[0] for r in conn.execute("SELECT document_id FROM enrichment")}
    seen, chosen = set(done), []
    for label, pattern in PRIORITY_PATTERNS:
        for r in conn.execute(
            "SELECT id, path, title, chars FROM document WHERE path LIKE ? "
            "ORDER BY chars DESC", (pattern,)
        ):
            if r["id"] in seen or r["chars"] < 200:
                continue
            if any(x.lower() in r["path"].lower() for x in EXCLUDE_SUBSTRINGS):
                continue
            seen.add(r["id"])
            chosen.append({"id": r["id"], "path": r["path"], "chars": r["chars"],
                           "label": label})
    if extra_limit:
        for r in conn.execute(
            "SELECT id, path, title, chars FROM document WHERE id NOT IN "
            "(SELECT id FROM document WHERE " +
            " OR ".join("path LIKE ?" for _, __ in PRIORITY_PATTERNS) + ") "
            "ORDER BY chars DESC LIMIT ?",
            tuple(p for _, p in PRIORITY_PATTERNS) + (extra_limit,)
        ):
            chosen.append({"id": r["id"], "path": r["path"], "chars": r["chars"],
                           "label": "other"})
    return chosen


def batches(docs: list[dict], budget_chars: int = 60000) -> list[list[dict]]:
    out, cur, size = [], [], 0
    for d in docs:
        if cur and size + d["chars"] > budget_chars:
            out.append(cur)
            cur, size = [], 0
        cur.append(d)
        size += d["chars"]
    if cur:
        out.append(cur)
    return out


def run(conn, docs: list[dict], dry_run: bool = False) -> list[dict]:
    results = []
    groups = batches(docs)
    for i, group in enumerate(groups, 1):
        listing = "\n".join(
            f"- document_id {d['id']}  ({d['chars']:,} chars)  {d['path']}" for d in group)
        print(f"[{i}/{len(groups)}] {len(group)} document(s), "
              f"{sum(d['chars'] for d in group):,} chars", flush=True)
        if dry_run:
            print(listing)
            continue
        with db.run(conn, "enrich", trigger=f"batch {i}/{len(groups)}") as run_id:
            prompt = runner.prompt("enrich", documents=listing)
            res = runner.run_claude(prompt, ENRICH_TOOLS, max_turns=60, timeout=2400)
            summary = str(res.get("result", ""))[:3000]
            conn.execute("UPDATE run_log SET summary = ? WHERE id = ?", (summary, run_id))
            if not res.get("is_error"):
                conn.executemany(
                    "INSERT OR IGNORE INTO enrichment (document_id, run_id) VALUES (?,?)",
                    [(d["id"], run_id) for d in group])
            conn.commit()
        print("   ", summary[:400].replace("\n", " "), flush=True)
        results.append({"batch": i, "docs": len(group), "summary": summary,
                        "error": res.get("is_error")})
    return results
