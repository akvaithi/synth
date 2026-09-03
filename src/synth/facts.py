"""Writing into the context database.

The rule that shapes this module: an assertion is never updated. Correcting a fact means
inserting the new one and marking the old superseded. That is what makes "when did this
change, and what told me" answerable months later, and it is the thing whose absence cost
the previous system its history.
"""
from __future__ import annotations

import json

from synth import db, predicates

ENTITY_KINDS = {"person", "org", "program", "course", "application", "project", "award", "topic"}


def upsert_entity(conn, kind: str, name: str, *, description: str | None = None,
                  status: str | None = None) -> int:
    if kind not in ENTITY_KINDS:
        raise ValueError(f"unknown entity kind {kind!r}; expected one of {sorted(ENTITY_KINDS)}")
    name = name.strip()
    cur = conn.execute(
        "INSERT INTO entity (kind, name, description, status) VALUES (?,?,?,?) "
        "ON CONFLICT (kind, name) DO UPDATE SET "
        "  description = COALESCE(excluded.description, entity.description), "
        "  status = COALESCE(excluded.status, entity.status) "
        "RETURNING id",
        (kind, name, description, status),
    )
    entity_id = cur.fetchone()[0]
    return entity_id


def live(conn, entity_id: int, predicate: str):
    """The current value of a predicate: the assertion nothing has superseded.

    Matched on the normalised key rather than the exact string. Keying on the string meant a
    predicate written a second time under a marginally different name superseded nothing and
    simply sat beside the first -- which is why the degree-audit GPA was recorded twice, once
    "on the official degree audit" and once "per" it. See predicates.key for what the
    normalisation will and will not collapse.
    """
    return conn.execute(
        "SELECT * FROM assertion WHERE entity_id = ? AND predicate_key = ? "
        "AND superseded_by IS NULL ORDER BY observed_at DESC LIMIT 1",
        (entity_id, predicates.key(predicate)),
    ).fetchone()


def assert_fact(conn, entity_id: int | None, predicate: str, *, text=None, num=None,
                date=None, source_id: int | None = None, confidence: float = 1.0,
                mail_derived: bool = False, supersede: bool = True) -> int:
    """Record a fact. If it contradicts the live value, supersede rather than overwrite."""
    if text is None and num is None and date is None:
        raise ValueError(f"assertion {predicate!r} has no value")

    prior = live(conn, entity_id, predicate) if (supersede and entity_id) else None
    if prior is not None and (prior["value_text"], prior["value_num"], prior["value_date"]) \
            == (text, num, date):
        return prior["id"]  # unchanged; nothing to record

    cur = conn.execute(
        "INSERT INTO assertion (entity_id, predicate, predicate_key, value_text, value_num, "
        "value_date, source_id, confidence, mail_derived) VALUES (?,?,?,?,?,?,?,?,?) "
        "RETURNING id",
        (entity_id, predicate, predicates.key(predicate), text, num, date, source_id,
         confidence, int(mail_derived)),
    )
    new_id = cur.fetchone()[0]
    if prior is not None:
        conn.execute("UPDATE assertion SET superseded_by = ? WHERE id = ?", (new_id, prior["id"]))
    return new_id


def add_edge(conn, src_id: int, dst_id: int, relation: str,
             source_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO edge (src_id, dst_id, relation, source_id) VALUES (?,?,?,?) "
        "ON CONFLICT (src_id, dst_id, relation) DO UPDATE SET "
        "  source_id = COALESCE(excluded.source_id, edge.source_id) RETURNING id",
        (src_id, dst_id, relation, source_id),
    )
    return cur.fetchone()[0]


def add_link(conn, url: str, *, title=None, kind=None, entity_id=None,
             source_id=None) -> int:
    cur = conn.execute(
        "INSERT INTO link (url, title, kind, entity_id, source_id) VALUES (?,?,?,?,?) "
        "ON CONFLICT (url, source_id) DO UPDATE SET "
        "  title = COALESCE(excluded.title, link.title) RETURNING id",
        (url, title, kind, entity_id, source_id),
    )
    link_id = cur.fetchone()[0]
    return link_id


