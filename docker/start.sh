#!/bin/sh
# Entry point of the single-container image (docker/app.Dockerfile): API + embedded workers +
# the built console on one port ($PORT, set by Railway/Render/Koyeb; default 8000).
#
# Secrets that are not supplied through the environment are generated once and kept in
# $DATA_DIR/secrets.env, so they survive restarts as long as $DATA_DIR is on a volume.
set -eu

DATA_DIR="${DATA_DIR:-/data}"
SECRETS="$DATA_DIR/secrets.env"
mkdir -p "$DATA_DIR"

# Some platforms (Railway) mount volumes owned by root, unwritable for the image's user. Started
# as root (Railway: RAILWAY_RUN_UID=0), hand the volume to that user and drop privileges.
if [ "$(id -u)" = 0 ] && id nirnay >/dev/null 2>&1; then
  [ "$(stat -c %u "$DATA_DIR")" = "$(id -u nirnay)" ] || chown -R nirnay:"$(id -g nirnay)" "$DATA_DIR"
  exec setpriv --reuid="$(id -u nirnay)" --regid="$(id -g nirnay)" --init-groups "$0" "$@"
fi

fail() { echo "[nirnay] $*" >&2; exit 1; }

case "${DATABASE_URL:-sqlite}" in
  sqlite*) external_db=0 ;;
  *) external_db=1 ;;
esac

# Camera credentials and evidence are encrypted with ENCRYPTION_KEY and stored in the
# database. With an external database the key must outlive this container's filesystem.
if [ "$external_db" = 1 ] && [ -z "${ENCRYPTION_KEY:-}" ] && [ ! -s "$SECRETS" ]; then
  fail "DATABASE_URL points to an external database: set ENCRYPTION_KEY (python -c \"from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())\") so stored credentials stay readable after a redeploy"
fi

# A hosted demo is reachable from the internet: never run it with the published demo password.
if [ "${DEMO_USERS:-true}" = "true" ] && { [ -z "${DEMO_PASSWORD:-}" ] || [ "${DEMO_PASSWORD}" = "nirnay-demo" ]; }; then
  fail "set DEMO_PASSWORD to a strong value (the demo accounts admin/operator/analyst/viewer use it), or set DEMO_USERS=false and ADMIN_USERNAME/ADMIN_PASSWORD"
fi

if [ ! -s "$SECRETS" ]; then
  umask 077
  python - "$SECRETS" <<'EOF'
import secrets, sys
from cryptography.fernet import Fernet
with open(sys.argv[1], "w") as f:
    f.write(f"JWT_SECRET={secrets.token_urlsafe(48)}\n")
    f.write(f"PSEUDONYM_SECRET={secrets.token_urlsafe(32)}\n")
    f.write(f"ENCRYPTION_KEY={Fernet.generate_key().decode()}\n")
EOF
  echo "[nirnay] generated secrets in $SECRETS"
fi

# environment values take precedence over the generated ones
while IFS='=' read -r key value; do
  [ -n "$key" ] || continue
  eval "current=\${$key:-}"
  [ -n "$current" ] || export "$key=$value"
done < "$SECRETS"

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips "*"
