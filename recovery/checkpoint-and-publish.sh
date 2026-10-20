#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/recovery/versions.env"

RUNTIME_DIR="${MAR20_RUNTIME_DIR:-$ROOT/.runtime/mar20-cvat}"
CVAT_DIR="$RUNTIME_DIR/cvat"
SNAPSHOT_DIR="$RUNTIME_DIR/snapshots/latest"
LABEL_DIR="$RUNTIME_DIR/labels"
BACKUP_NAME="task_25_MAR20_Portfolio_OBB_Review_200-latest.zip"
LABEL_ARCHIVE_NAME="task_25_MAR20_Portfolio_OBB_Review_200-labels-latest.zip"

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

for command in docker python3; do
  require "$command"
done
docker info >/dev/null

if [[ ! -d "$CVAT_DIR/.git" ]]; then
  echo "Restored CVAT runtime not found. Run recovery/bootstrap-and-restore.sh first." >&2
  exit 1
fi
if [[ ! -d "$LABEL_DIR" ]]; then
  echo "Live label directory not found: $LABEL_DIR" >&2
  exit 1
fi

mkdir -p "$SNAPSHOT_DIR"
read -r -p "CVAT username [jishuyi]: " CVAT_USER
CVAT_USER="${CVAT_USER:-jishuyi}"

if ! docker image inspect "mar20-recovery-client:$CVAT_VERSION" >/dev/null 2>&1; then
  docker build \
    --file "$ROOT/recovery/Dockerfile.recovery-client" \
    --tag "mar20-recovery-client:$CVAT_VERSION" \
    "$ROOT/recovery"
fi

CVAT_NETWORK="$(docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' cvat_server | head -n 1)"
if [[ -z "$CVAT_NETWORK" ]]; then
  echo "Unable to determine the CVAT Docker network" >&2
  exit 1
fi

docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  --network "$CVAT_NETWORK" \
  --env "CVAT_USERNAME=$CVAT_USER" \
  --env "CVAT_HOST=http://cvat_server:8080" \
  --volume "$ROOT/recovery/export_snapshot.py:/recovery/export_snapshot.py:ro" \
  --volume "$SNAPSHOT_DIR:/snapshot:rw" \
  --volume "$LABEL_DIR:/labels:ro" \
  --entrypoint python3 \
  "mar20-recovery-client:$CVAT_VERSION" \
  /recovery/export_snapshot.py \
  --output-dir /snapshot \
  --labels-dir /labels

python3 "$ROOT/recovery/verify_backup.py" "$SNAPSHOT_DIR/$BACKUP_NAME" \
  --manifest "$SNAPSHOT_DIR/snapshot_manifest.json"

if [[ "${MAR20_SKIP_PUBLISH:-0}" == "1" ]]; then
  echo "[PASS] Local checkpoint ready for the Windows publisher: $SNAPSHOT_DIR"
  exit 0
fi

require gh
gh auth status >/dev/null

if ! gh release view "$CHECKPOINT_RELEASE_TAG" \
  --repo shuyi-ji/MAR20-OBB-Annotation-Portfolio >/dev/null 2>&1; then
  gh release create "$CHECKPOINT_RELEASE_TAG" \
    --repo shuyi-ji/MAR20-OBB-Annotation-Portfolio \
    --title "Task #25 latest working checkpoint" \
    --notes-file "$ROOT/recovery/CHECKPOINT_NOTES.md"
fi

gh release upload "$CHECKPOINT_RELEASE_TAG" \
  "$SNAPSHOT_DIR/$BACKUP_NAME" \
  "$SNAPSHOT_DIR/$LABEL_ARCHIVE_NAME" \
  --repo shuyi-ji/MAR20-OBB-Annotation-Portfolio \
  --clobber

# Upload the manifest last. It is the integrity pointer used by restore; a partial
# asset upload therefore fails closed instead of importing an unverified backup.
gh release upload "$CHECKPOINT_RELEASE_TAG" \
  "$SNAPSHOT_DIR/snapshot_manifest.json" \
  --repo shuyi-ji/MAR20-OBB-Annotation-Portfolio \
  --clobber

echo "[PASS] Latest task and labels published to GitHub Release $CHECKPOINT_RELEASE_TAG"
echo "[PASS] Local checkpoint: $SNAPSHOT_DIR"
