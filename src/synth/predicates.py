"""Predicate names: normalising them, and telling two spellings of one fact apart from two facts.

Supersession keys on the predicate string. `facts.assert_fact` looks up the live value by exact
predicate and supersedes it when the new value differs -- so a predicate written a second time
under a slightly different name never supersedes anything, and both live forever. The UIN was
stored three times under three names, the RPD certification date twice, the degree-audit GPA
twice. Nothing was wrong with any individual write; the duplication is structural, and every
enrichment pass compounds it.

Two layers here, and the split between them is the point:

1. `key()` is a normalisation, and matching on it is AUTOMATIC. It collapses only differences
   that cannot carry meaning -- case, punctuation, and a tight set of filler words. "overall GPA
   *on* the official degree audit" and "overall GPA *per* the official degree audit" become one
   key, so the second supersedes the first the way it always should have.

2. `similar()` and `siblings()` back a WARNING, never an automatic merge. Fuzzy matching is not
   safe to act on unsupervised: "term GPA - Spring 2025" and "term GPA - Spring 2026" score 0.95
   and both hold 4.0, and merging them would destroy a real fact. `siblings()` is the guard --
   two predicates distinguished by a number are never the same predicate, whatever they score.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# Words that cannot change what a predicate means. Deliberately short: "for" and "from" look
# like filler and are not -- "GPA for Fall 2025" and "GPA from the transcript" say different
# things -- and every word added here is a pair of distinct predicates that might silently merge.
FILLER = {"the", "a", "an", "as", "on", "per", "of", "its", "stated"}

# Predicates whose VALUE is not printed by default. get_entity on the hub entity returned the
# UIN, date of birth, ISD student ID, home address and a parent's name in plain text inside a
# 150-fact payload nobody asked for by name. The predicate is still shown -- nothing is hidden,
# only the value withheld until it is asked for.
SENSITIVE = [
    "uin", "university identification number", "student id", "student identification",
    "date of birth", "birth date", "birthday",
    "address", "home address", "term address", "mailing address",
    "phone", "mobile", "cell number",
    "ssn", "social security",
    "passport", "driver license", "drivers license",
    "parent", "guardian", "mother", "father", "emergency contact",
    # "contact details" earns its place: on this database it holds a home street address.
    # Note the limit of the whole mechanism -- it reads the PREDICATE, not the value, so a
    # street address recorded under an innocuous name is not caught. This is a guard against
    # bulk exposure in a 160-fact dump, not a classifier.
    "contact details",
]


def key(predicate: str) -> str:
    """A predicate reduced to what two spellings of the same predicate agree on."""
    words = re.sub(r"[^0-9a-z]+", " ", (predicate or "").casefold()).split()
    return " ".join(w for w in words if w not in FILLER)


def similar(a: str, b: str) -> float:
    """How alike two predicates read, 0.0 to 1.0, comparing their normalised keys."""
    return SequenceMatcher(None, key(a), key(b)).ratio()


def siblings(a: str, b: str) -> bool:
    """Whether two predicates are one series distinguished by a number, not two spellings.

    "term GPA - Spring 2025" against "term GPA - Spring 2026"; 10th grade against 11th grade;
    an audit dated one day against an audit dated another. These score high and can hold the
    same value, so nothing else here would keep them apart. A differing digit-bearing token is
    the signal, and it is decisive: never merge on it.
    """
    ta, tb = key(a).split(), key(b).split()
    differing = set(ta).symmetric_difference(tb)
    return any(any(c.isdigit() for c in w) for w in differing)


def is_sensitive(predicate: str) -> bool:
    """Whether this predicate's value should be withheld unless asked for by name.

    The patterns run through key() as well, so a phrase carrying a filler word matches a
    predicate that dropped it -- "date of birth" normalises to "date birth" on both sides. The
    optional plural suffix is what catches "home and term addresses", which the singular
    "address" missed while being exactly the thing worth withholding.
    """
    k = key(predicate)
    return any(re.search(rf"\b{re.escape(key(p))}(?:e?s)?\b", k) for p in SENSITIVE)


REDACTED = "[redacted — re-ask with include_sensitive=true]"


def _value(row) -> tuple:
    """A row's value, normalised for comparison: whitespace and case cannot make it a new fact."""
    text = row["value_text"]
    if text is not None:
        text = " ".join(str(text).split()).casefold()
    return (text, row["value_num"], row["value_date"])


# ---------------------------------------------------------------- deduplication


