"""The loop that lets a local model use the tool layer, and the six ways it stops.

The stops are the whole file. A small model loops, keeps going after the answer is in front of
it, and retries calls that already succeeded -- and every one of those behaviours turns into
something in Arun's reminders unless the loop, not the prompt, prevents it.
"""
from __future__ import annotations

import json

import pytest

from synth import agent, db, registry


def _tool_call(name, **args):
    return {"function": {"name": name, "arguments": args}}


@pytest.fixture
def model(monkeypatch):
    """A scripted model. Each entry is one assistant turn."""
    script = []
    seen = []

    def fake_chat(messages, **kw):
        seen.append(messages[-1])
        if not script:
            return {"message": {"content": "done", "tool_calls": []}}
        return {"message": script.pop(0)}

    monkeypatch.setattr(agent.ollama, "chat", fake_chat)
    return {"script": script, "seen": seen}


@pytest.fixture
def tools(monkeypatch):
    """A read tool and a write tool that record what they were given."""
    log = []

    def a_read(conn, q: str = ""):
        log.append(("read", q))
        return {"found": q}

    def a_write(conn, title: str = "", reason: str = "", run_id=None):
        log.append(("write", title, run_id))
        return {"created": title}

    monkeypatch.setitem(registry.AUTONOMOUS, "a_read", a_read)
    monkeypatch.setitem(registry.AUTONOMOUS, "a_write", a_write)
    monkeypatch.setitem(registry.ALL, "a_read", a_read)
    monkeypatch.setitem(registry.ALL, "a_write", a_write)
    monkeypatch.setitem(registry.WRITE, "a_write", a_write)
    return log


@pytest.fixture(autouse=True)
def no_halt(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "HALT", str(tmp_path / "HALT"))
    return tmp_path / "HALT"


def _run(conn, script, model, **kw):
    model["script"].extend(script)
    return agent.run(conn, job="test", trigger="test", system="s", user="u",
                     tool_names=kw.pop("tool_names", ["a_read", "a_write"]), **kw)


# ---------------------------------------------------------------- the six stops

def test_a_run_ends_when_the_model_stops_calling_tools(conn, model, tools):
    r = _run(conn, [{"content": "here is the answer", "tool_calls": []}], model)
    assert r["stop"] == "done"
    assert r["text"] == "here is the answer"


def test_a_run_stops_at_the_turn_limit(conn, model, tools):
    """A model that never concludes must not run for ever."""
    r = _run(conn, [{"content": "", "tool_calls": [_tool_call("a_read", q=str(i))]}
                    for i in range(30)], model, max_turns=4)
    assert r["stop"] == "turns"
    assert len([c for c in r["calls"] if c["name"] == "a_read"]) == 4


def test_a_run_stops_at_the_write_cap(conn, model, tools):
    """The cap belongs in the loop, not in a tool: a tool cannot know how many siblings it
    has, and four separate reminders are four separate correct-looking calls."""
    r = _run(conn, [{"content": "", "tool_calls": [_tool_call("a_write", title=f"t{i}",
                                                              reason="a good reason here")]}
                    for i in range(6)], model, max_writes=2)
    assert r["stop"] == "writes"
    assert r["writes"] == 2
    assert len([e for e in tools if e[0] == "write"]) == 2


def test_a_run_stops_on_the_wall_clock(conn, model, tools, monkeypatch):
    r = _run(conn, [{"content": "", "tool_calls": [_tool_call("a_read", q="x")]}] * 5,
             model, wall_seconds=-1)
    assert r["stop"] == "wall"
    assert r["calls"] == []


def test_the_halt_file_stops_a_run_before_its_next_write(conn, model, tools, no_halt):
    """touch .state/HALT must stop an in-flight run at its next write rather than after it."""
    no_halt.write_text("")
    r = _run(conn, [{"content": "", "tool_calls": [_tool_call("a_write", title="t",
                                                              reason="a good reason here")]}],
             model)
    assert r["stop"] == "halt"
    assert [e for e in tools if e[0] == "write"] == []


