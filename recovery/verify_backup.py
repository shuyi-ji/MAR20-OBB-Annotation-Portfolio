#!/usr/bin/env python3
"""Offline integrity and structure gate for the Task #25 CVAT backup."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath


EXPECTED_SHA256 = "7e42b43f213e4e97333b8e8971d4085533bb80edefa12e3987822bff23c730e6"
EXPECTED_TASK = "MAR20_Portfolio_OBB_Review_200"
EXPECTED = {"images": 200, "labels": 20, "jobs": 4, "shapes": 1534}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup", type=Path)
    parser.add_argument(
        "--manifest",
        type=Path,
        help="checkpoint manifest supplying the expected hash and current shape count",
    )
    args = parser.parse_args()

    backup = args.backup.resolve()
    if not backup.is_file():
        raise SystemExit(f"backup not found: {backup}")

    expected_sha256 = EXPECTED_SHA256
    expected = dict(EXPECTED)
    manifest: dict | None = None
    if args.manifest:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        expected_sha256 = str(manifest["backup"]["sha256"])
        expected = {
            "images": int(manifest["task"]["frames"]),
            "labels": int(manifest["task"]["labels"]),
            "jobs": int(manifest["task"]["jobs"]),
            "shapes": int(manifest["task"]["shapes"]),
        }

    actual_hash = sha256(backup)
    failures: list[str] = []
    if actual_hash != expected_sha256:
        failures.append(f"sha256 mismatch: {actual_hash}")
    if manifest:
        if manifest.get("schema_version") != 1:
            failures.append("unsupported checkpoint manifest schema")
        if manifest.get("task", {}).get("name") != EXPECTED_TASK:
            failures.append("manifest task name mismatch")
        if expected["images"] != 200 or expected["labels"] != 20 or expected["jobs"] != 4:
            failures.append(f"manifest structure mismatch: {expected!r}")
        if expected["shapes"] <= 0:
            failures.append("manifest shape count must be positive")
        if manifest["backup"].get("filename") != backup.name:
            failures.append("manifest backup filename mismatch")
        if int(manifest["backup"].get("bytes", -1)) != backup.stat().st_size:
            failures.append("manifest backup byte count mismatch")

    with zipfile.ZipFile(backup) as archive:
        names = archive.namelist()
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                failures.append(f"unsafe ZIP path: {name}")

        required = {"task.json", "annotations.json"}
        missing = required.difference(names)
        if missing:
            failures.append(f"missing entries: {sorted(missing)}")
            task = {}
            annotations = []
        else:
            task = json.loads(archive.read("task.json"))
            annotations = json.loads(archive.read("annotations.json"))

        images = [
            name
            for name in names
            if PurePosixPath(name).suffix.lower() in IMAGE_EXTENSIONS
            and name.startswith("data/")
        ]

    labels = task.get("labels", [])
    jobs = task.get("jobs", [])
    shape_count = sum(len(job.get("shapes", [])) for job in annotations)
    track_count = sum(len(job.get("tracks", [])) for job in annotations)
    actual = {
        "images": len(images),
        "labels": len(labels),
        "jobs": len(jobs),
        "shapes": shape_count,
    }

    if task.get("name") != EXPECTED_TASK:
        failures.append(f"task name mismatch: {task.get('name')!r}")
    if actual != expected:
        failures.append(f"content mismatch: {actual!r}")
    if track_count:
        failures.append(f"unexpected tracks: {track_count}")
    if task.get("data", {}).get("storage") != "local":
        failures.append("backup is not self-contained local storage")
    if len(set(images)) != len(images):
        failures.append("duplicate image paths in backup")

    report = {
        "status": "PASS" if not failures else "FAIL",
        "backup": backup.name,
        "bytes": backup.stat().st_size,
        "sha256": actual_hash,
        "task": task.get("name"),
        "storage": task.get("data", {}).get("storage"),
        "metrics": actual,
        "expected_metrics": expected,
        "tracks": track_count,
        "failures": failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
