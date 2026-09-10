"""The line between what Arun can ask for and what Synth may do on its own.

Deletion is available because he is the one prompting. It is not available to anything running
unattended, and the way that is guaranteed is structural: the autonomous tool set is built
from a table that does not contain the names, so a model cannot reach one by asking for it.
"""
from __future__ import annotations

import pytest

from synth import mcp_server, registry


DELETE_NAMES = {"delete_reminder", "delete_event", "delete_note", "delete_document"}


# ---------------------------------------------------------------- the registry split

def test_the_autonomous_tier_contains_no_delete():
    """The whole permission model in one assertion."""
    assert DELETE_NAMES & set(registry.AUTONOMOUS) == set()


def test_the_full_registry_does_contain_them():
    assert DELETE_NAMES <= set(registry.ALL)


def test_delete_is_a_separate_table_not_extra_write_entries():
    """If these lived in WRITE, every future caller assembling 'reads and writes' for an
    unattended job would silently pick up the ability to delete."""
    assert DELETE_NAMES & set(registry.WRITE) == set()
    assert set(registry.DELETE) == DELETE_NAMES


def test_retract_reminder_stays_on_the_autonomous_side():
    """It is provenance-gated -- action_log must show Synth created that exact reminder -- so
    it is Synth cleaning up after itself, not removing anything of Arun's."""
    assert "retract_reminder" in registry.AUTONOMOUS


def test_every_delete_is_callable():
    assert all(callable(fn) for fn in registry.DELETE.values())


# ---------------------------------------------------------------- the MCP tiers

def _names(server):
    return {fn.__name__ for fn in server._added}


@pytest.fixture
def recording(monkeypatch):
    """Capture what build() registers, without standing up a real server."""
    class FakeServer:
        def __init__(self, name, instructions=""):
            self.name, self.instructions, self._added = name, instructions, []
        def add_tool(self, fn):
            self._added.append(fn)
    monkeypatch.setattr(mcp_server, "MCPServer", FakeServer)
    return FakeServer


def test_the_default_connection_gets_everything(recording):
    """Both callers of build() are sessions Arun drives -- the stdio server and the connector
    he reaches from the Claude app."""
    assert DELETE_NAMES <= _names(mcp_server.build())


def test_a_read_only_connection_gets_no_writes_and_no_deletes(recording):
    got = _names(mcp_server.build(caps=("read",)))
    assert DELETE_NAMES & got == set()
    assert "create_reminder" not in got
    assert "search_context" in got


def test_read_and_write_never_implies_delete(recording):
    """The case the old two-state flag could not express at all."""
    got = _names(mcp_server.build(caps=("read", "write")))
    assert "create_reminder" in got
    assert DELETE_NAMES & got == set()


def test_the_old_writable_flag_still_means_what_it_meant(recording):
    """An existing caller passing writable= must keep working, and must not be upgraded into
    a delete-capable server by the change."""
    got = _names(mcp_server.build(writable=True))
    assert "create_reminder" in got
    assert DELETE_NAMES & got == set()
    assert "search_context" in _names(mcp_server.build(writable=False))


def test_an_unknown_capability_is_refused(recording):
    with pytest.raises(ValueError):
        mcp_server.build(caps=("read", "root"))


# ---------------------------------------------------------------- what the server says

def test_a_delete_capable_connection_is_told_what_reverses_and_what_does_not(recording):
    """An undo that recreates with a new identifier is not an undo, and a model that believes
    otherwise will offer to put something back that it cannot."""
    body = mcp_server.build().instructions
    assert "delete_document" in body
    assert "NEW identifier" in body


def test_a_read_only_connection_is_told_not_to_promise_writes(recording):
    body = mcp_server.build(caps=("read",)).instructions
    assert "read-only" in body


def test_a_write_connection_is_not_told_about_deleting(recording):
    body = mcp_server.build(caps=("read", "write")).instructions
    assert "Deleting, on this connection" not in body


# ---------------------------------------------------------------- reversal honesty