def test_three_consecutive_tool_errors_end_the_run(conn, model, tools):
    r = _run(conn, [{"content": "", "tool_calls": [_tool_call("not_a_tool")]}] * 5, model)
    assert r["stop"] == "error"


# ---------------------------------------------------------------- looping

def test_the_same_call_with_the_same_arguments_is_not_executed_twice(conn, model, tools):
    """Local models retry calls that already succeeded. A repeated create_reminder is a
    duplicate in Arun's list, not a wasted turn."""
    same = _tool_call("a_write", title="once", reason="a perfectly good reason")
    r = _run(conn, [{"content": "", "tool_calls": [same]},
                    {"content": "", "tool_calls": [same]},
                    {"content": "stopping", "tool_calls": []}], model, max_writes=4)
    assert len([e for e in tools if e[0] == "write"]) == 1, "the tool ran twice"
    assert r["writes"] == 1
    assert "already made this exact call" in json.dumps(r["calls"][-1]["result"])


def test_a_repeat_is_told_to_stop_rather_than_silently_ignored(conn, model, tools):
    same = _tool_call("a_read", q="x")
    r = _run(conn, [{"content": "", "tool_calls": [same]},
                    {"content": "", "tool_calls": [same]},
                    {"content": "ok", "tool_calls": []}], model)
    assert "Do not call it again" in json.dumps(r["calls"][-1]["result"])


def test_different_arguments_are_not_treated_as_a_repeat(conn, model, tools):
    r = _run(conn, [{"content": "", "tool_calls": [_tool_call("a_read", q="a")]},
                    {"content": "", "tool_calls": [_tool_call("a_read", q="b")]},
                    {"content": "ok", "tool_calls": []}], model)
    assert len([e for e in tools if e[0] == "read"]) == 2
    assert r["stop"] == "done"


# ---------------------------------------------------------------- errors steer

def test_a_tool_that_raises_is_reported_to_the_model_without_ending_the_run(conn, model,
                                                                           monkeypatch):
    """The tools raise good sentences -- _require_reason, docwrite.resolve -- and handing one
    back is better steering for a small model than anything in the prompt."""
    def raises(conn, **kw):
        raise ValueError("'test' is not a reason. Say why this helps Arun.")
    monkeypatch.setitem(registry.AUTONOMOUS, "a_read", raises)
    monkeypatch.setitem(registry.ALL, "a_read", raises)
    r = _run(conn, [{"content": "", "tool_calls": [_tool_call("a_read", q="x")]},
                    {"content": "understood", "tool_calls": []}], model,
             tool_names=["a_read"])
    assert r["stop"] == "done"
    assert "is not a reason" in json.dumps(r["calls"][0]["result"])


def test_an_unknown_tool_name_is_refused_with_the_real_list(conn, model, tools):
    r = _run(conn, [{"content": "", "tool_calls": [_tool_call("delete_everything")]},
                    {"content": "ok", "tool_calls": []}], model)
    assert "is not a tool you have" in json.dumps(r["calls"][0]["result"])


# ---------------------------------------------------------------- dry run

def test_dry_run_executes_no_writes(conn, model, tools):
    """Phase 5 of the rollout depends entirely on this: a week of would_have output read by
    hand before anything touches Arun's calendar."""
    r = _run(conn, [{"content": "", "tool_calls": [_tool_call("a_write", title="t",
                                                              reason="a good enough reason")]},
                    {"content": "ok", "tool_calls": []}], model, dry_run=True)
    assert [e for e in tools if e[0] == "write"] == []
    assert r["calls"][0]["result"]["would_have"] == "a_write"


def test_dry_run_still_allows_reads(conn, model, tools):
    _run(conn, [{"content": "", "tool_calls": [_tool_call("a_read", q="x")]},
                    {"content": "ok", "tool_calls": []}], model, dry_run=True)
    assert [e for e in tools if e[0] == "read"] == [("read", "x")]


# ---------------------------------------------------------------- provenance and caps

