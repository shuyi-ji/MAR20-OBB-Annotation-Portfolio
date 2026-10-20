#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/recovery/versions.env"

RUNTIME_DIR="${MAR20_RUNTIME_DIR:-$ROOT/.runtime/mar20-cvat}"
CVAT_DIR="$RUNTIME_DIR/cvat"
ARTIFACT_DIR="$RUNTIME_DIR/artifacts"
REPORT_DIR="$RUNTIME_DIR/reports"
BACKUP_PATH="${1:-}"

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

for command in docker git curl sha256sum python3; do
  require "$command"
done
docker compose version >/dev/null
docker info >/dev/null

mkdir -p "$ARTIFACT_DIR" "$REPORT_DIR" "$RUNTIME_DIR/labels" "$RUNTIME_DIR/state"

if [[ -z "$BACKUP_PATH" ]]; then
  BACKUP_PATH="$(bash "$ROOT/recovery/download-preferred-backup.sh" "$ARTIFACT_DIR")"
elif [[ ! -f "$BACKUP_PATH" ]]; then
  echo "Explicit backup does not exist: $BACKUP_PATH" >&2
  exit 1
elif [[ -f "$(dirname "$BACKUP_PATH")/snapshot_manifest.json" ]]; then
  python3 "$ROOT/recovery/verify_backup.py" "$BACKUP_PATH" \
    --manifest "$(dirname "$BACKUP_PATH")/snapshot_manifest.json"
else
  python3 "$ROOT/recovery/verify_backup.py" "$BACKUP_PATH"
fi

if [[ ! -d "$CVAT_DIR/.git" ]]; then
  mkdir -p "$RUNTIME_DIR"
  git clone --filter=blob:none --no-checkout https://github.com/cvat-ai/cvat.git "$CVAT_DIR"
  git -C "$CVAT_DIR" fetch --depth 1 origin "$CVAT_COMMIT"
  git -C "$CVAT_DIR" checkout --detach "$CVAT_COMMIT"
fi

actual_commit="$(git -C "$CVAT_DIR" rev-parse HEAD)"
if [[ "$actual_commit" != "$CVAT_COMMIT" ]]; then
  echo "CVAT source mismatch: expected $CVAT_COMMIT, found $actual_commit" >&2
  exit 1
fi

echo "[CVAT] starting minimal annotation services at http://localhost:8080"
(
  cd "$CVAT_DIR"
  CVAT_VERSION="$CVAT_VERSION" CVAT_HOST=localhost docker compose up -d \
    traefik cvat_server cvat_ui cvat_worker_import cvat_worker_export \
    cvat_worker_annotation cvat_worker_chunks
)

healthy=0
for _ in $(seq 1 60); do
  if docker exec cvat_server python3 manage.py health_check >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 5
done
if [[ "$healthy" != 1 ]]; then
  echo "CVAT did not become healthy within 5 minutes" >&2
  exit 1
fi

read -r -p "CVAT username [jishuyi]: " CVAT_USER
CVAT_USER="${CVAT_USER:-jishuyi}"
if ! docker exec -e MAR20_USER="$CVAT_USER" cvat_server bash -lc \
  'python3 ~/manage.py shell -c '\''import os,sys; from django.contrib.auth import get_user_model; sys.exit(0 if get_user_model().objects.filter(username=os.environ["MAR20_USER"]).exists() else 1)'\'''; then
  echo "No user named '$CVAT_USER' exists. Create a superuser now."
  docker exec -it cvat_server bash -ic 'python3 ~/manage.py createsuperuser'
  read -r -p "Username just created [$CVAT_USER]: " CREATED_USER
  CVAT_USER="${CREATED_USER:-$CVAT_USER}"
fi

echo "[CLIENT] building the recovery verifier"
docker build \
  --file "$ROOT/recovery/Dockerfile.recovery-client" \
  --tag "mar20-recovery-client:$CVAT_VERSION" \
  "$ROOT/recovery"

CVAT_NETWORK="$(docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' cvat_server | head -n 1)"
if [[ -z "$CVAT_NETWORK" ]]; then
  echo "Unable to determine the CVAT Docker network" >&2
  exit 1
fi

docker run --rm -it \
  --network "$CVAT_NETWORK" \
  --env "CVAT_USERNAME=$CVAT_USER" \
  --env "CVAT_HOST=http://cvat_server:8080" \
  --volume "$BACKUP_PATH:/recovery/task.zip:ro" \
  --volume "$ROOT/recovery/import_and_verify.py:/recovery/import_and_verify.py:ro" \
  --volume "$REPORT_DIR:/reports:rw" \
  --entrypoint python3 \
  "mar20-recovery-client:$CVAT_VERSION" \
  /recovery/import_and_verify.py \
  --backup /recovery/task.zip \
  --report /reports/restore_report.json

export CVAT_VERSION
export MAR20_REPO_ROOT="$ROOT"
export MAR20_RUNTIME_DIR="$RUNTIME_DIR"

# cvat/server runs as UID:GID 1000:1000. Normalize only the two writable
# bind-mount directories so synchronization also works on hosts with another UID.
docker run --rm --user 0:0 \
  --volume "$RUNTIME_DIR/labels:/dataset/labels" \
  --volume "$RUNTIME_DIR/state:/state" \
  --entrypoint sh \
  "cvat/server:$CVAT_VERSION" \
  -c 'chown -R 1000:1000 /dataset/labels /state && chmod -R u+rwX,go+rX /dataset/labels /state'

(
  cd "$CVAT_DIR"
  docker compose \
    -f docker-compose.yml \
    -f "$ROOT/recovery/docker-compose.sync.yml" \
    up -d --no-deps mar20_portfolio_label_sync
)

label_count=0
for _ in $(seq 1 30); do
  label_count="$(find "$RUNTIME_DIR/labels" -maxdepth 1 -type f -name '*.txt' | wc -l)"
  if [[ "$label_count" -eq 200 ]]; then
    break
  fi
  sleep 3
done
if [[ "$label_count" -ne 200 ]]; then
  echo "Label synchronizer produced $label_count/200 files" >&2
  exit 1
fi

echo "[PASS] Interactive CVAT restored at http://localhost:8080"
echo "[PASS] 200 live YOLO-OBB labels: $RUNTIME_DIR/labels"
echo "[PASS] Restore report: $REPORT_DIR/restore_report.json"
