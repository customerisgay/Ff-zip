import asyncio
import csv
import io
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from html import escape as esc

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import dbcore  # shared data layer (Postgres when DATABASE_URL is set, else SQLite)
import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram import InputFile
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================================
# CONFIG
# ============================================================================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()

_DEFAULT_ADMINS = "8581890404,8725461956"
ADMIN_IDS = {
    int(x)
    for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", _DEFAULT_ADMINS))
    if x.strip().lstrip("-").isdigit()
}

FORCE_JOIN_CHAT = (os.getenv("FORCE_JOIN_CHAT", "@bomberjantaparty") or "").strip()
FORCE_JOIN_LINK = "https://t.me/" + FORCE_JOIN_CHAT.lstrip("@") if FORCE_JOIN_CHAT else ""

# Player lookup. Works with the free-fire-info-starter (refatbd) JSON API:
#   https://YOUR-APP/api/free-fire/v1/players/{uid}?region={region}
# or any query-style API:  https://host/info   (called as ?uid=..&key=..)
LOOKUP_URL = dbcore.normalize_lookup(os.getenv("LOOKUP_URL", ""))
LOOKUP_KEY = (os.getenv("LOOKUP_KEY", "") or "").strip()
LOOKUP_BAN_URL = (os.getenv("LOOKUP_BAN_URL", "") or "").strip()
DEFAULT_REGION = (os.getenv("DEFAULT_REGION", "IND") or "IND").strip().upper()
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "25"))

STATE_TTL = 3600
MAX_IGN, MAX_UID_DIGITS = 32, 15

API_PUBLIC_URL = dbcore.normalize_base(os.getenv("API_PUBLIC_URL", ""))

BOT_USERNAME = None
PREMIUM_BLOCKED_UNTIL = 0.0

_STARTED = time.time()

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ffbot")

# ============================================================================
# PREMIUM EMOJI
# ============================================================================
EMOJI = {
    "fire": ("5253997076169115794", "\U0001f525"),
    "trophy": ("5226431245918942763", "\U0001f3c6"),
    "crown": ("5229011542011299168", "\U0001f451"),
    "medal": ("5229045747130843073", "\U0001f3c5"),
    "game": ("5453921696354419743", "\U0001f3ae"),
    "swords": ("5454014806950429357", "\u2694\ufe0f"),
    "gem": ("5235630047959727475", "\U0001f48e"),
    "money": ("5375312095346704820", "\U0001f4b0"),
    "shield": ("5465154440287757794", "\U0001f6e1"),
    "eye": ("5228822494730797152", "\U0001f441"),
    "star": ("5226928895189598791", "\u2b50"),
    "hourglass": ("5454415424319931791", "\u23f3"),
    "pin": ("5316883252877738187", "\U0001f4cd"),
    "check": ("5316653334688446735", "\u2705"),
    "cross": ("5454350746407419714", "\u274c"),
    "warn": ("5316554554735607106", "\u26a0\ufe0f"),
    "rocket": ("5316571734604790521", "\U0001f680"),
    "sparkles": ("5319201848022808495", "\u2728"),
    "idcard": ("5422683699130933153", "\U0001faaa"),
    "clock": ("5316575093269214796", "\U0001f552"),
    "gift": ("5203996991054432397", "\U0001f381"),
    "chart": ("6100247479071019057", "\U0001f4ca"),
    "party": ("6309961712703379735", "\U0001f389"),
    "flex": ("5463413771647069835", "\U0001f4aa"),
    "alien": ("5226893221191237996", "\U0001f47e"),
    "envelope": ("5454113432284446338", "\u2709\ufe0f"),
    "lock": ("5455443379917629504", "\U0001f512"),
    "helmet": ("5454168390685965478", "\U0001fa96"),
}

_TG_EMOJI_RE = re.compile(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", re.S)


def premium_on():
    return time.time() >= PREMIUM_BLOCKED_UNTIL


def pe(key):
    eid, fallback = EMOJI.get(key, ("", "\u2022"))
    if not eid or not premium_on():
        return fallback
    return '<tg-emoji emoji-id="{}">{}</tg-emoji>'.format(eid, fallback)


def icon(key):
    eid, _ = EMOJI.get(key, ("", ""))
    if not eid or not premium_on():
        return None
    return eid


def strip_premium(text):
    return _TG_EMOJI_RE.sub(r"\1", text or "")


def block_premium(reason=""):
    global PREMIUM_BLOCKED_UNTIL
    PREMIUM_BLOCKED_UNTIL = time.time() + 900
    log.warning("premium emoji disabled for 15 min: %s", reason)


def B(label, data=None, key=None, url=None, style=None):
    kw = {"style": style} if style else {}
    if url:
        kw["url"] = url
    else:
        kw["callback_data"] = data or "u:noop"
    eid = icon(key) if key else None
    if eid:
        kw["icon_custom_emoji_id"] = eid
    return InlineKeyboardButton(label, **kw)


def plain_button(b):
    kw = {}
    if b.url:
        kw["url"] = b.url
    elif b.copy_text:
        kw["copy_text"] = b.copy_text
    elif b.callback_data:
        kw["callback_data"] = b.callback_data
    return InlineKeyboardButton(b.text, **kw)


def plain_markup(markup):
    if markup is None:
        return None
    return InlineKeyboardMarkup(
        [[plain_button(b) for b in row_] for row_ in markup.inline_keyboard]
    )


def KB(*rows):
    return InlineKeyboardMarkup([r for r in rows if r])


def row(*buttons):
    return [b for b in buttons if b]


# ============================================================================
# DATABASE
# ============================================================================
from dbcore import (  # noqa: E402
    clear_state,
    create_api_key,
    delete_key,
    get_setting,
    get_state,
    init_db,
    is_integrity_error,
    key_row,
    key_stats,
    q,
    q1,
    ex,
    reset_usage,
    revoke_key,
    set_setting,
    set_state,
    touch_user,
    PLANS,
)

try:
    from dbcore import del_setting
except ImportError:
    def del_setting(*keys):
        for k in keys:
            ex("DELETE FROM settings WHERE key=?", (k,))


# ============================================================================
# HELPERS
# ============================================================================
STATUS_LABEL = {
    "open": "\U0001f7e2 Registration open",
    "closed": "\U0001f7e0 Registration closed",
    "started": "\U0001f534 Live / Started",
    "finished": "\u26ab Finished",
}

DEFAULT_RULES = (
    "\u26a0\ufe0f <b>FAIR PLAY WARNING \u2014 READ CAREFULLY</b>\n\n"
    "\U0001f6ab <b>HACKING / CHEATING IS STRICTLY NOT ALLOWED.</b>\n"
    "\u2022 No mod APK, aimbot, auto-headshot, wallhack, script or panel.\n"
    "\u2022 No emulator, mouse/keyboard, teaming, exploits or bug abuse.\n"
    "\u2022 No account sharing, smurf accounts or fake UID.\n\n"
    "\u274c Anyone caught using hacks will be <b>immediately disqualified</b>, "
    "removed from the tournament, prize cancelled and <b>banned from all "
    "future tournaments</b>.\n\n"
    "\u2705 Play fair on your own account. Screenshots or screen recording "
    "may be requested for verification."
)


def is_admin(uid):
    return uid in ADMIN_IDS


def username_of(user):
    return "@" + user.username if getattr(user, "username", None) else "\u2014"


def link_for(code):
    return "https://t.me/{}?start={}".format(BOT_USERNAME or "your_bot", code)


def count_regs(tid):
    r = q1("SELECT COUNT(*) c FROM registrations WHERE tournament_id=?", (tid,))
    return r["c"] if r else 0


def get_tournament(tid):
    return q1("SELECT * FROM tournaments WHERE id=?", (tid,))


def get_tournament_by_code(code):
    return q1("SELECT * FROM tournaments WHERE lower(code)=lower(?)", (code,))


def parse_time(text):
    text = (text or "").strip()
    for fmt in ("%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M",
                "%d-%m-%Y", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime("%d-%m-%Y %H:%M" if "%H" in fmt else "%d-%m-%Y")
        except ValueError:
            continue
    return None


def next_code():
    r = q1("SELECT MAX(id) m FROM tournaments")
    return "t{}".format((r["m"] or 0) + 1)


def bar(pct, width=10):
    pct = max(0, min(100, int(pct or 0)))
    filled = round(width * pct / 100)
    return "\u25b0" * filled + "\u25b1" * (width - filled)


def ts(t):
    try:
        return datetime.fromtimestamp(int(t)).strftime("%d-%m-%Y %H:%M")
    except (TypeError, ValueError):
        return "\u2014"


# ============================================================================
# SEND / EDIT PIPELINE (premium-safe)
# ============================================================================
async def deliver(send_fn, text, markup=None):
    try:
        return await send_fn(text, markup)
    except BadRequest as e:
        msg = str(e).lower()
        if "emoji" in msg or "button" in msg or "entity" in msg:
            block_premium(str(e))
            return await send_fn(strip_premium(text), plain_markup(markup))
        raise


async def send(ctx_or_bot, chat_id, text, markup=None):
    bot = getattr(ctx_or_bot, "bot", ctx_or_bot)
    try:
        return await bot.send_message(
            chat_id, text, reply_markup=markup, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        msg = str(e).lower()
        if "emoji" in msg or "button" in msg or "entity" in msg:
            block_premium(str(e))
            return await bot.send_message(
                chat_id, strip_premium(text), reply_markup=plain_markup(markup),
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
        raise


async def show(update, text, markup=None):
    cq = update.callback_query
    if cq:
        try:
            return await deliver(
                lambda t, m: cq.edit_message_text(
                    t, reply_markup=m, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                ),
                text, markup,
            )
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return None
            log.warning("edit failed, sending new: %s", e)
    msg = getattr(update, "message", None)
    if msg is not None and hasattr(msg, "reply_text"):
        fn = msg.reply_text
    else:
        fn = update.effective_chat.send_message
    return await deliver(
        lambda t, m: fn(t, reply_markup=m, parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True),
        text, markup,
    )


async def answer(update, text=None, alert=False):
    cq = update.callback_query
    if cq:
        try:
            await cq.answer(text, show_alert=alert)
        except TelegramError:
            pass


# ============================================================================
# FORCE JOIN
# ============================================================================
def join_kb():
    rows = []
    if FORCE_JOIN_LINK:
        rows.append([B("\U0001f4e2 Join " + FORCE_JOIN_CHAT, url=FORCE_JOIN_LINK,
                       key="rocket", style="primary")])
    rows.append([B("\u2705 I Joined \u2014 Verify", "u:verify", key="check",
                   style="success")])
    return KB(*rows)


def join_text():
    return (
        "\U0001f512 <b>MEMBERS ONLY</b>\n"
        + "\u2501" * 18 + "\n\n"
        "{} To use this bot you must first join our official channel:\n\n"
        "\U0001f4e2 <b>{}</b>\n\n"
        "1\ufe0f\u20e3 Tap <b>Join</b> below\n"
        "2\ufe0f\u20e3 Tap <b>I Joined \u2014 Verify</b>\n\n"
        "<i>Verification is instant. After that everything unlocks.</i>"
    ).format(pe("lock"), esc(FORCE_JOIN_CHAT or "@channel"))


async def join_state(bot, uid):
    if not FORCE_JOIN_CHAT or is_admin(uid):
        return True
    try:
        m = await bot.get_chat_member(FORCE_JOIN_CHAT, uid)
    except Forbidden:
        return False
    except BadRequest as e:
        s = str(e).lower()
        if "user not found" in s or "participant" in s or "not a member" in s:
            return False
        log.warning("cannot verify join %s: %s", uid, e)
        return None
    except TelegramError as e:
        log.warning("cannot verify join %s: %s", uid, e)
        return None
    return m.status in ("member", "administrator", "creator", "owner")


async def gate(update, ctx):
    user = update.effective_user
    if user is None:
        return True
    uid = user.id
    data = update.callback_query.data if update.callback_query else ""
    if data.startswith("u:verify") or data.startswith("a:"):
        return True
    ok = await join_state(ctx.bot, uid)
    if ok is False:
        touch_user(user, joined_ok=False)
        if update.callback_query:
            await answer(update, "Join the channel first!", alert=True)
            await show(update, join_text(), join_kb())
        else:
            await send(ctx, uid, join_text(), join_kb())
        return False
    touch_user(user, joined_ok=ok)
    return True


# ============================================================================
# PLAYER LOOKUP
# ============================================================================
REGIONS = [
    ("IND", "\U0001f1ee\U0001f1f3"), ("BD", "\U0001f1e7\U0001f1e9"),
    ("PK", "\U0001f1f5\U0001f1f0"), ("SG", "\U0001f1f8\U0001f1ec"),
    ("ID", "\U0001f1ee\U0001f1e9"), ("TH", "\U0001f1f9\U0001f1ed"),
    ("VN", "\U0001f1fb\U0001f1f3"), ("TW", "\U0001f1f9\U0001f1fc"),
    ("BR", "\U0001f1e7\U0001f1f7"), ("US", "\U0001f1fa\U0001f1f8"),
    ("ME", "\U0001f1f2\U0001f1ea"), ("RU", "\U0001f1f7\U0001f1fa"),
    ("CIS", "\U0001f1f0\U0001f1ff"), ("EUROPE", "\U0001f1ea\U0001f1fa"),
    ("SAC", "\U0001f30e"), ("NA", "\U0001f30f"),
]


def lookup_url():
    return (get_setting("lookup_url", "") or LOOKUP_URL).strip()


def lookup_key():
    return (get_setting("lookup_key", "") or LOOKUP_KEY).strip()


def lookup_ban_url():
    return (get_setting("lookup_ban_url", "") or LOOKUP_BAN_URL).strip()


def saved_region():
    return (get_setting("lookup_region", "") or DEFAULT_REGION).upper()


def is_template(url=""):
    return "{uid}" in (url or lookup_url())


def needs_region():
    return "{region}" in lookup_url()


def build_url(url, ff_uid, region):
    out = (url or "").replace("{uid}", str(ff_uid))
    out = out.replace("{region}", (region or "").upper())
    key = lookup_key()
    if key:
        out = out.replace("{key}", key)
    return out


def normalise_url(raw):
    return dbcore.normalize_lookup(raw)


def media_url(kind, ff_uid, region):
    url = lookup_url()
    if "{uid}" not in url:
        return None
    path = url.split("?")[0]
    if not path.endswith("}"):
        return None
    path = path + "/" + kind
    path = path.replace("{uid}", str(ff_uid))
    if "{region}" in path:
        path = path.replace("{region}", (region or "").upper())
    elif needs_region() and region:
        path += "?region=" + region.upper()
    return path


def provider_label():
    url = lookup_url()
    if not url:
        return "not configured"
    if is_template(url):
        return "template API" + (" (region required)" if needs_region() else "")
    return "query API"


async def http_json(url, params=None):
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT,
                                     follow_redirects=True) as client:
            r = await client.get(url, params=params or {},
                                 headers={"User-Agent": "FF-Bot/1.0",
                                          "Accept": "application/json"})
        try:
            data = r.json()
        except ValueError:
            return None, "bad_response"
        if isinstance(data, dict) and data.get("error") and not data.get("basicInfo"):
            msg = data.get("message") or data.get("error")
            return None, str(msg)
        if r.status_code == 404:
            return None, "not found"
        if r.status_code >= 400:
            return None, "http_{}".format(r.status_code)
        if not isinstance(data, dict):
            return None, "bad_response"
        body = data.get("data") if isinstance(data.get("data"), dict) else data
        if not (body.get("basicInfo") or body.get("player") or body.get("nickname")):
            return None, "not found"
        return body, None
    except httpx.TimeoutException:
        return None, "timeout"
    except Exception as e:
        log.warning("lookup request failed: %s", e)
        return None, "network"


async def fetch_player(ff_uid, region=None):
    url = lookup_url()
    if not url:
        return None, "not_configured"
    if is_template(url):
        return await http_json(build_url(url, ff_uid, region))
    return await http_json(url, {"uid": ff_uid, "key": lookup_key()})


async def fetch_ban(ff_uid):
    url = lookup_ban_url()
    if not url:
        return None, "not_configured"
    return await http_json(build_url(url, ff_uid, saved_region()))


def _unix(value):
    try:
        return datetime.utcfromtimestamp(int(value)).strftime("%d-%m-%Y %H:%M")
    except (TypeError, ValueError):
        return "\u2014"


def _clean_sig(sig):
    if not sig:
        return ""
    clean = re.sub(r"\[[0-9a-fA-F]{6}\]|\[b\]|\[i\]|\[c\]|\[/[a-z]\]", "",
                   str(sig))
    return clean.strip()


def _pts(points, show_flag):
    if show_flag is False:
        return "\u2014"
    try:
        return "{:,} pts".format(int(points))
    except (TypeError, ValueError):
        return "\u2014"


def nice(n):
    try:
        return "{:,}".format(int(n))
    except (TypeError, ValueError):
        return "\u2014"


def lookup_error(err, ff_uid="", region=""):
    e = str(err or "").lower()
    if err == "not_configured":
        return (
            "{} <b>LOOKUP NOT SET UP YET</b>\n{}\n\n"
            "Admin has not connected a player API.\n\n"
            "{} <b>Admin \u2192 API Setup \u2192 Set URL</b>"
        ).format(pe("lock"), "\u2501" * 18, pe("rocket"))
    if "api key" in e or "unauthor" in e or "forbidden" in e or "http_403" in e:
        return (
            "{} <b>LOOKUP REJECTED</b>\n{}\n\n<code>{}</code>\n\n"
            "{} Admin: check key/URL in <b>Admin \u2192 API Setup</b>."
        ).format(pe("lock"), "\u2501" * 18, esc(str(err)[:160]), pe("rocket"))
    if "credential" in e or "configured" in e:
        return (
            "{} <b>API HAS NO CREDENTIALS</b>\n{}\n\n<code>{}</code>\n\n"
            "The API server is online but its Garena account is not configured.\n"
            "{} Set <code>FREEFIRE_DEFAULT_UID</code> and "
            "<code>FREEFIRE_DEFAULT_PASSWORD</code> in the API's <code>.env</code>."
        ).format(pe("lock"), "\u2501" * 18, esc(str(err)[:160]), pe("rocket"))
    if err == "timeout":
        return "{} <b>API TIMEOUT</b>\n\nTry again in a moment.".format(pe("hourglass"))
    if err in ("network", "bad_response"):
        return ("{} <b>API UNREACHABLE</b>\n{}\n\n<code>{}</code>\n\n"
                "Host did not return player JSON.").format(
            pe("warn"), "\u2501" * 18, esc(lookup_url() or "\u2014"))
    if "not found" in e or "404" in e:
        return (
            "{} <b>PLAYER NOT FOUND</b>\n{}\n\nUID <code>{}</code> in region "
            "<b>{}</b> was not found.\n\n{} Check the UID\n"
            "{} India accounts use <b>IND</b>"
        ).format(pe("cross"), "\u2501" * 18, esc(ff_uid),
                 esc(region or saved_region()), pe("pin"), pe("star"))
    return "{} <b>LOOKUP FAILED</b>\n{}\n\n<code>{}</code>".format(
        pe("warn"), "\u2501" * 18, esc(str(err)[:200]))


def _to_png(data):
    if data[:4] == b"\x89PNG":
        return data
    if data[:4] == b"RIFF" or data[:6] in (b"GIF87a", b"GIF89a") or data[:2] == b"\xff\xd8":
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as im:
                buf = io.BytesIO()
                im.convert("RGBA").save(buf, format="PNG")
                return buf.getvalue()
        except Exception as e:
            log.warning("image convert failed: %s", e)
            return None
    return data


async def fetch_image(url):
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT,
                                     follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "FF-Bot/1.0"})
        if r.status_code != 200 or len(r.content) < 100:
            return None
        return _to_png(r.content)
    except Exception as e:
        log.warning("image fetch failed: %s", e)
        return None


async def send_image(ctx, chat_id, url, caption=""):
    data = await fetch_image(url)
    if not data:
        return False
    try:
        await ctx.bot.send_photo(
            chat_id, photo=InputFile(io.BytesIO(data), filename="freefire.png"),
            caption=caption[:1000] or None, parse_mode=ParseMode.HTML,
        )
        return True
    except (BadRequest, TelegramError) as e:
        log.warning("image send failed: %s", e)
    return False


def ago(ts):
    try:
        then = int(ts)
    except (TypeError, ValueError):
        return "\u2014"
    diff = int(time.time()) - then
    if diff < 0:
        diff = 0
    if diff < 60:
        return "just now"
    units = ((31536000, "year"), (2592000, "month"), (604800, "week"),
             (86400, "day"), (3600, "hour"), (60, "minute"))
    for size, name in units:
        if diff >= size:
            qty = diff // size
            return "{} {}{} ago".format(qty, name, "" if qty == 1 else "s")
    return "just now"


def fmt_ts(ts):
    try:
        return datetime.utcfromtimestamp(int(ts)).strftime("%d %b %Y, %I:%M %p")
    except (TypeError, ValueError):
        return "\u2014"


def _id_list(values, limit=6):
    if not isinstance(values, list) or not values:
        return "\u2014"
    head = ", ".join(str(v) for v in values[:limit])
    more = "" if len(values) <= limit else " +{} more".format(len(values) - limit)
    return head + more


def _pretty(value):
    if value is None:
        return "\u2014"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str):
        return value.replace("Gender_", "").replace("Language_", "").replace(
            "RankShow_", "").replace("REWARD_STATE_", "").replace(
            "ExternalIconStatus_", "").replace("ExternalIconShowType_", "") or "\u2014"
    return str(value)


