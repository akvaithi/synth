"""The only code in Synth reachable from the internet.

A token issued here can create reminders, draft email and write documents, because
http_server builds the tool set with writable=True. So the negative cases matter more than
the happy path: each test below is something a caller can try on its own, and each one was
possible against the first version of this module.
"""
from __future__ import annotations

import base64
import hashlib
import secrets

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from synth import auth, db

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def client(conn, db_path, monkeypatch):
    """A real Starlette app over the temporary database, with Access stubbed as satisfied.

    Each request opens its own connection to the same file rather than borrowing the test's:
    TestClient serves the app on another thread, and a sqlite3 connection belongs to the
    thread that made it. The endpoints close what they open, exactly as they do in the server.
    """
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda *a, **k: real_connect(db_path))
    monkeypatch.setattr(auth, "REQUIRE_ACCESS", False)
    app = Starlette(routes=[
        Route("/register", auth.register, methods=["POST"]),
        Route("/authorize", auth.authorize),
        Route("/token", auth.token, methods=["POST"]),
    ])
    return TestClient(app)


def _register(client, uris=(REDIRECT,)):
    r = client.post("/register", json={"client_name": "Claude", "redirect_uris": list(uris)})
    assert r.status_code == 201
    return r.json()


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _authorize(client, reg, challenge, method="S256", **over):
    params = {"client_id": reg["client_id"], "redirect_uri": REDIRECT,
              "state": "xyz", "code_challenge": challenge,
              "code_challenge_method": method}
    params.update(over)
    params = {k: v for k, v in params.items() if v is not None}
    r = client.get("/authorize", params=params, follow_redirects=False)
    return r


def _code_from(response) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(response.headers["location"]).query)["code"][0]


# ---------------------------------------------------------------- the happy path

def test_a_full_pkce_flow_issues_a_working_token(client, conn):
    reg = _register(client)
    verifier, challenge = _pkce()
    code = _code_from(_authorize(client, reg, challenge))

    r = client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": reg["client_id"], "client_secret": reg["client_secret"],
        "redirect_uri": REDIRECT})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert auth.valid_token(body["access_token"])
    assert body["refresh_token"]


def test_the_state_is_returned_to_the_client(client):
    reg = _register(client)
    _, challenge = _pkce()
    assert "state=xyz" in _authorize(client, reg, challenge).headers["location"]


# ---------------------------------------------------------------- what is stored

def test_no_replayable_secret_is_written_to_the_database(client, conn):
    """synth.db holds the UIN, the date of birth and the home address alongside these."""
    reg = _register(client)
    verifier, challenge = _pkce()
    code = _code_from(_authorize(client, reg, challenge))
    token = client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": reg["client_id"], "client_secret": reg["client_secret"]}).json()

    stored = " ".join(str(v) for row in conn.execute(
        "SELECT * FROM oauth_client UNION ALL SELECT * FROM oauth_client") for v in row)
    assert reg["client_secret"] not in stored
    for column, value in (("token", token["access_token"]),
                          ("refresh_token", token["refresh_token"])):
        assert conn.execute(f"SELECT count(*) FROM oauth_token WHERE {column} = ?",
                            (value,)).fetchone()[0] == 0
        assert conn.execute(f"SELECT count(*) FROM oauth_token WHERE {column} = ?",
                            (auth._hash(value),)).fetchone()[0] == 1


# ---------------------------------------------------------------- PKCE is mandatory

def test_authorize_refuses_to_issue_a_code_without_a_challenge(client):
    """The downgrade the first version allowed: omit code_challenge at /authorize and the
    check at /token was skipped entirely, because it ran only `if row["challenge"]`."""
    reg = _register(client)
    r = _authorize(client, reg, challenge=None)
    assert r.status_code == 400
    assert "code_challenge is required" in r.json()["error_description"]


