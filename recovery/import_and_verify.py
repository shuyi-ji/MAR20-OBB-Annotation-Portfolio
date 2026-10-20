#!/usr/bin/env python3
"""Idempotently restore Task #25, then verify the live CVAT result."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import time
import zipfile
from pathlib import Path

import requests


TASK_NAME = "MAR20_Portfolio_OBB_Review_200"
EXPECTED_STRUCTURE = {"frames": 200, "labels": 20, "jobs": 4}


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


def wait_for_request(
    session: requests.Session,
    origin: str,
    rq_id: str,
    *,
    timeout: int = 1800,
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = session.get(f"{origin}/api/requests/{rq_id}", timeout=120)
        require_success(response, f"GET request {rq_id}")
        payload = response.json()
        status = str(payload.get("status", "")).lower()
        message = payload.get("message") or ""
        print(f"[RESTORE] request={rq_id} status={status} message={message}", flush=True)

        if status == "finished":
            result_id = payload.get("result_id")
            if result_id is None:
                raise RuntimeError(f"finished request has no result_id: {payload!r}")
            return int(result_id)
        if status == "failed":
            raise RuntimeError(f"backup import failed: {message or payload!r}")

        time.sleep(2)

    raise TimeoutError(f"backup import did not finish within {timeout} seconds")


def import_backup(session: requests.Session, origin: str, backup: Path) -> int:
    with backup.open("rb") as input_file:
        response = session.post(
            f"{origin}/api/tasks/backup",
            files={"task_file": (backup.name, input_file, "application/zip")},
            timeout=(120, 1800),
        )
    require_success(response, "POST task backup import")
    rq_id = response.json().get("rq_id")
    if not rq_id:
        raise RuntimeError(f"backup import response has no rq_id: {response.text}")
    return wait_for_request(session, origin, str(rq_id))


def expected_metrics(backup: Path) -> dict[str, int]:
    with zipfile.ZipFile(backup) as archive:
        annotations = json.loads(archive.read("annotations.json"))
    shapes = sum(len(job.get("shapes", [])) for job in annotations)
    tracks = sum(len(job.get("tracks", [])) for job in annotations)
    if tracks:
        raise RuntimeError(f"backup unexpectedly contains {tracks} tracks")
    if shapes <= 0:
        raise RuntimeError("backup unexpectedly contains no shapes")
    return {**EXPECTED_STRUCTURE, "shapes": shapes}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--host", default=os.environ.get("CVAT_HOST", "http://cvat_server:8080"))
    parser.add_argument("--host-header", default="localhost")
    parser.add_argument("--username", default=os.environ.get("CVAT_USERNAME", "jishuyi"))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    expected = expected_metrics(args.backup)

    # CI may provide a short-lived, masked password through the process
    # environment. Interactive recovery always falls back to getpass.
    password = os.environ.pop("CVAT_PASSWORD", None)
    if password is None:
        password = getpass.getpass(f"CVAT password for {args.username}: ")
    if not password:
        raise SystemExit("empty password")

    origin = args.host.rstrip("/")
    session = requests.Session()
    session.auth = (args.username, password)
    session.headers["Host"] = args.host_header

    self_response = session.get(f"{origin}/api/users/self", timeout=120)
    self_response.raise_for_status()
    existing = [
        item
        for item in fetch_all(session, f"{origin}/api/tasks", page_size=100)
        if item.get("name") == TASK_NAME
    ]
    if len(existing) > 1:
        raise RuntimeError(f"multiple tasks named {TASK_NAME!r}; refusing ambiguous restore")

    restored_now = False
    if existing:
        task_id = int(existing[0]["id"])
        print(f"[RESTORE] existing task reused: id={task_id}", flush=True)
    else:
        task_id = import_backup(session, origin, args.backup)
        restored_now = True
        print(f"[RESTORE] task imported: id={task_id}", flush=True)

    task = session.get(f"{origin}/api/tasks/{task_id}", timeout=120)
    task.raise_for_status()
    task_payload = task.json()
    meta = session.get(f"{origin}/api/tasks/{task_id}/data/meta", timeout=120)
    meta.raise_for_status()
    annotations = session.get(f"{origin}/api/tasks/{task_id}/annotations", timeout=120)
    annotations.raise_for_status()
    labels = fetch_all(session, f"{origin}/api/labels", task_id=task_id, page_size=100)
    jobs = fetch_all(session, f"{origin}/api/jobs", task_id=task_id, page_size=100)

    shapes = annotations.json().get("shapes", [])
    tracks = annotations.json().get("tracks", [])
    actual = {
        "frames": len(meta.json().get("frames", [])),
        "labels": len(labels),
        "jobs": len(jobs),
        "shapes": len(shapes),
    }
    failures: list[str] = []
    if task_payload.get("name") != TASK_NAME:
        failures.append(f"task name: {task_payload.get('name')!r}")
    if int(task_payload.get("size", -1)) != expected["frames"]:
        failures.append(f"task size: {task_payload.get('size')!r}")
    if actual != expected:
        failures.append(f"metrics: {actual!r}")
    if tracks:
        failures.append(f"unexpected tracks: {len(tracks)}")

    for frame in (0, expected["frames"] - 1):
        response = session.get(
            f"{origin}/api/tasks/{task_id}/data",
            params={"type": "frame", "quality": "original", "number": frame},
            timeout=180,
        )
        response.raise_for_status()
        if len(response.content) < 1024:
            failures.append(f"frame {frame} returned only {len(response.content)} bytes")

    report = {
        "status": "PASS" if not failures else "FAIL",
        "restored_now": restored_now,
        "task_id": task_id,
        "task_name": task_payload.get("name"),
        "metrics": actual,
        "expected_metrics": expected,
        "first_and_last_frames_readable": not any("frame " in item for item in failures),
        "failures": failures,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    password = None
    session.auth = None
    session.close()
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
