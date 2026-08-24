"""HTTP transport for the Claude app custom connector.

Serves the FULL tool set — reads and writes — at Arun's request, so the Claude app can
create reminders, draft email and record facts, not merely answer questions.

The security consequence is real and worth stating plainly: the endpoint can now act as him,
not just describe him. What stands between it and the internet is Cloudflare Access on
/authorize plus this server's own OAuth, and the doctrine still holds inside the tools —
there is no send path for email, and nothing deletes anything of his except a reminder Synth
itself created.

Auth is OAuth 2.1 with PKCE and Dynamic Client Registration, owned by this server, with
Cloudflare Access in front as the identity provider. See auth.py for why Access alone is not
enough: the claude.ai connector needs a `WWW-Authenticate` header that Access does not send.
"""
from __future__ import annotations

import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp.server.transport_security import TransportSecuritySettings

from synth import auth
from synth.mcp_server import build

PUBLIC_PATHS = {
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
    "/register",
    "/authorize",
    "/token",
    "/healthz",
}


class BearerGate(BaseHTTPMiddleware):
    """Everything under /mcp needs a token this server issued."""

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or not path.startswith("/mcp"):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return auth.unauthorized()
        if not auth.valid_token(header.split(" ", 1)[1].strip()):
            return auth.unauthorized()
        return await call_next(request)


async def healthz(request):
    return JSONResponse({"ok": True, "issuer": auth.ISSUER, "tools": "read-write"})


def app():
    """Add the OAuth routes to the MCP app rather than mounting it underneath one.

    Mounting looked tidier but Starlette does not run a mounted sub-app's lifespan, so the
    streamable-HTTP session manager never started and every authenticated call died with
    "Task group is not initialized".
    """
    # DNS-rebinding protection rejects any Host it does not know, so the tunnelled public
    # hostname has to be declared or every request through Cloudflare returns 421.
    public = auth.ISSUER.split("://", 1)[-1].split("/")[0]
    hosts = [h for h in {public, "127.0.0.1", "localhost",
                         f"127.0.0.1:{os.environ.get('SYNTH_HTTP_PORT', '8787')}"} if h]
    extra = os.environ.get("SYNTH_ALLOWED_HOSTS", "")
    hosts += [h.strip() for h in extra.split(",") if h.strip()]
    security = TransportSecuritySettings(
        allowed_hosts=hosts,
        allowed_origins=[auth.ISSUER, "https://claude.ai", "https://api.claude.ai"],
    )
    application = build(name="synth", writable=True).streamable_http_app(
        transport_security=security)
    application.router.routes[:0] = [
        Route("/.well-known/oauth-protected-resource", auth.protected_resource),
        Route("/.well-known/oauth-authorization-server", auth.authorization_server),
        Route("/register", auth.register, methods=["POST"]),
        Route("/authorize", auth.authorize),
        Route("/token", auth.token, methods=["POST"]),
        Route("/healthz", healthz),
    ]
    application.add_middleware(BearerGate)
    return application


def main():
    import uvicorn

    uvicorn.run(app(),
                host=os.environ.get("SYNTH_HTTP_HOST", "127.0.0.1"),
                port=int(os.environ.get("SYNTH_HTTP_PORT", "8787")),
                log_level="info")


if __name__ == "__main__":
    main()
