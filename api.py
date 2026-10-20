import logging
import os
import re
import time
import httpx
from fastapi import FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

import dbcore
from dbcore import check_key, init_db, key_stats

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("ffapi")

LOOKUP_URL = dbcore.normalize_lookup(os.getenv("LOOKUP_URL", ""))
UPSTREAM_KEY = (os.getenv("UPSTREAM_KEY", "") or "").strip()
DEFAULT_REGION = (os.getenv("DEFAULT_REGION", "IND") or "IND").strip().upper()
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))
BRAND = os.getenv("API_BRAND", "Bomber Janta Party Free Fire API")
OWNER = os.getenv("API_OWNER", "@bomberjantaparty")

app = FastAPI(title=BRAND, version="1.0.0", docs_url="/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

_MINUTE_BUCKET = {}


def region_ok(region):
    return bool(re.fullmatch(r"[A-Za-z]{2,6}", region or ""))


def upstream_url(uid, region):
    url = LOOKUP_URL
    if not url:
        return None
    if "{uid}" in url:
        out = url.replace("{uid}", str(uid)).replace("{region}", (region or "").upper())
        if "{key}" in out:
            out = out.replace("{key}", UPSTREAM_KEY)
        return out
    sep = "&" if "?" in url else "?"
    out = "{}{}uid={}".format(url, sep, uid)
    if UPSTREAM_KEY:
        out += "&key=" + UPSTREAM_KEY
    return out


def health_url():
    """Derive the upstream health URL from LOOKUP_URL, if possible."""
    if not LOOKUP_URL:
        return None
    base = re.sub(r"/players?/.*$", "", LOOKUP_URL.split("?")[0])
    if base == LOOKUP_URL.split("?")[0]:
        base = LOOKUP_URL.split("?")[0].rstrip("/")
    return base.rstrip("/") + "/health"


async def upstream_healthy():
    """True when the data source answers its own health check."""
    url = health_url()
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
            r = await c.get(url, headers={"Accept": "application/json"})
        if r.status_code != 200:
            return False
        body = r.json()
        return bool(body.get("ok")) if isinstance(body, dict) else True
    except Exception:
        return False


async def fetch_upstream(uid, region):
    url = upstream_url(uid, region)
    if not url:
        return None, "upstream_not_configured"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": "FF-API/1.0",
                                          "Accept": "application/json"})
    except httpx.TimeoutException:
        return None, "upstream_timeout"
    except Exception as e:
        log.warning("upstream error: %s", e)
        return None, "upstream_unreachable"
    try:
        data = r.json()
    except ValueError:
        return None, "upstream_bad_json"
    if r.status_code == 404:
        return None, "not_found"
    # The source returns 502/"UPSTREAM_ERROR" both for unknown UIDs and for real
    # outages. Ask its health endpoint: healthy service => that UID does not exist.
    if r.status_code >= 500 or (isinstance(data, dict) and data.get("code") == "UPSTREAM_ERROR"):
        if await upstream_healthy():
            return None, "not_found"
        return None, "upstream_unavailable"
    if r.status_code >= 400:
        msg = data.get("error") if isinstance(data, dict) else None
        return None, "upstream_{}{}".format(
            r.status_code, (": " + str(msg)[:80]) if msg else "")
    if isinstance(data, dict) and data.get("error"):
        return None, "upstream_error: " + str(data["error"])[:120]
    if not isinstance(data, dict):
        return None, "upstream_bad_json"
    return data, None


def err(status, code, message, extra=None):
    body = {
        "success": False,
        "error": code,
        "message": message,
        "owner": OWNER,
        "timestamp": int(time.time()),
    }
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status)


def get_client_key(request, key_q, key_h):
    return (key_q or key_h or request.headers.get("X-API-Key") or "").strip()


def minute_guard(key, per_minute=60):
    now = int(time.time())
    bucket = _MINUTE_BUCKET.setdefault(key, [now, 0])
    if now - bucket[0] >= 60:
        bucket[0], bucket[1] = now, 0
    bucket[1] += 1
    return bucket[1] <= per_minute


@app.get("/")
async def root():
    return {
        "api": BRAND,
        "version": "1.0.0",
        "owner": OWNER,
        "status": "online",
        "auth": "pass ?key=YOUR_KEY or header X-API-Key",
        "endpoints": {
            "player": "/api/v1/player/{uid}?region=IND&key=YOUR_KEY",
            "keyinfo": "/api/v1/key?key=YOUR_KEY",
            "health": "/api/v1/health",
            "docs": "/docs",
        },
        "example": "/api/v1/player/2384343917?region=IND&key=YOUR_KEY",
    }


