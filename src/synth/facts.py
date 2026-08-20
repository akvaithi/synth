"""Writing into the context database.

The rule that shapes this module: an assertion is never updated. Correcting a fact means
inserting the new one and marking the old superseded. That is what makes "when did this
change, and what told me" answerable months later, and it is the thing whose absence cost
the previous system its history.
"""
from __future__ import annotations

import json

from synth import db

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
    """The current value of a predicate: the assertion nothing has superseded."""
    return conn.execute(
        "SELECT * FROM assertion WHERE entity_id = ? AND predicate = ? "
        "AND superseded_by IS NULL ORDER BY observed_at DESC LIMIT 1",
        (entity_id, predicate),
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
        "INSERT INTO assertion (entity_id, predicate, value_text, value_num, value_date, "
        "source_id, confidence, mail_derived) VALUES (?,?,?,?,?,?,?,?) RETURNING id",
        (entity_id, predicate, text, num, date, source_id, confidence, int(mail_derived)),
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
        "WHERE a.entity_id = ? AND a.predicate = ? ORDER BY a.observed_at DESC, a.id DESC",
        (entity_id, predicate),
    ).fetchall()
    return [dict(r) for r in rows]


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

    for e in payload.get("entities", []):
        eid = upsert_entity(conn, e["kind"], e["name"],
                            description=e.get("description"), status=e.get("status"))
        ids[(e["kind"], e["name"])] = eid
        counts["entities"] += 1
        for f in e.get("facts", []):
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
    return counts
