"""
Slack MCP Proxy — mirrors the Gmail MCP proxy architecture.

opencode ←→ THIS PROXY ←→ https://mcp.slack.com/mcp
                         ↕ OAuth with client_id + client_secret

Why a proxy?
- Slack requires `client_secret` for token exchange (no public client support).
- We store the secret server-side and act as the OAuth middleman.
"""

import asyncio
import os
import secrets
import urllib.parse
from datetime import datetime

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SLACK_CLIENT_ID = os.environ["SLACK_CLIENT_ID"]
SLACK_CLIENT_SECRET = os.environ["SLACK_CLIENT_SECRET"]

SLACK_MCP_URL = "https://mcp.slack.com/mcp"
SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2_user/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.user.access"

SESSION_SECRET = os.environ.get("SESSION_SECRET", secrets.token_hex(32))
BASE_URL = os.environ.get("BASE_URL", "https://slack-mcp-proxy.onrender.com")

# Redirect URI registered in your Slack app
REDIRECT_URI = f"{BASE_URL}/callback"

SCOPES = (
    "search:read.public,search:read.private,search:read.mpim,search:read.im,"
    "search:read.files,search:read.users,chat:write,channels:history,"
    "groups:history,mpim:history,im:history,canvases:read,canvases:write,"
    "users:read,users:read.email,reactions:write,emoji:read,files:read,"
    "channels:write,groups:write,im:write,mpim:write,channels:read,"
    "groups:read,mpim:read"
)

# ---------------------------------------------------------------------------
# In-memory stores (survive within a single Render instance)
# ---------------------------------------------------------------------------

_token_store: dict[str, str] = {}   # our_token → slack_access_token
_pending_auth: dict[str, dict] = {} # internal_state → {mcp_state, redirect_uri, ...}
_auth_codes: dict[str, str] = {}    # auth_code → our_token

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Slack MCP Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"ok": True, "service": "slack-mcp-proxy"}


# ---------------------------------------------------------------------------
# OAuth Discovery (so opencode can discover us as an OAuth server)
# ---------------------------------------------------------------------------

@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource():
    return {
        "resource": BASE_URL,
        "authorization_servers": [BASE_URL],
        "bearer_methods_supported": ["header"],
        "resource_name": "Slack MCP Proxy",
    }


