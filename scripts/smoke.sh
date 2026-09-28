#!/usr/bin/env bash
#
# scripts/smoke.sh — minimal end-to-end smoke test for a CaddyPanel image.
#
# It builds nothing: it takes an ALREADY-BUILT image reference as its first
# argument, starts it detached, waits until the Flask UI answers over HTTP,
# fails loudly with the container logs on any problem, and ALWAYS cleans up.
#
# Usage:
#   bash scripts/smoke.sh <image> [host_port]
#
# Why a single-platform image: a multi-arch manifest cannot be RUN, so CI
# builds one linux/amd64 image locally (`load: true`) and feeds it here.
#
# Exit codes: 0 = healthy, 1 = failure, 2 = bad usage.
set -euo pipefail

IMAGE="${1:-}"
HOST_PORT="${2:-5000}"          # host port mapped onto the container's 5000
CONTAINER_NAME="caddypanel-smoke"
CONTAINER_PORT="5000"           # Flask/gunicorn port (Dockerfile FLASK_PORT)

if [ -z "$IMAGE" ]; then
    echo "usage: $0 <image> [host_port]" >&2
    exit 2
fi

# Cleanup on ANY exit path (success, failure, Ctrl-C).
cleanup() {
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# The app must receive a STRONG FLASK_SECRET_KEY: placeholder values and keys
# shorter than 32 chars are REFUSED by app.py (insecure_secret_key_reason),
# which makes every gunicorn worker generate its own ephemeral key -> login
# loop / random CSRF 400s. Generate a random 32-byte (64 hex chars) key.
gen_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import secrets; print(secrets.token_hex(32))'
    else
        head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
    fi
}
SECRET_KEY="$(gen_secret)"

echo ">>> Starting container '${CONTAINER_NAME}' from image '${IMAGE}'"
docker run -d \
    --name "$CONTAINER_NAME" \
    -e FLASK_SECRET_KEY="$SECRET_KEY" \
    -e TRUSTED_PROXY_COUNT=0 \
    -p "127.0.0.1:${HOST_PORT}:${CONTAINER_PORT}" \
    "$IMAGE" >/dev/null

URL="http://127.0.0.1:${HOST_PORT}/"
echo ">>> Waiting for the panel to answer at ${URL} ..."

# Portable HTTP status probe: curl -> wget -> python3 -> bash /dev/tcp.
http_status() {
    if command -v curl >/dev/null 2>&1; then
        curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$URL" 2>/dev/null || true
    elif command -v wget >/dev/null 2>&1; then
        wget -S -q -O /dev/null --timeout=5 "$URL" 2>&1 \
            | awk '/HTTP\// {code=$2} END {print code}' || true
    elif command -v python3 >/dev/null 2>&1; then
        python3 - "$URL" <<'PY' || true
import sys
import urllib.error
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=5) as resp:
        print(resp.status)
except urllib.error.HTTPError as exc:
    print(exc.code)
except Exception:
    print("000")
PY
    else
        # Last resort: raw TCP connect via a bash builtin. Only proves the port
        # is open, but that is enough when no HTTP client exists at all.
        if (exec 3<>"/dev/tcp/127.0.0.1/${HOST_PORT}") 2>/dev/null; then
            echo "200"
        else
            echo "000"
        fi
    fi
}

# On a fresh container there is no admin: GET / -> 302 to /login -> 302 to
# /setup -> 200. Any 2xx/3xx response therefore proves Flask+gunicorn serve
# HTTP. A 5xx or no answer is a failure.
STATUS=""
for _ in $(seq 1 60); do
    if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
        echo "!!! Container exited before becoming healthy." >&2
        docker logs "$CONTAINER_NAME" >&2 || true
        exit 1
    fi
    STATUS="$(http_status)"
    case "$STATUS" in
        2??|3??) break ;;
        *) sleep 1 ;;
    esac
done

if ! printf '%s' "$STATUS" | grep -qE '^[23][0-9]{2}$'; then
    echo "!!! Smoke test FAILED: the panel did not answer with a 2xx/3xx status (last: '${STATUS:-none}')." >&2
    echo "--- container logs ---" >&2
    docker logs "$CONTAINER_NAME" >&2 || true
    exit 1
fi

# The generated key must have been ACCEPTED (no "was REFUSED" in the logs).
if docker logs "$CONTAINER_NAME" 2>&1 | grep -q 'FLASK_SECRET_KEY .*REFUSED'; then
    echo "!!! Smoke test FAILED: the generated FLASK_SECRET_KEY was refused." >&2
    docker logs "$CONTAINER_NAME" >&2 || true
    exit 1
fi

echo ">>> OK: panel answered HTTP ${STATUS}; FLASK_SECRET_KEY accepted."
