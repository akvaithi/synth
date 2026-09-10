"""Predicate normalisation, and the supersession key built on it.

Keying supersession on the raw predicate string is what let the UIN go live three times under
three names and the degree-audit GPA twice. The tests that matter here are the pairs that MUST
collapse and the pairs that MUST NOT -- a threshold loose enough to catch the first while
sparing the second is the whole design, and it is not self-evidently correct.
"""
from __future__ import annotations

import pytest

from synth import predicates


# ---------------------------------------------------------------- key()

@pytest.mark.parametrize("a,b", [
    ("overall GPA on the official degree audit", "overall GPA per the official degree audit"),
    ("Date of Birth", "date of birth"),
    ("term-GPA", "term GPA"),
    ("the UIN", "UIN"),
])
def test_differences_that_cannot_carry_meaning_collapse(a, b):
    assert predicates.key(a) == predicates.key(b)


@pytest.mark.parametrize("a,b", [
    # "for" and "from" look like filler and are not.
    ("GPA for Fall 2025", "GPA from the transcript"),
    ("term GPA Spring 2025", "term GPA Spring 2026"),
    ("advisor", "advisors meeting"),
])
def test_differences_that_do_carry_meaning_survive(a, b):
    assert predicates.key(a) != predicates.key(b)


def test_key_of_nothing_is_empty():
    assert predicates.key("") == ""
    assert predicates.key(None) == ""


# ---------------------------------------------------------------- siblings()

def test_a_differing_number_makes_two_predicates_a_series_not_a_duplicate():
    """Both hold 4.0 and score 0.95. Merging them would destroy a real fact."""
    assert predicates.siblings("term GPA - Spring 2025", "term GPA - Spring 2026")
    assert predicates.similar("term GPA - Spring 2025", "term GPA - Spring 2026") > 0.9


def test_two_spellings_of_one_predicate_are_not_siblings():
    assert not predicates.siblings("overall GPA on the degree audit",
                                   "overall GPA per the degree audit")


# ---------------------------------------------------------------- is_sensitive()

@pytest.mark.parametrize("predicate", [
    "UIN", "university identification number", "date of birth", "home address",
    "term address", "phone number", "mother's name", "emergency contact",
    # The plural suffix is what catches this one; the singular "address" missed it.
    "home and term addresses",
])
def test_withholds_the_values_that_should_never_be_bulk_printed(predicate):
    assert predicates.is_sensitive(predicate)


@pytest.mark.parametrize("predicate", [
    "overall GPA", "expected graduation", "research advisor role", "major",
])
def test_leaves_ordinary_predicates_alone(predicate):
    assert not predicates.is_sensitive(predicate)


# ---------------------------------------------------------------- collapse_same_key()

def _entity(conn, name="Arun Vaithianathan", kind="person"):
    return conn.execute("INSERT INTO entity (kind, name) VALUES (?,?) RETURNING id",
                        (kind, name)).fetchone()[0]


def _assert_fact(conn, eid, predicate, value, observed_at):
    return conn.execute(
        "INSERT INTO assertion (entity_id, predicate, predicate_key, value_text, observed_at) "
        "VALUES (?,?,?,?,?) RETURNING id",
        (eid, predicate, predicates.key(predicate), value, observed_at)).fetchone()[0]


def _live(conn, eid):
    return [r["predicate"] for r in conn.execute(
        "SELECT predicate FROM assertion WHERE entity_id = ? AND superseded_by IS NULL", (eid,))]


def test_same_key_duplicates_collapse_onto_the_newest_spelling(conn):
    eid = _entity(conn)
    old = _assert_fact(conn, eid, "overall GPA on the official degree audit", "3.9",
                       "2026-01-01 00:00:00")
    new = _assert_fact(conn, eid, "overall GPA per the official degree audit", "3.9",
                       "2026-02-01 00:00:00")
    moved = predicates.collapse_same_key(conn)

    assert len(moved) == 1
    assert _live(conn, eid) == ["overall GPA per the official degree audit"]
    assert conn.execute("SELECT superseded_by FROM assertion WHERE id = ?",
                        (old,)).fetchone()[0] == new


def test_a_differing_value_still_collapses_because_that_is_what_supersession_is_for(conn):
    eid = _entity(conn)
    _assert_fact(conn, eid, "overall GPA on the audit", "3.7", "2026-01-01 00:00:00")
    _assert_fact(conn, eid, "overall GPA per the audit", "3.9", "2026-02-01 00:00:00")
    predicates.collapse_same_key(conn)

    live = conn.execute("SELECT value_text FROM assertion WHERE entity_id = ? "
                        "AND superseded_by IS NULL", (eid,)).fetchall()
    assert [r[0] for r in live] == ["3.9"]