@app.get("/api/v1/health")
async def health():
    ok = dbcore.q1("SELECT 1 AS ok")
    return {
        "success": bool(ok),
        "database": "up" if ok else "down",
        "upstream": ("reachable" if await upstream_healthy() else "unreachable")
        if LOOKUP_URL else "not configured",
        "keys": key_stats(),
        "timestamp": int(time.time()),
    }


@app.get("/api/v1/key")
async def key_info(key: str = Query(None), x_api_key: str = Header(None)):
    client = get_client_key(None, key, x_api_key)
    allowed, reason, row, remaining = check_key(client, consume=False)
    if not allowed:
        return err(401 if "invalid" in reason or "missing" in reason else 403,
                   reason.replace(" ", "_"), reason.title())
    expires = row.get("expires_at")
    return {
        "success": True,
        "key": row["key"][:12] + "\u2026",
        "label": row.get("label"),
        "plan": row.get("plan"),
        "daily_limit": row.get("daily_limit"),
        "used_today": row.get("used_day") if row.get("day_key") == dbcore._day() else 0,
        "used_total": row.get("used_total"),
        "remaining_today": remaining,
        "expires_at": expires,
        "expires_in_days": max(0, (int(expires) - int(time.time())) // 86400)
        if expires else None,
        "revoked": bool(row.get("revoked")),
        "owner": OWNER,
    }


@app.get("/api/v1/player/{uid}")
async def player(uid: str, request: Request, region: str = Query(None),
                 key: str = Query(None), x_api_key: str = Header(None)):
    client = get_client_key(request, key, x_api_key)
    allowed, reason, row, remaining = check_key(client, consume=False)
    if not allowed:
        status = 401 if reason in ("missing key", "invalid key") else 403
        if reason == "daily limit reached":
            status = 429
        return err(status, reason.replace(" ", "_"), reason.title())
    if not minute_guard(client):
        return err(429, "rate_limited", "Too many requests: max 60 per minute.")
    if not re.fullmatch(r"\d{5,20}", uid or ""):
        return err(400, "invalid_uid", "UID must be 5-20 digits.")
    region = (region or DEFAULT_REGION).upper()
    if not region_ok(region):
        return err(400, "invalid_region", "Region must be 2-6 letters, e.g. IND.")
    if not LOOKUP_URL:
        return err(503, "upstream_not_configured",
                   "Server has no Free Fire data source configured (LOOKUP_URL).")

    started = time.time()
    data, problem = await fetch_upstream(uid, region)
    if problem:
        if problem == "not_found":
            return err(404, "player_not_found",
                       "UID {} not found in region {}.".format(uid, region))
        if problem == "upstream_unavailable":
            return err(502, "upstream_unavailable",
                       "Free Fire data source is temporarily unavailable. Try again.")
        if problem == "upstream_not_configured":
            return err(503, "upstream_not_configured",
                       "Server has no Free Fire data source configured.")
        return err(502, "upstream_error", problem)

    _, _, _, remaining = check_key(client, consume=True)
    b = data.get("basicInfo") or {}
    return {
        "success": True,
        "uid": uid,
        "region": b.get("region") or region,
        "nickname": b.get("nickname"),
        "level": b.get("level"),
        "player": data,
        "quota": {
            "remaining_today": remaining,
            "daily_limit": row.get("daily_limit"),
            "plan": row.get("plan"),
        },
        "owner": OWNER,
        "took_ms": int((time.time() - started) * 1000),
        "timestamp": int(time.time()),
    }


@app.get("/api/v1/player/{uid}/avatar")
async def avatar(uid: str, region: str = Query(None), key: str = Query(None),
                 x_api_key: str = Header(None)):
    client = get_client_key(None, key, x_api_key)
    allowed, reason, _, _ = check_key(client, consume=False)
    if not allowed:
        return err(401 if reason in ("missing key", "invalid key") else 403,
                   reason.replace(" ", "_"), reason.title())
    if not LOOKUP_URL:
        return err(503, "upstream_not_configured", "No data source configured.")
    url = upstream_url(uid, (region or DEFAULT_REGION).upper())
    if url and "{uid}" in LOOKUP_URL:
        url = url.split("?")[0].rstrip("/") + "/avatar?region=" + \
            (region or DEFAULT_REGION).upper()
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": "FF-API/1.0"})
    except Exception:
        return err(502, "upstream_error", "Image fetch failed.")
    if r.status_code != 200:
        return err(502, "upstream_error", "Image not available.")
    check_key(client, consume=True)
    media = r.headers.get("content-type", "image/webp")
    return Response(content=r.content, media_type=media,
                    headers={"Cache-Control": "public, max-age=300"})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled: %s", exc)
    return err(500, "internal_error", "Unexpected server error.")


init_db()
