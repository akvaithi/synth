"""Local inference: the transport, and nothing that costs money.

This is deliberately not part of runner.py. Everything runner.py does is about *paying* for a
call -- budget.allowed before it, note_failure parsing refusal text after it, usage_record
pulling total_cost_usd into the ledger budget._sum reads. Local inference has no cost, no
session limit, no refusal text to parse, and an entirely different failure mode: a tunnel that
is down, a model being reloaded, a wall clock. Folding both behind one backend flag would make
every caller branch anyway, and writing `total_cost_usd: 0` rows into run_log would make
`synth budget` quietly lie about how many runs there were.

## Two things measured on the box that the code depends on

**The GPU holds about 8 GB.** qwen3.6 is 36B and 23.2 GB on disk; Ollama places 5.6 GB of it
on the GPU and runs the other three quarters on the CPU, which is why it benchmarks at 31
tok/s -- three times SLOWER than gemma4, an 8B model that fits entirely in VRAM at 90 tok/s.
The tempting model is the wrong one. Everything here runs on models that fit.

**num_ctx defaults to 4096 and truncates in silence.** An enrichment batch is 60,000
characters, roughly 15,000 tokens; at the default the model would see a quarter of it, answer
confidently about the part it saw, and report success. It is set explicitly on every call.
gemma4 at 32k stays 100% on the GPU (3.3 GB) and loses no speed, so there is no tradeoff to
manage -- only a default to override.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from synth import db

# The cloudflared forward, not the hostname. See bin/ollama-tunnel: the Access app is
# TCP-mode, so https://ollama.akvaithi.page returns an empty 200 on every path. A health
# check pointed at the hostname therefore passes while nothing works at all.
PORT = os.environ.get("SYNTH_OLLAMA_PORT", "11435")
BASE = os.environ.get("SYNTH_OLLAMA_URL", f"http://127.0.0.1:{PORT}")

TIERS = {
    # 8B, tools + thinking, 100% GPU-resident, ~90 tok/s. The workhorse.
    "fast": os.environ.get("SYNTH_OLLAMA_FAST", "gemma4:latest"),
    # Held separate so a heavier model can be pointed at the jobs that can afford one without
    # moving the hot path onto it. Same model until something bigger actually fits in VRAM.
    "reason": os.environ.get("SYNTH_OLLAMA_REASON", "gemma4:latest"),
    "embed": os.environ.get("SYNTH_OLLAMA_EMBED", "nomic-embed-text"),
}

NUM_CTX = int(os.environ.get("SYNTH_OLLAMA_NUM_CTX", "32768"))

# Cold load is 23-31s; a burst of five messages should pay it once, not five times.
KEEP_ALIVE = os.environ.get("SYNTH_OLLAMA_KEEP_ALIVE", "30m")

TIMEOUT_FAST = 120
TIMEOUT_REASON = 300
TIMEOUT_EMBED = 300
HEALTH_TIMEOUT = 3.0

EMBED_BATCH = 32          # measured: 31.6 chunks/s at this size, 5.1 at batch 8
EMBED_DIMS = 768          # nomic-embed-text


class OllamaError(RuntimeError):
    """Anything that went wrong talking to local inference."""


class OllamaDown(OllamaError):
    """The tunnel, the edge, or the daemon is not there."""


class OllamaTimeout(OllamaError):
    """Connected, and did not finish in time."""


class OllamaUnparseable(OllamaError):
    """A 200 that was not the shape that was asked for."""


def _post(path: str, body: dict, timeout: float) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise OllamaError(f"{path} returned {e.code}: "
                          f"{e.read()[:400].decode('utf-8', 'replace')}") from e
    except TimeoutError as e:
        raise OllamaTimeout(f"{path} did not answer within {timeout:.0f}s") from e
    except urllib.error.URLError as e:
        raise OllamaDown(f"{BASE} is not reachable: {e.reason}. Is the "
                         f"page.akvaithi.synth.ollama-tunnel job running?") from e
    except OSError as e:
        raise OllamaDown(f"{BASE} is not reachable: {e}") from e
    if not raw:
        # The signature of talking to the Access hostname instead of the local forward.
        raise OllamaDown(f"{path} returned an empty body — this is what the TCP-mode Access "
                         f"app answers to plain HTTP. Check that BASE is the cloudflared "
                         f"forward on 127.0.0.1 and not ollama.akvaithi.page.")
    try:
        return json.loads(raw)
    except ValueError as e:
        raise OllamaUnparseable(f"{path} did not return JSON: "
                                f"{raw[:400].decode('utf-8', 'replace')}") from e


# ---------------------------------------------------------------- health


def health(timeout: float = HEALTH_TIMEOUT) -> dict:
    """Is the whole chain up? One call proves the forward is listening, the edge is up and
    ollamad answered, which is the cheapest possible end-to-end probe."""
    try:
        req = urllib.request.Request(BASE + "/api/version")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        version = json.loads(raw).get("version") if raw else None
        if not version:
            return {"up": False, "error": "empty response (is BASE the local forward?)"}
        return {"up": True, "version": version}
    except Exception as e:
        return {"up": False, "error": f"{type(e).__name__}: {e}"}


def record_health(conn, service: str = "ollama", probe: dict | None = None) -> dict:
    """Write a probe into service_health, moving `since` only when the state flips.

    Overwriting `since` on every probe is how a warning meant to fire after sustained
    downtime never fires: the outage is permanently one probe old.
    """
    probe = health() if probe is None else probe
    up = 1 if probe.get("up") else 0
    detail = probe.get("version") if up else probe.get("error")
    now = db.now()
    row = conn.execute("SELECT up, since, last_ok_at, consecutive_bad FROM service_health "
                       "WHERE service = ?", (service,)).fetchone()
    if row is None:
        since, bad = now, 0 if up else 1
        last_ok = now if up else None
    else:
        flipped = bool(row["up"]) != bool(up)
        since = now if flipped else (row["since"] or now)
        bad = 0 if up else (row["consecutive_bad"] or 0) + 1
        last_ok = now if up else row["last_ok_at"]
    conn.execute(
        "INSERT INTO service_health (service, up, checked_at, since, last_ok_at, "
        "consecutive_bad, detail) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT (service) DO UPDATE SET up=excluded.up, checked_at=excluded.checked_at, "
        "since=excluded.since, last_ok_at=excluded.last_ok_at, "
        "consecutive_bad=excluded.consecutive_bad, detail=excluded.detail",
        (service, up, now, since, last_ok, bad, detail))
    conn.commit()
    return {"up": bool(up), "since": since, "consecutive_bad": bad, "detail": detail}


def degraded(conn, service: str = "ollama") -> str | None:
    """One sentence about an outage, or None. For a brief or a reminder."""
    row = conn.execute("SELECT up, since, detail FROM service_health WHERE service = ?",
                       (service,)).fetchone()
    if row is None or row["up"]:
        return None
    return (f"{service} has been unreachable since {db.local(row['since'])} "
            f"({row['detail']}). Anything that needs it is queued, not lost.")


# ---------------------------------------------------------------- generation


def _model(tier: str) -> str:
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}; have {sorted(TIERS)}")
    return TIERS[tier]


def generate(prompt: str, *, tier: str = "fast", system: str | None = None,
             format: str | dict | None = None, num_ctx: int = NUM_CTX,
             options: dict | None = None, keep_alive: str = KEEP_ALIVE,
             think: bool = False, timeout: float | None = None) -> dict:
    """One completion. `format` takes a JSON Schema object, not just the string "json"."""
    opts = {"num_ctx": num_ctx, "temperature": 0}
    opts.update(options or {})
    body = {"model": _model(tier), "prompt": prompt, "stream": False,
            "options": opts, "keep_alive": keep_alive, "think": think}
    if system:
        body["system"] = system
    if format is not None:
        body["format"] = format
    return _post("/api/generate", body,
                 timeout or (TIMEOUT_FAST if tier == "fast" else TIMEOUT_REASON))


def generate_json(prompt: str, schema: dict, **kw) -> dict:
    """A completion constrained to a schema, parsed.

    Schema-constrained decoding removes the class of failure where the answer has to be found
    inside prose. What it does NOT do, and both of these were measured rather than assumed:

    * It does not guarantee truth. Asked for a date with `{"type": ["string", "null"]}`,
      gemma4 returned the literal string "date" -- perfectly valid against the schema.
    * **It does not enforce `pattern`.** Asked for a date matching `^\\d{4}-\\d{2}-\\d{2}$`,
      it returned "30 September 2026". Only the JSON *type* is constrained by the decoder.

    So a schema buys the shape of the answer and nothing about its contents. Every caller
    validates and normalises what comes back -- a date especially, because a wrong one that
    parses is how a reminder lands on the wrong day.
    """
    result = generate(prompt, format=schema, **kw)
    text = result.get("response") or ""
    try:
        return json.loads(text)
    except ValueError as e:
        raise OllamaUnparseable(f"schema-constrained output was not JSON: {text[:300]}") from e


def chat(messages: list[dict], *, tier: str = "fast", tools: list[dict] | None = None,
         num_ctx: int = NUM_CTX, options: dict | None = None,
         keep_alive: str = KEEP_ALIVE, think: bool = False,
         timeout: float | None = None) -> dict:
    """A chat turn, optionally offering tools. Returns the raw response.

    The caller owns the message history, including feeding tool results back in.
    """
    opts = {"num_ctx": num_ctx, "temperature": 0}
    opts.update(options or {})
    body = {"model": _model(tier), "messages": messages, "stream": False,
            "options": opts, "keep_alive": keep_alive, "think": think}
    if tools:
        body["tools"] = tools
    return _post("/api/chat", body,
                 timeout or (TIMEOUT_FAST if tier == "fast" else TIMEOUT_REASON))


# ---------------------------------------------------------------- embeddings


def embed(texts: list[str], *, is_query: bool = False, batch: int = EMBED_BATCH,
          keep_alive: str = "5m", timeout: float = TIMEOUT_EMBED) -> list[list[float]]:
    """Embed texts, in batches.

    nomic-embed-text is asymmetric: it wants "search_document: " in front of a stored chunk
    and "search_query: " in front of a question. Getting this wrong does not fail, it just
    quietly retrieves worse, so the prefix scheme is recorded in embedding.model and the
    choice is made here rather than by each caller.
    """
    if not texts:
        return []
    prefix = "search_query: " if is_query else "search_document: "
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        chunk = [prefix + t for t in texts[i:i + batch]]
        result = _post("/api/embed",
                       {"model": TIERS["embed"], "input": chunk, "keep_alive": keep_alive},
                       timeout)
        vectors = result.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(chunk):
            raise OllamaUnparseable(
                f"asked for {len(chunk)} embeddings and got "
                f"{len(vectors) if isinstance(vectors, list) else type(vectors).__name__}")
        out.extend(vectors)
    return out


def model_prefix_scheme() -> str:
    """What goes in embedding.model, so a change of scheme is detectable rather than silent."""
    return f"{TIERS['embed']}/prefixed"


# ---------------------------------------------------------------- diagnostics


def loaded() -> list[dict]:
    """What is resident, and how much of it is actually on the GPU.

    size_vram well below size means the model is being run largely on the CPU, which is the
    difference between 90 tok/s and 31. It is invisible from the outside -- the only symptom
    is that everything is slow -- so it is worth being able to ask.
    """
    try:
        with urllib.request.urlopen(BASE + "/api/ps", timeout=10) as r:
            raw = r.read()
    except Exception as e:
        raise OllamaDown(str(e)) from e
    out = []
    for m in (json.loads(raw).get("models") or []) if raw else []:
        size, vram = m.get("size") or 0, m.get("size_vram") or 0
        out.append({"name": m.get("name"), "size": size, "vram": vram,
                    "on_gpu": round(vram / size * 100) if size else 0,
                    "context": m.get("context_length")})
    return out


def wait_until_up(seconds: float = 30.0, interval: float = 1.0) -> bool:
    """For a caller that has just started the tunnel and needs it before continuing."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if health().get("up"):
            return True
        time.sleep(interval)
    return False