def _parts(d):
    b = d.get("basicInfo") or d.get("player") or d
    return (
        b,
        d.get("clanBasicInfo") or {},
        d.get("captainBasicInfo") or {},
        d.get("petInfo") or {},
        d.get("socialInfo") or {},
        d.get("creditScoreInfo") or {},
        d.get("profileInfo") or {},
        d.get("diamondCostRes") or {},
        d.get("mediaInfo") or {},
    )


def head_block(title, key="idcard"):
    return "\u2501" * 20 + "\n{} <b>{}</b>\n".format(pe(key), title) + "\u2501" * 20 + "\n"


def page_overview(ff_uid, region, d):
    b, clan, cap, pet, social, credit, prof, dia, media = _parts(d)
    shown_region = str(b.get("region") or region or "\u2014").upper()
    lines = [
        "{} <b>{}</b>".format(pe("crown"), esc(str(b.get("nickname", "Unknown")))),
        "{} UID <code>{}</code>  \u2022  \U0001f310 Region <b>{}</b>".format(
            pe("pin"), esc(ff_uid), esc(shown_region)),
        "",
        "{} <b>LEVEL</b> <b>{}</b>".format(pe("star"),
                                            esc(str(b.get("level", "\u2014")))),
        "{} <b>EXP</b> <b>{}</b>".format(pe("sparkles"), nice(b.get("exp"))),
        "{} <b>LIKES</b> <b>{}</b>".format(pe("fire"), nice(b.get("liked"))),
        "{} <b>HONOR / CREDIT</b> <b>{}</b>".format(
            pe("shield"), esc(str(credit.get("creditScore", "\u2014")))),
        "{} <b>BADGES</b> <b>{}</b>".format(pe("gem"),
                                            esc(str(b.get("badgeCnt", "\u2014")))),
        "",
        "{} <b>ACCOUNT CREATED</b>".format(pe("gift")),
        "   <b>{}</b>".format(esc(fmt_ts(b.get("createAt")))),
        "   <i>{}</i>".format(esc(ago(b.get("createAt")))),
        "",
        "{} <b>LAST SEEN (LOGIN)</b>".format(pe("clock")),
        "   <b>{}</b>".format(esc(fmt_ts(b.get("lastLoginAt")))),
        "   <i>{}</i>".format(esc(ago(b.get("lastLoginAt")))),
        "",
        "{} Game version <b>{}</b>  \u2022  Season <b>{}</b>".format(
            pe("rocket"), esc(str(b.get("releaseVersion", "\u2014"))),
            esc(str(b.get("seasonId", "\u2014")))),
        "{} Account type <b>{}</b>".format(
            pe("idcard"), esc(str(b.get("accountType", "\u2014")))),
    ]
    if social.get("signature"):
        sig = _clean_sig(social.get("signature"))
        if sig:
            lines += ["", "\u201c<i>{}</i>\u201d".format(esc(sig[:150]))]
    return head_block("PLAYER OVERVIEW") + "\n".join(lines)


def page_ranks(ff_uid, region, d):
    b = _parts(d)[0]
    br_show = b.get("showBrRank", True)
    cs_show = b.get("showCsRank", True)
    lines = [
        "{} <b>BATTLE ROYALE</b>".format(pe("medal")),
        "   Current rank <b>{}</b>".format(esc(str(b.get("rank", "\u2014")))),
        "   Highest rank <b>{}</b>".format(esc(str(b.get("maxRank", "\u2014")))),
        "   Rank points <b>{}</b>".format(nice(b.get("rankingPoints"))),
        "   Visible on profile: <b>{}</b>".format(_pretty(br_show)),
        "",
        "{} <b>CLASH SQUAD</b>".format(pe("swords")),
        "   Current rank <b>{}</b>".format(esc(str(b.get("csRank", "\u2014")))),
        "   Highest rank <b>{}</b>".format(esc(str(b.get("csMaxRank", "\u2014")))),
        "   Rank points <b>{}</b>".format(nice(b.get("csRankingPoints"))),
        "   Visible on profile: <b>{}</b>".format(_pretty(cs_show)),
        "",
        "{} Season <b>{}</b>  \u2022  {} OB <b>{}</b>".format(
            pe("star"), esc(str(b.get("seasonId", "\u2014"))), pe("rocket"),
            esc(str(b.get("releaseVersion", "\u2014")))),
        "{} Rank display mode <b>{}</b>".format(
            pe("eye"), esc(_pretty((d.get("socialInfo") or {}).get("rankShow")))),
        "{} Ranked title ID <code>{}</code>".format(
            pe("crown"), esc(str(b.get("title", "\u2014")))),
    ]
    return head_block("RANKS & SEASON") + "\n".join(lines)


