#!/usr/bin/env python3
"""
Read-only Nightscout MCP server.

Exposes glucose, treatments, profile and device status from a Nightscout
instance to MCP clients (e.g. the Claude apps) over HTTP.

Security model:
  - Every request must carry a static bearer token (MCP_BEARER). The server
    REFUSES TO START without one: this thing serves personal health data over
    the public internet, and an unset environment variable must not be the
    difference between authenticated and open.
  - Nightscout is read with a least-privilege *readable* access token
    (NS_TOKEN), never the admin API_SECRET. There are no write tools here, so
    by construction it can only ever read.
  - The Nightscout token travels as a query parameter (Nightscout's own API
    convention), so upstream errors are sanitised before they surface — an
    unsanitised httpx error embeds the full request URL, token included, into
    logs and client-visible messages.

Env:
  NS_URL       required   e.g. https://your-nightscout.example.com
  NS_TOKEN     optional   Nightscout readable access token (subject-<hex>)
  MCP_BEARER   required   bearer token callers must present
  PORT         optional   listen port (default 8787)
  NS_UNITS     optional   mg/dl (default) or mmol -- display unit for stats
  TRUSTED_PROXY_IPS  optional  see main()
"""
import hmac
import os
import statistics
from datetime import datetime, timedelta, timezone

import httpx
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

NS_URL = os.environ["NS_URL"].rstrip("/")
NS_TOKEN = os.environ.get("NS_TOKEN", "")

# Required, and deliberately so.
#
# This was once optional, falling back to "the URL path is unguessable" —
# a workaround from when Claude connectors supported only OAuth or no auth.
# Static bearer headers are supported now (see README), so the workaround has
# no reason to exist, and secret-URL-only auth is a poor trade for health data:
# URLs leak through logs, proxies and browser history in a way headers do not.
#
# Failing at startup rather than at request time is the point. A server that
# boots happily and silently serves unauthenticated CGM data is exactly the
# failure a deployment forgets to notice.
MCP_BEARER = os.environ.get("MCP_BEARER", "")
if not MCP_BEARER:
    raise SystemExit(
        "MCP_BEARER is required — refusing to start an unauthenticated server "
        "that exposes personal health data. Set it to a long random string."
    )
PORT = int(os.environ.get("PORT", "8787"))
UNITS = os.environ.get("NS_UNITS", "mg/dl").lower()

MMOL = 18.0156  # mg/dL per mmol/L


def _fmt(mgdl: float) -> float:
    """Return a value in the configured display unit."""
    if UNITS.startswith("mmol"):
        return round(mgdl / MMOL, 1)
    return round(mgdl)


async def _ns_get(path: str, params: dict | None = None) -> object:
    """GET from Nightscout, raising errors that cannot carry the token.

    Nightscout authenticates by query parameter, so the credential is in the
    request URL. httpx's own `raise_for_status` puts that URL in the exception
    message, which then travels into logs and back to the MCP client — so the
    error is rebuilt here naming only the path and status.
    """
    params = dict(params or {})
    if NS_TOKEN:
        params["token"] = NS_TOKEN
    async with httpx.AsyncClient(timeout=20) as c:
        try:
            r = await c.get(f"{NS_URL}{path}", params=params)
        except httpx.RequestError as exc:
            raise RuntimeError(f"Nightscout request to {path} failed: {type(exc).__name__}") from None
        if r.status_code >= 400:
            raise RuntimeError(f"Nightscout returned {r.status_code} for {path}") from None
        try:
            return r.json()
        except ValueError:
            raise RuntimeError(f"Nightscout returned non-JSON for {path}") from None


# ---- bearer auth gate (everything except /health) -------------------------
class BearerAuth(BaseHTTPMiddleware):
    """Static bearer gate.

    `/health` is deliberately open so container orchestrators can probe it; it
    returns a fixed `{"ok": true}` and reads nothing, so there is nothing to
    leak through it.

    The comparison is constant-time. A plain `!=` on a secret leaks its length
    and matching prefix through response timing, which is a slow but real way
    to recover a token from a server anyone on the internet can call.
    """

    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {MCP_BEARER}"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


mcp = FastMCP("nightscout-readonly")


@mcp.tool
async def get_current_glucose() -> dict:
    """Latest glucose reading with trend direction and age."""
    e = await _ns_get("/api/v1/entries/sgv.json", {"count": 1})
    if not e:
        return {"error": "no readings"}
    r = e[0]
    mgdl = r.get("sgv")
    return {
        "glucose": _fmt(mgdl),
        "units": UNITS,
        "mgdl": mgdl,
        "direction": r.get("direction"),
        "time": r.get("dateString"),
        "device": r.get("device"),
    }


