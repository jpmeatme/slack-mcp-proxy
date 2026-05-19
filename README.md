# Slack MCP Server

Slack's official MCP server at `https://mcp.slack.com/mcp` — no proxy needed.

## Prerequisites

Slack MCP requires a registered Slack app. **Dynamic Client Registration is NOT supported**, so you must create the app manually.

## Step 1: Create a Slack App

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
2. Name: `Umbral MCP` (or similar), pick your workspace

## Step 2: Configure OAuth & Permissions

Add these **User Token Scopes**:

```
channels:history  channels:read  channels:write
groups:history    groups:read    groups:write
im:history        im:read        im:write
mpim:history      mpim:read      mpim:write
chat:write        reactions:write
search:read       users:read     users:read.email
canvases:read     canvases:write
emoji:read        files:read
```

Add **Redirect URL**:
```
http://127.0.0.1:PORT/mcp/oauth/callback
```
> Note: The port is dynamic (opencode picks it). For dev, add `http://localhost:*` if Slack allows wildcard, or use a fixed port like `http://127.0.0.1:19876/mcp/oauth/callback`.

## Step 3: Get Credentials

From the app's **Basic Information** page:
- `client_id`
- `client_secret`

## Step 4: Configure opencode.jsonc

```jsonc
"slack": {
  "type": "remote",
  "url": "https://mcp.slack.com/mcp",
  "oauth": {}
}
```

> opencode will discover the OAuth server from `https://mcp.slack.com/.well-known/oauth-protected-resource`.

## Important Notes

- The app must be an **internal app** or **Slack Marketplace-published** app — unlisted apps cannot use MCP.
- Slack uses **confidential OAuth** (requires `client_id` + `client_secret`). opencode's Dynamic Client Registration will fail; you need a pre-registered app.
- Slack MCP supports **Streamable HTTP only** (POST to `/mcp`) — SSE is not supported.

## Endpoint Reference

| Resource | URL |
|---|---|
| MCP Endpoint | `https://mcp.slack.com/mcp` |
| OAuth Resource Metadata | `https://mcp.slack.com/.well-known/oauth-protected-resource` |
| OAuth Auth Server Metadata | `https://mcp.slack.com/.well-known/oauth-authorization-server` |
| Authorization | `https://slack.com/oauth/v2_user/authorize` |
| Token Exchange | `https://slack.com/api/oauth.v2.user.access` |
