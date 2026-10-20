#!/bin/sh
# Writes .env from Render env vars, generates APP_KEY on first boot, then starts
# Apache. Nothing secret is baked into the image.
set -e

cd /var/www/html

[ -f .env ] || : > .env

set_env() {
    key="$1"; val="$2"
    [ -z "$val" ] && return 0
    # Values with spaces/specials must be quoted or Laravel refuses the file.
    case "$val" in
        *[!A-Za-z0-9_./:@+-]*) val="\"$val\"" ;;
    esac
    if grep -q "^${key}=" .env 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${val}|" .env
    else
        printf '%s=%s\n' "$key" "$val" >> .env
    fi
}

set_env APP_ENV            "${APP_ENV:-production}"
set_env APP_DEBUG          "${APP_DEBUG:-false}"
set_env APP_NAME           "${APP_NAME:-FF Info}"
set_env APP_URL            "${APP_URL:-http://localhost}"
set_env LOG_CHANNEL        stderr
set_env FREEFIRE_ENABLED           "${FREEFIRE_ENABLED:-true}"
set_env FREEFIRE_PROTOCOL          "${FREEFIRE_PROTOCOL:-OB55}"
set_env FREEFIRE_DEFAULT_REGION    "${FREEFIRE_DEFAULT_REGION:-IND}"
set_env FREEFIRE_DEFAULT_UID       "${FREEFIRE_DEFAULT_UID}"
set_env FREEFIRE_DEFAULT_PASSWORD  "${FREEFIRE_DEFAULT_PASSWORD}"
set_env FREEFIRE_ROUTES_ENABLED    "${FREEFIRE_ROUTES_ENABLED:-true}"
set_env FREEFIRE_MEDIA_ENABLED     "${FREEFIRE_MEDIA_ENABLED:-true}"
set_env FREEFIRE_SERVER_KEY        "${FREEFIRE_SERVER_KEY}"

# APP_KEY: use the provided one, else generate and keep it for this boot.
if [ -n "$APP_KEY" ]; then
    set_env APP_KEY "$APP_KEY"
elif ! grep -q '^APP_KEY=base64:' .env; then
    set_env APP_KEY "$(php artisan key:generate --show --no-interaction)"
fi

mkdir -p storage/framework/cache/data storage/framework/sessions \
         storage/framework/views storage/logs bootstrap/cache
chown -R www-data:www-data storage bootstrap/cache 2>/dev/null || true

php artisan config:clear >/dev/null 2>&1 || true

echo "-------------------------------------------------------------"
echo " Free Fire source ready"
echo "   protocol : ${FREEFIRE_PROTOCOL:-OB55}"
echo "   region   : ${FREEFIRE_DEFAULT_REGION:-IND}"
if [ -n "$FREEFIRE_DEFAULT_UID" ]; then
    echo "   creds    : configured"
else
    echo "   creds    : MISSING -> set FREEFIRE_DEFAULT_UID/PASSWORD"
fi
echo "   health   : http://127.0.0.1/api/free-fire/v1/health"
echo "-------------------------------------------------------------"

exec "$@"