def test_authorize_refuses_the_plain_challenge_method(client):
    reg = _register(client)
    _, challenge = _pkce()
    r = _authorize(client, reg, challenge, method="plain")
    assert r.status_code == 400
    assert "S256" in r.json()["error_description"]


def test_a_wrong_verifier_is_refused(client):
    reg = _register(client)
    _, challenge = _pkce()
    code = _code_from(_authorize(client, reg, challenge))

    r = client.post("/token", data={
        "grant_type": "authorization_code", "code": code,
        "code_verifier": "not the verifier", "client_id": reg["client_id"],
        "client_secret": reg["client_secret"]})
    assert r.status_code == 400
    assert "PKCE" in r.json()["error_description"]


def test_a_code_predating_the_pkce_rule_cannot_be_redeemed(client, conn):
    """Belt and braces: /authorize will not mint one, but a row already in the table must
    not sail through unverified either -- that is the hole being closed."""
    reg = _register(client)
    conn.execute("INSERT INTO oauth_code (code, client_id, redirect_uri, subject, expires_at) "
                 "VALUES (?,?,?,?,?)",
                 (auth._hash("legacy"), reg["client_id"], REDIRECT, "someone",
                  auth._iso(auth._now() + auth.CODE_TTL)))
    conn.commit()

    r = client.post("/token", data={"grant_type": "authorization_code", "code": "legacy",
                                    "client_id": reg["client_id"]})
    assert r.status_code == 400
    assert "did not use PKCE" in r.json()["error_description"]


# ---------------------------------------------------------------- binding the code

def test_a_code_cannot_be_redeemed_by_another_registered_client(client):
    """RFC 6749 4.1.3. Without this a code leaked to one client is spendable by any other."""
    victim = _register(client)
    attacker = _register(client)
    verifier, challenge = _pkce()
    code = _code_from(_authorize(client, victim, challenge))

    r = client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": attacker["client_id"], "client_secret": attacker["client_secret"]})
    assert r.status_code == 400
    assert "not issued to this client" in r.json()["error_description"]


def test_a_mismatched_redirect_uri_is_refused_at_the_token_endpoint(client):
    reg = _register(client, uris=[REDIRECT, "https://claude.ai/other"])
    verifier, challenge = _pkce()
    code = _code_from(_authorize(client, reg, challenge))

    r = client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": reg["client_id"], "client_secret": reg["client_secret"],
        "redirect_uri": "https://claude.ai/other"})
    assert r.status_code == 400
    assert "redirect_uri does not match" in r.json()["error_description"]


def test_an_unregistered_redirect_uri_never_gets_a_code(client):
    reg = _register(client)
    _, challenge = _pkce()
    r = _authorize(client, reg, challenge, redirect_uri="https://evil.example/callback")
    assert r.status_code == 400
    assert "not registered" in r.json()["error_description"]


def test_an_unknown_client_gets_no_code(client):
    _, challenge = _pkce()
    r = client.get("/authorize", params={"client_id": "synth-nope", "redirect_uri": REDIRECT,
                                         "code_challenge": challenge},
                   follow_redirects=False)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_client"


# ---------------------------------------------------------------- client authentication

def test_a_wrong_client_secret_is_refused(client):
    reg = _register(client)
    verifier, challenge = _pkce()
    code = _code_from(_authorize(client, reg, challenge))

    r = client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": reg["client_id"], "client_secret": "wrong"})
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"


# ---------------------------------------------------------------- single use

def test_a_code_cannot_be_replayed(client):
    reg = _register(client)
    verifier, challenge = _pkce()
    code = _code_from(_authorize(client, reg, challenge))
    data = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
            "client_id": reg["client_id"], "client_secret": reg["client_secret"]}

    assert client.post("/token", data=data).status_code == 200
    assert client.post("/token", data=data).status_code == 400