# Two predicate names mean the same thing when they READ the same, which SequenceMatcher
# measures, or when they MEAN the same, which it cannot see at all. "UIN", "university ID
# number" and "student identification number" share almost no characters and score far below
# any usable threshold -- and lowering the threshold far enough to catch them starts merging
# things that are genuinely different. That is why the three UIN spellings sat live for
# months: the tool meant to find them was measuring the wrong thing.
#
# Embedding cosine was tried here first and does not work, which is worth recording so it is
# not tried again. nomic-embed-text measures topical relatedness, not synonymy, and the two
# distributions overlap completely:
#
#     UIN / university ID number            0.69   <- same fact
#     overall GPA / cumulative GPA          0.72   <- same fact
#     start date / end date                 0.76   <- DIFFERENT facts
#     home address / email address          0.80   <- DIFFERENT facts
#
# There is no threshold that admits the first pair and refuses the last. Asking a model is the
# right shape of question -- "are these two names for one fact?" is a judgement, not a
# distance -- and it is free, because the pairs it is asked about already share an entity and
# an identical value, so there are only ever a handful.
SAME_FACT_SCHEMA = {
    "type": "object",
    "properties": {
        "same_fact": {"type": "boolean"},
        "why": {"type": "string"},
    },
    "required": ["same_fact", "why"],
}

SAME_FACT_PROMPT = """These are two predicate names from a personal-knowledge database about
one person. They are recorded against the same subject and hold exactly the same value.

Decide whether they are two names for ONE fact, or two genuinely different facts that happen
to share a value.

Same fact: "UIN" and "university ID number". "overall GPA" and "cumulative grade point
average". "deadline" and "application due date".

Different facts: "home address" and "email address" — both are addresses, and a person has
both. "start date" and "end date". "term GPA - Spring 2025" and "term GPA - Spring 2026".

If in doubt, answer false. Leaving two names live is untidy; merging two real facts destroys
one of them.

A: {a}
B: {b}

Answer with JSON: same_fact (boolean), why (one short sentence)."""


def same_fact(a: str, b: str, cache: dict | None = None) -> tuple[bool, str]:
    """Are these two predicate names one fact? Judged by a model, not by distance."""
    from synth import ollama

    ck = tuple(sorted((key(a), key(b))))
    if cache is not None and ck in cache:
        return cache[ck]
    try:
        answer = ollama.generate_json(SAME_FACT_PROMPT.format(a=a, b=b), SAME_FACT_SCHEMA,
                                      tier="fast")
        out = (bool(answer.get("same_fact")), str(answer.get("why", ""))[:200])
    except Exception as e:
        # Unavailable means "do not merge", never "merge anyway".
        out = (False, f"unjudged: {type(e).__name__}")
    if cache is not None:
        cache[ck] = out
    return out


def clusters(conn, threshold: float = 0.80, use_semantic: bool = True
             ) -> tuple[list[dict], list[dict]]:
    """Live assertions that are one fact under several predicate names.

    Returns (mergeable, reported). A cluster is MERGEABLE when the values are identical, the
    keys read as near-identical, and they are not siblings -- at which point the older members
    are provably the same fact and can be superseded onto the newest. Everything else that
    shares a value on one entity is REPORTED and left alone: the three UIN spellings share one
    token and score far below any usable threshold, and lowering it far enough to catch them
    would start merging things that are genuinely different.
    """
    rows = [dict(r, _key=key(r["predicate"]), _val=_value(r)) for r in conn.execute(
        "SELECT a.id, a.entity_id, a.predicate, a.value_text, a.value_num, a.value_date, "
        "  a.observed_at, e.name AS entity FROM assertion a "
        "JOIN entity e ON e.id = a.entity_id "
        "WHERE a.superseded_by IS NULL ORDER BY a.entity_id, a.observed_at, a.id")]

    by_value: dict = {}
    for r in rows:
        by_value.setdefault((r["entity_id"], r["_val"]), []).append(r)

    judged: dict = {}

    def alike(a: str, b: str) -> tuple[bool, str]:
        """Do these two names mean one fact? Spelling first, then a judgement.

        Spelling is free and settles the easy cases. The model is only asked about pairs that
        already share an entity and an identical value and did not match on spelling, which is
        a handful per run.
        """
        if similar(a, b) >= threshold:
            return True, "spelling"
        if not use_semantic:
            return False, ""
        same, why = same_fact(a, b, cache=judged)
        return (True, f"judged: {why}") if same else (False, "")

    mergeable, reported = [], []
    for (_eid, _val), group in by_value.items():
        if len(group) < 2 or all(v is None for v in _val):
            continue
        used = set()
        for i, a in enumerate(group):
            if a["id"] in used:
                continue
            same = [a]
            for b in group[i + 1:]:
                if b["id"] in used or b["_key"] == a["_key"]:
                    continue
                # siblings() stays decisive and is checked FIRST: a differing digit-bearing
                # token means a series, and "term GPA Spring 2025" and "term GPA Spring 2026"
                # are near-identical by both measures while being two different facts.
                if siblings(a["predicate"], b["predicate"]):
                    continue
                match, why = alike(a["predicate"], b["predicate"])
                if match:
                    b["_matched_by"] = why
                    same.append(b)
                    used.add(b["id"])
            if len(same) > 1:
                used.add(a["id"])
                # The anchor is in the cluster too; without this it reports as unmatched.
                a.setdefault("_matched_by", "anchor")
                # Newest survives: it is the spelling most recently in use.
                same.sort(key=lambda r: (r["observed_at"], r["id"]))
                mergeable.append({"entity": a["entity"], "keep": same[-1],
                                  "supersede": same[:-1]})
        rest = [r for r in group if r["id"] not in used]
        if len(rest) > 1 and len({r["_key"] for r in rest}) > 1:
            # A cluster whose members are all pairwise siblings is a SERIES sharing a value --
            # term GPA Spring 2025 and Spring 2026 are both 4.0 -- not a duplicate. Say so, or
            # the report reads as a list of things somebody ought to merge.
            series = all(siblings(a["predicate"], b["predicate"])
                         for i, a in enumerate(rest) for b in rest[i + 1:])
            reported.append({"entity": rest[0]["entity"], "members": rest, "series": series})
    return mergeable, reported


