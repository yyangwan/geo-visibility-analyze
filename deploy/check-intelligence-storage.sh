#!/usr/bin/env bash
# Runner-only transport; no application checkout, database access or service switch on ECS.
set -euo pipefail
[[ "${RELEASE_SHA:-}" =~ ^[a-f0-9]{40}$ ]]
[[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ ]]
[[ "${GITHUB_RUN_ATTEMPT:-}" =~ ^[0-9]+$ ]]
[[ "${STORAGE_HOST:-}" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]]
[[ "${STORAGE_USER:-}" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]]
IMAGE="ghcr.io/yyangwan/geo-visibility-analyze:$RELEASE_SHA"
TARGET="$STORAGE_USER@$STORAGE_HOST"
SSH=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes
  -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=20
  -o UserKnownHostsFile="$HOME/.ssh/citation_storage_known_hosts"
  -i "$HOME/.ssh/citation_storage")

# Refuse before transfer if headroom is insufficient. No automatic cleanup.
ssh "${SSH[@]}" "$TARGET" 'set -eu; command -v docker >/dev/null; test "$(df -Pk /var/lib/docker | awk "NR==2 {print \$4}")" -ge 3145728; test "$(awk "/MemAvailable:/ {print \$2}" /proc/meminfo)" -ge 786432'
docker pull "$IMAGE"
test "$(docker image inspect "$IMAGE" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')" = "$RELEASE_SHA"
docker save "$IMAGE" | gzip -1 | ssh "${SSH[@]}" "$TARGET" "bash -o pipefail -c 'gzip -d | docker load'"

# Only validated hex/digits reach the remote command. No server source/config is copied.
ssh "${SSH[@]}" "$TARGET" "bash -s -- $RELEASE_SHA $GITHUB_RUN_ID $GITHUB_RUN_ATTEMPT" <<'REMOTE'
set -euo pipefail
SHA="$1"
[[ "$SHA" =~ ^[a-f0-9]{40}$ && "$2" =~ ^[0-9]+$ && "$3" =~ ^[0-9]+$ ]]
IMAGE="ghcr.io/yyangwan/geo-visibility-analyze:$SHA"
test "$(docker image inspect "$IMAGE" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')" = "$SHA"
REPORT_DIR="/opt/genilink-storage-checks/$SHA-$2-$3"
umask 077
mkdir -p /opt/genilink-storage-checks
mkdir "$REPORT_DIR"
chmod 700 "$REPORT_DIR"
docker run --rm --pull never --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 128 --memory 512m --cpus 0.5 \
  --log-driver json-file --log-opt max-size=1m --log-opt max-file=1 \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --mount "type=bind,src=$REPORT_DIR,dst=/checks" \
  --env AISCOPE_SECRET_KEY=storage-acceptance-only-not-an-auth-service-0000 \
  --env AISCOPE_DATABASE_URL=mysql+aiomysql://unused:unused@127.0.0.1:1/unused \
  --env AISCOPE_INTELLIGENCE_ENVIRONMENT=production \
  --env AISCOPE_INTELLIGENCE_BLOB_BACKEND=oss \
  --env AISCOPE_INTELLIGENCE_OSS_BUCKET=genilink-citation-evidence \
  --env AISCOPE_INTELLIGENCE_OSS_ROLE=GenilinkCitationWorkerRole \
  --env AISCOPE_INTELLIGENCE_OSS_ENDPOINT=https://oss-cn-wulanchabu-internal.aliyuncs.com \
  "$IMAGE" python -m app.intelligence.storage_check --write-probe --output /checks/report.json
printf 'Verified image: %s\nPrivate storage report: %s/report.json\n' "$SHA" "$REPORT_DIR"
REMOTE
