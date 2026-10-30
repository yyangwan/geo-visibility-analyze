#!/usr/bin/env bash
# Run only from the pull-only production checkout after runner image transfer.
set -Eeuo pipefail
ROOT=/opt/geo-visibility-analyze
STATE=/opt/geo-visibility-deploy/intelligence
LEGACY_ENV=/opt/geo-visibility-analyze/.intelligence.env
VALUE_ENV=/opt/geo-visibility-analyze/.intelligence-value.env
VALUE_DATABASE=intelligence_value_20261030
UPSTREAM=/etc/nginx/conf.d/genilink-intelligence-upstream.inc
NGINX_CONF=/etc/nginx/conf.d/genilink-intelligence.conf
IMAGE_ROOT=ghcr.io/yyangwan/geo-visibility-analyze
LEGACY_CONTAINER=geo-visibility-analyze-backend-1
MODE=${1:-}
[[ "$MODE" == deploy || "$MODE" == rollback ]] || { echo 'Usage: deploy-intelligence.sh deploy FULL_SHA | rollback' >&2; exit 2; }
umask 077
mkdir -p "$STATE"
exec 9>"$STATE/deploy.lock"
flock -n 9 || { echo 'An intelligence deployment is already running' >&2; exit 1; }
cd "$ROOT"
WORKER_ID=$(openssl rand -hex 16)
[[ "$WORKER_ID" =~ ^[a-f0-9]{32}$ ]]
ACTIVE=$(cat "$STATE/active-slot" 2>/dev/null || true)
ACTIVE_IMAGE=$(cat "$STATE/active-image" 2>/dev/null || true)
ACTIVE_ENV=$(cat "$STATE/active-env" 2>/dev/null || printf '%s' "$LEGACY_ENV")
valid_env() { [[ "$1" == "$LEGACY_ENV" || "$1" == "$VALUE_ENV" ]]; }
valid_env "$ACTIVE_ENV"
[[ -z "$ACTIVE" || "$ACTIVE" == blue || "$ACTIVE" == green || "$ACTIVE" == legacy ]]
http_ok() {
  test "$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 8 "$1")" = 200
}
legacy_ready() {
  [[ "$(docker inspect --format '{{.State.Running}}' "$LEGACY_CONTAINER")" == true ]] && \
    [[ "$(docker inspect --format '{{.Image}}' "$LEGACY_CONTAINER")" == "$1" ]] && \
    http_ok http://127.0.0.1:8000/api/health
}
if [[ -z "$ACTIVE" ]]; then
  # Record a verified initial rollback target without starting/stopping legacy.
  ACTIVE_IMAGE=$(docker inspect --format '{{.Image}}' "$LEGACY_CONTAINER")
  [[ "$ACTIVE_IMAGE" =~ ^sha256:[a-f0-9]{64}$ ]]
  legacy_ready "$ACTIVE_IMAGE"
  ACTIVE=legacy
