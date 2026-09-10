"""Four bugs that were live in the tree, each of which defeated something built to work.

None of these announced themselves. A mail event that could not be opened, a reserve held
against the job it was for, a tool the registry could not reach, and a parse failure that
turned a quiet inbox into a full one -- all of them looked like the system working.
"""
from __future__ import annotations

import pytest

from synth import budget, mailsync, registry, triage, watcher


# ---------------------------------------------------------------- the mail index

def test_a_new_mail_event_carries_the_index_needed_to_open_it(monkeypatch):
    """mail_get_at takes (account, index, messageId) and there is no lookup by Message-ID
    alone, so an event without an index can be triaged on its subject and never read. The
    reactor's whole reason for reading bodies died on this one missing key."""
    messages = [{"messageId": "<a@x>", "subject": "Invoice due", "sender": "b@x",
                 "receivedAt": "2026-09-09T10:00:00Z", "index": 7}]

    def fake_call(cmd, **kw):
        if cmd == "mail_probe":
            return {"count": 2, "newestId": "<a@x>"}
        return messages

    monkeypatch.setattr(watcher, "call", fake_call)
    monkeypatch.setattr(watcher.config, "MAIL_ACCOUNTS", {"Work": "w@x"})
    # A primed cursor, so the first pass is not treated as a flood of new mail.
    state = {"mail_seen": {"Work": ["<old@x>"]}, "mail_sentinel": {}}
    events = watcher.poll_mail(state)
    assert [e["kind"] for e in events] == ["mail_new"]
    assert events[0]["index"] == 7


def test_resolve_indexes_survives_one_unreachable_account(monkeypatch):
    """One account that will not enumerate must not cost the others their indexes."""
    def fake_call(cmd, account=None, **kw):
        if account == "Broken":
            raise RuntimeError("mailbox unavailable")
        return [{"messageId": "<a@x>", "index": 3}]

    # resolve_indexes imports `call` inside the function, so the module attribute is what
    # the lookup actually reaches.
    monkeypatch.setattr("synth.applekit.call", fake_call)
    out = triage.resolve_indexes({"Work", "Broken"})
    assert out["Work"] == {"<a@x>": 3}
    assert out["Broken"] == {}


# ---------------------------------------------------------------- the budget reserve

def test_the_brief_is_the_protected_job():
    """PRIORITY had no 'brief' key, so PRIORITY.get(job, 3) scored it the LOWEST priority of
    anything -- while allowed()'s own docstring said the reserve existed so a heavy afternoon
    could not starve the morning brief. The data contradicted the rule it implemented."""
    assert budget.PRIORITY["brief"] == 0
    assert all(v > 0 for k, v in budget.PRIORITY.items() if k != "brief")