def test_the_run_id_is_injected_not_offered(conn, model, tools):
    """A model that can pass run_id can attribute its own write to another run, or to none."""
    _run(conn, [{"content": "", "tool_calls": [_tool_call("a_write", title="t",
                                                          reason="a good enough reason")]},
                {"content": "ok", "tool_calls": []}], model, run_id=4242)
    assert [e for e in tools if e[0] == "write"][0][2] == 4242


def test_no_schema_offers_conn_or_run_id_to_the_model():
    for name in registry.ALL:
        props = agent.schema_for(name)["function"]["parameters"]["properties"]
        assert not (set(props) & agent.INJECTED), f"{name} leaks an injected argument"


def test_every_registry_tool_produces_a_schema():
    assert len(agent.schemas(list(registry.ALL))) == len(registry.ALL)


def test_required_arguments_are_exactly_those_without_defaults():
    """Breaks silently the moment someone adds a parameter to create_reminder."""
    req = agent.schema_for("create_reminder")["function"]["parameters"]["required"]
    assert set(req) == {"title", "reason"}


def test_the_autonomous_tier_cannot_be_handed_a_delete(conn, model):
    """Structural, not advisory: the set is built from a table without the names in it."""
    with pytest.raises(ValueError):
        agent.run(conn, job="t", trigger="t", system="s", user="u",
                  tool_names=["delete_reminder"])


def test_a_delete_named_at_dispatch_is_still_refused(conn, model, tools):
    """Even if the model invents the name mid-run, having read it in an email."""
    r = _run(conn, [{"content": "", "tool_calls": [_tool_call("delete_document", path="x")]},
                    {"content": "ok", "tool_calls": []}], model)
    assert "is not a tool you have" in json.dumps(r["calls"][0]["result"])


# ---------------------------------------------------------------- writes that are not writes

def test_a_call_that_changed_nothing_does_not_consume_the_write_cap(conn, model, tools,
                                                                    monkeypatch):
    """gemma4 sent an add_facts payload of the wrong shape; ingest_batch accepted it and
    recorded nothing; the cap counted both attempts anyway. The run ended believing it was
    finished having written nothing at all."""
    def wrote_nothing(conn, **kw):
        return {"entities": 0, "facts": 0, "edges": 0, "links": 0}
    monkeypatch.setitem(registry.AUTONOMOUS, "a_write", wrote_nothing)
    monkeypatch.setitem(registry.ALL, "a_write", wrote_nothing)
    monkeypatch.setitem(registry.WRITE, "a_write", wrote_nothing)
    r = _run(conn, [{"content": "", "tool_calls": [_tool_call("a_write", title=f"t{i}")]}
                    for i in range(3)] + [{"content": "ok", "tool_calls": []}],
             model, max_writes=2)
    assert r["writes"] == 0
    assert r["stop"] == "done", "an empty write must not exhaust the cap"


def test_facts_actually_recorded_do_consume_it(conn, model, tools, monkeypatch):
    """The tallies come back at the top level, so they have to be read rather than looked
    for -- testing for the KEYS reported 'nothing happened' every time facts were written."""
    def wrote_two(conn, **kw):
        return {"entities": 1, "facts": 2, "edges": 0, "links": 0}
    monkeypatch.setitem(registry.AUTONOMOUS, "a_write", wrote_two)
    monkeypatch.setitem(registry.ALL, "a_write", wrote_two)
    monkeypatch.setitem(registry.WRITE, "a_write", wrote_two)
    r = _run(conn, [{"content": "", "tool_calls": [_tool_call("a_write", title="t")]},
                    {"content": "ok", "tool_calls": []}], model)
    assert r["writes"] == 1


def test_a_nested_payload_tool_is_described_not_left_as_a_bare_object():
    """A signature says `payload: dict` and nothing else, so the model is told a dict goes
    here and nothing about its shape. The most important tool in the enrichment pass then
    silently does nothing."""
    props = agent.schema_for("add_facts")["function"]["parameters"]["properties"]
    entities = props["payload"]["properties"]["entities"]
    assert entities["type"] == "array"
    assert "predicate" in entities["items"]["properties"]["facts"]["items"]["properties"]