def page_clan(ff_uid, region, d):
    b, clan, cap, pet = _parts(d)[:4]
    lines = []
    if clan.get("clanName") or clan.get("clanId"):
        lines += [
            "{} <b>{}</b>".format(pe("shield"), esc(str(clan.get("clanName", "\u2014")))),
            "   Guild ID <code>{}</code>".format(esc(str(clan.get("clanId", "\u2014")))),
            "   Guild level <b>{}</b>".format(esc(str(clan.get("clanLevel", "\u2014")))),
            "   Members <b>{}/{}</b>".format(esc(str(clan.get("memberNum", "\u2014"))),
                                            esc(str(clan.get("capacity", "\u2014")))),
            "   Leader UID <code>{}</code>".format(
                esc(str(clan.get("captainId", "\u2014")))),
        ]
        if cap.get("nickname"):
            lines += [
                "",
                "{} <b>LEADER PROFILE</b>".format(pe("crown")),
                "   Name <b>{}</b>".format(esc(str(cap.get("nickname")))),
                "   UID <code>{}</code>  \u2022  Level <b>{}</b>".format(
                    esc(str(cap.get("accountId", "\u2014"))),
                    esc(str(cap.get("level", "\u2014")))),
                "   BR <b>{}</b> pts  \u2022  CS <b>{}</b> pts".format(
                    nice(cap.get("rankingPoints")), nice(cap.get("csRankingPoints"))),
                "   Last seen <b>{}</b> (<i>{}</i>)".format(
                    esc(fmt_ts(cap.get("lastLoginAt"))), esc(ago(cap.get("lastLoginAt")))),
            ]
    else:
        lines.append("{} <b>No guild</b> \u2014 this player is not in a guild.".format(
            pe("cross")))
    lines += [""]
    if pet.get("id") or pet.get("name") or pet.get("level"):
        lines += [
            "{} <b>PET</b>".format(pe("game")),
            "   Name <b>{}</b>".format(esc(str(pet.get("name") or pet.get("id") or "\u2014"))),
            "   Level <b>{}</b>  \u2022  EXP <b>{}</b>".format(
                esc(str(pet.get("level", "\u2014"))), nice(pet.get("exp"))),
            "   Equipped now: <b>{}</b>".format(_pretty(pet.get("isSelected"))),
            "   Pet ID <code>{}</code>  \u2022  Skin <code>{}</code>".format(
                esc(str(pet.get("id", "\u2014"))), esc(str(pet.get("skinId", "\u2014")))),
            "   Skill ID <code>{}</code>".format(
                esc(str(pet.get("selectedSkillId", "\u2014")))),
        ]
    else:
        lines.append("{} <b>No pet equipped</b>".format(pe("cross")))
    return head_block("GUILD & PET") + "\n".join(lines)


def page_loadout(ff_uid, region, d):
    b, clan, cap, pet, social, credit, prof, dia, media = _parts(d)
    clothes = prof.get("clothes") or []
    skills = prof.get("equipedSkills") or []
    skins = b.get("weaponSkinShows") or []
    lines = [
        "{} <b>AVATAR</b> <code>{}</code>".format(
            pe("eye"), esc(str(prof.get("avatarId", "\u2014")))),
        "{} <b>HEAD PIC</b> <code>{}</code>".format(
            pe("idcard"), esc(str(b.get("headPic", "\u2014")))),
        "{} <b>BANNER</b> <code>{}</code>".format(
            pe("gem"), esc(str(b.get("bannerId", "\u2014")))),
        "{} <b>PIN</b> <code>{}</code>".format(
            pe("pin"), esc(str(b.get("pinId", "\u2014")))),
        "{} <b>TITLE</b> <code>{}</code>".format(
            pe("crown"), esc(str(b.get("title", "\u2014")))),
        "{} <b>GAME BAG</b> <code>{}</code>".format(
            pe("gift"), esc(str(b.get("gameBagShow", "\u2014")))),
        "{} <b>SKIN COLOR</b> <b>{}</b>".format(
            pe("sparkles"), esc(str(prof.get("skinColor", "\u2014")))),
        "",
        "{} <b>OUTFIT ITEMS</b> ({})".format(pe("flex"), len(clothes)),
        "   <code>{}</code>".format(esc(_id_list(clothes, 8))),
        "",
        "{} <b>SKILLS EQUIPPED</b> ({})".format(pe("swords"), len(skills)),
        "   <code>{}</code>".format(esc(_id_list(skills, 10))),
        "",
        "{} <b>WEAPON SKINS SHOWN</b> ({})".format(pe("medal"), len(skins)),
        "   <code>{}</code>".format(esc(_id_list(skins, 8))),
    ]
    unlock = prof.get("unlockTime")
    if unlock:
        lines += ["", "{} Character unlocked <b>{}</b> (<i>{}</i>)".format(
            pe("hourglass"), esc(fmt_ts(unlock)), esc(ago(unlock)))]
    if prof:
        lines.append("{} Unlock type <b>{}</b>  \u2022  Awakened <b>{}</b>".format(
            pe("star"), esc(_pretty(prof.get("unlockType"))),
            _pretty(prof.get("isSelectedAwaken"))))
    if dia.get("diamondCost"):
        lines.append("{} Diamond value <b>{}</b>".format(
            pe("money"), nice(dia.get("diamondCost"))))
    return head_block("LOADOUT & COSMETICS", "gem") + "\n".join(lines)


def page_social(ff_uid, region, d):
    b, clan, cap, pet, social, credit, prof, dia, media = _parts(d)
    lines = [
        "{} Gender <b>{}</b>".format(pe("eye"), esc(_pretty(social.get("gender")))),
        "{} Language <b>{}</b>".format(pe("pin"), esc(_pretty(social.get("language")))),
        "{} Rank display <b>{}</b>".format(pe("star"),
                                           esc(_pretty(social.get("rankShow")))),
        "{} Bio <b>{}</b>".format(pe("envelope"),
                                  esc(_clean_sig(social.get("signature")) or "\u2014")),
        "",
        "{} <b>HONOR / CREDIT</b>".format(pe("shield")),
        "   Score <b>{}</b>".format(esc(str(credit.get("creditScore", "\u2014")))),
        "   Reward state <b>{}</b>".format(esc(_pretty(credit.get("rewardState")))),
        "   Period ends <b>{}</b>".format(
            esc(fmt_ts(credit.get("periodicSummaryEndTime")))),
        "",
        "{} <b>ACCOUNT FLAGS</b>".format(pe("idcard")),
        "   Account ID <code>{}</code>".format(
            esc(str(b.get("accountId", ff_uid)))),
        "   Badge ID <code>{}</code>".format(esc(str(b.get("badgeId", "\u2014")))),
        "   External icon <b>{}</b> / <b>{}</b>".format(
            esc(_pretty((b.get("externalIconInfo") or {}).get("status"))),
            esc(_pretty((b.get("externalIconInfo") or {}).get("showType")))),
        "   Show BR rank <b>{}</b>  \u2022  Show CS rank <b>{}</b>".format(
            _pretty(b.get("showBrRank")), _pretty(b.get("showCsRank"))),
    ]
    highlights = b.get("socialHighLightsWithBasicInfo")
    if highlights:
        lines.append("{} Highlights <b>{}</b>".format(pe("sparkles"),
                                                      esc(str(highlights)[:120])))
    return head_block("SOCIAL & FLAGS") + "\n".join(lines)


PAGES = (
    ("ov", "Overview", page_overview),
    ("rk", "Ranks", page_ranks),
    ("cl", "Guild & Pet", page_clan),
    ("ld", "Loadout", page_loadout),
    ("so", "Social", page_social),
)


def render_page(name, ff_uid, region, d):
    for pid, _, fn in PAGES:
        if pid == name:
            return fn(ff_uid, region, d)
    return page_overview(ff_uid, region, d)


def player_kb(ff_uid, region, page="ov"):
    tag = region or "-"
    nav = [B(label, "u:pg:{}:{}".format(pid, tag),
             key="check" if pid == page else None) for pid, label, _ in PAGES]
    rows = [nav[:3], nav[3:]] if len(nav) > 3 else [nav]
    rows = [r for r in rows if r]
    rows.append(row(B("Avatar", "u:av:{}:{}".format(ff_uid, tag), key="eye"),
                    B("Banner", "u:bn:{}:{}".format(ff_uid, tag), key="gem")))
    rows.append(row(B("Full JSON", "u:js:{}".format(ff_uid), key="chart"),
                    B("Refresh", "u:re:{}:{}".format(ff_uid, tag), key="rocket")))
    if lookup_ban_url():
        rows.append(row(B("Ban Check", "u:bc:{}".format(ff_uid), key="shield")))
    rows.append(row(B("New Lookup", "u:lk", key="idcard", style="success"),
                    B("Main Menu", "u:menu", key="game")))
    return KB(*rows)


def media_from_payload(d, kind, ff_uid, region):
    media = (d or {}).get("mediaInfo") or {}
    url = media.get("avatarUrl") if kind == "avatar" else media.get("bannerUrl")
    if url and "127.0.0.1" not in url and "localhost" not in url:
        return url
    if url and is_local_host():
        return url
    return media_url(kind, ff_uid, region)


def is_local_host():
    host = lookup_url()
    return "127.0.0.1" in host or "localhost" in host


def region_kb():
    rows, pair = [], []
    for code, flag in REGIONS:
        tag = "\u2705 " if code == saved_region() else ""
        pair.append(B("{}{} {}".format(tag, flag, code), "u:rg:{}".format(code)))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append(row(B("Cancel", "u:menu", key="cross")))
    return KB(*rows)


async def ask_region(update, ctx, ff_uid):
    set_state(update.effective_user.id, "lk_region", {"uid": ff_uid})
    await show(
        update,
        "{} <b>CHOOSE REGION</b>\n{}\n\nUID <code>{}</code>\n\n"
        "Pick the server region of this account.\n"
        "{} India = <b>IND</b> (saved as your default)".format(
            "\U0001f310", "\u2501" * 18, esc(ff_uid),
            pe("star")),
        region_kb(),
    )


async def do_lookup(update, ctx, ff_uid, region=None):
    uid = update.effective_user.id
    await answer(update)
    if not lookup_url():
        return await show(update, lookup_error("not_configured", ff_uid),
                          KB(row(B("Main Menu", "u:menu", key="game"))))
    if needs_region() and not region:
        return await ask_region(update, ctx, ff_uid)
    region = (region or saved_region()).upper()

    await show(update, "{} <b>Searching</b> <code>{}</code> in <b>{}</b> \u2026".format(
        pe("hourglass"), esc(ff_uid), esc(region)))

    data, err = await fetch_player(ff_uid, region)
    b = (data or {}).get("basicInfo") or (data or {}).get("player") or {}
    ex("INSERT INTO lookups(tg_id, ff_uid, nickname, level, region, ok, created_at) "
       "VALUES(?,?,?,?,?,?,?)",
       (uid, ff_uid, b.get("nickname"), b.get("level"),
        b.get("region") or region, 1 if data else 0, int(time.time())))

    if not data:
        return await show(update, lookup_error(err, ff_uid, region),
                          player_kb(ff_uid, region))

    set_state(uid, "lk_done", {"uid": ff_uid, "region": region, "data": data})
    avatar = media_from_payload(data, "avatar", ff_uid, region)
    if avatar:
        await send_image(ctx, uid, avatar,
                         "{} <b>{}</b> \u2022 <code>{}</code>".format(
                             pe("idcard"), esc(str(b.get("nickname", "Free Fire"))),
                             esc(ff_uid)))
    await show(update, render_page("ov", ff_uid, region, data),
               player_kb(ff_uid, region, "ov"))


async def show_lookup_prompt(update, ctx):
    set_state(update.effective_user.id, "lk_uid", {})
    await show(
        update,
        "{} <b>PLAYER LOOKUP</b>\n{}\n\n"
        "Send the <b>Free Fire UID</b> you want to check.\n\n"
        "{} Example: <code>2384343917</code>\n"
        "{} You get level, rank, clan, pet and more".format(
            pe("eye"), "\u2501" * 18, pe("pin"), pe("sparkles")),
        KB(row(B("Cancel", "u:menu", key="cross"))),
    )


# ============================================================================
# USER VIEWS
# ============================================================================
def welcome_text(user):
    return (
        "\u2501" * 20 + "\n"
        "{} <b>BOMBER JANTA PARTY</b> {}\n".format(pe("fire"), pe("fire"))
        + "\u2501" * 20 + "\n"
        "{} <b>Free Fire Tournaments</b>\n"
        "{} <b>Custom Rooms \u2022 Rewards \u2022 Fair Play</b>\n\n"
        "Hey <b>{}</b> \U0001f44b\n"
        "{} Look up any Free Fire UID \u2014 profile + outfit card \u2014 or "
        "register for custom rooms and get the room ID & password here.\n\n"
        "{} <b>HACKING IS NOT ALLOWED.</b> Mod APK, aimbot, scripts or "
        "emulator = <b>permanent ban</b>.".format(
            pe("trophy"), pe("game"), esc(user.first_name or "player"), pe("eye"),
            pe("warn"))
    )


def main_menu_kb(uid):
    rows = [
        row(B("Player Lookup", "u:lk", key="eye", style="primary")),
        row(B("Open Tournaments", "u:list", key="trophy", style="primary")),
        row(B("My Registrations", "u:my", key="idcard"),
            B("Fair Play Rules", "u:rules", key="shield")),
        row(B("My Telegram ID", "u:id", key="pin"),
            B("Help", "u:help", key="envelope")),
    ]
    if FORCE_JOIN_LINK:
        rows.append(row(B("Join Channel", url=FORCE_JOIN_LINK, key="rocket")))
    if is_admin(uid):
        rows.append(row(B("ADMIN PANEL", "a:home", key="crown", style="danger")))
    return KB(*rows)


def t_card(t, with_link=True):
    filled = count_regs(t["id"])
    lines = [
        "{} <b>{}</b>".format(pe("trophy"), esc(t["name"])),
        "{} Code: <code>{}</code>".format(pe("pin"), esc(t["code"])),
        "{} Mode: <b>{}</b>".format(pe("game"), esc(t["mode"])),
        "{} Slots: <b>{}/{}</b>  <code>{}</code>".format(
            pe("flex"), filled, t["slots"],
            bar(filled * 100 / max(1, t["slots"]))),
        "{} Entry: <b>{}</b>".format(pe("money"), esc(t["entry_fee"] or "Free")),
        "{} Start: <b>{}</b>".format(pe("clock"), esc(t["start_at"] or "TBA")),
        "{} {}".format(pe("star"), STATUS_LABEL.get(t["status"], esc(t["status"]))),
    ]
    if with_link:
        lines.append("{} {}".format(pe("rocket"), esc(link_for(t["code"]))))
    if t["room_id"]:
        lines.append("{} Room ID: <code>{}</code>".format(pe("lock"),
                                                          esc(t["room_id"])))
    if t["room_pass"]:
        lines.append("{} Password: <code>{}</code>".format(pe("shield"),
                                                           esc(t["room_pass"])))
    return "\n".join(lines)


