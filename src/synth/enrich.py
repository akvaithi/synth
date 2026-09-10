"""LLM extraction of structured facts from ingested documents.

The mechanical scan answered "what exists and what does it say". This answers "what does it
mean" -- which used to be the expensive part, so it ran over about seventy curated files and
left the other 1,850 to full-text search.

It runs on a model on Arun's own network now, so the cost argument is gone. What replaces it
is a correctness argument, and it points the other way. The corpus is mostly not about him:
CHEN 201 and POLS 207 textbooks, lab handouts, other people's papers. Asked to extract "facts
about Arun" from a thermodynamics chapter, a model will find some, record them at high
confidence with a real source, and supersede true facts with them.

So PRIORITY_PATTERNS stops being a gate and becomes an ordering, the exclusions stay exactly
as they are -- they are about privacy and correctness, never about cost -- and a cheap gate
in front of extraction answers "is this about Arun at all" before the expensive pass is
allowed to have an opinion about it.
"""
from __future__ import annotations

from synth import agent, db, ollama, runner

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

# What the local extractor may touch. Reads to check what already exists, and exactly one
# write. No reminders, no events, no documents: enrichment records what a file says, and a
# pass that could also act on what it read would be a reactor with a different name.
LOCAL_TOOLS = ["search", "entity", "document", "add_facts"]

GATE_SCHEMA = {
    "type": "object",
    "properties": {
        "about_arun": {"type": "boolean"},
        "kind": {"type": "string"},
        "why": {"type": "string"},
    },
    "required": ["about_arun", "kind", "why"],
}

GATE_PROMPT = """Arun Vaithianathan is a chemical engineering undergraduate at Texas A&M.

Below is the beginning of a file from his Documents folder. Decide whether it is a record of
HIS OWN work, standing or commitments, or whether it is material he merely holds a copy of.

The question is authorship and subject, NOT whether the content is technical. This is the
distinction that matters:

- **His own work — yes.** Research he is conducting, analyses and lab results he produced,
  weekly reports he wrote, projects he built, code he authored. A CO2 reaction analysis in
  his own research folder is his work, however technical it reads.
- **His record — yes.** Transcripts, degree audits, resumes, applications, scholarship
  material, correspondence with advisors, anything stating a deadline or an outcome.
- **Material he is studying — no.** Textbook chapters, a professor's lecture notes, problem
  sets, practice exams, recitation handouts, reference tables. He did not write these and
  they say nothing about him.
- **Other people's work — no.** Published papers by others, someone else's application,
  blank forms, templates.

A file authored by him is about him even when its subject is chemistry. A file authored by
his professor is not about him even when he is the one studying it.

FILE: {path}
---
{head}
---

Answer with JSON: about_arun (boolean), kind (a short phrase like "lab analysis",
"lecture notes", "degree audit", "resume"), why (one short sentence)."""

# Enough to tell a transcript from a textbook, and small enough that the gate stays cheap.
GATE_CHARS = 1500


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


def gate(conn, doc_id: int, path: str, text: str, force: bool = False) -> dict:
    """Is this document about Arun, or is it reference material that lives in his folder?

    Cached on the document's text hash: the answer only changes when the file does, and the
    gate is the cheap half of the pass.
    """
    text_hash = db.text_hash(text or "")
    if not force:
        row = conn.execute("SELECT relevant, why FROM enrich_gate WHERE document_id = ? "
                           "AND text_hash = ?", (doc_id, text_hash)).fetchone()
        if row is not None:
            return {"relevant": bool(row["relevant"]), "why": row["why"], "cached": True}

    prompt = GATE_PROMPT.format(path=path, head=(text or "")[:GATE_CHARS])
    try:
        answer = ollama.generate_json(prompt, GATE_SCHEMA, tier="fast")
    except ollama.OllamaError as e:
        # A gate that cannot run must not silently drop a document from enrichment. Say so
        # and let the caller decide; nothing is cached, so it is asked again next time.
        return {"relevant": None, "why": f"gate unavailable: {e}", "cached": False}

    relevant = bool(answer.get("about_arun"))
    why = f"{answer.get('kind', '?')}: {answer.get('why', '')}"[:300]
    conn.execute(
        "INSERT INTO enrich_gate (document_id, relevant, why, text_hash, model) "
        "VALUES (?,?,?,?,?) ON CONFLICT (document_id) DO UPDATE SET "
        "relevant=excluded.relevant, why=excluded.why, text_hash=excluded.text_hash, "
        "model=excluded.model, at=datetime('now')",
        (doc_id, 1 if relevant else 0, why, text_hash, ollama.TIERS["fast"]))
    conn.commit()
    return {"relevant": relevant, "why": why, "cached": False}


