#!/bin/sh
set -eu

if [ -z "${BACKEND_UPSTREAM_URL:-}" ]; then
  echo "ERROR: BACKEND_UPSTREAM_URL must be set (e.g. http://backend.railway.internal:8000)" >&2
  exit 1
fi

escaped_url=$(printf '%s' "$BACKEND_UPSTREAM_URL" | sed 's/[&|\\]/\\&/g')
sed "s|\${BACKEND_UPSTREAM_URL}|${escaped_url}|g" /etc/kong/kong.yml.template > /tmp/kong.yml

# When GATEWAY_SHARED_SECRET is set, strip any client-supplied X-Gateway-Secret
# and inject the shared secret on every upstream request to FastAPI (#256).
if [ -n "${GATEWAY_SHARED_SECRET:-}" ]; then
  yaml_secret=$(printf '%s' "$GATEWAY_SHARED_SECRET" | sed "s/'/''/g")
  cat <<EOF >> /tmp/kong.yml

  - name: request-transformer
    config:
      remove:
        headers:
          - X-Gateway-Secret
      add:
        headers:
          - 'X-Gateway-Secret:${yaml_secret}'
EOF
fi

export KONG_DATABASE=off
export KONG_DECLARATIVE_CONFIG=/tmp/kong.yml
export KONG_ADMIN_LISTEN=off
export KONG_PROXY_LISTEN="0.0.0.0:${PORT:-8000}"
export KONG_PROXY_ACCESS_LOG=/dev/stdout
export KONG_PROXY_ERROR_LOG=/dev/stderr
# Restrict trusted proxy ranges to private/Railway networks (never 0.0.0.0/0, #256)
# so spoofed X-Forwarded-For headers from untrusted clients cannot evade rate limits.
export KONG_TRUSTED_IPS="${KONG_TRUSTED_IPS:-10.0.0.0/8,100.64.0.0/10,172.16.0.0/12,192.168.0.0/16,fd00::/8}"
export KONG_REAL_IP_HEADER="${KONG_REAL_IP_HEADER:-X-Forwarded-For}"
export KONG_REAL_IP_RECURSIVE="${KONG_REAL_IP_RECURSIVE:-on}"

exec /docker-entrypoint.sh kong docker-start
