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

# Header Cloudflare Access injects once a human has authenticated. Its presence is the proof
# that the browser passed Access; absence means the request never went through it.
ACCESS_HEADER = "cf-access-jwt-assertion"
# Escape hatch for local testing only, never for the tunnelled deployment.
REQUIRE_ACCESS = os.environ.get("SYNTH_REQUIRE_ACCESS", "1") != "0"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


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
    """Dynamic Client Registration — how claude.ai onboards a custom connector."""
    body = await request.json()
    uris = body.get("redirect_uris") or []
    if not uris:
        return JSONResponse({"error": "invalid_client_metadata",
                             "error_description": "redirect_uris is required"}, 400)
    client_id = f"synth-{secrets.token_urlsafe(16)}"
    client_secret = secrets.token_urlsafe(32)
    conn = db.connect()
    conn.execute("INSERT INTO oauth_client (client_id, client_secret, name, redirect_uris) "
                 "VALUES (?,?,?,?)",
                 (client_id, client_secret, body.get("client_name", "unnamed"),
                  json.dumps(uris)))
    conn.commit()
    conn.close()
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

    subject = _subject(request)
    if subject is None:
        # Access has not vouched for this browser. Say so plainly rather than issuing a code.
        return JSONResponse({"error": "access_denied",
                             "error_description": "Cloudflare Access did not authenticate "
                                                  "this request"}, 403)

    conn = db.connect()
    row = conn.execute("SELECT redirect_uris FROM oauth_client WHERE client_id = ?",
                       (client_id,)).fetchone()
    if row is None:
        conn.close()
        return JSONResponse({"error": "invalid_client"}, 400)
    if redirect_uri not in json.loads(row["redirect_uris"]):
        conn.close()
        return JSONResponse({"error": "invalid_request",
                             "error_description": "redirect_uri not registered"}, 400)

    code = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO oauth_code (code, client_id, redirect_uri, challenge, "
                 "challenge_method, subject, expires_at) VALUES (?,?,?,?,?,?,?)",
                 (code, client_id, redirect_uri, q.get("code_challenge"),
                  q.get("code_challenge_method", "S256"), subject,
                  _iso(_now() + CODE_TTL)))
    conn.commit()
    conn.close()
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}", status_code=302)


async def token(request: Request) -> Response:
    form = await request.form()
    code = form.get("code", "")
    verifier = form.get("code_verifier", "")
    conn = db.connect()
    row = conn.execute("SELECT * FROM oauth_code WHERE code = ?", (code,)).fetchone()
    if row is None or row["used"]:
        conn.close()
        return JSONResponse({"error": "invalid_grant"}, 400)
    if datetime.fromisoformat(row["expires_at"]) < _now():
        conn.close()
        return JSONResponse({"error": "invalid_grant",
                             "error_description": "code expired"}, 400)
    if row["challenge"]:
        digest = hashlib.sha256(verifier.encode()).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if expected != row["challenge"]:
            conn.close()
            return JSONResponse({"error": "invalid_grant",
                                 "error_description": "PKCE verification failed"}, 400)

    access = secrets.token_urlsafe(40)
    conn.execute("UPDATE oauth_code SET used = 1 WHERE code = ?", (code,))
    conn.execute("INSERT INTO oauth_token (token, client_id, subject, expires_at) "
                 "VALUES (?,?,?,?)",
                 (access, row["client_id"], row["subject"], _iso(_now() + TOKEN_TTL)))
    conn.commit()
    conn.close()
    return JSONResponse({"access_token": access, "token_type": "Bearer",
                         "expires_in": int(TOKEN_TTL.total_seconds()),
                         "scope": "synth:read synth:write"})


def valid_token(token_value: str) -> bool:
    conn = db.connect()
    row = conn.execute("SELECT expires_at, revoked_at FROM oauth_token WHERE token = ?",
                       (token_value,)).fetchone()
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