def select_all(conn, limit: int = 0) -> tuple[list[dict], list[dict]]:
    """Every document that is not excluded, ordered by how authoritative it is likely to be.

    PRIORITY_PATTERNS is the ordering here rather than the gate: the curated files still go
    first, and everything else follows by size instead of being left out. The exclusions are
    unchanged and still applied -- they keep out other people's material and superseded
    versions of Arun's own, neither of which was ever a question of cost.
    """
    done = {r[0] for r in conn.execute("SELECT document_id FROM enrichment")}
    seen, chosen, skipped = set(done), [], []

    def consider(row, label):
        if row["id"] in seen or row["chars"] < 200:
            return
        seen.add(row["id"])
        why = excluded(row["path"])
        if why:
            skipped.append({"id": row["id"], "path": row["path"], "why": why})
            return
        chosen.append({"id": row["id"], "path": row["path"], "chars": row["chars"],
                       "label": label})

    for label, pattern in PRIORITY_PATTERNS:
        for r in conn.execute("SELECT id, path, title, chars FROM document WHERE path LIKE ? "
                              "ORDER BY chars DESC", (pattern,)):
            consider(r, label)
    # Everything else, newest first rather than biggest first. Size was the right order when
    # this was a curated list of Arun's own documents -- the longest resume is the most
    # informative one. Over the whole corpus it inverts: the largest unenriched files are the
    # 200,000-character course textbooks that the gate is about to reject, so ordering by size
    # spends the first half hour of every run rejecting the same textbooks again. Newest first
    # also matches what the nightly job is for, which is whatever has just arrived.
    for r in conn.execute("SELECT id, path, title, chars FROM document "
                          "ORDER BY indexed_at DESC, id DESC"):
        consider(r, "other")
    if limit:
        chosen = chosen[:limit]
    return chosen, skipped


# The nightly job holds the sweep lock while it runs, so it cannot be allowed to run for
# arbitrarily long: every sweep that lands during it is skipped. Fifty minutes is enough to
# make real progress through a backlog and short enough that 03:00 is finished well before
# Arun is awake, with the rest picked up the following night.
NIGHTLY_SECONDS = 50 * 60


def run_local(conn, docs: list[dict], dry_run: bool = False, use_gate: bool = True,
              budget_seconds: float = NIGHTLY_SECONDS) -> list[dict]:
    """Extract facts with the local model, one document at a time.

    One document per run rather than a batch of 60,000 characters. That batching existed to
    amortise the cost of a metered call across as many files as would fit; there is no such
    cost now, and one document per run means a failure loses one document, the summary says
    which file it is about, and `enrichment` advances one row at a time.
    """
    import time as _time

    results = []
    started = _time.time()
    for i, d in enumerate(docs, 1):
        if _time.time() - started > budget_seconds:
            print(f"[{i}/{len(docs)}] stopping: {budget_seconds / 60:.0f} min budget spent; "
                  f"the rest stays queued for the next run", flush=True)
            results.append({"stopped": "budget", "remaining": len(docs) - i + 1})
            break
        row = conn.execute("SELECT text FROM document WHERE id = ?", (d["id"],)).fetchone()
        text = (row["text"] if row else "") or ""

        if use_gate:
            verdict = gate(conn, d["id"], d["path"], text)
            if verdict["relevant"] is None:
                results.append({"doc": d["path"], "skipped": verdict["why"]})
                print(f"[{i}/{len(docs)}] gate unavailable — stopping", flush=True)
                break
            if not verdict["relevant"]:
                results.append({"doc": d["path"], "skipped": verdict["why"],
                                "gated": True})
                print(f"[{i}/{len(docs)}] skip  {d['path'][:60]}  ({verdict['why'][:60]})",
                      flush=True)
                continue

        print(f"[{i}/{len(docs)}] read  {d['path'][:60]}  ({d['chars']:,} chars)", flush=True)
        if dry_run:
            results.append({"doc": d["path"], "would_read": True})
            continue

        with db.run(conn, "enrich", trigger=f"local {i}/{len(docs)}") as run_id:
            user = (f"document_id {d['id']} — {d['path']}\n\n"
                    f"Read it with `document`, then record what it states with `add_facts`, "
                    f"passing document_id {d['id']}.")
            result = agent.run(
                conn, job="enrich", trigger="nightly",
                system=runner.prompt("enrich", documents=f"- document_id {d['id']}  "
                                                         f"{d['path']}"),
                user=user, tool_names=LOCAL_TOOLS, tier="reason",
                max_turns=8, max_writes=2, wall_seconds=420,
                run_id=run_id, dry_run=dry_run)
            agent.record(conn, run_id, result)
            wrote = result["writes"] > 0
            if result["stop"] not in ("error",) and wrote:
                conn.execute("INSERT OR IGNORE INTO enrichment (document_id, run_id) "
                             "VALUES (?,?)", (d["id"], run_id))
            conn.commit()
        results.append({"doc": d["path"], "stop": result["stop"], "writes": result["writes"],
                        "summary": (result.get("text") or "")[:300]})
        print(f"      {result['stop']}, {result['writes']} write(s), "
              f"{result.get('seconds')}s", flush=True)
    return results


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
