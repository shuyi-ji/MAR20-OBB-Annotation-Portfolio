#!/usr/bin/env python3
"""Reversibly prove that a CVAT geometry edit reaches the YOLO-OBB volume."""

from __future__ import annotations

import argparse
import copy
import getpass
import hashlib
import time
from pathlib import Path

import requests


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wait_for(path: Path, expected: str, equal: bool, timeout: float = 20.0) -> float:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        current = digest(path)
        if (current == expected) == equal:
            return time.monotonic() - started
        time.sleep(0.25)
    relation = "equal" if equal else "different from"
    raise TimeoutError(f"{path.name} did not become {relation} the reference hash")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="http://cvat_server:8080")
    parser.add_argument("--username", default="jishuyi")
    parser.add_argument("--task-id", type=int, default=25)
    parser.add_argument("--labels-dir", default="/labels")
    args = parser.parse_args()

    password = getpass.getpass(f"CVAT password for {args.username}: ")
    if not password:
        raise SystemExit("empty password")

    session = requests.Session()
    session.auth = (args.username, password)
    session.headers["Host"] = "localhost"
    password = None
    base = args.host.rstrip("/") + "/api"

    def request(method: str, path: str, **kwargs):
        response = session.request(method, base + path, timeout=60, **kwargs)
        if not response.ok:
            raise RuntimeError(f"{method} {path}: {response.status_code} {response.text[:1000]}")
        return response

    request("GET", "/users/self")
    metadata = request("GET", f"/tasks/{args.task_id}/data/meta").json()
    annotations = request("GET", f"/tasks/{args.task_id}/annotations").json()

    candidate = None
    for shape in annotations.get("shapes", []):
        points = [float(value) for value in shape.get("points", [])]
        if shape.get("type") == "rectangle" and len(points) == 4:
            if points[2] - points[0] > 2 and points[3] - points[1] > 2:
                candidate = shape
                break
    if candidate is None:
        raise RuntimeError("no suitable rectangle found")

    frame_id = int(candidate["frame"])
    filename = Path(metadata["frames"][frame_id]["name"]).stem + ".txt"
    label_path = Path(args.labels_dir) / filename
    if not label_path.is_file():
        raise RuntimeError(f"label file is missing: {label_path}")

    original = copy.deepcopy(candidate)
    modified = copy.deepcopy(candidate)
    xtl, ytl, xbr, ybr = map(float, modified["points"])
    epsilon = 0.125
    modified["points"] = [xtl + epsilon, ytl + epsilon, xbr - epsilon, ybr - epsilon]

    original_hash = digest(label_path)
    payload_base = {"version": 0, "tags": [], "tracks": []}
    restored = False
    try:
        request(
            "PATCH",
            f"/tasks/{args.task_id}/annotations",
            params={"action": "update"},
            json={**payload_base, "shapes": [modified]},
        )
        change_seconds = wait_for(label_path, original_hash, equal=False)
    finally:
        request(
            "PATCH",
            f"/tasks/{args.task_id}/annotations",
            params={"action": "update"},
            json={**payload_base, "shapes": [original]},
        )
        restore_seconds = wait_for(label_path, original_hash, equal=True)
        restored = True

    final_annotations = request("GET", f"/tasks/{args.task_id}/annotations").json()
    final_shape = next(shape for shape in final_annotations["shapes"] if shape["id"] == original["id"])
    if [float(value) for value in final_shape["points"]] != [float(value) for value in original["points"]]:
        raise RuntimeError("CVAT geometry was not restored exactly")
    if not restored or digest(label_path) != original_hash:
        raise RuntimeError("label file hash was not restored")

    print(
        {
            "status": "PASS",
            "task_id": args.task_id,
            "shape_id": original["id"],
            "frame": frame_id,
            "label_file": filename,
            "change_detected_seconds": round(change_seconds, 3),
            "restore_detected_seconds": round(restore_seconds, 3),
            "original_hash_restored": True,
        }
    )


if __name__ == "__main__":
    main()
