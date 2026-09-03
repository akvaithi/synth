"""Sensitive values are withheld, and the predicate never is.

get_entity on the hub entity once returned the UIN, date of birth, ISD student ID, home
address and a parent's name in plain text inside a 150-fact payload nobody asked for by name.
The rule that replaced it has two halves and both matter: the value goes, the predicate stays
-- that Synth holds a UIN is not the secret.
"""
from __future__ import annotations

from synth import predicates, tools


def _rows():
    return [
        {"predicate": "UIN", "value_text": "123006789", "value_num": None, "value_date": None},
        {"predicate": "date of birth", "value_text": None, "value_num": None,
         "value_date": "2004-03-11"},
        {"predicate": "overall GPA", "value_text": None, "value_num": 3.9, "value_date": None},
        {"predicate": "home address", "value_text": "1 Example St", "value_num": None,
         "value_date": None},
    ]


def test_withholds_sensitive_values_and_counts_them():
    rows = _rows()
    assert tools._redact(rows, include_sensitive=False) == 3


def test_leaves_every_predicate_visible():
    rows = _rows()
    tools._redact(rows, include_sensitive=False)
    assert [r["predicate"] for r in rows] == [
        "UIN", "date of birth", "overall GPA", "home address"]


def test_redacts_across_all_three_value_columns():
    """A date of birth lives in value_date and a UIN in value_text; missing either column
    would leak the value while the payload claimed to be redacted."""
    rows = _rows()
    tools._redact(rows, include_sensitive=False)
    assert rows[0]["value_text"] == predicates.REDACTED
    assert rows[1]["value_date"] == predicates.REDACTED
    assert rows[3]["value_text"] == predicates.REDACTED


def test_leaves_ordinary_facts_untouched():
    rows = _rows()
    tools._redact(rows, include_sensitive=False)
    assert rows[2]["value_num"] == 3.9
    assert "sensitive" not in rows[2]


def test_marks_what_it_withheld():
    rows = _rows()
    tools._redact(rows, include_sensitive=False)
    assert [r.get("sensitive") for r in rows] == [True, True, None, True]


def test_include_sensitive_is_the_deliberate_act_that_shows_the_value():
    rows = _rows()
    assert tools._redact(rows, include_sensitive=True) == 0
    assert rows[0]["value_text"] == "123006789"
    assert rows[1]["value_date"] == "2004-03-11"


def test_a_null_value_is_not_reported_as_withheld():
    """value_num on a UIN row is None; redacting it would invent a value that is not there."""
    rows = [{"predicate": "UIN", "value_text": "123006789", "value_num": None,
             "value_date": None}]
    tools._redact(rows, include_sensitive=False)
    assert rows[0]["value_num"] is None