@mcp.tool
async def get_recent_glucose(hours: float = 3) -> dict:
    """Glucose readings from the past `hours` (default 3)."""
    since = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp() * 1000)
    e = await _ns_get(
        "/api/v1/entries/sgv.json",
        {"count": 5000, "find[date][$gte]": since},
    )
    pts = [
        {"t": x.get("dateString"), "glucose": _fmt(x["sgv"]), "direction": x.get("direction")}
        for x in e
        if x.get("sgv") is not None
    ]
    return {"units": UNITS, "count": len(pts), "readings": pts}


@mcp.tool
async def time_in_range(hours: float = 24) -> dict:
    """Time-in-range, average, GMI (est. A1C) and CV% over the past `hours`."""
    since = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp() * 1000)
    e = await _ns_get(
        "/api/v1/entries/sgv.json",
        {"count": 100000, "find[date][$gte]": since},
    )
    vals = [x["sgv"] for x in e if x.get("sgv")]
    if not vals:
        return {"error": "no readings in window"}
    n = len(vals)
    def pct(lo, hi):
        return round(100 * sum(1 for v in vals if lo <= v < hi) / n, 1)
    mean = statistics.mean(vals)
    sd = statistics.pstdev(vals) if n > 1 else 0
    gmi = 3.31 + 0.02392 * mean  # GMI (%) from mean mg/dL
    return {
        "hours": hours,
        "readings": n,
        "units": UNITS,
        "average": _fmt(mean),
        "gmi_a1c_percent": round(gmi, 1),
        "cv_percent": round(100 * sd / mean, 1) if mean else None,
        "very_low_lt54": pct(0, 54),
        "low_54_70": pct(54, 70),
        "in_range_70_180": pct(70, 180),
        "high_180_250": pct(180, 250),
        "very_high_ge250": pct(250, 10000),
    }


@mcp.tool
async def get_recent_treatments(hours: float = 12) -> dict:
    """Insulin boluses, carbs and other treatments from the past `hours`."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    t = await _ns_get(
        "/api/v1/treatments.json",
        {"count": 1000, "find[created_at][$gte]": since},
    )
    out = [
        {
            "time": x.get("created_at"),
            "type": x.get("eventType"),
            "insulin": x.get("insulin"),
            "carbs": x.get("carbs"),
            "notes": x.get("notes"),
        }
        for x in t
    ]
    return {"count": len(out), "treatments": out}


@mcp.tool
async def get_insulin_on_board() -> dict:
    """Current IOB/COB from the latest device status (Loop/AAPS)."""
    d = await _ns_get("/api/v1/devicestatus.json", {"count": 1})
    if not d:
        return {"error": "no device status"}
    s = d[0]
    loop = s.get("loop", {})
    return {
        "time": s.get("created_at"),
        "device": s.get("device"),
        "iob": (loop.get("iob") or {}).get("iob") if isinstance(loop.get("iob"), dict) else loop.get("iob"),
        "cob": (loop.get("cob") or {}).get("cob") if isinstance(loop.get("cob"), dict) else loop.get("cob"),
        "raw": loop or s.get("openaps") or {},
    }


@mcp.tool
async def get_profile() -> dict:
    """Active treatment profile (basal, ISF, carb ratio, targets)."""
    p = await _ns_get("/api/v1/profile.json")
    if not p:
        return {"error": "no profile"}
    return p[0]


@mcp.tool
async def server_status() -> dict:
    """Nightscout version, name and configured thresholds."""
    return await _ns_get("/api/v1/status.json")


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def main():
    app = mcp.http_app(middleware=[Middleware(BearerAuth)])
    import uvicorn

    # X-Forwarded-* is only trustworthy from a proxy you control. The previous
    # value here was "*", which trusts the header from any caller — fine when
    # nothing but Traefik can reach the port, and a spoofing hole the moment
    # the container is exposed directly. Default to trusting nobody and let a
    # deployment name its proxy explicitly.
    trusted = os.environ.get("TRUSTED_PROXY_IPS", "").strip()
    uvicorn.run(
        app,
        host=os.environ.get("BIND_HOST", "0.0.0.0"),
        port=PORT,
        proxy_headers=bool(trusted),
        forwarded_allow_ips=trusted or None,
    )


if __name__ == "__main__":
    main()