def test_an_expired_code_is_refused(client, conn):
    reg = _register(client)
    verifier, challenge = _pkce()
    code = _code_from(_authorize(client, reg, challenge))
    conn.execute("UPDATE oauth_code SET expires_at = ?", ("2020-01-01T00:00:00+00:00",))
    conn.commit()

    r = client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": reg["client_id"], "client_secret": reg["client_secret"]})
    assert "expired" in r.json()["error_description"]


# ---------------------------------------------------------------- refresh

def test_a_refresh_token_buys_a_new_pair(client):
    """Advertised in grant_types_supported from the first day with nothing behind it, so the
    connector died silently at TOKEN_TTL with no way to renew."""
    reg = _register(client)
    verifier, challenge = _pkce()
    code = _code_from(_authorize(client, reg, challenge))
    first = client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": reg["client_id"], "client_secret": reg["client_secret"]}).json()

    r = client.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": first["refresh_token"],
        "client_id": reg["client_id"], "client_secret": reg["client_secret"]})
    assert r.status_code == 200
    second = r.json()
    assert auth.valid_token(second["access_token"])
    assert second["refresh_token"] != first["refresh_token"]


def test_refreshing_rotates_and_burns_the_token_presented(client):
    """Rotation is what makes theft visible: the thief's first use breaks the real client."""
    reg = _register(client)
    verifier, challenge = _pkce()
    code = _code_from(_authorize(client, reg, challenge))
    first = client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": reg["client_id"], "client_secret": reg["client_secret"]}).json()
    data = {"grant_type": "refresh_token", "refresh_token": first["refresh_token"],
            "client_id": reg["client_id"], "client_secret": reg["client_secret"]}

    assert client.post("/token", data=data).status_code == 200
    assert client.post("/token", data=data).status_code == 400
    assert not auth.valid_token(first["access_token"]), "the rotated-away token still works"


def test_an_unknown_refresh_token_is_refused(client):
    reg = _register(client)
    r = client.post("/token", data={"grant_type": "refresh_token",
                                    "refresh_token": "made up",
                                    "client_id": reg["client_id"]})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_an_unsupported_grant_type_says_so(client):
    r = client.post("/token", data={"grant_type": "password"})
    assert r.json()["error"] == "unsupported_grant_type"


# ---------------------------------------------------------------- registration

def test_registration_requires_a_redirect_uri(client):
    r = client.post("/register", json={"client_name": "Claude"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_client_metadata"


def test_registration_is_capped_because_the_endpoint_is_unauthenticated(client, monkeypatch):
    monkeypatch.setattr(auth, "MAX_REGISTRATIONS_PER_HOUR", 2)
    assert client.post("/register", json={"redirect_uris": [REDIRECT]}).status_code == 201
    assert client.post("/register", json={"redirect_uris": [REDIRECT]}).status_code == 201
    assert client.post("/register", json={"redirect_uris": [REDIRECT]}).status_code == 429


# ---------------------------------------------------------------- housekeeping

def test_spent_codes_do_not_accumulate(client, conn):
    reg = _register(client)
    for _ in range(3):
        verifier, challenge = _pkce()
        code = _code_from(_authorize(client, reg, challenge))
        client.post("/token", data={
            "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
            "client_id": reg["client_id"], "client_secret": reg["client_secret"]})

    _, challenge = _pkce()
    _authorize(client, reg, challenge)
    live = conn.execute("SELECT count(*) FROM oauth_code").fetchone()[0]
    assert live == 1, "used codes were left in the table"


def test_a_revoked_token_stops_working(client, conn):
    reg = _register(client)
    verifier, challenge = _pkce()
    code = _code_from(_authorize(client, reg, challenge))
    body = client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": reg["client_id"], "client_secret": reg["client_secret"]}).json()

    conn.execute("UPDATE oauth_token SET revoked_at = ?", (auth._iso(auth._now()),))
    conn.commit()
    assert not auth.valid_token(body["access_token"])


def test_an_unknown_bearer_token_is_not_valid(client):
    assert not auth.valid_token("anything at all")
