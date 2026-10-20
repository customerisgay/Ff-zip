# Deploy on Render — bot + API + data source, all wired automatically

You do **not** need a Free Fire API URL. Nothing about URLs has to be typed in:
the blueprint below deploys the data source itself and Render hands its address
to the bot and the API through `fromService: property: host`.

```
Internet ─┬─> ff-api    /api/v1/player/{uid}?key=...   (public, key-protected)
          │        │
          │        └──> ff-source  (Laravel, OB55) ──> Garena
          └─> ff-bot  (Telegram; admins create API keys here)
                     │
                ff-db (Postgres, shared by bot + API)
```

---

## 1. Put the files on GitHub

**Method A — drag & drop (no git, no terminal)**

1. Unzip `ffbot-render.zip` on your computer.
2. Open the `ffbot-render` folder, select **everything inside it**
   (`main.py`, `api.py`, `dbcore.py`, `requirements.txt`, `render.yaml`,
   `DEPLOY.md`, `.env.example`, `source/`, `.gitignore`) and **not the folder itself**.
3. github.com → **New repository** → name it e.g. `ff-bot` → Private → **Create**.
4. **Add file → Upload files** → drag those items in → **Commit changes**.

The repo root must look like this (Render reads `render.yaml` from the root):

```
ff-bot/
├── main.py  api.py  dbcore.py  requirements.txt  render.yaml
└── source/   (Dockerfile, entrypoint.sh, php.ini, README.md)
```

> ⚠️ **Do not upload `.env`** — it holds your Garena password and every value in
> it is either injected by Render or typed into the dashboard. The web uploader
> ignores `.gitignore`, so just leave that one file on your computer.
>
> Chrome/Edge keep `source/` and its files intact. If your browser skips the
> subfolder, upload it in a second pass: **Add file → Upload files → source**.

**Method B — git CLI**

```bash
cd ffbot-render
git init && git add . && git commit -m "FF bot + API"
git remote add origin https://github.com/YOU/ff-bot.git && git push -u origin main
```

`.gitignore` keeps `.env` out of the commit automatically.

## 2. Blueprint

Render → **New → Blueprint** → choose the repo → **Apply**.

It creates `ff-source`, `ff-api`, `ff-bot`, `ff-db` and links them.

### The only values you type

| Service | Variable | Where to get it |
|---|---|---|
| `ff-bot` | `BOT_TOKEN` | @BotFather |
| `ff-source` | `FREEFIRE_DEFAULT_UID` | copy from `.env` (`5466935505`) |
| `ff-source` | `FREEFIRE_DEFAULT_PASSWORD` | copy from `.env` (the 64-char hex) |

Everything else — `LOOKUP_URL`, `API_PUBLIC_URL`, `DATABASE_URL` — is injected by
Render. The code normalises the bare hostname it receives
(`ff-source.onrender.com` → `https://ff-source.onrender.com/api/free-fire/v1/players/{uid}?region={region}`).

> If a service name is already taken, Render appends a suffix (`ff-source-x1y2`).
> That's fine — `fromService` follows the real name.

## 3. Order of first boot

`ff-source` takes the longest (Docker build ~3–5 min, it installs Laravel with
Composer). Until it's healthy, the API answers `503 upstream_not_configured`
and the bot shows "lookup API not configured". Wait for
`https://ff-source.onrender.com/api/free-fire/v1/health` to return `"ok": true`,
then restart `ff-api` and `ff-bot` once (Render → Manual Deploy → Restart).

## 4. Create API keys in the bot

Telegram → `/admin` → **💎 API Keys** → **✨ Create Key**

1. Send a label (e.g. `rahul-bot`)
2. Pick a plan: **Free** 100/day · **Pro** 1K/day · **VIP** 10K/day · **Unlimited**
3. The bot replies with the key **and a ready-to-use test URL** pointing at your
   real `ff-api` domain — tap to copy.

The list view shows every key with `used today / limit`, total calls, expiry, and
**Revoke / Enable**, **Reset usage**, **Delete** buttons.

## 5. API contract

```bash
# player
curl "https://ff-api.onrender.com/api/v1/player/2384343917?region=IND&key=ff_pro_xxxx"

# same, header auth
curl -H "X-API-Key: ff_pro_xxxx" "https://ff-api.onrender.com/api/v1/player/2384343917?region=IND"

# key stats
curl "https://ff-api.onrender.com/api/v1/key?key=ff_pro_xxxx"

# avatar image
curl "https://ff-api.onrender.com/api/v1/player/2384343917/avatar?key=ff_pro_xxxx" -o av.webp

# health (no key)
curl "https://ff-api.onrender.com/api/v1/health"
```

```json
{
  "success": true, "uid": "2384343917", "region": "IND",
  "nickname": "☠︎︎1ᴛᴀᴘ☠︎︎", "level": 54,
  "player": { "basicInfo": {...}, "clanBasicInfo": {...}, "petInfo": {...} },
  "quota": { "remaining_today": 999, "daily_limit": 1000, "plan": "pro" },
  "took_ms": 49
}
```

| HTTP | `error` | Meaning |
|---|---|---|
| 400 | `invalid_uid` / `invalid_region` | bad input |
| 401 | `missing_key` / `invalid_key` | no / unknown key |
| 403 | `key_revoked` / `key_expired` | disabled or out of time |
| 404 | `player_not_found` | UID not in that region |
| 429 | `daily_limit_reached` / `rate_limited` | quota or >60 req/min |
| 502 | `upstream_unavailable` | Garena side is down (health check confirmed) |
| 503 | `upstream_not_configured` | source service not up yet |

## 6. Local run

```bash
pip install -r requirements.txt

# 1) data source: any local Laravel/OB55 app, or
docker build -t ffsource ./source && docker run -p 8080:80 --env-file .env ffsource

# 2) API + bot (bare host works — same as Render)
export DATABASE_URL=postgresql://user:pass@localhost:5432/ffdb
LOOKUP_URL=127.0.0.1:8080 uvicorn api:app --port 8090
BOT_TOKEN=123:ABC LOOKUP_URL=127.0.0.1:8080 API_PUBLIC_URL=127.0.0.1:8090 python main.py
```

No `DATABASE_URL`? Everything falls back to SQLite (`DB_PATH`) — dev only.

## Notes

* **Free plan sleeps** after ~15 min idle; the first call afterwards is slow.
* The Garena guest account is **shared and rate-limited**, and can be banned —
  for real traffic use your own account credentials.
* The 60 req/min limiter is per-process. The daily quota is in Postgres, so it
  is enforced correctly across restarts.
* Bot reads `.env` from its working directory; on Render it uses dashboard vars.
* `FORCE_JOIN_CHAT=@bomberjantaparty` requires the bot to be **admin** in that
  channel, otherwise every user is rejected.