def test_the_document_delete_is_the_only_one_that_truly_restores():
    """docversions keeps the bytes and the path does not change. The other three go through
    the daemon and come back with a new identifier."""
    from synth import db

    assert "delete_document" in db.PY_REVERSALS
    assert {"delete_reminder", "delete_event", "delete_note"} <= set(db.REVERSALS)


def test_every_delete_can_be_reversed_somehow():
    from synth import db

    reversible = set(db.REVERSALS) | set(db.PY_REVERSALS)
    assert DELETE_NAMES <= reversible, "a delete with no reversal at all is not acceptable"


# ---------------------------------------------------------------- what each audience is told

def test_the_connector_is_never_given_the_autonomy_rules(recording):
    """autonomy.md is a closed list of things to do WITHOUT being asked. A session Arun is
    driving has no such list, because it does nothing unasked -- handing it one would read as
    permission."""
    body = _flat(mcp_server.build().instructions)
    assert "list is closed" not in body
    assert "You are running without being asked" not in body


def test_the_connector_is_not_told_autonomy_is_gone():
    """This text shipped as the server's instructions while briefs were being delivered, the
    reactor was running and enrichment was writing facts. Being wrong in that direction is
    worse than being wrong in the other: it describes a system that no longer exists."""
    from synth import mcp_server as m

    body = m._instructions(m.FULL)
    assert "not coming back" not in body
    assert "only ever *record*, never act" not in body


def test_the_connector_says_what_actually_runs_unprompted(recording):
    body = mcp_server.build().instructions
    for real in ("The sweep", "The brief", "Enrichment", "The reactor"):
        assert real in body, f"the doctrine does not mention {real}"


def test_the_doctrine_agrees_with_the_job_that_actually_runs(recording):
    """The doctrine ships as the MCP server's instructions, so a stale sentence here is a
    lie told to the connector -- which is exactly what happened once already, when it said
    autonomy "is not coming back" while the reactor was running.

    Checked against the plist rather than against a fixed string, because the claim that
    matters is agreement: whichever way the flag is set, the doctrine has to say so. This
    fails if someone flips one without the other, in either direction.
    """
    import pathlib

    plist = pathlib.Path(__file__).parent.parent / "launchd" \
        / "page.akvaithi.synth.worker.plist"
    dry = "SYNTH_REACTOR_DRY_RUN" in plist.read_text()
    body = _flat(mcp_server.build().instructions)

    if dry:
        assert "in dry-run" in body, "the worker writes nothing and the doctrine implies it does"
    else:
        assert "in dry-run" not in body, "the worker writes and the doctrine claims dry-run"
        assert "it **writes**" in body, "the doctrine must say plainly that it writes"


def _flat(text: str) -> str:
    """Prose in these files is hard-wrapped, so a phrase can span a line break. Asserting on
    the raw text makes a test fail when someone reflows a paragraph."""
    return " ".join(text.split())


def test_unattended_jobs_get_the_autonomy_rules():
    from synth import runner

    for job in ("reactor", "brief", "enrich"):
        assert job in runner.UNATTENDED
    text = _flat(runner.prompt("reactor", core_only=True, events="(none)",
                               obligations="(none)"))
    assert "list is closed" in text
    assert "no web access at all" in text
    assert "Doing nothing is a valid and frequent outcome" in text


def test_the_autonomy_list_names_only_tools_the_reactor_actually_has():
    """A closed list that names a tool the loop will refuse teaches the model to reach for
    something it cannot have, and it gets a refusal it cannot act on."""
    from synth import reactor

    everything = set(reactor.MAIL_TOOLS + reactor.NOTE_TOOLS + reactor.SCHEDULE_TOOLS)
    for named in ("already_scheduled", "complete_reminder", "create_event", "add_facts",
                  "update_obligation", "read_invitation"):
        assert named in everything, f"autonomy.md promises {named} and the reactor lacks it"
    for withheld in ("draft_email", "create_note", "update_document", "undo",
                     "create_reminders"):
        assert withheld not in everything, f"{withheld} is excluded in prose but offered"
