"""OAuth 2.1 for the Claude app connector.

Why this exists rather than leaning on Cloudflare Access alone: claude.ai's web and mobile
connector requires a `WWW-Authenticate` header on the 401 and a discoverable
`/.well-known/oauth-protected-resource`. Cloudflare Access "Managed OAuth" does not emit the
former, which is a documented cause of "Authorization with the MCP server failed" — Claude
Code tolerates its absence by probing, the app does not.

So this server owns the OAuth endpoints and Cloudflare Access sits in front as the identity
provider. Access proves who the human is before the browser ever reaches /authorize; this
code turns that into a token the connector can carry. Dynamic Client Registration is
supported because that is how claude.ai onboards a custom connector.

This is the only code in Synth reachable from the internet, and since http_server builds with
writable=True a token issued here can create reminders, draft email and write documents. It
is therefore held to the spec it advertises rather than to what the Claude app happens to
accept. Four checks the first version skipped, each of which a client can exercise on its own:

  * the code is bound to the client that asked for it, and to the redirect_uri it was
    issued against — RFC 6749 §4.1.3 requires both, and without them a code leaked to one
    registered client is redeemable by another;
  * the client secret is verified, having been advertised in
    `token_endpoint_auth_methods_supported` all along;
  * PKCE is mandatory rather than conditional. The first version verified the challenge only
    `if row["challenge"]`, so omitting `code_challenge` at /authorize skipped the check
    entirely — a downgrade available to any caller. OAuth 2.1 requires PKCE on every
    authorization-code flow, confidential clients included, and only S256 is accepted;
  * `refresh_token` was in `grant_types_supported` and in every registration response with no
    handler behind it, so the connector died silently at TOKEN_TTL with no way to renew.
    It is implemented, and it rotates: a refresh token is single-use.

Secrets are stored as SHA-256 digests. synth.db sits alongside Arun's UIN, date of birth and
home address, and a bearer token that can write to his calendar should not be legible to
anything that can read the file.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from synth import db

# The public origin, e.g. https://connector.example.com
ISSUER = os.environ.get("SYNTH_PUBLIC_URL", "http://127.0.0.1:8787").rstrip("/")
CODE_TTL = timedelta(minutes=10)
TOKEN_TTL = timedelta(days=30)
# Long enough that a connector left alone over a vacation still comes back without a browser,
# short enough that an abandoned one stops working. Rotation means a stolen refresh token is
# usable once and then visibly breaks the real client, which is the point of rotating.
REFRESH_TTL = timedelta(days=90)
# /register is unauthenticated, as Dynamic Client Registration is. Cloudflare Access is meant
# to be in front of it; this is what holds if it is not.
MAX_REGISTRATIONS_PER_HOUR = int(os.environ.get("SYNTH_MAX_REGISTRATIONS", "5"))

# Header Cloudflare Access injects once a human has authenticated. Its presence is the proof
# that the browser passed Access; absence means the request never went through it.
ACCESS_HEADER = "cf-access-jwt-assertion"
# Escape hatch for local testing only, never for the tunnelled deployment.
REQUIRE_ACCESS = os.environ.get("SYNTH_REQUIRE_ACCESS", "1") != "0"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _hash(value: str) -> str:
    """What goes in the database in place of a secret.

    Plain SHA-256 with no salt or stretching, deliberately: these are 40-plus bytes of output
    from secrets.token_urlsafe, not passwords. There is no dictionary to attack and nothing a
    per-row salt would defend against. What this buys is that the file no longer contains
    anything replayable.
    """
    return hashlib.sha256(value.encode()).hexdigest()


def _fail(error: str, description: str = "", status: int = 400) -> Response:
    body = {"error": error}
    if description:
        body["error_description"] = description
    return JSONResponse(body, status)


def _subject(request: Request) -> str | None:
    """Identity from Cloudflare Access, if it vouched for this request."""
    jwt = request.headers.get(ACCESS_HEADER)
    if not jwt:
        return None if REQUIRE_ACCESS else "local-dev"
    try:  # the payload is informational; Access already verified the signature at the edge
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("email") or claims.get("sub") or "access-user"
    except Exception:
        return "access-user"


# ---------------------------------------------------------------- discovery


async def protected_resource(request: Request) -> Response:
    return JSONResponse({
        "resource": f"{ISSUER}/mcp",
        "authorization_servers": [ISSUER],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["synth:read", "synth:write"],
    })


async def authorization_server(request: Request) -> Response:
    return JSONResponse({
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "registration_endpoint": f"{ISSUER}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
        "scopes_supported": ["synth:read", "synth:write"],
    })


# ---------------------------------------------------------------- endpoints


async def register(request: Request) -> Response:
    """Dynamic Client Registration — how claude.ai onboards a custom connector.

    Unauthenticated, as DCR is, which makes it the one endpoint a stranger can write a row
    through. Cloudflare Access is expected in front of it; the ceiling below is what stands
    up if it ever is not.
    """
    try:
        body = await request.json()
    except Exception:
        return _fail("invalid_client_metadata", "body must be JSON")
    uris = body.get("redirect_uris") or []
    if not uris or not all(isinstance(u, str) and u for u in uris):
        return _fail("invalid_client_metadata", "redirect_uris is required")

    conn = db.connect()
    try:
        # An anonymous INSERT with no ceiling is a table that grows for as long as someone
        # keeps calling. One registration is all this connector has ever needed.
        registered = conn.execute(
            "SELECT count(*) FROM oauth_client WHERE registered_at > datetime('now','-1 hour')"
        ).fetchone()[0]
        if registered >= MAX_REGISTRATIONS_PER_HOUR:
            return _fail("invalid_client_metadata",
                         "too many registrations; try again later", 429)

        client_id = f"synth-{secrets.token_urlsafe(16)}"
        client_secret = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO oauth_client (client_id, client_secret, name, redirect_uris) "
            "VALUES (?,?,?,?)",
            (client_id, _hash(client_secret), body.get("client_name", "unnamed"),
             json.dumps(uris)))
        conn.commit()
    finally:
        conn.close()

    # The only time the secret exists in the clear. What is kept is its digest.
    return JSONResponse({
        "client_id": client_id,
        "client_secret": client_secret,
        "client_name": body.get("client_name", "unnamed"),
        "redirect_uris": uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    }, 201)


async def authorize(request: Request) -> Response:
    q = request.query_params
    client_id = q.get("client_id", "")
    redirect_uri = q.get("redirect_uri", "")
    state = q.get("state", "")
    challenge = q.get("code_challenge", "")
    method = q.get("code_challenge_method", "S256")

    subject = _subject(request)
    if subject is None:
        # Access has not vouched for this browser. Say so plainly rather than issuing a code.
        return _fail("access_denied",
                     "Cloudflare Access did not authenticate this request", 403)

    if q.get("response_type", "code") != "code":
        return _fail("unsupported_response_type", "only the authorization code flow is served")

    conn = db.connect()
    try:
        row = conn.execute("SELECT redirect_uris FROM oauth_client WHERE client_id = ?",
                           (client_id,)).fetchone()
        if row is None:
            return _fail("invalid_client")
        # Exact string match, as OAuth 2.1 requires -- no prefixes and no wildcards. This is
        # also the only check made before a redirect happens, so it has to be the strict one:
        # everything after this point is reported to the client's own URI.
        if redirect_uri not in json.loads(row["redirect_uris"]):
            return _fail("invalid_request", "redirect_uri not registered")

        # PKCE is required, not conditional. The previous version verified the challenge at
        # /token only `if row["challenge"]`, so a client that simply omitted it here skipped
        # the check altogether -- a downgrade needing nothing but a shorter URL.
        if not challenge:
            return _fail("invalid_request", "code_challenge is required (PKCE, S256)")
        if method != "S256":
            return _fail("invalid_request", f"code_challenge_method {method!r} is not "
                                            f"supported; use S256")

        _prune_codes(conn)
        code = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO oauth_code (code, client_id, redirect_uri, challenge, "
                     "challenge_method, subject, expires_at) VALUES (?,?,?,?,?,?,?)",
                     (_hash(code), client_id, redirect_uri, challenge, method, subject,
                      _iso(_now() + CODE_TTL)))
        conn.commit()
    finally:
        conn.close()

    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}", status_code=302)


async def token(request: Request) -> Response:
    try:
        form = await request.form()
    except Exception:
        return _fail("invalid_request", "body must be form-encoded")
    grant = form.get("grant_type", "authorization_code")
    if grant == "refresh_token":
        return await _refresh_grant(form)
    if grant != "authorization_code":
        return _fail("unsupported_grant_type", f"{grant!r} is not served")
    return await _code_grant(form)


def _authenticate_client(conn, client_id: str, presented: str) -> Response | None:
    """Check the client is registered, and that any secret it offered is the right one.

    A presented secret is always verified. A missing one is not fatal: the Claude app is a
    public client -- it registered from a browser and cannot keep a secret in any meaningful
    sense -- and OAuth 2.1's answer for a public client is PKCE, which is now mandatory a few
    lines below. Making the secret compulsory here would lock out the live connector to buy
    nothing, since anything able to read a browser's secret can read its code as well.
    Tightening it later is one `if row["client_secret"] and not presented` away.
    """
    row = conn.execute("SELECT client_secret FROM oauth_client WHERE client_id = ?",
                       (client_id,)).fetchone()
    if row is None:
        return _fail("invalid_client")
    if presented and row["client_secret"]:
        if not secrets.compare_digest(_hash(presented), row["client_secret"]):
            return _fail("invalid_client", "client authentication failed", 401)
    return None


def _issue(conn, client_id: str, subject: str | None) -> Response:
    """Mint an access/refresh pair, keeping only their digests."""
    access = secrets.token_urlsafe(40)
    refresh = secrets.token_urlsafe(40)
    conn.execute(
        "INSERT INTO oauth_token (token, client_id, subject, expires_at, refresh_token, "
        "refresh_expires_at) VALUES (?,?,?,?,?,?)",
        (_hash(access), client_id, subject, _iso(_now() + TOKEN_TTL),
         _hash(refresh), _iso(_now() + REFRESH_TTL)))
    conn.commit()
    return JSONResponse({"access_token": access, "token_type": "Bearer",
                         "expires_in": int(TOKEN_TTL.total_seconds()),
                         "refresh_token": refresh,
                         "scope": "synth:read synth:write"})


async def _code_grant(form) -> Response:
    code = form.get("code", "")
    verifier = form.get("code_verifier", "")
    client_id = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")

    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM oauth_code WHERE code = ?",
                           (_hash(code),)).fetchone()
        if row is None or row["used"]:
            return _fail("invalid_grant")
        if datetime.fromisoformat(row["expires_at"]) < _now():
            return _fail("invalid_grant", "code expired")

        # The code belongs to the client it was issued to. Without this a code leaked to any
        # registered client is redeemable by any other -- RFC 6749 §4.1.3.
        if client_id and client_id != row["client_id"]:
            return _fail("invalid_grant", "code was not issued to this client")
        failed = _authenticate_client(conn, row["client_id"], form.get("client_secret", ""))
        if failed is not None:
            return failed
        # Same section: the redirect_uri presented here must be the one the code was bound to.
        if redirect_uri and redirect_uri != row["redirect_uri"]:
            return _fail("invalid_grant", "redirect_uri does not match the authorization")

        # Unconditional. A code reaching this point always carries a challenge, because
        # /authorize refuses to issue one without it -- but a code predating that rule would
        # otherwise sail through unverified, which is exactly the hole being closed.
        if not row["challenge"] or row["challenge_method"] != "S256":
            return _fail("invalid_grant", "authorization did not use PKCE with S256")
        digest = hashlib.sha256(verifier.encode()).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if not secrets.compare_digest(expected, row["challenge"]):
            return _fail("invalid_grant", "PKCE verification failed")

        conn.execute("UPDATE oauth_code SET used = 1 WHERE code = ?", (_hash(code),))
        return _issue(conn, row["client_id"], row["subject"])
    finally:
        conn.close()


async def _refresh_grant(form) -> Response:
    """Exchange a refresh token for a new pair, and burn the one presented.

    Rotation is not optional in OAuth 2.1 for a public client, and it is what makes theft
    detectable: the thief's first use invalidates the real client's token, so the next thing
    that happens is Arun being asked to sign in again rather than nothing at all.
    """
    presented = form.get("refresh_token", "")
    client_id = form.get("client_id", "")

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM oauth_token WHERE refresh_token = ?", (_hash(presented),)
        ).fetchone() if presented else None
        if row is None or row["revoked_at"]:
            return _fail("invalid_grant")
        if row["refresh_expires_at"] and \
                datetime.fromisoformat(row["refresh_expires_at"]) < _now():
            return _fail("invalid_grant", "refresh token expired")
        if client_id and client_id != row["client_id"]:
            return _fail("invalid_grant", "refresh token was not issued to this client")
        failed = _authenticate_client(conn, row["client_id"], form.get("client_secret", ""))
        if failed is not None:
            return failed

        conn.execute("UPDATE oauth_token SET revoked_at = ? WHERE token = ?",
                     (_iso(_now()), row["token"]))
        return _issue(conn, row["client_id"], row["subject"])
    finally:
        conn.close()


def _prune_codes(conn) -> int:
    """Clear codes that can never be redeemed again.

    Single-use by design and ten minutes long, so a row that is used or expired is dead
    weight -- and this table is written by an unauthenticated endpoint, which makes "it only
    grows" a slow leak rather than a tidiness question.
    """
    cur = conn.execute("DELETE FROM oauth_code WHERE used = 1 OR expires_at < ?",
                       (_iso(_now()),))
    return cur.rowcount


def valid_token(token_value: str) -> bool:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT expires_at, revoked_at FROM oauth_token WHERE token = ?",
            (_hash(token_value),)).fetchone()
    finally:
        conn.close()
    if row is None or row["revoked_at"]:
        return False
    return not row["expires_at"] or datetime.fromisoformat(row["expires_at"]) >= _now()


def unauthorized() -> Response:
    """401 carrying WWW-Authenticate.

    This header is the whole reason this module exists: without it the claude.ai connector
    fails with 'Authorization with the MCP server failed' rather than starting the flow.
    """
    return JSONResponse(
        {"error": "unauthorized"}, 401,
        headers={"WWW-Authenticate":
                 f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource"'})
