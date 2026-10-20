# Free Fire data source (Docker)

Render → **New → Web Service → Docker** → this folder (or `rootDir: source`).
Nothing to install locally; the image installs the starter app at build time.

Env vars (dashboard → Environment):

| Key | Value |
|---|---|
| `FREEFIRE_DEFAULT_UID` | your Garena guest UID (already in `.env`) |
| `FREEFIRE_DEFAULT_PASSWORD` | your Garena guest password (already in `.env`) |
| `FREEFIRE_PROTOCOL` | `OB55` |
| `FREEFIRE_DEFAULT_REGION` | `IND` |

`APP_KEY` is generated on first boot if you don't set one.
Health: `GET /api/free-fire/v1/health` → `{"ok":true,...}`
Players: `GET /api/free-fire/v1/players/2384343917?region=IND`