@app.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server():
    return {
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "registration_endpoint": f"{BASE_URL}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


# ---------------------------------------------------------------------------
# Dynamic Client Registration (opencode registers itself here)
# ---------------------------------------------------------------------------

@app.post("/oauth/register")
async def oauth_register(request: Request):
    body = await request.json()
    client_id = f"umbral-{secrets.token_urlsafe(12)}"
    return JSONResponse({
        "client_id": client_id,
        "client_secret": "not-used",
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, status_code=201)


# ---------------------------------------------------------------------------
# OAuth Authorize — redirect to Slack's auth page
# ---------------------------------------------------------------------------

@app.get("/oauth/authorize")
async def oauth_authorize(request: Request):
    mcp_state = request.query_params.get("state", "")
    redirect_uri_back = request.query_params.get("redirect_uri", "")
    code_challenge = request.query_params.get("code_challenge", "")
    code_challenge_method = request.query_params.get("code_challenge_method", "S256")

    internal_state = secrets.token_urlsafe(32)

    # Keep only the latest pending auth to avoid state mismatches
    _pending_auth.clear()
    _pending_auth[internal_state] = {
        "redirect_uri": redirect_uri_back,
        "mcp_state": mcp_state,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
    }
    request.session[f"pending_{internal_state}"] = _pending_auth[internal_state]

    params = urllib.parse.urlencode({
        "client_id": SLACK_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "user_scope": SCOPES,
        "state": internal_state,
    })
    print(f"[AUTHORIZE] mcp_state={mcp_state!r} internal_state={internal_state!r}")
    return RedirectResponse(url=f"{SLACK_AUTHORIZE_URL}?{params}")


# ---------------------------------------------------------------------------
# Slack OAuth Callback
# ---------------------------------------------------------------------------

@app.get("/callback")
async def oauth_callback(request: Request):
    code = request.query_params.get("code", "")
    internal_state = request.query_params.get("state", "")
    error = request.query_params.get("error", "")

    if error:
        return JSONResponse({"error": f"Slack OAuth error: {error}"}, status_code=400)

    # Resolve pending auth (memory or session fallback)
    pending = _pending_auth.get(internal_state)
    if not pending:
        pending = request.session.get(f"pending_{internal_state}")
    if not pending:
        print(f"[CALLBACK] state not found: {internal_state!r}")
        return _csrf_error_page()

    print(f"[CALLBACK] state={internal_state!r} code=yes")

    # Exchange code for Slack token
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            SLACK_TOKEN_URL,
            data={
                "client_id": SLACK_CLIENT_ID,
                "client_secret": SLACK_CLIENT_SECRET,
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
        )
    data = resp.json()
    if not data.get("ok"):
        return JSONResponse({"error": f"Token exchange failed: {data.get('error')}"}, status_code=500)

    slack_token = data.get("authed_user", {}).get("access_token") or data.get("access_token")
    if not slack_token:
        return JSONResponse({"error": "No access token in response"}, status_code=500)

    # Store our token → Slack token mapping
    our_token = secrets.token_urlsafe(32)
    _token_store[our_token] = slack_token

    # Mint an auth_code to hand back to opencode
    auth_code = secrets.token_urlsafe(16)
    _auth_codes[auth_code] = our_token

    redirect_uri = pending["redirect_uri"]
    mcp_state = pending.get("mcp_state", "")
    sep = "&" if "?" in redirect_uri else "?"
    print(f"[CALLBACK] redirecting back to opencode: {redirect_uri!r} state={mcp_state!r}")
    return RedirectResponse(url=f"{redirect_uri}{sep}code={auth_code}&state={mcp_state}")


def _csrf_error_page():
    html = """<!DOCTYPE html><html><body>
    <h1>Authorization Failed</h1>
    <p>Invalid or expired state parameter.</p>
    </body></html>"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html, status_code=400)


# ---------------------------------------------------------------------------
# OAuth Token Exchange
# ---------------------------------------------------------------------------

@app.post("/oauth/token")
async def oauth_token(request: Request):
    try:
        body = await request.json()
    except Exception:
        form = await request.form()
        body = dict(form)

    grant_type = body.get("grant_type", "")
    code = body.get("code", "")

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    our_token = _auth_codes.pop(code, None)
    if not our_token:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    return JSONResponse({
        "access_token": our_token,
        "token_type": "Bearer",
        "issued_at": datetime.utcnow().isoformat(),
    })


# ---------------------------------------------------------------------------
# MCP Proxy — POST to https://mcp.slack.com/mcp
# ---------------------------------------------------------------------------

def _resolve_slack_token(auth_header: str | None) -> str | None:
    if auth_header and auth_header.startswith("Bearer "):
        our_token = auth_header[7:]
        return _token_store.get(our_token)
    return None


def _unauthorized_response():
    return Response(
        status_code=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer resource_metadata="{BASE_URL}/.well-known/oauth-protected-resource"'
            ),
        },
    )


from fastapi.responses import Response


@app.post("/mcp")
@app.post("/sse")  # compat alias opencode sometimes uses
async def proxy_mcp_post(request: Request):
    """Proxy MCP requests to https://mcp.slack.com/mcp with Slack Bearer token."""
    slack_token = _resolve_slack_token(request.headers.get("authorization"))
    if not slack_token:
        return _unauthorized_response()

    body = await request.body()
    headers = {
        "Authorization": f"Bearer {slack_token}",
        "Content-Type": request.headers.get("content-type", "application/json"),
        "Accept": request.headers.get("accept", "application/json, text/event-stream"),
    }

    accept = request.headers.get("accept", "")

    if "text/event-stream" in accept:
        # Streaming response
        async def stream():
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST", SLACK_MCP_URL, content=body, headers=headers
                ) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk

        return StreamingResponse(stream(), media_type="text/event-stream")
    else:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(SLACK_MCP_URL, content=body, headers=headers)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )


@app.get("/sse")
async def proxy_sse_get(request: Request):
    """GET /sse — keep-alive to avoid SSE errors; MCP data flows via POST."""
    async def keepalive():
        while True:
            yield ": keepalive\n\n"
            await asyncio.sleep(15)

    return StreamingResponse(
        keepalive(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