def history(conn, entity_id: int, predicate: str) -> list[dict]:
    """Every value this predicate has held, newest first, with what told us."""
    rows = conn.execute(
        "SELECT a.*, s.kind AS source_kind, s.native_id, s.detail "
        "FROM assertion a LEFT JOIN source s ON s.id = a.source_id "
        "WHERE a.entity_id = ? AND a.predicate_key = ? "
        "ORDER BY a.observed_at DESC, a.id DESC",
        (entity_id, predicates.key(predicate)),
    ).fetchall()
    return [dict(r) for r in rows]


def near_duplicates(conn, entity_id: int, predicate: str, value: tuple,
                    threshold: float = 0.80) -> list[dict]:
    """Live predicates on this entity that look like another name for the one being written.

    Reported, never acted on. The caller is a model that can re-issue the fact under the
    existing name, which is the fix; refusing the write instead would lose the fact over a
    guess. A near-identical name is enough on its own, and so is an identical value, because
    the same value under two names is what the duplication actually looks like.
    """
    out = []
    for r in conn.execute(
        "SELECT id, predicate, value_text, value_num, value_date FROM assertion "
        "WHERE entity_id = ? AND superseded_by IS NULL", (entity_id,)
    ):
        if predicates.key(r["predicate"]) == predicates.key(predicate):
            continue  # same key: supersession already handled it
        if predicates.siblings(r["predicate"], predicate):
            continue  # a series distinguished by a number is not a duplicate
        same_value = predicates._value(r) == value
        if same_value or predicates.similar(r["predicate"], predicate) >= threshold:
            out.append({"predicate": r["predicate"], "assertion_id": r["id"],
                        "same_value": same_value,
                        "similarity": round(predicates.similar(r["predicate"], predicate), 2)})
    return out


def ingest_batch(conn, payload: dict, source_id: int | None = None,
                 mail_derived: bool = False) -> dict:
    """Apply a batch of extracted facts.

    Shape:
      {"entities": [{"kind","name","description","status",
                     "facts": [{"predicate","text"|"num"|"date","confidence"}]}],
       "edges":    [{"src","src_kind","dst","dst_kind","relation"}],
       "links":    [{"url","title","kind","entity"}]}
    """
    counts = {"entities": 0, "facts": 0, "edges": 0, "links": 0}
    ids: dict[tuple[str, str], int] = {}
    warnings: list[dict] = []

    for e in payload.get("entities", []):
        eid = upsert_entity(conn, e["kind"], e["name"],
                            description=e.get("description"), status=e.get("status"))
        ids[(e["kind"], e["name"])] = eid
        counts["entities"] += 1
        for f in e.get("facts", []):
            # Checked BEFORE the write, against what is live now -- afterwards the new row is
            # itself live and would be compared against.
            near = near_duplicates(
                conn, eid, f["predicate"],
                predicates._value({"value_text": f.get("text"), "value_num": f.get("num"),
                                   "value_date": f.get("date")}))
            if near:
                warnings.append({"entity": e["name"], "predicate": f["predicate"],
                                 "existing": near})
            assert_fact(conn, eid, f["predicate"], text=f.get("text"), num=f.get("num"),
                        date=f.get("date"), source_id=source_id,
                        confidence=float(f.get("confidence", 1.0)),
                        mail_derived=mail_derived)
            counts["facts"] += 1

    for edge in payload.get("edges", []):
        src = ids.get((edge["src_kind"], edge["src"]))
        dst = ids.get((edge["dst_kind"], edge["dst"]))
        if src is None or dst is None:
            continue
        add_edge(conn, src, dst, edge["relation"], source_id)
        counts["edges"] += 1

    for l in payload.get("links", []):
        eid = None
        for (k, n), v in ids.items():
            if n == l.get("entity"):
                eid = v
                break
        add_link(conn, l["url"], title=l.get("title"), kind=l.get("kind"),
                 entity_id=eid, source_id=source_id)
        counts["links"] += 1

    conn.commit()
    if warnings:
        counts["near_duplicates"] = warnings
        counts["note"] = (
            "Each of these was written under a NEW predicate that looks like another name for "
            "one already on that entity, so it superseded nothing and both are now live. If "
            "they are the same fact, write it again using the existing predicate name exactly "
            "— that supersedes properly. If they are genuinely different, say so to Arun and "
            "leave them.")
    return counts