def test_a_series_is_left_alone(conn):
    eid = _entity(conn)
    _assert_fact(conn, eid, "term GPA Spring 2025", "4.0", "2026-01-01 00:00:00")
    _assert_fact(conn, eid, "term GPA Spring 2026", "4.0", "2026-02-01 00:00:00")
    assert predicates.collapse_same_key(conn) == []
    assert len(_live(conn, eid)) == 2


def test_the_same_predicate_on_two_entities_is_two_facts(conn):
    a, b = _entity(conn, "Arun Vaithianathan"), _entity(conn, "Someone Else")
    _assert_fact(conn, a, "overall GPA", "3.9", "2026-01-01 00:00:00")
    _assert_fact(conn, b, "overall GPA", "3.1", "2026-01-01 00:00:00")
    assert predicates.collapse_same_key(conn) == []


def test_collapse_is_idempotent(conn):
    """Stated by the docstring, run on every migrate, and never previously proved."""
    eid = _entity(conn)
    _assert_fact(conn, eid, "overall GPA on the audit", "3.9", "2026-01-01 00:00:00")
    _assert_fact(conn, eid, "overall GPA per the audit", "3.9", "2026-02-01 00:00:00")

    assert len(predicates.collapse_same_key(conn)) == 1
    assert predicates.collapse_same_key(conn) == []


# ---------------------------------------------------------------- one fact, several names

def test_embedding_distance_cannot_separate_synonyms_from_neighbours():
    """Recorded so it is not tried again. nomic-embed-text measures topical relatedness, not
    synonymy, and the two distributions overlap completely -- 'home address' and 'email
    address' scored 0.80 while 'UIN' and 'university ID number' scored 0.69. No threshold
    admits the second and refuses the first, which is why this is a model judgement."""
    import inspect

    from synth import predicates

    source = inspect.getsource(predicates)
    assert "SEMANTIC_THRESHOLD" not in source, "a cosine threshold cannot do this job"
    assert "same_fact" in source


def test_a_judgement_that_cannot_be_made_never_merges(monkeypatch):
    """Local inference being away must mean 'do not merge', never 'merge anyway'. Leaving two
    names live is untidy; merging two real facts destroys one of them."""
    from synth import ollama, predicates

    monkeypatch.setattr(ollama, "generate_json",
                        lambda *a, **k: (_ for _ in ()).throw(ollama.OllamaDown("away")))
    same, why = predicates.same_fact("UIN", "university ID number")
    assert same is False
    assert "unjudged" in why


def test_a_judgement_is_asked_once_per_pair(monkeypatch):
    """The pairs are few, but a cluster compares each against each and the same two names come
    round repeatedly."""
    from synth import ollama, predicates

    calls = []

    def counted(prompt, schema, **kw):
        calls.append(prompt)
        return {"same_fact": True, "why": "the same"}

    monkeypatch.setattr(ollama, "generate_json", counted)
    cache = {}
    predicates.same_fact("UIN", "university ID number", cache=cache)
    predicates.same_fact("university ID number", "UIN", cache=cache)
    assert len(calls) == 1, "the reversed pair asked again"


def test_a_numbered_series_is_never_offered_for_merging(conn, monkeypatch):
    """siblings() stays decisive and is checked before any judgement: 'term GPA - Spring 2025'
    and 'term GPA - Spring 2026' are near-identical by every measure and are two facts."""
    from synth import facts, ollama, predicates

    monkeypatch.setattr(ollama, "generate_json",
                        lambda *a, **k: {"same_fact": True, "why": "they look alike"})
    eid = facts.upsert_entity(conn, "person", "Arun")
    facts.assert_fact(conn, eid, "term GPA - Spring 2025", num=4.0)
    facts.assert_fact(conn, eid, "term GPA - Spring 2026", num=4.0)
    conn.commit()
    mergeable, _ = predicates.clusters(conn)
    assert mergeable == [], "a series was offered for merging"


def test_two_names_for_one_fact_are_found_even_when_they_share_no_words(conn, monkeypatch):
    """The UIN was live three times under three names for months, because the tool meant to
    find it was comparing characters."""
    from synth import facts, ollama, predicates

    monkeypatch.setattr(ollama, "generate_json",
                        lambda *a, **k: {"same_fact": True, "why": "both are the UIN"})
    eid = facts.upsert_entity(conn, "person", "Arun")
    facts.assert_fact(conn, eid, "UIN", text="123456789")
    facts.assert_fact(conn, eid, "university identification number", text="123456789")
    conn.commit()
    mergeable, _ = predicates.clusters(conn)
    assert len(mergeable) == 1
    cluster = [mergeable[0]["keep"], *mergeable[0]["supersede"]]
    names = {r["predicate"] for r in cluster}
    assert names == {"UIN", "university identification number"}
    # Which one survives depends on observed_at; what matters is that the pair was found by
    # judgement rather than by spelling, since they share no words at all.
    assert any("judged" in r.get("_matched_by", "") for r in cluster)