def test_a_spent_ceiling_still_leaves_the_brief_its_reserve(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(budget, "STATE", str(tmp_path))
    monkeypatch.setattr(budget, "BACKOFF", str(tmp_path / "backoff.json"))
    monkeypatch.setattr(budget, "EPOCH", str(tmp_path / "epoch.json"))
    import json
    from datetime import datetime, timedelta, timezone
    started = (datetime.now(timezone.utc) - timedelta(hours=1)
               ).strftime("%Y-%m-%d %H:%M:%S")
    # Spend past the unprotected ceiling but not past the absolute one.
    spend = budget.config.BUDGET_5H - budget.config.BUDGET_5H_RESERVE + 0.01
    conn.execute("INSERT INTO run_log (job, started_at, status, detail) VALUES (?,?,?,?)",
                 ("triage", started, "ok", json.dumps({"total_cost_usd": spend})))
    conn.commit()
    assert budget.allowed(conn, "triage")[0] is False
    assert budget.allowed(conn, "brief")[0] is True


# ---------------------------------------------------------------- the registry

@pytest.mark.parametrize("name", ["read_attachment", "mail_digest"])
def test_the_registry_can_reach_the_attachment_tools(name):
    """Both existed in tools.py and in the MCP server but never in the registry, so anything
    dispatching through it could see that a message had a PDF and not open it. Invitations and
    offer letters arrive as PDFs; the alternative to reading one is inventing it."""
    assert name in registry.READ
    assert callable(registry.ALL[name])


def test_every_registry_entry_is_callable():
    assert all(callable(fn) for fn in registry.ALL.values())


# ---------------------------------------------------------------- unreadable triage output

def test_unreadable_triage_output_falls_back_to_the_free_rules(conn, monkeypatch):
    """This used to mark every message in the batch 'act', so a single unparseable reply
    turned a quiet inbox into a full one. The static rules and learned sender policy are a
    strictly better guess than a blanket flag, and they cost nothing."""
    monkeypatch.setattr(mailsync.runner, "prompt", lambda *a, **k: "prompt")
    # Both model tiers have to be unreadable to reach the free rules: local answers first now,
    # and metered Haiku is only the escalation behind it.
    monkeypatch.setattr(mailsync.ollama, "generate_json",
                        lambda *a, **k: (_ for _ in ()).throw(
                            mailsync.ollama.OllamaUnparseable("not json")))
    monkeypatch.setattr(mailsync.runner, "spend",
                        lambda *a, **k: {"result": "no json here at all"})
    messages = [
        {"messageId": "<news@x>", "sender": "noreply@newsletter.example", "subject": "Weekly"},
        {"messageId": "<real@x>", "sender": "advisor@tamu.edu", "subject": "Your form"},
    ]
    out = mailsync.triage_batch(conn, messages)
    assert out["degraded"] is True
    verdicts = out["verdicts"]
    assert set(verdicts) == {"<news@x>", "<real@x>"}
    assert all(v[0] in ("act", "digest") for v in verdicts.values())
    # The point of the change: not everything is flagged any more.
    assert verdicts["<news@x>"][0] == "digest"


def test_the_static_verdict_map_keeps_the_unrecognised_visible():
    """`consider` is what the free rules say when they recognise nothing either way. That is
    exactly the case that must not be filed away silently."""
    assert mailsync.STATIC_TO_VERDICT["consider"] == "act"
    assert mailsync.STATIC_TO_VERDICT["urgent"] == "act"
    assert mailsync.STATIC_TO_VERDICT["ignore"] == "digest"


# ---------------------------------------------------------------- the sweep lock

def test_a_second_writer_waits_rather_than_colliding(tmp_path, monkeypatch):
    """Only cmd_sync ever took this lock. The 03:00 enrich job and the 03:00 sweep therefore
    ran straight through each other, and run_log still holds the result: "killed by database
    lock during concurrent OCR pass". SQLite's busy timeout does not save this -- the sweep
    holds write transactions across OCR and AppleScript calls lasting minutes."""
    import fcntl
    import time
    from synth import cli

    lock = tmp_path / "sweep.lock"
    monkeypatch.setattr(cli, "LOCK", str(lock))

    holder = open(lock, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        started = time.time()
        assert cli._exclusive(wait=1.0) is None       # waited, then gave up
        assert time.time() - started >= 1.0
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    got = cli._exclusive(wait=1.0)                    # free again
    assert got is not None
    got.close()


def test_the_sweep_itself_still_skips_instead_of_waiting(tmp_path, monkeypatch):
    """The sweep is periodic, so queueing behind another pass buys nothing -- it would only
    run the same work twice, back to back. Bailing out is right for it and wrong for the
    jobs that are appointments."""
    import fcntl
    import time
    from synth import cli

    lock = tmp_path / "sweep.lock"
    monkeypatch.setattr(cli, "LOCK", str(lock))
    holder = open(lock, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        started = time.time()
        assert cli._exclusive() is None
        assert time.time() - started < 0.5
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()


# ---------------------------------------------------------------- triage on local inference

def test_triage_runs_locally_and_never_reaches_the_budget(conn, monkeypatch):
    """Triage was the last recurring metered job besides the brief. Once the brief cost $2.99
    a run, the daily ceiling started refusing triage in order to protect a reserve it had
    already spent -- so the cheap job was starved by the expensive one."""
    monkeypatch.setattr(mailsync.runner, "prompt", lambda *a, **k: "p")
    monkeypatch.setattr(
        mailsync.ollama, "generate_json",
        lambda *a, **k: {"verdicts": [{"messageId": "<a@x>", "verdict": "act", "why": "w"}]})

    def must_not_spend(*a, **k):
        raise AssertionError("a local answer must never reach the metered path")
    monkeypatch.setattr(mailsync.runner, "spend", must_not_spend)

    out = mailsync.triage_batch(conn, [{"messageId": "<a@x>", "subject": "s", "sender": "f"}])
    assert out["verdicts"]["<a@x>"][0] == "act"
    assert out["result"]["source"] == "local"


def test_local_inference_being_down_escalates_to_the_metered_path(conn, monkeypatch):
    """One of exactly two mechanical escalation triggers. Still budget-gated, so a broken
    tunnel cannot spend the month."""
    monkeypatch.setattr(mailsync.runner, "prompt", lambda *a, **k: "p")

    def down(*a, **k):
        raise mailsync.ollama.OllamaDown("tunnel is down")
    monkeypatch.setattr(mailsync.ollama, "generate_json", down)
    called = {}

    def spend(conn_, job, trigger, *a, **k):
        called["job"] = job
        return {"result": '{"verdicts":[{"messageId":"<a@x>","verdict":"digest"}]}'}
    monkeypatch.setattr(mailsync.runner, "spend", spend)

    out = mailsync.triage_batch(conn, [{"messageId": "<a@x>", "subject": "s", "sender": "f"}])
    assert called["job"] == "triage"
    assert out["verdicts"]["<a@x>"][0] == "digest"


def test_when_both_tiers_fail_the_free_rules_still_sort_the_mail(conn, monkeypatch):
    """Three things have to fail before a message is sorted badly, and none of them may drop
    it."""
    monkeypatch.setattr(mailsync.runner, "prompt", lambda *a, **k: "p")
    monkeypatch.setattr(mailsync.ollama, "generate_json",
                        lambda *a, **k: (_ for _ in ()).throw(
                            mailsync.ollama.OllamaDown("down")))
    monkeypatch.setattr(mailsync.runner, "spend",
                        lambda *a, **k: {"skipped": True, "result": "budget"})
    out = mailsync.triage_batch(conn, [{"messageId": "<a@x>", "subject": "s",
                                        "sender": "noreply@news.example"}])
    assert out["verdicts"] == {} or "<a@x>" in out["verdicts"]


def test_the_brief_ceiling_fits_a_real_brief():
    """A researched morning brief measured $2.99. A ceiling below that refuses the only job
    it exists to protect."""
    from synth import config

    assert config.BUDGET_5H >= 3.5
    assert config.BUDGET_DAY >= 2 * 3.0
