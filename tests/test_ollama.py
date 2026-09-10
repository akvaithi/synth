"""The client for local inference, and the three traps it exists to not fall into.

Nothing here touches the network. The transport is stubbed, because the point of these tests
is the decisions made around a call, not the call.
"""
from __future__ import annotations

import json

import pytest

from synth import ollama


@pytest.fixture
def sent(monkeypatch):
    """Capture the body of every request instead of making one."""
    calls = []

    def fake_post(path, body, timeout):
        calls.append({"path": path, "body": body, "timeout": timeout})
        if path == "/api/embed":
            return {"embeddings": [[0.0] * ollama.EMBED_DIMS] * len(body["input"])}
        return {"response": "{}"}

    monkeypatch.setattr(ollama, "_post", fake_post)
    return calls


# ---------------------------------------------------------------- the silent truncation

def test_every_generation_sets_num_ctx_explicitly(sent):
    """num_ctx defaults to 4096. An enrichment batch is 60,000 characters -- roughly 15,000
    tokens -- so at the default the model sees a quarter of its input, answers confidently
    about that quarter, and reports success. Nothing anywhere would say so."""
    ollama.generate("hello")
    assert sent[0]["body"]["options"]["num_ctx"] == ollama.NUM_CTX
    assert ollama.NUM_CTX > 4096


def test_every_chat_turn_sets_num_ctx_explicitly(sent):
    ollama.chat([{"role": "user", "content": "hi"}])
    assert sent[0]["body"]["options"]["num_ctx"] == ollama.NUM_CTX


def test_a_caller_may_lower_num_ctx_but_never_by_accident(sent):
    ollama.generate("hello", num_ctx=8192)
    assert sent[0]["body"]["options"]["num_ctx"] == 8192


# ---------------------------------------------------------------- the empty 200

def test_an_empty_body_is_reported_as_the_tcp_mode_trap(monkeypatch):
    """ollama.akvaithi.page is a TCP-mode Access app: plain HTTPS to it authenticates and
    returns an empty 200 on every path, including /api/tags. Pointed at the hostname instead
    of the local forward, everything 'succeeds' and returns nothing -- so the empty body has
    to name its own cause or the next person loses an hour to it."""
    class FakeResponse:
        def read(self):
            return b""
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ollama.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    with pytest.raises(ollama.OllamaDown) as e:
        ollama._post("/api/generate", {}, 10)
    assert "empty body" in str(e.value)
    assert "127.0.0.1" in str(e.value)


def test_health_reports_an_empty_body_as_down(monkeypatch):
    class FakeResponse:
        def read(self):
            return b""
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ollama.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    assert ollama.health()["up"] is False


def test_an_unreachable_forward_is_down_not_a_bare_oserror(monkeypatch):
    def boom(*a, **k):
        raise OSError("Connection refused")
    monkeypatch.setattr(ollama.urllib.request, "urlopen", boom)
    with pytest.raises(ollama.OllamaDown):
        ollama._post("/api/generate", {}, 10)


# ---------------------------------------------------------------- schema output

def test_unparseable_schema_output_is_its_own_error(monkeypatch):
    monkeypatch.setattr(ollama, "generate", lambda *a, **k: {"response": "not json"})
    with pytest.raises(ollama.OllamaUnparseable):
        ollama.generate_json("x", {"type": "object"})


def test_a_schema_is_passed_through_as_an_object_not_the_string_json(sent):
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    ollama.generate("x", format=schema)
    assert sent[0]["body"]["format"] == schema


# ---------------------------------------------------------------- embeddings

def test_a_stored_chunk_and_a_question_get_different_prefixes(sent):
    """nomic-embed-text is asymmetric. Using one prefix for both does not fail -- it just
    retrieves measurably worse, for ever, with nothing to show for it."""
    ollama.embed(["a chunk"])
    ollama.embed(["a question"], is_query=True)
    assert sent[0]["body"]["input"] == ["search_document: a chunk"]
    assert sent[1]["body"]["input"] == ["search_query: a question"]


