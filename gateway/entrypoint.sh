#!/bin/sh
set -eu

if [ -z "${BACKEND_UPSTREAM_URL:-}" ]; then
  echo "ERROR: BACKEND_UPSTREAM_URL must be set (e.g. http://backend.railway.internal:8000)" >&2
  exit 1
fi

escaped_url=$(printf '%s' "$BACKEND_UPSTREAM_URL" | sed 's/[&|\\]/\\&/g')
sed "s|\${BACKEND_UPSTREAM_URL}|${escaped_url}|g" /etc/kong/kong.yml.template > /tmp/kong.yml

export KONG_DATABASE=off
export KONG_DECLARATIVE_CONFIG=/tmp/kong.yml
export KONG_ADMIN_LISTEN=off
export KONG_PROXY_LISTEN="0.0.0.0:${PORT:-8000}"
export KONG_PROXY_ACCESS_LOG=/dev/stdout
export KONG_PROXY_ERROR_LOG=/dev/stderr
export KONG_TRUSTED_IPS="${KONG_TRUSTED_IPS:-0.0.0.0/0,::/0}"
export KONG_REAL_IP_HEADER="${KONG_REAL_IP_HEADER:-X-Forwarded-For}"
export KONG_REAL_IP_RECURSIVE="${KONG_REAL_IP_RECURSIVE:-on}"

exec /docker-entrypoint.sh kong docker-start