def test_a_suppressed_repeat_is_not_logged_as_a_second_write(conn, model, tools):
    """A suppressed repeat hands the first call's result back to the model, so it carries no
    error and reads exactly like another write. Reporting it as one made a dry run claim four
    writes where the loop had correctly performed two -- and a dry-run log that overstates
    what would have happened is worse than none."""
    same = _tool_call("a_write", title="once", reason="a perfectly good reason")
    r = _run(conn, [{"content": "", "tool_calls": [same]},
                    {"content": "", "tool_calls": [same]},
                    {"content": "ok", "tool_calls": []}], model, max_writes=4)
    with db.run(conn, "test", trigger="t") as run_id:
        agent.record(conn, run_id, r)
        row = conn.execute("SELECT detail FROM run_log WHERE id = ?", (run_id,)).fetchone()
    detail = json.loads(row["detail"])
    assert detail["writes"] == 1
    assert len(detail["wrote"]) == 1, "the suppressed repeat was logged as a write"


def test_a_failed_call_is_logged_with_the_arguments_it_used(conn, model, tools):
    """Names alone say a tool failed and not what it was asked. 'mail_read failed three times'
    and 'it passed a College message with the Work account' are different bug reports."""
    r = _run(conn, [{"content": "", "tool_calls": [_tool_call("nope", account="Work")]},
                    {"content": "ok", "tool_calls": []}], model)
    with db.run(conn, "test", trigger="t") as run_id:
        agent.record(conn, run_id, r)
        row = conn.execute("SELECT detail FROM run_log WHERE id = ?", (run_id,)).fetchone()
    failures = json.loads(row["detail"])["failures"]
    assert failures and failures[0]["args"] == {"account": "Work"}


def test_a_run_that_narrates_a_plan_is_asked_once_to_finish(conn, model, tools):
    """A small model says what it is going to do and treats having said it as having done it.

    Watching the reactor decide about an email headed "Action Required - RSVP ... for TAMU
    Dell Night 2026": it read the body, judged it actionable, called already_scheduled, got
    back no matches at all -- and stopped, closing with "I need to check if this is already
    scheduled. I will check for an event titled ...". It had already checked. The answer was
    in front of it and it described the intention instead of using it.
    """
    r = _run(conn, [{"content": "", "tool_calls": [_tool_call("a_read", q="x")]},
                    {"content": "I will create a reminder for this.", "tool_calls": []},
                    {"content": "", "tool_calls": [_tool_call("a_write", title="t",
                                                              reason="a good enough reason")]},
                    {"content": "done", "tool_calls": []}], model)
    assert r["writes"] == 1, "the nudge did not get it to finish"
    assert [e for e in tools if e[0] == "write"]


def test_a_run_is_only_nudged_once(conn, model, tools):
    """A model that declines twice is declining. Pushing further would be arguing with it
    until it writes something to make the question stop."""
    r = _run(conn, [{"content": "", "tool_calls": [_tool_call("a_read", q="x")]},
                    {"content": "I will do it.", "tool_calls": []},
                    {"content": "I will really do it.", "tool_calls": []}], model)
    assert r["stop"] == "done"
    assert r["writes"] == 0


def test_a_run_that_simply_concluded_is_not_nudged(conn, model, tools):
    """Doing nothing is a valid and frequent outcome. Only an unfinished INTENTION is pushed
    on -- a plain conclusion is the answer."""
    r = _run(conn, [{"content": "", "tool_calls": [_tool_call("a_read", q="x")]},
                    {"content": "This is a newsletter. No action needed.", "tool_calls": []}],
             model)
    assert r["stop"] == "done"
    assert r["turns"] == 1


def test_ambiguous_parameters_are_described_to_the_model():
    """A derived schema gives every string the same shape, so `account` and `mailbox` arrived
    indistinguishable and gemma4 filled both with "College" -- ten wasted calls in the first
    live hour, each burning a turn."""
    props = agent.schema_for("mail_links")["function"]["parameters"]["properties"]
    assert "NOT the account name" in props["mailbox"]["description"]
    assert "Work, College, Personal or iCloud" in props["account"]["description"]
