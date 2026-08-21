"""HTTP transport for the Claude app custom connector.

Serves READ tools only. The write tools stay on the stdio transport, reachable only from the
VM, so a leaked endpoint can expose information but cannot act as Arun.

Auth is deliberately not implemented here. claude.ai's web and mobile connector requires a
`WWW-Authenticate` header on the 401 and a discoverable `/.well-known/oauth-protected-
resource`; Cloudflare Access "Managed OAuth" does not emit the former, which is a documented
cause of "Authorization with the MCP server failed". The intended shape is that this server
owns the OAuth endpoints with Cloudflare Access as the upstream identity provider. Until that
is wired, bind to localhost and reach it through the tunnel only.
"""
from __future__ import annotations

import os

from synth.mcp_server import build


def app():
    server = build(name="synth", writable=False)
    return server.streamable_http_app()


def main():
    import uvicorn

    host = os.environ.get("SYNTH_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("SYNTH_HTTP_PORT", "8787"))
    uvicorn.run(app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