fi
if [[ "$MODE" == rollback ]]; then
  SLOT=$(cat "$STATE/previous-slot")
  IMAGE=$(cat "$STATE/previous-image")
  ENV_FILE=$(cat "$STATE/previous-env" 2>/dev/null || printf '%s' "$LEGACY_ENV")
  valid_env "$ENV_FILE"
  [[ "$SLOT" == blue || "$SLOT" == green || "$SLOT" == legacy ]]
  [[ "$SLOT" != "$ACTIVE" ]]
  SHA=${IMAGE##*:}
else
  SHA=${2:-}
  [[ "$SHA" =~ ^[a-f0-9]{40}$ ]]
  # No unlanded, stale or different-revision deployment code may be executed.
  test "$(git rev-parse HEAD)" = "$SHA"
  test "$(git branch --show-current)" = main
  IMAGE="$IMAGE_ROOT:$SHA"
  ENV_FILE="$VALUE_ENV"
  if [[ "$ACTIVE_IMAGE" == "$IMAGE" ]]; then echo 'Release already active'; exit 0; fi
  SLOT=blue
  [[ "$ACTIVE" != blue ]] || SLOT=green
fi
if [[ "$ACTIVE" == legacy ]]; then
  [[ "$ACTIVE_IMAGE" =~ ^sha256:[a-f0-9]{64}$ ]]
  legacy_ready "$ACTIVE_IMAGE"
else
  [[ "$ACTIVE_IMAGE" =~ ^ghcr.io/yyangwan/geo-visibility-analyze:[a-f0-9]{40}$ ]]
fi
if [[ "$SLOT" != legacy ]]; then
  [[ "$SHA" =~ ^[a-f0-9]{40}$ && "$IMAGE" == "$IMAGE_ROOT:$SHA" ]]
  test -f "$ENV_FILE"
  test "$(stat -c '%a' "$ENV_FILE")" = 600
  test "$(docker image inspect "$IMAGE" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')" = "$SHA"
  docker network inspect geo-visibility-analyze_default >/dev/null
else
  [[ "$IMAGE" =~ ^sha256:[a-f0-9]{64}$ ]]
  legacy_ready "$IMAGE"
fi
PORT=18016
[[ "$SLOT" != green ]] || PORT=18017
compose() {
  local slot=$1 image=$2 runtime_env=$3 port=18016
  shift 3
  [[ "$slot" != green ]] || port=18017
  INTELLIGENCE_IMAGE="$image" INTELLIGENCE_ENV_FILE="$runtime_env" INTELLIGENCE_API_PORT="$port" INTELLIGENCE_RELEASE_SHA="${image##*:}" INTELLIGENCE_WORKER_MONITOR_ID="$WORKER_ID" \
    docker compose --project-name "genilink-intelligence-$slot" \
    -f "$ROOT/compose.intelligence-runtime.yml" -f "$ROOT/compose.intelligence-production.yml" \
    --profile worker --profile maintenance "$@"
}
if [[ "$SLOT" != legacy ]]; then
  compose "$SLOT" "$IMAGE" "$ENV_FILE" config --quiet
  # This effective-settings check happens inside exactly the image being migrated.
  compose "$SLOT" "$IMAGE" "$ENV_FILE" run --rm --no-deps migrate python -m scripts.check_intelligence_deployment validate
  if [[ "$MODE" == deploy ]]; then
    compose "$SLOT" "$IMAGE" "$ENV_FILE" run --rm --no-deps migrate python -m scripts.initialize_value_database --expected-database "$VALUE_DATABASE"
  fi
fi
BACKUP=$(mktemp -d "$STATE/attempt.XXXXXXXX")
HAD_UPSTREAM=0 HAD_CONF=0 TOUCHED=0 COMMITTED=0 CANDIDATE_STARTED=0
if [[ -f "$UPSTREAM" ]]; then cp "$UPSTREAM" "$BACKUP/upstream"; HAD_UPSTREAM=1; fi
if [[ -f "$NGINX_CONF" ]]; then cp "$NGINX_CONF" "$BACKUP/nginx.conf"; HAD_CONF=1; fi
STATE_NAMES=(active-slot active-image active-env previous-slot previous-image previous-env)
for name in "${STATE_NAMES[@]}"; do
  if [[ -f "$STATE/$name" ]]; then cp "$STATE/$name" "$BACKUP/$name"; fi
done
recover() {
  local code=$?
  trap - EXIT
  if [[ "$COMMITTED" == 0 ]]; then
    set +e
    local restored=1
    if [[ "$TOUCHED" == 1 ]]; then
      if [[ "$HAD_UPSTREAM" == 1 ]]; then cp "$BACKUP/upstream" "$UPSTREAM" || restored=0; else rm -f "$UPSTREAM" || restored=0; fi
      if [[ "$HAD_CONF" == 1 ]]; then cp "$BACKUP/nginx.conf" "$NGINX_CONF" || restored=0; else rm -f "$NGINX_CONF" || restored=0; fi
      nginx -t && nginx -s reload || restored=0
    fi
    for name in "${STATE_NAMES[@]}"; do
      if [[ -f "$BACKUP/$name" ]]; then
        cp "$BACKUP/$name" "$STATE/.$name.restore" && mv "$STATE/.$name.restore" "$STATE/$name" || restored=0
      else
        rm -f "$STATE/$name" || restored=0
      fi
    done
    # If Nginx restoration itself fails, retain both instances for recovery.
    if [[ "$CANDIDATE_STARTED" == 1 && "$restored" == 1 ]]; then compose "$SLOT" "$IMAGE" "$ENV_FILE" stop worker api; fi
    echo "Intelligence deployment failed; previous release preserved. Recovery files: $BACKUP" >&2
    [[ "$code" != 0 ]] || code=1
  fi
  exit "$code"
}
trap recover EXIT
wait_for() {
  local count=$1
  shift
  for ((attempt=0; attempt<count; attempt++)); do
    if "$@"; then return 0; fi
    sleep 2
  done
  return 1
}
api_ready() {
  local id
  id=$(compose "$SLOT" "$IMAGE" "$ENV_FILE" ps -q api)
  [[ -n "$id" ]] && [[ "$(docker inspect --format '{{.State.Health.Status}}' "$id")" == healthy ]] && \
    compose "$SLOT" "$IMAGE" "$ENV_FILE" exec -T api python -m scripts.check_intelligence_deployment health
}
worker_ready() {
  local id
  id=$(compose "$SLOT" "$IMAGE" "$ENV_FILE" ps -q worker)
  [[ -n "$id" ]] && [[ "$(docker inspect --format '{{.State.Running}}' "$id")" == true ]] && \
    compose "$SLOT" "$IMAGE" "$ENV_FILE" exec -T api python -m scripts.check_intelligence_deployment worker --since "$WATERMARK" --worker-id "$WORKER_ID"
}
public_ready() {
  curl --fail --silent --max-time 8 http://127.0.0.1:18018/api/health | \
    docker run --rm -i --network none --pull never "$IMAGE" python -c \
      'import json,sys; d=json.load(sys.stdin); assert d.get("status")=="ok" and d.get("db")=="connected" and d.get("deploymentVersion")==sys.argv[1]' "$SHA"
}
write_state() {
  printf '%s\n' "$2" > "$STATE/.$1.next"
  mv "$STATE/.$1.next" "$STATE/$1"
}
record_switch() {
  write_state previous-slot "$ACTIVE"
  write_state previous-image "$ACTIVE_IMAGE"
  write_state previous-env "$ACTIVE_ENV"
  write_state active-slot "$SLOT"
  write_state active-image "$IMAGE"
  write_state active-env "$ENV_FILE"
}
if [[ "$SLOT" == legacy ]]; then
  TOUCHED=1
  printf 'server 127.0.0.1:8000;\n' > "$UPSTREAM.tmp"
  mv "$UPSTREAM.tmp" "$UPSTREAM"
  nginx -t
  nginx -s reload
  # Legacy has no deploymentVersion contract; verify its exact image/container
  # and both direct and switched HTTP health instead of requiring a new SHA.
  wait_for 15 legacy_ready "$IMAGE"
  wait_for 15 http_ok http://127.0.0.1:18018/api/health
  record_switch
  COMMITTED=1
  compose "$ACTIVE" "$ACTIVE_IMAGE" "$ACTIVE_ENV" stop worker api
  echo 'Intelligence upstream restored to verified running legacy service on port 8000'
  exit 0
fi
CANDIDATE_STARTED=1
compose "$SLOT" "$IMAGE" "$ENV_FILE" up -d --no-deps api
wait_for 60 api_ready
WATERMARK=$(compose "$SLOT" "$IMAGE" "$ENV_FILE" exec -T api python -m scripts.check_intelligence_deployment watermark)
[[ "$WATERMARK" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}$ ]]
# MySQL heartbeat fields are second precision: do not match any pre-start row.
sleep 2
compose "$SLOT" "$IMAGE" "$ENV_FILE" up -d --no-deps --force-recreate worker
wait_for 120 worker_ready
TOUCHED=1
printf 'server 127.0.0.1:%s;\n' "$PORT" > "$BACKUP/next-upstream"
cp "$BACKUP/next-upstream" "$UPSTREAM.tmp"
mv "$UPSTREAM.tmp" "$UPSTREAM"
cat > "$NGINX_CONF" <<'NGINX'
upstream genilink_intelligence {
    include /etc/nginx/conf.d/genilink-intelligence-upstream.inc;
}
server {
    listen 127.0.0.1:18018;
    server_name localhost;
    location / {
        proxy_pass http://genilink_intelligence;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
    }
}
NGINX
nginx -t
nginx -s reload
wait_for 15 public_ready
record_switch
COMMITTED=1
# Stop only the preceding dedicated intelligence slot after both probes passed.
# Never touch the separate legacy geo-visibility-analyze-backend-1 container.
if [[ "$ACTIVE" != legacy ]]; then compose "$ACTIVE" "$ACTIVE_IMAGE" "$ACTIVE_ENV" stop worker api; fi
printf 'Intelligence active: %s %s, previous slot retained for rollback\n' "$SLOT" "$SHA"