def test_embedding_is_batched_and_keeps_every_input(sent):
    vectors = ollama.embed([f"chunk {i}" for i in range(70)], batch=32)
    assert len(vectors) == 70
    assert [len(c["body"]["input"]) for c in sent] == [32, 32, 6]


def test_a_short_batch_of_vectors_is_refused(monkeypatch):
    """Silently returning fewer vectors than inputs would misalign every chunk after it
    against the wrong document."""
    monkeypatch.setattr(ollama, "_post", lambda p, b, t: {"embeddings": [[0.0]]})
    with pytest.raises(ollama.OllamaUnparseable):
        ollama.embed(["a", "b", "c"])


def test_embedding_nothing_costs_nothing(sent):
    assert ollama.embed([]) == []
    assert sent == []


def test_the_prefix_scheme_is_recorded_with_the_model():
    """A change of prefix scheme invalidates every stored vector. Recording it beside the
    model name is what makes that detectable instead of a slow unexplained decay in recall."""
    assert ollama.model_prefix_scheme().endswith("/prefixed")
    assert ollama.TIERS["embed"] in ollama.model_prefix_scheme()


# ---------------------------------------------------------------- service_health

def test_since_moves_only_when_the_state_flips(conn, monkeypatch):
    """Overwriting `since` on every probe is how a warning meant to fire after sustained
    downtime never fires at all: the outage is permanently one probe old.

    The clock is driven by hand because db.now() has one-second resolution and three probes
    in the same second cannot show the difference between holding a timestamp and rewriting
    it with an identical one.
    """
    ticks = iter([f"2026-09-09T10:0{i}:00+00:00" for i in range(9)])
    monkeypatch.setattr(ollama.db, "now", lambda: next(ticks))

    first = ollama.record_health(conn, "ollama", {"up": False, "error": "refused"})
    second = ollama.record_health(conn, "ollama", {"up": False, "error": "refused"})
    assert second["since"] == first["since"], "a continuing outage must keep its start time"
    assert second["consecutive_bad"] == 2

    recovered = ollama.record_health(conn, "ollama", {"up": True, "version": "0.33.3"})
    assert recovered["since"] != first["since"], "recovery is a flip and starts a new period"
    assert recovered["consecutive_bad"] == 0


def test_a_recovery_keeps_the_last_good_time(conn):
    ollama.record_health(conn, "ollama", {"up": True, "version": "0.33.3"})
    ollama.record_health(conn, "ollama", {"up": False, "error": "gone"})
    row = conn.execute("SELECT last_ok_at, up FROM service_health "
                       "WHERE service = 'ollama'").fetchone()
    assert row["up"] == 0
    assert row["last_ok_at"] is not None


def test_degraded_says_nothing_when_the_service_is_up(conn):
    ollama.record_health(conn, "ollama", {"up": True, "version": "0.33.3"})
    assert ollama.degraded(conn) is None


def test_degraded_is_one_sentence_naming_when_it_started(conn):
    ollama.record_health(conn, "ollama", {"up": False, "error": "connection refused"})
    msg = ollama.degraded(conn)
    assert "unreachable since" in msg
    assert "queued, not lost" in msg


def test_an_unknown_service_is_not_an_outage(conn):
    assert ollama.degraded(conn, "never-probed") is None


# ---------------------------------------------------------------- tiers

def test_an_unknown_tier_fails_loudly():
    with pytest.raises(ValueError):
        ollama.generate("x", tier="enormous")


def test_the_hot_path_runs_a_model_that_fits_in_vram():
    """The GPU holds about 8 GB. qwen3.6 is 23.2 GB and Ollama places 24% of it on the card,
    running the rest on the CPU -- which is why a 36B model benchmarks at 31 tok/s against
    gemma4's 90. Reaching for the biggest model available is the wrong instinct here."""
    assert "qwen3.6" not in ollama.TIERS["fast"]


def test_the_base_url_is_the_local_forward_not_the_access_hostname():
    assert "127.0.0.1" in ollama.BASE
    assert "akvaithi.page" not in ollama.BASE


def test_json_bodies_are_serialisable(sent):
    ollama.chat([{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}])
    json.dumps(sent[0]["body"])
