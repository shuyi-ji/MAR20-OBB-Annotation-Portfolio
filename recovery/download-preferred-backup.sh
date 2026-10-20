#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/recovery/versions.env"

OUTPUT_DIR="${1:-$ROOT/.runtime/mar20-cvat/artifacts}"
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

manifest="$OUTPUT_DIR/snapshot_manifest.json"
manifest_part="$manifest.part"

if curl --fail --location --retry 3 --retry-all-errors \
  --output "$manifest_part" "$CHECKPOINT_MANIFEST_URL"; then
  mv "$manifest_part" "$manifest"
  mapfile -t checkpoint < <(python3 - "$manifest" <<'PY'
import json
import re
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
filename = str(manifest["backup"]["filename"])
digest = str(manifest["backup"]["sha256"]).lower()
size = int(manifest["backup"]["bytes"])
if Path(filename).name != filename or not filename.endswith(".zip"):
    raise SystemExit("unsafe checkpoint backup filename")
if not re.fullmatch(r"[0-9a-f]{64}", digest):
    raise SystemExit("invalid checkpoint SHA-256")
if size <= 0:
    raise SystemExit("invalid checkpoint byte count")
print(filename)
print(digest)
print(size)
PY
  )
  backup_name="${checkpoint[0]}"
  expected_sha="${checkpoint[1]}"
  expected_bytes="${checkpoint[2]}"
  backup="$OUTPUT_DIR/$backup_name"
  partial="$backup.part"
  echo "[DOWNLOAD] latest checkpoint $CHECKPOINT_RELEASE_TAG" >&2
  curl --fail --location --retry 6 --retry-all-errors \
    --output "$partial" "$CHECKPOINT_DOWNLOAD_BASE/$backup_name"
  test "$(wc -c < "$partial")" -eq "$expected_bytes"
  echo "$expected_sha  $partial" | sha256sum --check --strict >&2
  mv "$partial" "$backup"
  python3 "$ROOT/recovery/verify_backup.py" "$backup" --manifest "$manifest" >&2
  printf '%s\n' "$backup"
  exit 0
fi

rm -f "$manifest_part"
backup="$OUTPUT_DIR/$TASK_BACKUP_FILE"
partial="$backup.part"
echo "[INFO] No latest checkpoint is published; using immutable $RECOVERY_RELEASE_TAG" >&2
curl --fail --location --retry 6 --retry-all-errors \
  --output "$partial" "$RECOVERY_DOWNLOAD_URL"
echo "$TASK_BACKUP_SHA256  $partial" | sha256sum --check --strict >&2
mv "$partial" "$backup"
python3 "$ROOT/recovery/verify_backup.py" "$backup" >&2
printf '%s\n' "$backup"
