"""LLM extraction of structured facts from ingested documents.

The mechanical scan answered "what exists and what does it say". This answers "what does it
mean", which is the expensive part, so it runs over a curated selection rather than over
every file.
"""
from __future__ import annotations


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

# Folders enrichment must never read, matched on the start of the path.
#
# Superseded/ is the one that did damage. A superseded resume is read with today's
# observed_at, so an old resume enters the database as the most recent evidence and supersedes
# the correction that replaced it -- facts about the math minor and a 3.918 GPA sit at
# confidence 1.0 alongside the corrections, sourced from files Arun had already retired.
#
# The Goldwater folder is other students' application material, kept as writing samples, with
# a README beside it saying not to redistribute. Nothing has leaked -- no entity was ever made
# for either author -- but nothing prevented the next pass from extracting their personal
# details as facts about people who never asked to be in this database.
EXCLUDE_PREFIXES = [
    "Career/Resume & Applications/Superseded/",
    "Archive/College Applications/",
    "Academics/Scholarships/Goldwater - Example Applications/",
]

# Paths that match a priority pattern but are not *about* Arun -- generic workshop decks,
# bookmarks, other people's application material kept as writing samples.
#
# "Goldwater Examples" was here and matched nothing: the folder is called "Goldwater - Example
# Applications". An exclusion that silently matches nothing is worse than none, because it
# reads as covered.
EXCLUDE_SUBSTRINGS = ["Workshop", "Career Center", ".url", "other students",
                      "Example Applications", "Superseded/", "Sample"]


def excluded(path: str) -> str | None:
    """Why this document is not enrichment's to read, or None if it is.

    Returns the reason rather than a bool so both the caller and the dry run can say WHICH
    rule kept a file out -- an exclusion nobody can see the effect of is one nobody notices
    has stopped working.
    """
    for prefix in EXCLUDE_PREFIXES:
        if path.lower().startswith(prefix.lower()):
            return f"under {prefix}"
    for frag in EXCLUDE_SUBSTRINGS:
        if frag.lower() in path.lower():
            return f"path contains {frag!r}"
    return None

# Reached through `synth call`, not MCP. These were mcp__synth__* names until the VM side
# stopped loading MCP servers on 2026-08-23 (--strict-mcp-config with no --mcp-config loads
# none at all). Enrichment asked for tools that no longer existed from that day until this
# one; it last ran successfully on 2026-08-21 and nothing noticed, because nothing ran it.
ENRICH_TOOLS = list(runner.DIRECT_TOOLS)


def select(conn, extra_limit: int = 0) -> list[dict]:
    """The curated set, without the exclusion report. See select_with_skipped."""
    return select_with_skipped(conn, extra_limit)[0]


def select_with_skipped(conn, extra_limit: int = 0) -> tuple[list[dict], list[dict]]:
    """The curated set and what was deliberately left out of it.

    The distilled context documents first, then degree and record material. The folder was
    called Archive/Consort until 2026-09-01, after the system that exported it; the documents
    are the same ones.

    The second return is every document an exclusion rule kept out, with the rule that did it,
    so `dry_run` can show it. An exclusion whose effect is invisible is one nobody notices has
    stopped matching -- which is exactly how "Goldwater Examples" sat here matching nothing.
    """
    done = {r[0] for r in conn.execute("SELECT document_id FROM enrichment")}
    seen, chosen, skipped = set(done), [], []
    for label, pattern in PRIORITY_PATTERNS:
        for r in conn.execute(
            "SELECT id, path, title, chars FROM document WHERE path LIKE ? "
            "ORDER BY chars DESC", (pattern,)
        ):
            if r["id"] in seen or r["chars"] < 200:
                continue
            why = excluded(r["path"])
            if why:
                seen.add(r["id"])
                skipped.append({"id": r["id"], "path": r["path"], "why": why})
                continue
            seen.add(r["id"])
            chosen.append({"id": r["id"], "path": r["path"], "chars": r["chars"],
                           "label": label})
    if extra_limit:
        # The exclusions were checked in the loop above and NOT here, which is the hole that
        # mattered: this branch orders by size, and the largest unenriched file in the index is
        # another student's 45,588-character Goldwater application. It would have been read
        # first. Anything excluded is skipped rather than counted against the limit.
        taken = 0
        for r in conn.execute(
            "SELECT id, path, title, chars FROM document WHERE id NOT IN "
            "(SELECT id FROM document WHERE " +
            " OR ".join("path LIKE ?" for _, __ in PRIORITY_PATTERNS) + ") "
            "ORDER BY chars DESC",
            tuple(p for _, p in PRIORITY_PATTERNS)
        ):
            if taken >= extra_limit:
                break
            if r["id"] in seen:
                continue
            why = excluded(r["path"])
            if why:
                skipped.append({"id": r["id"], "path": r["path"], "why": why})
                continue
            chosen.append({"id": r["id"], "path": r["path"], "chars": r["chars"],
                           "label": "other"})
            taken += 1
    return chosen, skipped


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
