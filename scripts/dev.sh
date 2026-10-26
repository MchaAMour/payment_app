#!/usr/bin/env bash
# Start everything for local development in one terminal: Postgres + Redis (Docker), migrations,
# the web server, the Celery worker, and the Stripe webhook forwarder. Ctrl+C stops all of it.
#
#   ./scripts/dev.sh
#
# Needs the Stripe CLI logged in (`stripe login`). To use the secret key from the environment
# instead (it will be visible in the process list): STRIPE_LISTEN_WITH_KEY=1 STRIPE_SECRET_KEY=... ./scripts/dev.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PY:-.venv/bin/python}
CELERY=${CELERY:-.venv/bin/celery}
PORT=${PORT:-8000}
EVENTS=payment_intent.succeeded,payment_intent.payment_failed

docker compose up -d --wait db redis
"$PY" manage.py migrate --noinput

pids=()
cleanup() {
  trap - INT TERM EXIT
  # each service runs in its own process group (setsid), so wrappers like the Stripe CLI's
  # node launcher are stopped together with the real process they spawn
  for p in "${pids[@]}"; do kill -TERM -- "-$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# run NAME CMD...: start CMD in the background with its output prefixed by [NAME]
run() {
  local name=$1; shift
  ( exec setsid "$@" ) > >(sed -u "s/^/[$name] /") 2>&1 &
  pids+=($!)
}

run web "$PY" manage.py runserver "$PORT" --noreload
run worker "$CELERY" -A config worker -l info

if command -v stripe >/dev/null; then
  args=(listen --events "$EVENTS" --forward-to "localhost:$PORT/webhooks/stripe/")
  if [ "${STRIPE_LISTEN_WITH_KEY:-0}" = 1 ]; then args+=(--api-key "$STRIPE_SECRET_KEY"); fi
  run stripe stripe "${args[@]}"
else
  echo "[dev] stripe CLI not found: payments will be created but webhooks will not arrive" >&2
fi

echo "[dev] http://localhost:$PORT/  (Ctrl+C to stop)"
wait