async def show_tournament(update, t, uid):
    registered = q1(
        "SELECT 1 FROM registrations WHERE tournament_id=? AND tg_id=?", (t["id"], uid))
    full = count_regs(t["id"]) >= t["slots"]
    text = t_card(t) + "\n\n"
    if registered:
        text += "{} <b>You are already registered.</b>".format(pe("check"))
        kb_ = KB(row(B("My Registrations", "u:my", key="idcard")))
    elif t["status"] != "open":
        text += "{} Registration is not open.".format(pe("cross"))
        kb_ = KB(row(B("Back", "u:list", key="pin")))
    elif full:
        text += "{} <b>All slots are full.</b>".format(pe("cross"))
        kb_ = KB(row(B("Back", "u:list", key="pin")))
    else:
        text += "{} Tap below to lock your slot.".format(pe("sparkles"))
        kb_ = KB(row(B("REGISTER NOW", "u:reg:{}".format(t["id"]), key="fire",
                      style="success")),
                 row(B("Back", "u:list", key="pin")))
    await show(update, text, kb_)


async def show_open_list(update, ctx):
    rows_ = q("SELECT * FROM tournaments WHERE status='open' ORDER BY id DESC LIMIT 20")
    if not rows_:
        return await show(
            update,
            "{} <b>No open tournaments right now.</b>\n\n"
            "New tournaments are announced in the channel \u2014 stay tuned!".format(
                pe("hourglass")),
            KB(row(B("Refresh", "u:list", key="rocket")),
               row(B("Main Menu", "u:menu", key="game"))),
        )
    buttons = [row(B("{} \u2022 {}/{}".format(t["name"][:28], count_regs(t["id"]),
                                             t["slots"]),
                     "u:t:{}".format(t["id"]), key="trophy")) for t in rows_]
    buttons.append(row(B("Main Menu", "u:menu", key="game")))
    await show(
        update,
        "{} <b>OPEN TOURNAMENTS</b>\n{}\n\nTap a tournament to see details "
        "and register.".format(pe("trophy"), "\u2501" * 18),
        KB(*buttons),
    )


def my_summary(uid):
    rows_ = q(
        "SELECT r.*, t.name tname, t.code tcode, t.status tstatus, "
        "t.room_id room_id, t.room_pass room_pass FROM registrations r "
        "JOIN tournaments t ON t.id=r.tournament_id WHERE r.tg_id=? "
        "ORDER BY r.id DESC LIMIT 20",
        (uid,),
    )
    if not rows_:
        return None
    lines = ["{} <b>MY REGISTRATIONS</b>".format(pe("idcard")), "\u2501" * 18, ""]
    for r in rows_:
        room = ""
        if r["tstatus"] in ("started", "finished") and r["room_id"]:
            room = "\n   {} Room <code>{}</code> | {} <code>{}</code>".format(
                pe("lock"), esc(r["room_id"] or "-"), pe("shield"),
                esc(r["room_pass"] or "-"))
        lines.append(
            "{} <b>{}</b> (<code>{}</code>)\n"
            "   {} {} | UID <code>{}</code>\n"
            "   {} {}{}".format(
                pe("trophy"), esc(r["tname"]), esc(r["tcode"]), pe("game"),
                esc(r["ff_ign"]), esc(r["ff_uid"]), pe("star"),
                STATUS_LABEL.get(r["tstatus"], esc(r["tstatus"])), room))
        lines.append("")
    return "\n".join(lines)


def consent_kb(tid):
    return KB(row(B("I AGREE \u2014 Continue", "u:agree:{}".format(tid), key="check",
                    style="success")),
              row(B("No, Cancel", "u:cancel", key="cross", style="danger")))


async def begin_registration(update, ctx, tid):
    uid = update.effective_user.id
    t = get_tournament(tid)
    if not t:
        return await show(update, "{} Tournament not found.".format(pe("cross")),
                          KB(row(B("Main Menu", "u:menu", key="game"))))
    if t["status"] != "open":
        return await show(update, "{} Registration is closed.".format(pe("cross")),
                          KB(row(B("Back", "u:list", key="pin"))))
    if count_regs(tid) >= t["slots"]:
        return await show(update, "{} All slots are full.".format(pe("cross")),
                          KB(row(B("Back", "u:list", key="pin"))))
    if q1("SELECT 1 FROM registrations WHERE tournament_id=? AND tg_id=?", (tid, uid)):
        return await show(update, "{} Already registered.".format(pe("check")),
                          KB(row(B("My Registrations", "u:my", key="idcard"))))
    set_state(uid, "reg_consent", {"tid": tid})
    await show(
        update,
        "{} <b>REGISTRATION \u2014 {}</b>\n{}\n\n{}\n\n"
        "You will enter: 1\ufe0f\u20e3 in-game name  2\ufe0f\u20e3 Free Fire UID".format(
            pe("trophy"), esc(t["name"]), "\u2501" * 18,
            get_setting("rules", DEFAULT_RULES)),
        consent_kb(tid),
    )


async def finish_registration(update, ctx, tid):
    uid = update.effective_user.id
    state, data, _ = get_state(uid)
    if state != "reg_confirm":
        return await show(update, "{} Session expired. Start again.".format(pe("hourglass")),
                          KB(row(B("Back", "u:list", key="pin"))))
    t = get_tournament(tid)
    if not t or t["status"] != "open":
        clear_state(uid)
        return await show(update, "{} Registration is closed.".format(pe("cross")),
                          KB(row(B("Back", "u:list", key="pin"))))
    if count_regs(tid) >= t["slots"]:
        clear_state(uid)
        return await show(update, "{} All slots are full.".format(pe("cross")),
                          KB(row(B("Back", "u:list", key="pin"))))

    user = update.effective_user
    ign = (data.get("ign") or "").strip()
    ff_uid = (data.get("uid") or "").strip()
    try:
        rid = ex(
            "INSERT INTO registrations(tournament_id, tg_id, tg_username, tg_name, "
            "ff_ign, ff_uid, created_at) VALUES(?,?,?,?,?,?,?)",
            (tid, uid, user.username, user.full_name, ign, ff_uid, int(time.time())),
        )
    except Exception as e:
        if not is_integrity_error(e):
            raise
        clear_state(uid)
        return await show(
            update,
            "{} This Telegram account or UID is already registered.".format(pe("warn")),
            KB(row(B("My Registrations", "u:my", key="idcard"))),
        )
    clear_state(uid)
    t = get_tournament(tid)
    await show(
        update,
        "\u2501" * 20 + "\n"
        "{} <b>REGISTRATION CONFIRMED</b>\n".format(pe("check"))
        + "\u2501" * 20 + "\n\n"
        "{} Ticket: <code>#{}</code>\n".format(pe("gift"), rid)
        + "{} <b>{}</b>\n".format(pe("trophy"), esc(t["name"]))
        + "{} IGN: <b>{}</b>\n".format(pe("game"), esc(ign))
        + "{} UID: <code>{}</code>\n".format(pe("idcard"), esc(ff_uid))
        + "{} Telegram: {}\n".format(pe("envelope"), esc(username_of(user)))
        + "{} Start: <b>{}</b>\n\n".format(pe("clock"), esc(t["start_at"] or "TBA"))
        + "{} <b>Hacking / cheating is not allowed.</b> Room ID & password will "
          "be sent here before the match. Good luck!".format(pe("warn")),
        KB(row(B("My Registrations", "u:my", key="idcard")),
           row(B("Main Menu", "u:menu", key="game"))),
    )
    await notify_registration(ctx, t, user, ign, ff_uid)


# ============================================================================
# ADMIN NOTIFICATIONS
# ============================================================================
async def notify_admins(ctx, text, markup=None):
    for aid in sorted(ADMIN_IDS):
        try:
            await send(ctx, aid, text, markup)
        except (TelegramError, Forbidden) as e:
            log.warning("admin %s notify failed: %s", aid, e)


async def notify_registration(ctx, t, user, ign, ff_uid):
    await notify_admins(
        ctx,
        "\u2501" * 20 + "\n"
        "{} <b>NEW REGISTRATION</b>\n".format(pe("fire"))
        + "\u2501" * 20 + "\n\n"
        + "{} <b>{}</b> (<code>{}</code>)\n".format(pe("trophy"), esc(t["name"]),
                                                   esc(t["code"]))
        + "{} IGN: <b>{}</b>\n".format(pe("game"), esc(ign))
        + "{} FF UID: <code>{}</code>\n\n".format(pe("idcard"), esc(ff_uid))
        + "{} Telegram: {}\n".format(pe("envelope"), esc(user.full_name or "-"))
        + "{} Username: {}\n".format(pe("pin"), esc(username_of(user)))
        + "{} Telegram ID: <code>{}</code>\n".format(pe("eye"), user.id)
        + "{} Slots: <b>{}/{}</b>\n\n".format(pe("flex"), count_regs(t["id"]),
                                              t["slots"])
        + "{} Remind the player: hacks = disqualification + ban.".format(pe("warn")),
        KB(row(B("View Players", "a:pl:{}:0".format(t["id"]), key="eye"),
               B("Admin Panel", "a:home", key="crown"))),
    )


# ============================================================================
# ADMIN VIEWS
# ============================================================================
def admin_home_text():
    tot_t = q1("SELECT COUNT(*) c FROM tournaments")["c"]
    open_t = q1("SELECT COUNT(*) c FROM tournaments WHERE status='open'")["c"]
    tot_r = q1("SELECT COUNT(*) c FROM registrations")["c"]
    tot_u = q1("SELECT COUNT(*) c FROM users")["c"]
    return (
        "\u2501" * 20 + "\n"
        "{} <b>ADMIN PANEL</b>\n".format(pe("crown"))
        + "\u2501" * 20 + "\n\n"
        + "{} Tournaments: <b>{}</b> (open {})\n".format(pe("trophy"), tot_t, open_t)
        + "{} Registrations: <b>{}</b>\n".format(pe("idcard"), tot_r)
        + "{} Users in bot: <b>{}</b>\n".format(pe("eye"), tot_u)
        + "{} Lookups: <b>{}</b>\n".format(
            pe("chart"), q1("SELECT COUNT(*) c FROM lookups")["c"])
        + "{} API keys: <b>{}</b> active / <b>{}</b> total \u2022 <b>{}</b> requests\n".format(
            pe("gem"), key_stats()["active"], key_stats()["total"],
            key_stats()["requests"])
        + "{} Lookup key: <b>{}</b>\n".format(
            pe("lock"), provider_label())
        + "{} Force join: <b>{}</b>\n\n".format(
            pe("lock"), esc(FORCE_JOIN_CHAT or "disabled"))
        + "Only admins can create or start tournaments."
    )


def admin_home_kb():
    return KB(
        row(B("Create Tournament", "a:new", key="fire", style="success"),
            B("Tournaments", "a:list:0", key="trophy")),
        row(B("Tournament Links", "a:links", key="rocket"),
            B("Registrations", "a:regs", key="idcard")),
        row(B("Users", "a:users:0", key="crown"),
            B("Stats", "a:stats", key="chart")),
        row(B("Lookup Log", "a:logs", key="eye"),
            B("API Setup", "a:key", key="lock")),
        row(B("API Keys", "a:keys:0", key="gem", style="primary"),
            B("Create Key", "a:newkey", key="sparkles", style="success")),
        row(B("Edit Rules", "a:rules", key="shield"),
            B("Broadcast", "a:cast", key="envelope")),
    )


def admin_t_kb(t):
    tid = t["id"]
    rows = [
        row(B("Get Link", "a:act:link:{}".format(tid), key="rocket"),
            B("Players ({})".format(count_regs(tid)), "a:pl:{}:0".format(tid),
              key="eye")),
    ]
    if t["status"] == "open":
        rows.append(row(B("Close Registration", "a:act:close:{}".format(tid),
                          key="lock")))
    elif t["status"] == "closed":
        rows.append(row(B("Open Registration", "a:act:open:{}".format(tid),
                          key="check")))
    if t["status"] in ("open", "closed"):
        rows.append(row(B("START TOURNAMENT", "a:act:start:{}".format(tid),
                          key="fire", style="danger")))
    if t["status"] == "started":
        rows.append(row(B("Finish Tournament", "a:act:finish:{}".format(tid),
                          key="medal")))
    rows.append(row(B("Export CSV", "a:act:csv:{}".format(tid), key="chart"),
                    B("Delete", "a:act:del:{}".format(tid), key="cross",
                      style="danger")))
    rows.append(row(B("Back", "a:list:0", key="pin")))
    return KB(*rows)


