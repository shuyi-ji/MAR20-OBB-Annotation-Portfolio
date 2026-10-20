#!/usr/bin/env python3
"""Create and verify the MAR20 CVAT portfolio task through CVAT's REST API.

Credentials are prompted interactively and retained only in process memory.
Only a project/task created by this invocation can be rolled back.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import math
import os
import sys
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from PIL import Image


PROJECT = "Remote_Sensing_Annotation_Portfolio"
TASK = "MAR20_Portfolio_OBB_Review_200"
LABELS = [
    "A1_SU-35", "A2_C-130", "A3_C-17", "A4_C-5", "A5_F-16",
    "A6_TU-160", "A7_E-3", "A8_B-52", "A9_P-3C", "A10_B-1B",
    "A11_E-8", "A12_TU-22", "A13_F-15", "A14_KC-135", "A15_F-22",
    "A16_FA-18", "A17_TU-95", "A18_KC-10", "A19_SU-34", "A20_SU-24",
]
COLORS = [
    "#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231",
    "#911eb4", "#46f0f0", "#f032e6", "#bcf60c", "#fabebe",
    "#008080", "#e6beff", "#9a6324", "#fffac8", "#800000",
    "#aaffc3", "#808000", "#ffd8b1", "#000075", "#808080",
]


class CvatAPI:
    def __init__(self, host: str, username: str, password: str) -> None:
        self.origin = host.rstrip("/")
        self.base = self.origin + "/api"
        self.session = requests.Session()
        self.session.auth = (username, password)
        # The Docker service name is an internal route, not a public CVAT_HOST.
        # Preserve the existing deployment's host allow-list by presenting a
        # host name it already accepts instead of changing CVAT configuration.
        self.session.headers["Host"] = "localhost"

    def close(self) -> None:
        self.session.auth = None
        self.session.close()

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            source = urlsplit(path)
            target = urlsplit(self.origin)
            return urlunsplit((target.scheme, target.netloc, source.path, source.query, ""))
        if path.startswith("/api/"):
            return self.origin + path
        return self.base + (path if path.startswith("/") else "/" + path)

    def request(self, method: str, path: str, expected=(200,), **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", 120)
        response = self.session.request(method, self._url(path), **kwargs)
        if response.status_code not in set(expected):
            body = response.text[:2000]
            raise RuntimeError(
                f"{method} {path} returned {response.status_code}; expected {sorted(expected)}\n{body}"
            )
        return response

    def fetch_all(self, path: str, params: dict | None = None) -> list[dict]:
        results: list[dict] = []
        next_url: str | None = path
        first = True
        while next_url:
            response = self.request("GET", next_url, params=params if first else None).json()
            first = False
            if isinstance(response, list):
                results.extend(response)
                break
            results.extend(response.get("results", []))
            next_url = response.get("next")
        return results

    def wait_request(self, rq_id: str, description: str, timeout: int = 900) -> dict:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            data = self.request("GET", f"/requests/{rq_id}").json()
            status = str(data.get("status", "")).lower()
            progress = data.get("progress")
            marker = (status, progress)
            if marker != last:
                print(f"[{description}] status={status} progress={progress}", flush=True)
                last = marker
            if status == "finished":
                return data
            if status in {"failed", "canceled", "cancelled"}:
                raise RuntimeError(f"{description} failed: {data.get('message')}")
            time.sleep(2)
        raise TimeoutError(f"{description} did not finish within {timeout}s")

    def wait_task_data(self, task_id: int, timeout: int = 900) -> dict:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            data = self.request("GET", f"/tasks/{task_id}/status").json()
            state = str(data.get("state", ""))
            if state != last:
                print(f"[TASK DATA] state={state} message={data.get('message', '')}", flush=True)
                last = state
            if state.lower() == "finished":
                return data
            if state.lower() == "failed":
                raise RuntimeError(f"task data creation failed: {data.get('message')}")
            time.sleep(2)
        raise TimeoutError(f"task data creation did not finish within {timeout}s")


def rotated_corners(shape: dict) -> list[tuple[float, float]]:
    points = [float(value) for value in shape.get("points", [])]
    if len(points) != 4:
        raise RuntimeError(f"shape {shape.get('id')} has invalid rectangle points: {points}")
    xtl, ytl, xbr, ybr = points
    width, height = xbr - xtl, ybr - ytl
    if width <= 0 or height <= 0:
        raise RuntimeError(f"shape {shape.get('id')} has non-positive size")
    cx, cy = (xtl + xbr) / 2, (ytl + ybr) / 2
    angle = math.radians(float(shape.get("rotation") or 0))
    cosine, sine = math.cos(angle), math.sin(angle)
    return [
        (cx + dx * cosine - dy * sine, cy + dx * sine + dy * cosine)
        for dx, dy in (
            (-width / 2, -height / 2),
            (width / 2, -height / 2),
            (width / 2, height / 2),
            (-width / 2, height / 2),
        )
    ]


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="http://cvat_server:8080")
    parser.add_argument("--username", default="jishuyi")
    parser.add_argument("--work-dir", default="/work")
    args = parser.parse_args()

    work = Path(args.work_dir)
    prepared = work / "prepared"
    metadata = prepared / "metadata"
    gate = json.loads((prepared / "READY.json").read_text(encoding="utf-8"))
    required_gate = {"status": "READY", "images": 200, "labels": 20, "instances": 1534}
    for key, expected in required_gate.items():
        if gate.get(key) != expected:
            raise RuntimeError(f"preparation gate {key!r}: expected {expected!r}, got {gate.get(key)!r}")

    with (metadata / "selection_manifest.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != 200 or [int(row["frame"]) for row in rows] != list(range(200)):
        raise RuntimeError("selection manifest must contain frames 0..199 exactly once")
    if len({row["filename"] for row in rows}) != 200:
        raise RuntimeError("selection manifest filenames are not unique")

    annotation = metadata / "cvat_annotations.xml"
    if not annotation.is_file() or annotation.stat().st_size == 0:
        raise RuntimeError("CVAT annotation XML is missing or empty")
    resources = [f"mar20_portfolio/images/{row['filename']}" for row in rows]

    password = getpass.getpass(f"CVAT password for {args.username}: ")
    if not password:
        raise SystemExit("empty password")

    api = CvatAPI(args.host, args.username, password)
    password = None
    project_id: int | None = None
    task_id: int | None = None
    try:
        user = api.request("GET", "/users/self").json()
        print(f"[AUTH] {user.get('username')} (id={user.get('id')})", flush=True)

        projects = api.fetch_all("/projects", {"page_size": 100})
        tasks = api.fetch_all("/tasks", {"page_size": 100})
        if any(item.get("name") == PROJECT for item in projects):
            raise RuntimeError(f"project name already exists: {PROJECT}")
        if any(item.get("name") == TASK for item in tasks):
            raise RuntimeError(f"task name already exists: {TASK}")

        project_payload = {
            "name": PROJECT,
            "bug_tracker": "",
            "labels": [
                {"name": name, "type": "rectangle", "color": color, "attributes": []}
                for name, color in zip(LABELS, COLORS)
            ],
        }
        project = api.request("POST", "/projects", expected=(201,), json=project_payload).json()
        project_id = int(project["id"])
        print(f"[PROJECT] id={project_id}", flush=True)

        task_payload = {
            "name": TASK,
            "project_id": project_id,
            "segment_size": 50,
            "overlap": 0,
            "bug_tracker": "",
        }
        task = api.request("POST", "/tasks", expected=(201,), json=task_payload).json()
        task_id = int(task["id"])
        print(f"[TASK] id={task_id}", flush=True)

        data_payload: dict[str, str] = {
            "image_quality": "90",
            "sorting_method": "predefined",
            "copy_data": "false",
            "use_cache": "true",
            "use_zip_chunks": "true",
        }
        for index, resource in enumerate(resources):
            data_payload[f"server_files[{index}]"] = resource
        multipart_fields = [(key, (None, value)) for key, value in data_payload.items()]
        api.request(
            "POST",
            f"/tasks/{task_id}/data",
            expected=(202,),
            files=multipart_fields,
            timeout=180,
        )
        api.wait_task_data(task_id)

        with annotation.open("rb") as stream:
            response = api.request(
                "POST",
                f"/tasks/{task_id}/annotations",
                expected=(202,),
                params={"format": "CVAT 1.1", "import_mode": "replace"},
                files={"annotation_file": (annotation.name, stream, "application/xml")},
                timeout=180,
            )
        request_id = response.json().get("rq_id")
        if not request_id:
            raise RuntimeError("annotation import did not return rq_id")
        api.wait_request(request_id, "ANNOTATION IMPORT")

        task = api.request("GET", f"/tasks/{task_id}").json()
        frame_meta = api.request("GET", f"/tasks/{task_id}/data/meta").json()
        frames = frame_meta.get("frames", [])
        labels = api.fetch_all("/labels", {"task_id": task_id, "page_size": 100})
        jobs = api.fetch_all("/jobs", {"task_id": task_id, "page_size": 100})
        annotations = api.request("GET", f"/tasks/{task_id}/annotations").json()
        shapes = annotations.get("shapes", [])
        tracks = annotations.get("tracks", [])

        if int(task.get("size", -1)) != 200 or len(frames) != 200:
            raise RuntimeError(f"post-create frame count mismatch: task={task.get('size')} meta={len(frames)}")
        if len(labels) != 20 or [label.get("name") for label in labels] != LABELS:
            raise RuntimeError(f"post-create labels mismatch: {[label.get('name') for label in labels]}")
        if len(jobs) != 4:
            raise RuntimeError(f"post-create jobs mismatch: expected 4, got {len(jobs)}")
        if len(shapes) != 1534 or tracks:
            raise RuntimeError(f"post-create annotations mismatch: shapes={len(shapes)} tracks={len(tracks)}")

        for index, frame in enumerate(frames):
            if Path(frame.get("name", "")).name != rows[index]["filename"]:
                raise RuntimeError(f"frame order mismatch at {index}: {frame.get('name')}")

        for shape in shapes:
            if shape.get("type") != "rectangle":
                raise RuntimeError(f"non-rectangle shape after import: {shape.get('id')}")
            frame_index = int(shape.get("frame", -1))
            if not 0 <= frame_index < len(frames):
                raise RuntimeError(f"shape {shape.get('id')} references invalid frame {frame_index}")
            frame = frames[frame_index]
            width, height = int(frame["width"]), int(frame["height"])
            for x, y in rotated_corners(shape):
                if not (-1e-3 <= x <= width + 1e-3 and -1e-3 <= y <= height + 1e-3):
                    raise RuntimeError(f"shape {shape.get('id')} is outside frame bounds")

        for index, frame in enumerate(frames):
            response = api.request(
                "GET",
                f"/tasks/{task_id}/data",
                params={"type": "frame", "quality": "original", "number": index},
                timeout=180,
            )
            with Image.open(BytesIO(response.content)) as image:
                image.verify()
                if image.size != (int(frame["width"]), int(frame["height"])):
                    raise RuntimeError(f"frame {index} dimensions mismatch")
            if (index + 1) % 25 == 0:
                print(f"[FRAMES] verified {index + 1}/200", flush=True)

        backup_dir = work / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"task_{task_id}_MAR20_Portfolio_OBB_Review_200.zip"
        response = api.request(
            "POST", f"/tasks/{task_id}/backup/export", expected=(202,), json={}, timeout=180
        )
        request_id = response.json().get("rq_id")
        if not request_id:
            raise RuntimeError("backup export did not return rq_id")
        export_result = api.wait_request(request_id, "NATIVE BACKUP", timeout=1800)
        result_url = export_result.get("result_url")
        if not result_url:
            raise RuntimeError("backup export did not return result_url")
        download = api.request("GET", result_url, timeout=300)
        temporary_backup = backup.with_suffix(backup.suffix + ".part")
        temporary_backup.write_bytes(download.content)
        if temporary_backup.stat().st_size == 0:
            raise RuntimeError("downloaded task backup is empty")
        os.replace(temporary_backup, backup)

        result = {
            "status": "COMPLETE",
            "project_id": project_id,
            "project_name": PROJECT,
            "task_id": task_id,
            "task_name": TASK,
            "task_size": 200,
            "labels": 20,
            "jobs": 4,
            "job_ids": [int(job["id"]) for job in jobs],
            "shapes": len(shapes),
            "frames_verified": 200,
            "image_quality": 90,
            "segment_size": 50,
            "copy_data": False,
            "backup": str(backup),
            "backup_bytes": backup.stat().st_size,
        }
        atomic_json(metadata / "cvat_creation_result.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    except Exception:
        print("[ROLLBACK] removing only resources created by this invocation", file=sys.stderr)
        if task_id is not None:
            try:
                api.request("DELETE", f"/tasks/{task_id}", expected=(204,))
            except Exception as error:
                print(f"task cleanup failed: {error}", file=sys.stderr)
        if project_id is not None:
            try:
                api.request("DELETE", f"/projects/{project_id}", expected=(204,))
            except Exception as error:
                print(f"project cleanup failed: {error}", file=sys.stderr)
        raise
    finally:
        api.close()


if __name__ == "__main__":
    main()
