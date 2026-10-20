#!/usr/bin/env python3
"""Export the current CVAT task and its live YOLO-OBB labels as a checkpoint."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests


TASK_NAME = "MAR20_Portfolio_OBB_Review_200"
EXPECTED_STRUCTURE = {"frames": 200, "labels": 20, "jobs": 4}
BACKUP_NAME = "task_25_MAR20_Portfolio_OBB_Review_200-latest.zip"
LABEL_ARCHIVE_NAME = "task_25_MAR20_Portfolio_OBB_Review_200-labels-latest.zip"
MANIFEST_NAME = "snapshot_manifest.json"


def require_success(response: requests.Response, context: str) -> None:
    if response.ok:
        return
    body = response.text.strip().replace("\x00", "")[:4000]
    raise RuntimeError(
        f"{context}: HTTP {response.status_code} {response.reason}; response={body!r}"
    )


def fetch_all(session: requests.Session, url: str, **params: object) -> list[dict]:
    output: list[dict] = []
    next_url: str | None = url
    first = True
    while next_url:
        response = session.get(next_url, params=params if first else None, timeout=120)
        require_success(response, f"GET {next_url}")
        payload = response.json()
        first = False
        if isinstance(payload, list):
            output.extend(payload)
            break
        output.extend(payload.get("results", []))
        next_url = payload.get("next")
    return output


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wait_for_export(
    session: requests.Session,
    origin: str,
    rq_id: str,
    *,
    timeout: int = 1800,
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = session.get(f"{origin}/api/requests/{rq_id}", timeout=120)
        require_success(response, f"GET export request {rq_id}")
        payload = response.json()
        status = str(payload.get("status", "")).lower()
        message = payload.get("message") or ""
        print(f"[EXPORT] request={rq_id} status={status} message={message}", flush=True)
        if status == "finished":
            result_url = payload.get("result_url")
            if not result_url:
                raise RuntimeError(f"finished export has no result_url: {payload!r}")
            return str(result_url)
        if status == "failed":
            raise RuntimeError(f"backup export failed: {message or payload!r}")
        time.sleep(2)
    raise TimeoutError(f"backup export did not finish within {timeout} seconds")


def localize_result_url(origin: str, result_url: str) -> str:
    parsed = urlsplit(result_url)
    path = parsed.path
    if parsed.query:
        path += f"?{parsed.query}"
    return f"{origin.rstrip('/')}/{path.lstrip('/')}"


def download_backup(
    session: requests.Session,
    origin: str,
    task_id: int,
    output_path: Path,
) -> None:
    response = session.post(
        f"{origin}/api/tasks/{task_id}/backup/export",
        params={"location": "local", "lightweight": "false"},
        timeout=120,
    )
    require_success(response, "POST task backup export")
    rq_id = response.json().get("rq_id")
    if not rq_id:
        raise RuntimeError(f"backup export response has no rq_id: {response.text}")

    result_url = wait_for_export(session, origin, str(rq_id))
    download_url = localize_result_url(origin, result_url)
    temporary = output_path.with_suffix(output_path.suffix + ".part")
    with session.get(download_url, stream=True, timeout=(120, 1800)) as download:
        require_success(download, "GET prepared task backup")
        with temporary.open("wb") as stream:
            for chunk in download.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
    os.replace(temporary, output_path)


def archive_labels(
    labels_dir: Path, output_path: Path, expected_rows: int
) -> tuple[int, int]:
    labels = sorted(labels_dir.glob("*.txt"))
    if len(labels) != EXPECTED_STRUCTURE["frames"]:
        raise RuntimeError(f"expected 200 label files, found {len(labels)}")
    line_count = sum(
        sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        for path in labels
    )
    if line_count != expected_rows:
        raise RuntimeError(
            f"CVAT has {expected_rows} shapes but synchronized labels have {line_count} rows"
        )

    temporary = output_path.with_suffix(output_path.suffix + ".part")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in labels:
            archive.write(path, f"labels/{path.name}")
    os.replace(temporary, output_path)
    return len(labels), line_count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("CVAT_HOST", "http://cvat_server:8080"))
    parser.add_argument("--host-header", default="localhost")
    parser.add_argument("--username", default=os.environ.get("CVAT_USERNAME", "jishuyi"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--labels-dir", type=Path, required=True)
    args = parser.parse_args()

    password = os.environ.pop("CVAT_PASSWORD", None)
    if password is None:
        password = getpass.getpass(f"CVAT password for {args.username}: ")
    if not password:
        raise SystemExit("empty password")

    origin = args.host.rstrip("/")
    session = requests.Session()
    session.auth = (args.username, password)
    session.headers["Host"] = args.host_header

    tasks = [
        item
        for item in fetch_all(session, f"{origin}/api/tasks", page_size=100)
        if item.get("name") == TASK_NAME
    ]
    if len(tasks) != 1:
        raise RuntimeError(f"expected exactly one task named {TASK_NAME!r}, found {len(tasks)}")
    task_id = int(tasks[0]["id"])

    meta_response = session.get(f"{origin}/api/tasks/{task_id}/data/meta", timeout=120)
    require_success(meta_response, "GET task data metadata")
    annotations_response = session.get(f"{origin}/api/tasks/{task_id}/annotations", timeout=120)
    require_success(annotations_response, "GET task annotations")
    annotation_payload = annotations_response.json()
    actual = {
        "frames": len(meta_response.json().get("frames", [])),
        "labels": len(fetch_all(session, f"{origin}/api/labels", task_id=task_id, page_size=100)),
        "jobs": len(fetch_all(session, f"{origin}/api/jobs", task_id=task_id, page_size=100)),
        "shapes": len(annotation_payload.get("shapes", [])),
    }
    structure = {key: actual[key] for key in EXPECTED_STRUCTURE}
    if structure != EXPECTED_STRUCTURE or actual["shapes"] <= 0 or annotation_payload.get("tracks"):
        raise RuntimeError(
            f"refusing incomplete snapshot: metrics={actual!r}, tracks={len(annotation_payload.get('tracks', []))}"
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    backup_path = args.output_dir / BACKUP_NAME
    label_archive_path = args.output_dir / LABEL_ARCHIVE_NAME
    manifest_path = args.output_dir / MANIFEST_NAME
    download_backup(session, origin, task_id, backup_path)
    label_files, label_rows = archive_labels(
        args.labels_dir, label_archive_path, actual["shapes"]
    )

    manifest = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "task": {"id": task_id, "name": TASK_NAME, **actual},
        "backup": {
            "filename": backup_path.name,
            "bytes": backup_path.stat().st_size,
            "sha256": sha256(backup_path),
        },
        "labels": {
            "filename": label_archive_path.name,
            "files": label_files,
            "rows": label_rows,
            "bytes": label_archive_path.stat().st_size,
            "sha256": sha256(label_archive_path),
        },
    }
    temporary_manifest = manifest_path.with_suffix(".json.part")
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary_manifest, manifest_path)
    print(json.dumps({"status": "PASS", **manifest}, ensure_ascii=False, indent=2))

    password = None
    session.auth = None
    session.close()


if __name__ == "__main__":
    main()