def admin_list_view(page=0, per=8):
    total = q1("SELECT COUNT(*) c FROM tournaments")["c"]
    rows_ = q("SELECT * FROM tournaments ORDER BY id DESC LIMIT ? OFFSET ?",
              (per, page * per))
    if not rows_:
        return ("{} <b>No tournaments yet.</b>\nCreate your first one.".format(
            pe("hourglass")),
            KB(row(B("Create Tournament", "a:new", key="fire", style="success"))))
    lines = ["{} <b>TOURNAMENTS</b>  ({}/{})".format(
        pe("trophy"), page + 1, max(1, -(-total // per))), "\u2501" * 18, ""]
    buttons = []
    for t in rows_:
        lines.append("{} <b>{}</b> \u2022 <code>{}</code> \u2022 {}/{} \u2022 {}".format(
            {"open": "\U0001f7e2", "closed": "\U0001f7e0", "started": "\U0001f534",
             "finished": "\u26ab"}.get(t["status"], "\u2022"),
            esc(t["name"]), esc(t["code"]), count_regs(t["id"]), t["slots"],
            STATUS_LABEL.get(t["status"], esc(t["status"]))))
        buttons.append(row(B(t["name"][:30], "a:t:{}".format(t["id"]), key="trophy")))
    nav = []
    if page > 0:
        nav.append(B("Prev", "a:list:{}".format(page - 1), key="pin"))
    if (page + 1) * per < total:
        nav.append(B("Next", "a:list:{}".format(page + 1), key="rocket"))
    if nav:
        buttons.append(nav)
    buttons.append(row(B("Admin Home", "a:home", key="crown")))
    return "\n".join(lines), KB(*buttons)


def players_page(tid, page, per=15):
    t = get_tournament(tid)
    if not t:
        return "Tournament not found.", KB(row(B("Back", "a:list:0")))
    total = count_regs(tid)
    rows_ = q("SELECT * FROM registrations WHERE tournament_id=? ORDER BY id "
              "LIMIT ? OFFSET ?", (tid, per, page * per))
    lines = ["{} <b>{}</b> \u2014 {}/{} slots".format(
        pe("eye"), esc(t["name"]), total, t["slots"]), "\u2501" * 18, ""]
    if not rows_:
        lines.append("No registrations yet.")
    for i, r in enumerate(rows_, start=page * per + 1):
        lines.append(
            "{}. <b>{}</b> \u2022 <code>{}</code> \u2022 {} \u2022 <code>{}</code>".format(
                i, esc(r["ff_ign"]), esc(r["ff_uid"]),
                esc(r["tg_username"] or "no username"), r["tg_id"]))
    nav = []
    if page > 0:
        nav.append(B("Prev", "a:pl:{}:{}".format(tid, page - 1), key="pin"))
    if (page + 1) * per < total:
        nav.append(B("Next", "a:pl:{}:{}".format(tid, page + 1), key="rocket"))
    rows_kb = [nav] if nav else []
    rows_kb.append(row(B("Export CSV", "a:act:csv:{}".format(tid), key="chart"),
                       B("Tournament", "a:t:{}".format(tid), key="trophy")))
    return "\n".join(lines), KB(*rows_kb)


def registrations_csv(tid):
    t = get_tournament(tid)
    rows_ = q("SELECT * FROM registrations WHERE tournament_id=? ORDER BY id", (tid,))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["#", "Tournament", "Code", "In-Game Name", "Free Fire UID",
                "Telegram ID", "Telegram Username", "Telegram Name", "Registered At"])
    for i, r in enumerate(rows_, start=1):
        w.writerow([i, t["name"] if t else "", t["code"] if t else "", r["ff_ign"],
                    r["ff_uid"], r["tg_id"], r["tg_username"] or "",
                    r["tg_name"] or "", ts(r["created_at"])])
    return buf.getvalue().encode("utf-8-sig"), len(rows_)


async def create_tournament(ctx, chat_id, creator_id, data, code):
    try:
        tid = ex(
            "INSERT INTO tournaments(code, name, mode, slots, entry_fee, start_at, "
            "status, created_by, created_at) VALUES(?,?,?,?,?,?,'open',?,?)",
            (code, data.get("name"), data.get("mode", "Solo"), data.get("slots", 48),
             data.get("fee", "Free"), data.get("time"), creator_id, int(time.time())),
        )
    except Exception as e:
        if not is_integrity_error(e):
            raise
        return None
    t = get_tournament(tid)
    await send(
        ctx, chat_id,
        "{} <b>TOURNAMENT CREATED</b>\n{}\n\n{}\n\n{} Share link:\n<code>{}</code>".format(
            pe("check"), "\u2501" * 18, t_card(t), pe("rocket"),
            esc(link_for(t["code"]))),
        KB(row(B("Open Link", url=link_for(t["code"]), key="rocket")),
           row(B("Admin Panel", "a:home", key="crown"))),
    )
    await notify_admins(
        ctx, "{} <b>New tournament</b> \u2014 {}".format(pe("sparkles"),
                                                         esc(t["name"])))
    return tid


async def broadcast_start(ctx, t):
    rows_ = q("SELECT tg_id FROM registrations WHERE tournament_id=?", (t["id"],))
    text = (
        "\u2501" * 20 + "\n"
        "{} <b>TOURNAMENT STARTED</b>\n".format(pe("fire"))
        + "\u2501" * 20 + "\n\n"
        + "{} <b>{}</b>\n".format(pe("trophy"), esc(t["name"]))
        + "{} Room ID: <code>{}</code>\n".format(pe("lock"),
                                                esc(t["room_id"] or "\u2014"))
        + "{} Password: <code>{}</code>\n\n".format(pe("shield"),
                                                   esc(t["room_pass"] or "\u2014"))
        + "{} Join the custom room now.\n".format(pe("hourglass"))
        + "{} Hacking/cheating = instant disqualification + ban.".format(pe("warn"))
    )
    sent = 0
    for r in rows_:
        try:
            await send(ctx, r["tg_id"], text)
            sent += 1
        except TelegramError:
            continue
        await asyncio.sleep(0.05)
    return sent


# ============================================================================
# TEXT ENTRY STATE MACHINE
# ============================================================================
ADMIN_STATES = {"nt_name", "nt_mode", "nt_slots", "nt_fee", "nt_time", "nt_code",
                "room_id", "room_pass", "room_confirm", "edit_rules", "cast_msg",
                "set_key", "set_url", "test_uid", "test_region",
                "key_label", "key_plan"}


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    user = update.effective_user
    uid = user.id
    text = (update.message.text or "").strip()
    state, data, _ = get_state(uid)

    if state in ADMIN_STATES and not is_admin(uid):
        clear_state(uid)
        state = None

    if not state:
        return await deliver(
            lambda t, m: update.message.reply_text(
                t, reply_markup=m, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True),
            welcome_text(user), main_menu_kb(uid))

    try:
        if state == "lk_uid":
            if not re.fullmatch(r"\d{5,%d}" % MAX_UID_DIGITS, text):
                return await update.message.reply_text(
                    "{} UID must be numbers only (5\u2013{} digits). Send it again:".format(
                        "\u26a0\ufe0f", MAX_UID_DIGITS))
            clear_state(uid)
            return await do_lookup(update, ctx, text)

        if state == "reg_ign":
            if not (2 <= len(text) <= MAX_IGN):
                return await update.message.reply_text(
                    "{} In-game name must be 2\u2013{} characters.\nTry again:".format(
                        "\u26a0\ufe0f", MAX_IGN))
            data["ign"] = text
            set_state(uid, "reg_uid", data)
            return await update.message.reply_text(
                "{} <b>Step 2 of 2</b>\n\nSend your <b>Free Fire UID</b> "
                "(numbers only, 6\u2013{} digits).".format("\U0001f3ae", MAX_UID_DIGITS),
                parse_mode=ParseMode.HTML,
                reply_markup=KB(row(B("Cancel", "u:cancel", key="cross"))),
            )

        if state == "reg_uid":
            if not re.fullmatch(r"\d{6,%d}" % MAX_UID_DIGITS, text):
                return await update.message.reply_text(
                    "{} UID must be numbers only, 6\u2013{} digits.\nTry again:".format(
                        "\u26a0\ufe0f", MAX_UID_DIGITS))
            tid = data.get("tid")
            if q1("SELECT 1 FROM registrations WHERE tournament_id=? AND ff_uid=?",
                  (tid, text)):
                clear_state(uid)
                return await update.message.reply_text(
                    "{} This UID is already registered for this tournament.".format(
                        "\u26a0\ufe0f"),
                    reply_markup=main_menu_kb(uid))
            data["uid"] = text
            set_state(uid, "reg_confirm", data)
            t = get_tournament(tid)
            return await update.message.reply_text(
                "{} <b>Confirm your details</b>\n{}\n\n"
                "\U0001f3c6 {}\n\U0001f3ae IGN: <b>{}</b>\n\U0001faaa UID: <code>{}</code>\n"
                "\u2709\ufe0f Telegram: {}\n\U0001f441 ID: <code>{}</code>\n\n"
                "\u26a0\ufe0f Wrong IGN/UID = disqualification. Hacking = ban.".format(
                    "\U0001f50e", "\u2501" * 18, t["name"] if t else "-", data["ign"],
                    text, username_of(user), uid),
                parse_mode=ParseMode.HTML,
                reply_markup=KB(
                    row(B("Confirm & Register", "u:ok:{}".format(tid), key="check",
                          style="success")),
                    row(B("Cancel", "u:cancel", key="cross", style="danger")),
                ),
            )

        if state == "nt_name":
            if not (3 <= len(text) <= 60):
                return await update.message.reply_text(
                    "{} Name must be 3\u201360 characters.".format("\u26a0\ufe0f"),
                    reply_markup=KB(row(B("Cancel", "a:cancel", key="cross"))))
            data["name"] = text
            set_state(uid, "nt_mode", data)
            return await update.message.reply_text(
                "\U0001f3ae <b>Step 2/6</b> \u2014 choose the mode:",
                parse_mode=ParseMode.HTML,
                reply_markup=KB(
                    row(B("Solo", "a:mode:Solo"), B("Duo", "a:mode:Duo")),
                    row(B("Squad", "a:mode:Squad"), B("Lone Wolf", "a:mode:Lone Wolf")),
                    row(B("Cancel", "a:cancel", key="cross")),
                ),
            )

        if state == "nt_slots":
            if not (text.isdigit() and 2 <= int(text) <= 1000):
                return await update.message.reply_text(
                    "{} Send slots as a number 2\u20131000.".format("\u26a0\ufe0f"),
                    reply_markup=KB(row(B("Cancel", "a:cancel", key="cross"))))
            data["slots"] = int(text)
            set_state(uid, "nt_fee", data)
            return await update.message.reply_text(
                "\U0001f4b0 <b>Step 4/6</b> \u2014 entry fee "
                "(<code>Free</code>, <code>20 coins</code>, <code>\u20b930</code>):",
                parse_mode=ParseMode.HTML,
                reply_markup=KB(row(B("Cancel", "a:cancel", key="cross"))),
            )

        if state == "nt_fee":
            if not (1 <= len(text) <= 30):
                return await update.message.reply_text(
                    "{} Entry fee must be 1\u201330 characters.".format("\u26a0\ufe0f"),
                    reply_markup=KB(row(B("Cancel", "a:cancel", key="cross"))))
            data["fee"] = text
            set_state(uid, "nt_time", data)
            return await update.message.reply_text(
                "\U0001f552 <b>Step 5/6</b> \u2014 start date & time\n\n"
                "Format: <code>DD-MM-YYYY HH:MM</code>\n"
                "Example: <code>25-10-2026 20:30</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=KB(row(B("Cancel", "a:cancel", key="cross"))),
            )

        if state == "nt_time":
            when = parse_time(text)
            if not when:
                return await update.message.reply_text(
                    "{} Could not read that date.\nUse <code>DD-MM-YYYY HH:MM</code>".format(
                        "\u26a0\ufe0f"), parse_mode=ParseMode.HTML,
                    reply_markup=KB(row(B("Cancel", "a:cancel", key="cross"))))
            data["time"] = when
            set_state(uid, "nt_code", data)
            return await update.message.reply_text(
                "\U0001f517 <b>Step 6/6</b> \u2014 send a code for the bot link "
                "(a\u2013z, 0\u20139, 2\u201320 chars, e.g. <code>t1</code>).\n\n"
                "It becomes <code>t.me/{}?start=YOURCODE</code>".format(
                    esc(BOT_USERNAME or "your_bot")),
                parse_mode=ParseMode.HTML,
                reply_markup=KB(row(B("Auto Code", "a:ntcode:auto", key="rocket")),
                                row(B("Cancel", "a:cancel", key="cross"))),
            )

        if state == "nt_code":
            code = text.lower()
            if not re.fullmatch(r"[a-z0-9_]{2,20}", code):
                return await update.message.reply_text(
                    "{} Code must be 2\u201320 chars: a-z, 0-9, _".format("\u26a0\ufe0f"),
                    reply_markup=KB(row(B("Auto Code", "a:ntcode:auto", key="rocket")),
                                    row(B("Cancel", "a:cancel", key="cross"))))
            if get_tournament_by_code(code):
                return await update.message.reply_text(
                    "{} That code is taken. Send another:".format("\u26a0\ufe0f"),
                    reply_markup=KB(row(B("Auto Code", "a:ntcode:auto", key="rocket")),
                                    row(B("Cancel", "a:cancel", key="cross"))))
            tid = await create_tournament(ctx, update.effective_chat.id, uid, data, code)
            if tid:
                clear_state(uid)
            else:
                await update.message.reply_text(
                    "{} Code conflict. Send another:".format("\u26a0\ufe0f"),
                    reply_markup=KB(row(B("Auto Code", "a:ntcode:auto", key="rocket"))))
            return

        if state == "room_id":
            data["room_id"] = text[:40]
            set_state(uid, "room_pass", data)
            return await update.message.reply_text(
                "\U0001f511 Now send the <b>room password</b>:",
                parse_mode=ParseMode.HTML,
                reply_markup=KB(row(B("Cancel", "a:cancel", key="cross"))),
            )

        if state == "room_pass":
            data["room_pass"] = text[:40]
            set_state(uid, "room_confirm", data)
            t = get_tournament(data["tid"])
            return await update.message.reply_text(
                "\u25b6\ufe0f <b>Start this tournament?</b>\n{}\n\n"
                "\U0001f3c6 {}\n\U0001f512 Room <code>{}</code>\n\U0001f6e1 Password "
                "<code>{}</code>\n\nRegistration closes and <b>{} players</b> get "
                "the room details.".format(
                    "\u2501" * 18, t["name"] if t else "-", data["room_id"],
                    data["room_pass"], count_regs(data["tid"])),
                parse_mode=ParseMode.HTML,
                reply_markup=KB(
                    row(B("Yes, Start Now", "a:startnow", key="fire",
                          style="danger")),
                    row(B("Cancel", "a:cancel", key="cross")),
                ),
            )

        if state == "key_label":
            if not (2 <= len(text) <= 60):
                return await update.message.reply_text(
                    "{} Label must be 2\u201360 characters.".format("\u26a0\ufe0f"),
                    reply_markup=KB(row(B("Cancel", "a:cancel", key="cross"))))
            set_state(uid, "key_plan", {"label": text})
            return await update.message.reply_text(
                "{} <b>NEW API KEY \u2014 2/2</b>\n{}\n\nLabel: <b>{}</b>\n\n"
                "Choose the plan:".format(pe("gem"), "\u2501" * 18, esc(text)),
                parse_mode=ParseMode.HTML,
                reply_markup=KB(
                    row(B("Free \u2014 100/day", "a:keyplan:free", key="star"),
                        B("Pro \u2014 1K/day", "a:keyplan:pro", key="gem")),
                    row(B("VIP \u2014 10K/day", "a:keyplan:vip", key="crown"),
                        B("Unlimited", "a:keyplan:unlimited", key="fire")),
                    row(B("Cancel", "a:cancel", key="cross")),
                ))

        if state == "set_key":
            if text == "-":
                ex("DELETE FROM settings WHERE key='lookup_key'")
                clear_state(uid)
                return await update.message.reply_text(
                    "{} Key cleared.".format(pe("check")),
                    reply_markup=KB(row(B("Admin Panel", "a:home", key="crown"))))
            if not (3 <= len(text) <= 200):
                return await update.message.reply_text(
                    "{} Key looks wrong (3\u2013200 chars). Send again:".format(
                        "\u26a0\ufe0f"),
                    reply_markup=KB(row(B("Cancel", "a:cancel", key="cross"))))
            set_setting("lookup_key", text)
            clear_state(uid)
            return await update.message.reply_text(
                "{} API key saved.".format(pe("check")),
                reply_markup=KB(row(B("Admin Panel", "a:home", key="crown"))))

        if state == "set_url":
            if not re.match(r"^https?://\S+", text):
                return await update.message.reply_text(
                    "{} Send a full URL starting with http:// or https://".format(
                        "\u26a0\ufe0f"),
                    reply_markup=KB(row(B("Cancel", "a:cancel", key="cross"))))
            url = normalise_url(text)
            set_setting("lookup_url", url)
            clear_state(uid)
            return await update.message.reply_text(
                "{} <b>API connected</b>\n{}\n\n{} <code>{}</code>\n\n"
                "{} Provider: <b>{}</b>".format(
                    pe("check"), "\u2501" * 18, pe("rocket"), esc(url),
                    pe("star"), esc(provider_label())),
                parse_mode=ParseMode.HTML,
                reply_markup=KB(row(B("Test Lookup", "a:atest", key="check")),
                                row(B("Admin Panel", "a:home", key="crown"))))

        if state == "test_uid":
            if not re.fullmatch(r"\d{5,%d}" % MAX_UID_DIGITS, text):
                return await update.message.reply_text(
                    "{} UID must be numbers only.".format("\u26a0\ufe0f"),
                    reply_markup=KB(row(B("Cancel", "a:cancel", key="cross"))))
            set_state(uid, "test_region", {"uid": text})
            return await update.message.reply_text(
                "\U0001f310 Test with which region?",
                reply_markup=KB(
                    row(B("IND", "a:atestreg:IND"), B("BD", "a:atestreg:BD"),
                        B("SG", "a:atestreg:SG"), B("PK", "a:atestreg:PK")),
                    row(B("Cancel", "a:cancel", key="cross"))))

        if state == "edit_rules":
            if len(text) < 20:
                return await update.message.reply_text(
                    "{} Rules text is too short (min 20 chars).".format("\u26a0\ufe0f"),
                    reply_markup=KB(row(B("Cancel", "a:cancel", key="cross"))))
            set_setting("rules", text)
            clear_state(uid)
            return await update.message.reply_text(
                "{} Rules updated.".format("\u2705"),
                reply_markup=KB(row(B("Admin Panel", "a:home", key="crown"))))

        if state == "cast_msg":
            clear_state(uid)
            users = q("SELECT tg_id FROM users")
            ok = 0
            for u in users:
                try:
                    await send(ctx, u["tg_id"],
                               "\U0001f4e2 <b>ANNOUNCEMENT</b>\n" + "\u2501" * 18 +
                               "\n\n" + esc(text), None)
                    ok += 1
                except TelegramError:
                    continue
                await asyncio.sleep(0.05)
            return await update.message.reply_text(
                "{} Broadcast sent to <b>{}/{}</b> users.".format("\U0001f4e4", ok,
                                                                 len(users)),
                reply_markup=KB(row(B("Admin Panel", "a:home", key="crown"))))

        clear_state(uid)
        return await update.message.reply_text(
            "{} Session expired. Use /start.".format("\u231b"),
            reply_markup=main_menu_kb(uid))
    except Exception:
        log.exception("on_text failed")
        clear_state(uid)
        await update.message.reply_text(
            "\u26a0\ufe0f Something went wrong. Please start again with /start.")


# ============================================================================
# USER CALLBACKS
# ============================================================================
async def user_cb(update, ctx, parts, uid):
    a = parts[1]
    if a == "noop":
        return
    if a == "verify":
        ok = await join_state(ctx.bot, uid)
        await answer(update)
        if ok is False:
            return await show(
                update,
                "{} <b>Not joined yet.</b>\n\nJoin <b>{}</b> and tap Verify again.".format(
                    pe("cross"), esc(FORCE_JOIN_CHAT)),
                join_kb(),
            )
        touch_user(update.effective_user, joined_ok=True)
        return await show(update, welcome_text(update.effective_user),
                          main_menu_kb(uid))
    if a == "menu":
        return await show(update, welcome_text(update.effective_user),
                          main_menu_kb(uid))
    if a == "help":
        admin_line = "\n/admin \u2014 admin panel" if is_admin(uid) else ""
        return await show(
            update,
            "\U0001f4d1 <b>HOW TO USE</b>\n{}\n\n"
            "{} <b>Player Lookup</b> \u2014 any UID: profile + outfit\n"
            "{} <b>Open Tournaments</b> \u2014 pick an event and register\n"
            "{} <b>My Registrations</b> \u2014 your tickets + room ID\n"
            "{} <b>Fair Play Rules</b> \u2014 hack policy\n\n"
            "Commands: /start /lookup /tournaments /my /rules /id{}".format(
                "\u2501" * 18, pe("eye"), pe("trophy"), pe("idcard"), pe("shield"),
                admin_line),
            KB(row(B("Main Menu", "u:menu", key="game"))))
    if a == "lk":
        return await show_lookup_prompt(update, ctx)
    if a == "rg":
        state, data, _ = get_state(uid)
        ff_uid = data.get("uid")
        if not ff_uid:
            return await show(update, "{} Session expired. Send the UID again.".format(
                pe("hourglass")), KB(row(B("Player Lookup", "u:lk", key="eye"))))
        region = parts[2].upper()
        ex("INSERT INTO settings(key, value) VALUES('lookup_region', ?) "
           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (region,))
        return await do_lookup(update, ctx, ff_uid, region)
    if a == "re":
        region = parts[3] if len(parts) > 3 and parts[3] != "-" else None
        return await do_lookup(update, ctx, parts[2], region)
    if a == "pg":
        pid = parts[2]
        state, data, _ = get_state(uid)
        payload = data.get("data")
        if not payload:
            return await show(
                update, "{} Session expired \u2014 look the player up again.".format(
                    pe("hourglass")), KB(row(B("New Lookup", "u:lk", key="eye"))))
        region = data.get("region") or saved_region()
        ff_uid = data.get("uid") or parts[3]
        return await show(update, render_page(pid, ff_uid, region, payload),
                          player_kb(ff_uid, region, pid))
    if a in ("av", "bn"):
        kind = "avatar" if a == "av" else "banner"
        region = parts[3] if len(parts) > 3 and parts[3] != "-" else saved_region()
        state, data, _ = get_state(uid)
        payload = data.get("data") or {}
        await answer(update)
        url = media_from_payload(payload, kind, parts[2], region)
        label = "Avatar" if kind == "avatar" else "Banner"
        ok = await send_image(ctx, uid, url,
                              "{} <b>{}</b> \u2022 <code>{}</code>".format(
                                  pe("eye") if kind == "avatar" else pe("gem"),
                                  label, esc(parts[2])))
        if not ok:
            return await show(
                update, "{} {} image not available from this API.".format(
                    pe("cross"), label), player_kb(parts[2], region))
        return
    if a == "js":
        state, data, _ = get_state(uid)
        payload = data.get("data")
        if not payload:
            return await show(update, "{} Session expired.".format(pe("hourglass")),
                              KB(row(B("New Lookup", "u:lk", key="eye"))))
        await answer(update)
        raw = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        try:
            await ctx.bot.send_document(
                uid, document=io.BytesIO(raw),
                filename="freefire_{}.json".format(parts[2]),
                caption="{} <b>Full API data</b> \u2022 <code>{}</code>".format(
                    pe("chart"), esc(parts[2])),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            log.warning("json send failed: %s", e)
        return
    if a == "bc":
        await answer(update)
        await show(update, "{} Checking ban status \u2026".format(pe("hourglass")))
        data, err = await fetch_ban(parts[2])
        if err:
            return await show(update, lookup_error(err, parts[2]),
                              player_kb(parts[2], None))
        banned = False
        if isinstance(data, dict):
            flat = json.dumps(data).lower()
            if data.get("banned") is True or data.get("is_banned") is True:
                banned = True
            elif str(data.get("status", "")).lower() in ("banned", "ban"):
                banned = True
            elif '"banned": true' in flat:
                banned = True
        return await show(
            update,
            "\u2501" * 20 + "\n"
            "{} <b>BAN CHECK</b>\n".format(pe("shield"))
            + "\u2501" * 20 + "\n\n"
            + "{} UID <code>{}</code>\n".format(pe("pin"), esc(parts[2]))
            + "{} Result: {}\n\n<pre>{}</pre>".format(
                pe("cross") if banned else pe("check"),
                "<b>\U0001f6ab BANNED</b>" if banned else "<b>\u2705 CLEAN</b>",
                esc(json.dumps(data, ensure_ascii=False)[:600])),
            player_kb(parts[2], None))
    if a == "list":
        return await show_open_list(update, ctx)
    if a == "rules":
        return await show(update, "{} <b>FAIR PLAY RULES</b>\n{}\n\n{}".format(
            pe("shield"), "\u2501" * 18, get_setting("rules", DEFAULT_RULES)),
            KB(row(B("Main Menu", "u:menu", key="game"))))
    if a == "my":
        text = my_summary(uid)
        return await show(
            update, text or "{} You have no registrations yet.".format(pe("hourglass")),
            KB(row(B("Open Tournaments", "u:list", key="trophy")),
               row(B("Main Menu", "u:menu", key="game"))))
    if a == "id":
        u = update.effective_user
        return await show(
            update,
            "{} <b>YOUR TELEGRAM</b>\n{}\n\nName: <b>{}</b>\nUsername: {}\n"
            "ID: <code>{}</code>".format(pe("idcard"), "\u2501" * 18,
                                         esc(u.full_name or "-"),
                                         esc(username_of(u)), uid),
            KB(row(B("Main Menu", "u:menu", key="game"))))
    if a == "cancel":
        clear_state(uid)
        return await show(update, "{} Cancelled. Nothing was saved.".format(pe("cross")),
                          KB(row(B("Main Menu", "u:menu", key="game"))))
    if a == "t":
        t = get_tournament(int(parts[2]))
        if not t:
            return await show(update, "{} Tournament not found.".format(pe("cross")),
                              KB(row(B("Main Menu", "u:menu", key="game"))))
        return await show_tournament(update, t, uid)
    if a == "reg":
        return await begin_registration(update, ctx, int(parts[2]))
    if a == "agree":
        tid = int(parts[2])
        set_state(uid, "reg_ign", {"tid": tid})
        return await show(
            update,
            "{} \U0001f3ae <b>STEP 1 OF 2</b>\n{}\n\nSend your <b>Free Fire "
            "in-game name</b> exactly as in the game.".format(pe("game"),
                                                              "\u2501" * 18),
            KB(row(B("Cancel", "u:cancel", key="cross"))))
    if a == "ok":
        return await finish_registration(update, ctx, int(parts[2]))


# ============================================================================
# ADMIN CALLBACKS
# ============================================================================
async def admin_action(update, ctx, action, tid, uid):
    t = get_tournament(tid)
    if not t:
        return await show(update, "{} Tournament not found.".format(pe("cross")),
                          KB(row(B("Back", "a:list:0"))))
    if action == "link":
        await send(
            ctx, uid,
            "{} <b>LINK \u2014 {}</b>\n{}\n\n<code>{}</code>\n\n"
            "Players who open it land directly on the registration screen.".format(
                pe("rocket"), esc(t["name"]), "\u2501" * 18,
                esc(link_for(t["code"]))),
            KB(row(B("Open Link", url=link_for(t["code"]), key="rocket")),
               row(B("Tournament", "a:t:{}".format(tid), key="trophy"))),
        )
        return await show(update, t_card(t), admin_t_kb(t))
    if action in ("open", "close", "finish"):
        status = {"open": "open", "close": "closed", "finish": "finished"}[action]
        ex("UPDATE tournaments SET status=? WHERE id=?", (status, tid))
        t = get_tournament(tid)
        return await show(update, t_card(t), admin_t_kb(t))
    if action == "del":
        return await show(
            update,
            "{} <b>DELETE TOURNAMENT?</b>\n{}\n\n{} registrations will be removed "
            "permanently. This cannot be undone.".format(
                pe("cross"), "\u2501" * 18, count_regs(tid)),
            KB(row(B("Yes, Delete", "a:act:delyes:{}".format(tid), key="cross",
                      style="danger")),
               row(B("No, Go Back", "a:t:{}".format(tid), key="pin"))))
    if action == "delyes":
        ex("DELETE FROM registrations WHERE tournament_id=?", (tid,))
        ex("DELETE FROM tournaments WHERE id=?", (tid,))
        return await show(update, "{} Tournament <code>{}</code> deleted.".format(
            pe("check"), esc(t["code"])), KB(row(B("Tournaments", "a:list:0"))))
    if action == "csv":
        data, n = registrations_csv(tid)
        if not n:
            return await show(update, "No registrations to export.",
                              KB(row(B("Back", "a:t:{}".format(tid)))))
        await ctx.bot.send_document(
            uid, document=io.BytesIO(data),
            filename="tournament_{}_players.csv".format(t["code"]),
            caption="\U0001f4e4 {} registrations \u2014 {}".format(n, t["name"]),
        )
        return await show(update, t_card(t), admin_t_kb(t))
    if action == "start":
        set_state(uid, "room_id", {"tid": tid})
        return await show(
            update,
            "{} <b>START \u2014 {}</b>\n{}\n\nSend the <b>room ID</b> for players.\n"
            "Send <code>-</code> if not ready yet.".format(
                pe("fire"), esc(t["name"]), "\u2501" * 18),
            KB(row(B("Cancel", "a:cancel", key="cross"))))


async def admin_cb(update, ctx, parts, uid):
    a = parts[1]
    if a == "home":
        return await show(update, admin_home_text(), admin_home_kb())
    if a == "cancel":
        clear_state(uid)
        return await show(update, "{} Cancelled.".format(pe("cross")), admin_home_kb())
    if a == "new":
        set_state(uid, "nt_name", {})
        return await show(
            update,
            "{} <b>CREATE TOURNAMENT \u2014 1/6</b>\n{}\n\nSend the tournament name "
            "(e.g. <code>Friday Squad Clash</code>):".format(pe("fire"), "\u2501" * 18),
            KB(row(B("Cancel", "a:cancel", key="cross"))))
    if a == "mode":
        state, data, _ = get_state(uid)
        if state != "nt_mode":
            return await show(update, "{} Session expired.".format(pe("hourglass")),
                              admin_home_kb())
        data["mode"] = parts[2]
        set_state(uid, "nt_slots", data)
        return await show(
            update,
            "{} <b>3/6</b> \u2014 send total slots (2\u20131000), e.g. <code>48</code>:".format(
                pe("flex")), KB(row(B("Cancel", "a:cancel", key="cross"))))
    if a == "ntcode":
        state, data, _ = get_state(uid)
        if state != "nt_code":
            return await show(update, "{} Session expired.".format(pe("hourglass")),
                              admin_home_kb())
        code = next_code()
        while get_tournament_by_code(code):
            code += "x"
        tid = await create_tournament(ctx, update.effective_chat.id, uid, data, code)
        if not tid:
            return await show(update, "{} Code conflict, try again.".format(pe("warn")),
                              KB(row(B("Auto Code", "a:ntcode:auto", key="rocket"))))
        clear_state(uid)
        return
    if a == "startnow":
        state, data, _ = get_state(uid)
        if state != "room_confirm":
            return await show(update, "{} Session expired.".format(pe("hourglass")),
                              admin_home_kb())
        tid = data["tid"]
        if not get_tournament(tid):
            clear_state(uid)
            return await show(update, "{} Tournament not found.".format(pe("cross")),
                              admin_home_kb())
        ex("UPDATE tournaments SET status='started', room_id=?, room_pass=? WHERE id=?",
           (data.get("room_id"), data.get("room_pass"), tid))
        clear_state(uid)
        t = get_tournament(tid)
        sent = await broadcast_start(ctx, t)
        return await show(
            update,
            "{} <b>TOURNAMENT STARTED</b>\n{}\n\n{}\n\n{} Room details sent to "
            "<b>{}</b> players.".format(pe("fire"), "\u2501" * 18, t_card(t),
                                        pe("envelope"), sent),
            admin_t_kb(t))
    if a == "list":
        text, markup = admin_list_view(int(parts[2]) if len(parts) > 2 else 0)
        return await show(update, text, markup)
    if a == "t":
        t = get_tournament(int(parts[2]))
        if not t:
            return await show(update, "{} Not found.".format(pe("cross")),
                              KB(row(B("Back", "a:list:0"))))
        return await show(update, t_card(t), admin_t_kb(t))
    if a == "act":
        return await admin_action(update, ctx, parts[2], int(parts[3]), uid)
    if a == "pl":
        text, markup = players_page(int(parts[2]), int(parts[3]))
        return await show(update, text, markup)
    if a == "links":
        rows_ = q("SELECT * FROM tournaments ORDER BY id DESC LIMIT 12")
        if not rows_:
            return await show(update, "No tournaments yet.",
                              KB(row(B("Create Tournament", "a:new", key="fire"))))
        buttons = [row(B("{} {}".format(
            "\U0001f7e2" if t["status"] == "open" else "\u2022", t["name"][:26]),
            url=link_for(t["code"]), key="rocket")) for t in rows_]
        buttons.append(row(B("Admin Home", "a:home", key="crown")))
        return await show(
            update,
            "{} <b>TOURNAMENT LINKS</b>\n{}\n\nFormat: <code>t.me/{}?start=CODE</code>".format(
                pe("rocket"), "\u2501" * 18, esc(BOT_USERNAME or "your_bot")),
            KB(*buttons))
    if a == "regs":
        rows_ = q("SELECT r.*, t.name tname, t.code tcode FROM registrations r "
                  "JOIN tournaments t ON t.id=r.tournament_id ORDER BY r.id DESC "
                  "LIMIT 25")
        if not rows_:
            return await show(update, "No registrations yet.", admin_home_kb())
        lines = ["{} <b>LATEST REGISTRATIONS</b>".format(pe("idcard")),
                 "\u2501" * 18, ""]
        for r in rows_:
            lines.append("\u2022 <b>{}</b> \u2022 <code>{}</code> \u2022 {} \u2022 "
                         "<code>{}</code> \u2022 {}".format(
                             esc(r["ff_ign"]), esc(r["ff_uid"]),
                             esc(r["tg_username"] or "no username"), r["tg_id"],
                             esc(r["tcode"])))
        return await show(update, "\n".join(lines),
                          KB(row(B("Admin Home", "a:home", key="crown"))))
    if a == "users":
        page = int(parts[2]) if len(parts) > 2 else 0
        per = 15
        total = q1("SELECT COUNT(*) c FROM users")["c"]
        rows_ = q("SELECT * FROM users ORDER BY last_seen DESC LIMIT ? OFFSET ?",
                  (per, page * per))
        lines = ["{} <b>USERS</b> ({})".format(pe("crown"), total), "\u2501" * 18, ""]
        for r in rows_:
            lines.append("\u2022 <b>{}</b> \u2022 {} \u2022 <code>{}</code> \u2022 {}".format(
                esc(r["full_name"] or "-"),
                esc("@" + r["username"] if r["username"] else "no username"),
                r["tg_id"], ts(r["last_seen"])))
        nav = []
        if page > 0:
            nav.append(B("Prev", "a:users:{}".format(page - 1), key="pin"))
        if (page + 1) * per < total:
            nav.append(B("Next", "a:users:{}".format(page + 1), key="rocket"))
        buttons = [nav] if nav else []
        buttons.append(row(B("Admin Home", "a:home", key="crown")))
        return await show(update, "\n".join(lines), KB(*buttons))
    if a == "stats":
        tot_t = q1("SELECT COUNT(*) c FROM tournaments")["c"]
        tot_r = q1("SELECT COUNT(*) c FROM registrations")["c"]
        uniq = q1("SELECT COUNT(DISTINCT tg_id) c FROM registrations")["c"]
        tot_u = q1("SELECT COUNT(*) c FROM users")["c"]
        top = q("SELECT t.name tname, COUNT(r.id) c FROM tournaments t "
                "LEFT JOIN registrations r ON r.tournament_id=t.id "
                "GROUP BY t.id ORDER BY c DESC LIMIT 3")
        lines = [
            "{} <b>STATS</b>".format(pe("chart")), "\u2501" * 18, "",
            "{} Tournaments: <b>{}</b>".format(pe("trophy"), tot_t),
            "{} Registrations: <b>{}</b> ({} unique players)".format(
                pe("idcard"), tot_r, uniq),
            "{} Users in bot: <b>{}</b>".format(pe("eye"), tot_u),
        ]
        if top:
            lines += ["", "{} <b>Top tournaments</b>".format(pe("fire"))]
            for r in top:
                lines.append("\u2022 <b>{}</b> \u2022 {}".format(esc(r["tname"]),
                                                                r["c"]))
        return await show(update, "\n".join(lines),
                          KB(row(B("Admin Home", "a:home", key="crown"))))
    if a == "key":
        url = lookup_url()
        key = lookup_key()
        shown_key = (key[:3] + "\u2026" + key[-2:]) if len(key) > 6 else (key or "none")
        return await show(
            update,
            "{} <b>API SETUP</b>\n{}\n\n"
            "{} Provider: <b>{}</b>\n"
            "{} URL: <code>{}</code>\n"
            "{} Key: <code>{}</code>\n"
            "{} Avatar/Banner: <b>{}</b>\n"
            "{} Ban check: <b>{}</b>\n\n"
            "Paste your <b>deployed base URL</b> and I will wire the API path "
            "automatically:\n<code>https://your-app.example.com</code>\n"
            "\u2192 <code>/api/free-fire/v1/players/{{uid}}?region={{region}}</code>".format(
                pe("lock"), "\u2501" * 18, pe("rocket"), esc(provider_label()),
                pe("pin"), esc(url or "\u2014"), pe("eye"), esc(shown_key),
                pe("gem"), "yes" if media_url("avatar", "1", "IND") else "no",
                pe("shield"), "yes" if lookup_ban_url() else "no"),
            KB(row(B("Set URL", "a:asurl", key="rocket"),
                   B("Set Key", "a:askey", key="eye")),
               row(B("Test Lookup", "a:atest", key="check"),
                   B("Reset", "a:keyreset", key="cross")),
               row(B("Admin Home", "a:home", key="crown"))))
    if a == "askey":
        set_state(uid, "set_key", {})
        return await show(
            update,
            "{} Send the <b>API key</b> (only needed if your API requires one).\n"
            "Send <code>-</code> to clear.".format(pe("eye")),
            KB(row(B("Cancel", "a:cancel", key="cross"))))
    if a == "asurl":
        set_state(uid, "set_url", {})
        return await show(
            update,
            "{} Send your API URL.\n\n"
            "<b>Easiest</b> \u2014 just the base URL of your deployed "
            "free-fire-info-starter app:\n<code>https://your-app.example.com</code>\n\n"
            "<b>Or</b> a full template:\n"
            "<code>https://your-app.example.com/api/free-fire/v1/players/"
            "{{uid}}?region={{region}}</code>\n\n"
            "<b>Or</b> a query API: <code>https://host/info</code>".format(pe("rocket")),
            KB(row(B("Cancel", "a:cancel", key="cross"))))
    if a == "atest":
        set_state(uid, "test_uid", {})
        return await show(
            update,
            "{} Send a UID to test the API with (e.g. <code>2384343917</code>):".format(
                pe("check")),
            KB(row(B("Cancel", "a:cancel", key="cross"))))
    if a == "atestreg":
        state, data, _ = get_state(uid)
        ff_uid = data.get("uid")
        if not ff_uid:
            return await show(update, "{} Session expired.".format(pe("hourglass")),
                              admin_home_kb())
        clear_state(uid)
        region = parts[2].upper()
        await show(update, "{} Testing <code>{}</code> ({}) \u2026".format(
            pe("hourglass"), esc(ff_uid), esc(region)))
        player, err = await fetch_player(ff_uid, region)
        url_used = (build_url(lookup_url(), ff_uid, region)
                    if is_template() else lookup_url())
        if player:
            b = player.get("basicInfo") or player.get("player") or {}
            return await show(
                update,
                "{} <b>API WORKS</b>\n{}\n\n"
                "{} <b>{}</b> \u2022 Lv <b>{}</b> \u2022 {}\n"
                "{} <code>{}</code>\n\n"
                "{} Avatar <b>{}</b> \u2022 Banner <b>{}</b>".format(
                    pe("check"), "\u2501" * 18, pe("crown"),
                    esc(str(b.get("nickname", "?"))), esc(str(b.get("level", "?"))),
                    esc(str(b.get("region", region))), pe("rocket"),
                    esc(url_used[:120]), pe("gem"),
                    "yes" if media_url("avatar", ff_uid, region) else "n/a",
                    "yes" if media_url("banner", ff_uid, region) else "n/a"),
                KB(row(B("Open Lookup", "u:lk", key="eye")),
                   row(B("API Setup", "a:key", key="lock"))))
        return await show(
            update,
            "{} <b>TEST FAILED</b>\n{}\n\n{} <code>{}</code>\n\n"
            "<code>{}</code>".format(
                pe("cross"), "\u2501" * 18, pe("rocket"), esc(url_used[:120]),
                esc(str(err)[:200])),
            KB(row(B("Set URL", "a:asurl", key="rocket"),
                   B("Set Key", "a:askey", key="eye")),
               row(B("API Setup", "a:key", key="lock"))))
    if a == "keyreset":
        ex("DELETE FROM settings WHERE key IN "
           "('lookup_url','lookup_key','lookup_ban_url','lookup_region')")
        return await show(
            update, "{} Reset to environment values.\nProvider: <b>{}</b>".format(
                pe("check"), esc(provider_label())),
            admin_home_kb())
    if a == "keys":
        page = int(parts[2]) if len(parts) > 2 else 0
        per = 6
        total = q1("SELECT COUNT(*) c FROM api_keys")["c"]
        rows_ = q("SELECT * FROM api_keys ORDER BY id DESC LIMIT ? OFFSET ?",
                  (per, page * per))
        if not rows_:
            return await show(
                update,
                "{} <b>API KEYS</b>\n{}\n\nNo keys yet. Tap "
                "<b>Create Key</b> to generate one for a user.".format(
                    pe("gem"), "\u2501" * 18),
                KB(row(B("Create Key", "a:newkey", key="sparkles", style="success")),
                   row(B("Admin Home", "a:home", key="crown"))))
        lines = ["{} <b>API KEYS</b>  ({}/{})".format(
            pe("gem"), page + 1, max(1, -(-total // per))), "\u2501" * 18, ""]
        buttons = []
        for k in rows_:
            state_txt = "revoked" if k["revoked"] else (
                "expired" if k["expires_at"] and k["expires_at"] < time.time()
                else "active")
            lim = k["daily_limit"] or "\u221e"
            lines.append(
                "{} <b>{}</b> \u2022 {} \u2022 {}\n"
                "   <code>{}</code>\n"
                "   {}/{} today \u2022 {} total \u2022 {}".format(
                    pe("check") if state_txt == "active" else pe("cross"),
                    esc(k["label"] or "key"), esc(k["plan"]), state_txt,
                    esc(k["key"]), k["used_day"] or 0, lim,
                    k["used_total"] or 0,
                    ("until " + ts(k["expires_at"])) if k["expires_at"] else "no expiry"))
            buttons.append(row(
                B("Revoke" if not k["revoked"] else "Enable",
                  "a:keyrev:{}".format(k["id"]), key="lock"),
                B("Reset usage", "a:keyreset2:{}".format(k["id"]), key="rocket"),
                B("Delete", "a:keydel:{}".format(k["id"]), key="cross",
                  style="danger"),
            ))
        nav = []
        if page > 0:
            nav.append(B("Prev", "a:keys:{}".format(page - 1), key="pin"))
        if (page + 1) * per < total:
            nav.append(B("Next", "a:keys:{}".format(page + 1), key="rocket"))
        if nav:
            buttons.append(nav)
        buttons.append(row(B("Create Key", "a:newkey", key="sparkles",
                             style="success")))
        buttons.append(row(B("Admin Home", "a:home", key="crown")))
        return await show(update, "\n".join(lines), KB(*buttons))

    if a == "newkey":
        set_state(uid, "key_label", {})
        return await show(
            update,
            "{} <b>NEW API KEY \u2014 1/2</b>\n{}\n\n"
            "Send a short label for this key (who it is for), e.g. "
            "<code>rahul-bot</code> or <code>reseller-01</code>.".format(
                pe("sparkles"), "\u2501" * 18),
            KB(row(B("Cancel", "a:cancel", key="cross"))))
    if a == "keyplan":
        state, data, _ = get_state(uid)
        if state != "key_plan":
            return await show(update, "{} Session expired.".format(pe("hourglass")),
                              admin_home_kb())
        plan = parts[2]
        key = create_api_key(data.get("label"), owner_tg=uid, plan=plan)
        clear_state(uid)
        row_ = key_row(key) or {}
        base = API_PUBLIC_URL or "https://your-api.onrender.com"
        days = PLANS.get(plan, {}).get("days", 0)
        return await show(
            update,
            "{} <b>API KEY CREATED</b>\n{}\n\n"
            "{} Label: <b>{}</b>\n"
            "{} Plan: <b>{}</b>\n"
            "{} Limit: <b>{}</b> requests/day\n"
            "{} Valid: <b>{}</b>\n\n"
            "{} <b>KEY</b> (tap to copy):\n<code>{}</code>\n\n"
            "{} <b>Test URL</b>\n<code>{}/api/v1/player/2384343917?region=IND&key={}</code>".format(
                pe("check"), "\u2501" * 18, pe("pin"), esc(row_.get("label") or "-"),
                pe("gem"), esc(plan), pe("chart"),
                row_.get("daily_limit") or "\u221e", pe("clock"),
                "{} days".format(days) if days else "no expiry",
                pe("lock"), esc(key), pe("rocket"), esc(base), esc(key)),
            KB(row(B("Create Another", "a:newkey", key="sparkles"),
                   B("All Keys", "a:keys:0", key="gem")),
               row(B("Admin Home", "a:home", key="crown"))))
    if a in ("keyrev", "keyreset2", "keydel"):
        kid = int(parts[2])
        if a == "keyrev":
            r = q1("SELECT revoked FROM api_keys WHERE id=?", (kid,))
            revoke_key(kid, 0 if (r and r["revoked"]) else 1)
        elif a == "keyreset2":
            reset_usage(kid)
        else:
            delete_key(kid)
        return await admin_cb(update, ctx, ["a", "keys", "0"], uid)
    if a == "logs":
        rows_ = q("SELECT l.*, u.username FROM lookups l LEFT JOIN users u "
                  "ON u.tg_id=l.tg_id ORDER BY l.id DESC LIMIT 20")
        if not rows_:
            return await show(update, "No lookups yet.", admin_home_kb())
        lines = ["{} <b>LOOKUP LOG</b>".format(pe("eye")), "\u2501" * 18, ""]
        for r in rows_:
            lines.append("{} <b>{}</b> \u2022 <code>{}</code> \u2022 {} \u2022 {}".format(
                pe("check") if r["ok"] else pe("cross"),
                esc(r["nickname"] or "-"), esc(r["ff_uid"]),
                esc(r["username"] or "no username"), ts(r["created_at"])))
        return await show(update, "\n".join(lines),
                          KB(row(B("Admin Home", "a:home", key="crown"))))
    if a == "rules":
        set_state(uid, "edit_rules", {})
        return await show(
            update,
            "{} <b>EDIT RULES</b>\n{}\n\nCurrent:\n\n{}\n\nSend the new text "
            "(HTML like <code>&lt;b&gt;</code> allowed).".format(
                pe("shield"), "\u2501" * 18, get_setting("rules", DEFAULT_RULES)),
            KB(row(B("Cancel", "a:cancel", key="cross"))))
    if a == "cast":
        set_state(uid, "cast_msg", {})
        return await show(
            update,
            "{} <b>BROADCAST</b>\n{}\n\nSend the message to send to every bot "
            "user.".format(pe("envelope"), "\u2501" * 18),
            KB(row(B("Cancel", "a:cancel", key="cross"))))


# ============================================================================
# ROUTERS
# ============================================================================
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    data = update.callback_query.data or ""
    head = data.split(":")[0]

    if head == "a" and not is_admin(uid):
        return await answer(update, "\u26d4 Admins only.", alert=True)

    if not await gate(update, ctx):
        return
    await answer(update)

    parts = data.split(":")
    try:
        if head == "u":
            return await user_cb(update, ctx, parts, uid)
        if head == "a":
            return await admin_cb(update, ctx, parts, uid)
    except Exception:
        log.exception("callback failed: %s", data)
        try:
            await send(ctx, uid, "\u26a0\ufe0f Something went wrong. Please try again.")
        except TelegramError:
            pass


async def _start_link(update, t, uid):
    registered = q1("SELECT 1 FROM registrations WHERE tournament_id=? AND tg_id=?",
                    (t["id"], uid))
    full = count_regs(t["id"]) >= t["slots"]
    text = t_card(t) + "\n\n"
    if registered:
        text += "{} <b>You are already registered.</b>".format(pe("check"))
        markup = KB(row(B("My Registrations", "u:my", key="idcard")))
    elif t["status"] != "open" or full:
        text += "{} Registration is closed.".format(pe("cross"))
        markup = KB(row(B("Main Menu", "u:menu", key="game")))
    else:
        text += "{} Tap below to lock your slot.".format(pe("sparkles"))
        markup = KB(row(B("REGISTER NOW", "u:reg:{}".format(t["id"]), key="fire",
                          style="success")),
                    row(B("Main Menu", "u:menu", key="game")))
    await update.message.reply_text(
        text, reply_markup=markup, parse_mode=ParseMode.HTML,
        disable_web_page_preview=True)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    uid = update.effective_user.id
    payload = (ctx.args[0] if ctx.args else "").strip()
    clear_state(uid)
    if payload:
        t = get_tournament_by_code(payload)
        if t:
            return await _start_link(update, t, uid)
        await update.message.reply_text(
            "\u274c This link is invalid or the tournament was removed.",
            reply_markup=main_menu_kb(uid), parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(
        welcome_text(update.effective_user), reply_markup=main_menu_kb(uid),
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )


async def cmd_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    args = ctx.args or []
    if args and re.fullmatch(r"\d{5,%d}" % MAX_UID_DIGITS, args[0]):
        return await do_lookup(update, ctx, args[0])
    await update.message.reply_text(
        "{} <b>PLAYER LOOKUP</b>\n{}\n\nSend the <b>Free Fire UID</b>, or use "
        "<code>/lookup 2384343917</code>.".format("\U0001f50d", "\u2501" * 18),
        parse_mode=ParseMode.HTML, reply_markup=KB(row(B("Cancel", "u:menu"))))


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text(
            "\u26d4 <b>Admins only.</b>", parse_mode=ParseMode.HTML)
    clear_state(update.effective_user.id)
    await update.message.reply_text(
        admin_home_text(), reply_markup=admin_home_kb(), parse_mode=ParseMode.HTML)


async def cmd_my(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    uid = update.effective_user.id
    text = my_summary(uid)
    await deliver(
        lambda t, m: update.message.reply_text(
            t, reply_markup=m, parse_mode=ParseMode.HTML),
        text or "\U0001f3ab No registrations yet.",
        KB(row(B("Open Tournaments", "u:list", key="trophy"))))


async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    await update.message.reply_text(
        get_setting("rules", DEFAULT_RULES), parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(update.effective_user.id),
        disable_web_page_preview=True)


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(
        "\U0001faaa Name: <b>{}</b>\n\u2709\ufe0f Username: {}\n\U0001f441 ID: "
        "<code>{}</code>".format(esc(u.full_name or "-"), esc(username_of(u)), u.id),
        parse_mode=ParseMode.HTML)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    clear_state(update.effective_user.id)
    await update.message.reply_text(
        "\u2716\ufe0f Cancelled.",
        reply_markup=main_menu_kb(update.effective_user.id))


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "\U0001f4d1 <b>COMMANDS</b>\n"
        + "\u2501" * 18 + "\n"
        "/start \u2014 main menu\n"
        "/tournaments \u2014 open tournaments\n"
        "/my \u2014 my registrations\n"
        "/rules \u2014 fair play rules\n"
        "/id \u2014 my telegram id"
        + ("\n/admin \u2014 admin panel" if is_admin(update.effective_user.id) else ""),
        parse_mode=ParseMode.HTML)


async def cmd_tournaments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    rows_ = q("SELECT * FROM tournaments WHERE status='open' ORDER BY id DESC LIMIT 20")
    if not rows_:
        return await update.message.reply_text(
            "\u23f3 No open tournaments right now.",
            reply_markup=main_menu_kb(update.effective_user.id))
    await update.message.reply_text(
        "\U0001f3c6 <b>OPEN TOURNAMENTS</b>",
        reply_markup=KB(*[row(B(t["name"][:30], "u:t:{}".format(t["id"]),
                               key="trophy")) for t in rows_]),
        parse_mode=ParseMode.HTML)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("unhandled error", exc_info=ctx.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await ctx.bot.send_message(
                update.effective_chat.id,
                "\u26a0\ufe0f Something went wrong. Please try again in a moment.")
    except TelegramError:
        pass


async def on_startup(app: Application):
    global BOT_USERNAME
    me = await app.bot.get_me()
    BOT_USERNAME = me.username
    log.info("online as @%s | admins=%s | force_join=%s", BOT_USERNAME,
             sorted(ADMIN_IDS), FORCE_JOIN_CHAT or "off")


def start_health_server():
    """Serve 200 on $PORT so Render keeps the bot alive as a web service."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    port = int(os.getenv("PORT", "10000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({
                "ok": True,
                "service": "ff-bot",
                "bot": BOT_USERNAME or "starting",
                "keys": key_stats(),
                "uptime": int(time.time() - _STARTED),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("health server on :%s", port)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Use os.getenv('BOT_TOKEN') or a .env file.")
    init_db()
    if os.getenv("PORT") and os.getenv("HEALTH_SERVER", "1") != "0":
        start_health_server()
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("lookup", cmd_lookup))
    app.add_handler(CommandHandler("tournaments", cmd_tournaments))
    app.add_handler(CommandHandler("my", cmd_my))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("starting polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
