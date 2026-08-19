# nightscout-mcp

A read-only [MCP](https://modelcontextprotocol.io) server that exposes your
[Nightscout](https://nightscout.github.io) data to Claude.

Ask *"what's my glucose doing?"*, *"how was my time in range this week?"*, or
*"how much insulin is still active?"* and get an answer from your own data.

> **Not a medical device.** This is a read-only view of data you already have.
> Do not make treatment decisions from it. Confirm anything that matters
> against your CGM, pump and clinician. Readings can be stale, missing, or
> wrong, and an LLM can misread them.

## Tools

| Tool | Returns |
|---|---|
| `get_current_glucose` | Latest reading, trend direction, and how old it is |
| `get_recent_glucose` | Readings over the last N hours (default 3) |
| `time_in_range` | Low / in-range / high split over N hours (default 24) |
| `get_recent_treatments` | Boluses, carbs and site changes (default 12h) |
| `get_insulin_on_board` | Active insulin, from Nightscout's own IOB calc |
| `get_profile` | Basal rates, ISF, carb ratio, targets |
| `server_status` | Nightscout version and configured thresholds |

There are no write tools. The server cannot change anything in Nightscout.

## Security

This serves personal health data over the public internet, so the defaults are
deliberately strict:

- **A bearer token is required, and the server refuses to start without one.**
  There is no "unauthenticated if you forget the variable" mode — that is the
  failure a deployment never notices.
- **Least privilege upstream.** It reads Nightscout with a *readable* access
  token, never the admin `API_SECRET`. Even a total compromise of this server
  cannot write to your Nightscout.
- **Constant-time token comparison**, so response timing can't be used to
  recover the bearer a character at a time.
- **Errors are sanitised.** Nightscout authenticates by query parameter, so the
  token is in the request URL; upstream failures are re-raised naming only the
  path and status, keeping the credential out of logs and client-visible errors.
- **Forwarded headers are trusted only from proxies you name**, via
  `TRUSTED_PROXY_IPS`. Unset means trust nobody.
- **Runs as a non-root user** in the container.

`/health` is the one unauthenticated route. It returns a fixed `{"ok": true}`
and reads nothing.

## Setup

### 1. Make a read-only Nightscout token

In Nightscout: **Admin Tools → Subjects → Add**. Give it the `readable` role
only. You get a token like `claude-1a2b3c4d5e6f7890`.

Do **not** use your `API_SECRET`. Nothing here writes, so handing it write
access buys you nothing and costs you everything if the token leaks.

### 2. Configure

```bash
cp .env.example .env
# edit .env — at minimum NS_URL, NS_TOKEN, MCP_BEARER
```

Generate the bearer with something you didn't invent yourself:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3. Run

```bash
docker build -t nightscout-mcp .
docker run -d --name nightscout-mcp --env-file .env -p 8787:8787 nightscout-mcp
```

Or without Docker:

```bash
pip install -r requirements.txt
set -a && source .env && set +a
python server.py
```

### 4. Put it behind HTTPS

The server speaks plain HTTP and expects TLS to be terminated in front of it
(Traefik, Caddy, nginx, Cloudflare Tunnel — anything). Claude connects from
Anthropic's servers, not from your device, so the URL must be reachable from
the public internet.

If your proxy sets `X-Forwarded-*`, name it:

```
TRUSTED_PROXY_IPS=172.18.0.0/16
```

### 5. Connect it to Claude

In Claude: **Customize → Connectors → Add custom connector**

- **URL**: `https://your-host.example.com/mcp/`
- Open **Request headers**, add `authorization` with value
  `Bearer <your MCP_BEARER>`

Enter the value including the word `Bearer` and the space — Claude sends the
header verbatim and adds no prefix of its own.

> Request-header auth is a Claude beta. If you don't see a **Request headers**
> section, ask Anthropic for access. Claude Code can use the same server today
> via its own MCP configuration.

## Units

`NS_UNITS=mg/dl` (default) or `mmol`. This only affects how values are
presented; Nightscout is always read in mg/dL and converted on the way out.

## Relationship to Nightscout

This is an independent project. It is not affiliated with, endorsed by, or part
of the Nightscout Foundation.

Nightscout itself ([cgm-remote-monitor](https://github.com/nightscout/cgm-remote-monitor))
is licensed **AGPL-3.0**. This server contains no Nightscout code and links no
Nightscout library — it only makes HTTP requests to a running instance's REST
API. Consuming an API at arm's length does not create a derivative work, and
the AGPL's network clause is conditioned on modifying the covered program,
which this does not do. So Nightscout's copyleft does not extend here, and this
project is separately licensed.

If you fork this and vendor any Nightscout source into it, that reasoning stops
applying to your fork.

## License

MIT — see [LICENSE](LICENSE).

Chosen for reach: this is a small connector whose value is that anyone can run
it in ten minutes. Copyleft would protect nobody in the common case, which is
one person self-hosting it for their own data.