def collapse_same_key(conn) -> list[str]:
    """Live assertions that now share a normalised key: keep the newest, supersede the rest.

    The safest merge there is, and the one the key was introduced for. Two rows with the same
    predicate_key ARE the same predicate as far as facts.live is concerned -- "overall GPA on
    the official degree audit" and "...per the official degree audit" normalise identically --
    so leaving both live means get_entity prints one fact twice and only one of them will ever
    be superseded again.

    Values are not compared. Same predicate with a different value is exactly what supersession
    is FOR: the newest observation wins and the older becomes history, which is what should
    have happened at the time.

    Idempotent by construction -- once it has run there are no same-key live duplicates left.
    """
    groups: dict = {}
    for r in conn.execute(
        "SELECT id, entity_id, predicate, predicate_key, observed_at FROM assertion "
        "WHERE superseded_by IS NULL AND entity_id IS NOT NULL AND predicate_key IS NOT NULL "
        "ORDER BY entity_id, predicate_key, observed_at, id"
    ):
        groups.setdefault((r["entity_id"], r["predicate_key"]), []).append(dict(r))

    moved = []
    for rows in groups.values():
        if len(rows) < 2:
            continue
        keep = rows[-1]
        for old in rows[:-1]:
            conn.execute("UPDATE assertion SET superseded_by = ? WHERE id = ?",
                         (keep["id"], old["id"]))
            moved.append(f"{old['predicate']} -> {keep['predicate']}")
    conn.commit()
    return moved


def merge_ids(conn, keep_id: int, old_ids: list[int]) -> list[str]:
    """Supersede named assertions onto a named survivor, for a cluster judged by hand.

    The automatic pass only takes clusters it can prove; most real duplication -- three
    spellings of the UIN, "hire date" against "appointment start" -- reads as obviously one
    fact to a person and scores far too low for any threshold that is safe to run unattended.
    This is how that judgement gets applied without loosening the threshold.
    """
    keep = conn.execute("SELECT id, predicate, entity_id FROM assertion WHERE id = ?",
                        (keep_id,)).fetchone()
    if keep is None:
        raise ValueError(f"no assertion {keep_id}")
    moved = []
    for oid in old_ids:
        row = conn.execute("SELECT id, predicate, entity_id, superseded_by FROM assertion "
                           "WHERE id = ?", (oid,)).fetchone()
        if row is None:
            raise ValueError(f"no assertion {oid}")
        if row["entity_id"] != keep["entity_id"]:
            raise ValueError(
                f"assertion {oid} is on a different entity than {keep_id}; refusing to merge "
                f"across entities.")
        if row["superseded_by"] is not None:
            continue
        conn.execute("UPDATE assertion SET superseded_by = ? WHERE id = ?", (keep_id, oid))
        moved.append(f"{row['predicate']} -> {keep['predicate']}")
    conn.commit()
    return moved


def merge(conn, mergeable: list[dict]) -> int:
    """Supersede each cluster's older spellings onto its newest. Nothing is deleted.

    Supersession is how this database has always recorded a correction, so a merge leaves the
    same trail any other changed fact leaves: the old rows stay, pointed at the row that
    replaced them, and `fact_history` still walks the whole chain.
    """
    n = 0
    for c in mergeable:
        for old in c["supersede"]:
            conn.execute("UPDATE assertion SET superseded_by = ? WHERE id = ?",
                         (c["keep"]["id"], old["id"]))
            n += 1
    conn.commit()
    return n
