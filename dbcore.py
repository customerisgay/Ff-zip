import json
import os
import re
import secrets
import sqlite3
import threading
import time

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
DB_PATH = os.getenv("DB_PATH", "ffbot.db")
USE_PG = bool(DATABASE_URL)

_conn = None
_lock = threading.RLock()

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
    tg_id       INTEGER PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    first_seen  INTEGER,
    last_seen   INTEGER,
    joined_ok   INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tournaments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    mode        TEXT DEFAULT 'Solo',
    slots       INTEGER DEFAULT 48,
    entry_fee   TEXT DEFAULT 'Free',
    start_at    TEXT,
    status      TEXT DEFAULT 'open',
    room_id     TEXT,
    room_pass   TEXT,
    created_by  INTEGER,
    created_at  INTEGER
);
CREATE TABLE IF NOT EXISTS registrations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL,
    tg_id         INTEGER NOT NULL,
    tg_username   TEXT,
    tg_name       TEXT,
    ff_ign        TEXT NOT NULL,
    ff_uid        TEXT NOT NULL,
    created_at    INTEGER,
    UNIQUE (tournament_id, tg_id),
    UNIQUE (tournament_id, ff_uid)
);
CREATE TABLE IF NOT EXISTS lookups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id      INTEGER,
    ff_uid     TEXT,
    nickname   TEXT,
    level      INTEGER,
    region     TEXT,
    ok         INTEGER,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS states (
    user_id    INTEGER PRIMARY KEY,
    state      TEXT NOT NULL,
    data       TEXT DEFAULT '{}',
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT UNIQUE NOT NULL,
    label        TEXT,
    owner_tg     INTEGER,
    plan         TEXT DEFAULT 'free',
    daily_limit  INTEGER DEFAULT 100,
    used_total   INTEGER DEFAULT 0,
    used_day     INTEGER DEFAULT 0,
    day_key      TEXT,
    revoked      INTEGER DEFAULT 0,
    created_at   INTEGER,
    expires_at   INTEGER,
    last_used_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_reg_t ON registrations(tournament_id);
CREATE INDEX IF NOT EXISTS idx_lk_uid ON lookups(ff_uid);
CREATE INDEX IF NOT EXISTS idx_key ON api_keys(key);
"""

SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS users (
    tg_id       BIGINT PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    first_seen  BIGINT,
    last_seen   BIGINT,
    joined_ok   INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tournaments (
    id          SERIAL PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    mode        TEXT DEFAULT 'Solo',
    slots       INTEGER DEFAULT 48,
    entry_fee   TEXT DEFAULT 'Free',
    start_at    TEXT,
    status      TEXT DEFAULT 'open',
    room_id     TEXT,
    room_pass   TEXT,
    created_by  BIGINT,
    created_at  BIGINT
);
CREATE TABLE IF NOT EXISTS registrations (
    id            SERIAL PRIMARY KEY,
    tournament_id INTEGER NOT NULL,
    tg_id         BIGINT NOT NULL,
    tg_username   TEXT,
    tg_name       TEXT,
    ff_ign        TEXT NOT NULL,
    ff_uid        TEXT NOT NULL,
    created_at    BIGINT,
    UNIQUE (tournament_id, tg_id),
    UNIQUE (tournament_id, ff_uid)
);
CREATE TABLE IF NOT EXISTS lookups (
    id         SERIAL PRIMARY KEY,
    tg_id      BIGINT,
    ff_uid     TEXT,
    nickname   TEXT,
    level      INTEGER,
    region     TEXT,
    ok         INTEGER,
    created_at BIGINT
);
CREATE TABLE IF NOT EXISTS states (
    user_id    BIGINT PRIMARY KEY,
    state      TEXT NOT NULL,
    data       TEXT DEFAULT '{}',
    updated_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id           SERIAL PRIMARY KEY,
    key          TEXT UNIQUE NOT NULL,
    label        TEXT,
    owner_tg     BIGINT,
    plan         TEXT DEFAULT 'free',
    daily_limit  INTEGER DEFAULT 100,
    used_total   INTEGER DEFAULT 0,
    used_day     INTEGER DEFAULT 0,
    day_key      TEXT,
    revoked      INTEGER DEFAULT 0,
    created_at   BIGINT,
    expires_at   BIGINT,
    last_used_at BIGINT
);
CREATE INDEX IF NOT EXISTS idx_reg_t ON registrations(tournament_id);
CREATE INDEX IF NOT EXISTS idx_lk_uid ON lookups(ff_uid);
CREATE INDEX IF NOT EXISTS idx_key ON api_keys(key);
"""


def _pg_url():
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def db():
    global _conn
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is not None:
            return _conn
        if USE_PG:
            import psycopg2
            import psycopg2.extras

            _conn = psycopg2.connect(_pg_url(), sslmode=os.getenv("PGSSLMODE", "prefer"))
            _conn.autocommit = False
            _conn.cursor_factory = psycopg2.extras.RealDictCursor
        else:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA busy_timeout=5000")
    return _conn


def _sql(sql):
    return sql.replace("?", "%s") if USE_PG else sql


def _cursor(sql, args):
    c = db()
    if USE_PG:
        import psycopg2.extras

        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        cur = c.cursor()
    cur.execute(_sql(sql), tuple(args or ()))
    return cur


def q(sql, args=()):
    with _lock:
        c = db()
        cur = _cursor(sql, args)
        rows = cur.fetchall()
        cur.close()
        c.commit()
        # sqlite3.Row has no .get(); normalise to plain dicts so callers can
        # treat both backends identically.
        if not USE_PG:
            rows = [dict(r) for r in rows]
        return rows


def q1(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


ID_TABLES = {"tournaments", "registrations", "lookups", "api_keys"}
_INSERT_RE = re.compile(r"INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)


def ex(sql, args=()):
    with _lock:
        c = db()
        m = _INSERT_RE.search(sql)
        table = m.group(1).lower() if m else ""
        want_id = bool(m) and table in ID_TABLES
        run_sql = sql
        if want_id and USE_PG and "RETURNING" not in sql.upper():
            run_sql = sql + " RETURNING id"
        cur = _cursor(run_sql, args)
        new_id = None
        if want_id:
            try:
                row = cur.fetchone()
                if row is not None:
                    new_id = row["id"] if hasattr(row, "keys") else row[0]
            except Exception:
                new_id = None
        if not USE_PG and want_id:
            new_id = cur.lastrowid
        cur.close()
        c.commit()
        return new_id


def is_integrity_error(err):
    if isinstance(err, sqlite3.IntegrityError):
        return True
    try:
        import psycopg2

        return isinstance(err, psycopg2.IntegrityError)
    except ImportError:
        return False


def init_db():
    with _lock:
        c = db()
        if USE_PG:
            cur = c.cursor()
            cur.execute(SCHEMA_PG)  # psycopg2 accepts multi-statement SQL
            cur.close()
        else:
            c.executescript(SCHEMA_SQLITE)  # sqlite3 needs one statement per call
        c.commit()


LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal")


def normalize_base(raw):
    """Turn 'ff-api.onrender.com' into 'https://ff-api.onrender.com'."""
    url = (raw or "").strip().rstrip("/")
    if not url or "://" in url:
        return url
    host = url.split("/")[0].split(":")[0]
    local = host in LOCAL_HOSTS or host.startswith(("192.168.", "10.", "172."))
    return ("http://" if local else "https://") + url


def normalize_lookup(raw):
    """Accept 'ff-source.onrender.com', 'https://x/', or a full template URL.

    Render's blueprint hands us a bare hostname (fromService: property: host),
    so add the scheme and the players path when they are missing. This is what
    lets the deployment wire the data source with zero copy-pasting.
    """
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""
    url = normalize_base(url)
    if "{uid}" in url:
        return url
    if "/api/" in url or "/players/" in url or "?" in url:
        return url
    return url + "/api/free-fire/v1/players/{uid}?region={region}"


def get_setting(key, default=""):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


def set_setting(key, value):
    ex(
        "INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def del_setting(*keys):
    for k in keys:
        ex("DELETE FROM settings WHERE key=?", (k,))


def touch_user(user, joined_ok=None):
    now = int(time.time())
    ex(
        "INSERT INTO users(tg_id, username, full_name, first_seen, last_seen, joined_ok) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(tg_id) DO UPDATE SET "
        "username=excluded.username, full_name=excluded.full_name, "
        "last_seen=excluded.last_seen, joined_ok=excluded.joined_ok",
        (user.id, getattr(user, "username", None), getattr(user, "full_name", None),
         now, now, 1 if joined_ok else 0),
    )


def get_state(uid, ttl=3600):
    r = q1("SELECT state, data, updated_at FROM states WHERE user_id=?", (uid,))
    if not r:
        return None, {}, 0
    if time.time() - r["updated_at"] > ttl:
        clear_state(uid)
        return None, {}, 0
    try:
        data = json.loads(r["data"] or "{}")
    except ValueError:
        data = {}
    return r["state"], data, r["updated_at"]


def set_state(uid, state, data=None):
    ex(
        "INSERT INTO states(user_id, state, data, updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, "
        "data=excluded.data, updated_at=excluded.updated_at",
        (uid, state, json.dumps(data or {}), int(time.time())),
    )


def clear_state(uid):
    ex("DELETE FROM states WHERE user_id=?", (uid,))


PLANS = {
    "free": {"label": "Free \u2014 100 req/day", "daily": 100, "days": 30},
    "pro": {"label": "Pro \u2014 1,000 req/day", "daily": 1000, "days": 30},
    "vip": {"label": "VIP \u2014 10,000 req/day", "daily": 10000, "days": 365},
    "unlimited": {"label": "Unlimited (admin)", "daily": 0, "days": 0},
}
KEY_RE = re.compile(r"^ff_[a-z]+_[0-9a-f]{28}$")


def new_key(plan="free"):
    return "ff_{}_{}".format((plan or "free")[:6], secrets.token_hex(14))


def create_api_key(label, owner_tg=None, plan="free", daily_limit=None, days=None):
    plan = plan if plan in PLANS else "free"
    cfg = PLANS[plan]
    limit = cfg["daily"] if daily_limit is None else int(daily_limit)
    span = cfg["days"] if days is None else int(days)
    key = new_key(plan)
    now = int(time.time())
    expires = now + span * 86400 if span else None
    ex(
        "INSERT INTO api_keys(key, label, owner_tg, plan, daily_limit, used_total, "
        "used_day, day_key, revoked, created_at, expires_at, last_used_at) "
        "VALUES(?,?,?,?,?,0,0,?,0,?,?,NULL)",
        (key, (label or "api")[:60], owner_tg, plan, limit, _day(), now, expires),
    )
    return key


def _day():
    return time.strftime("%Y-%m-%d", time.gmtime())


def list_api_keys(limit=30):
    return q("SELECT * FROM api_keys ORDER BY id DESC LIMIT ?", (limit,))


def key_row(key):
    return q1("SELECT * FROM api_keys WHERE key=?", (key,))


def revoke_key(key_id, revoked=1):
    ex("UPDATE api_keys SET revoked=? WHERE id=?", (revoked, key_id))


def delete_key(key_id):
    ex("DELETE FROM api_keys WHERE id=?", (key_id,))


def reset_usage(key_id):
    ex("UPDATE api_keys SET used_total=0, used_day=0 WHERE id=?", (key_id,))


def check_key(key, consume=True):
    """Validate a key and (optionally) count one request.

    Returns (allowed: bool, reason: str, row: dict|None, remaining: int|None)
    """
    if not key:
        return False, "missing key", None, None
    row = key_row(key)
    if not row:
        return False, "invalid key", None, None
    if row["revoked"]:
        return False, "key revoked", dict(row), 0
    now = int(time.time())
    exp = row["expires_at"]
    if exp and now > int(exp):
        return False, "key expired", dict(row), 0
    day = _day()
    used_day = row["used_day"] or 0
    if row["day_key"] != day:
        used_day = 0
    limit = row["daily_limit"] or 0
    if limit and used_day >= limit:
        return False, "daily limit reached", dict(row), 0
    if consume:
        ex(
            "UPDATE api_keys SET used_total=COALESCE(used_total,0)+1, used_day=?, "
            "day_key=?, last_used_at=? WHERE id=?",
            (used_day + 1, day, now, row["id"]),
        )
        used_day += 1
    remaining = None if not limit else max(0, limit - used_day)
    return True, "ok", dict(row), remaining


def key_stats():
    total = q1("SELECT COUNT(*) c FROM api_keys")["c"]
    active = q1(
        "SELECT COUNT(*) c FROM api_keys WHERE revoked=0 AND "
        "(expires_at IS NULL OR expires_at > ?)", (int(time.time()),)
    )["c"]
    reqs = q1("SELECT COALESCE(SUM(used_total),0) c FROM api_keys")["c"]
    return {"total": total, "active": active, "requests": int(reqs or 0)}
