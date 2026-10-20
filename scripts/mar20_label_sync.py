#!/usr/bin/env python3
"""Continuously mirror one CVAT task into YOLO-OBB label files.

The service runs inside the local CVAT server image and reads annotations through
CVAT's Django model layer. No password, session cookie, PAT, or API token is used.
Only the dedicated labels volume and sync-state volume are writable.
"""

import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cvat.settings.production")

import django  # noqa: E402

django.setup()

from cvat.apps.dataset_manager.task import TaskAnnotation  # noqa: E402
from cvat.apps.engine.models import Task  # noqa: E402
from django.db import close_old_connections, transaction  # noqa: E402


TASK_NAME = os.environ.get("MAR20_TASK_NAME", "MAR20_Portfolio_OBB_Review_200")
LABEL_DIR = Path(os.environ.get("MAR20_LABEL_DIR", "/dataset/labels"))
STATE_FILE = Path(os.environ.get("MAR20_STATE_FILE", "/state/sync_state.json"))
POLL_SECONDS = float(os.environ.get("MAR20_POLL_SECONDS", "3"))

CLASS_NAMES = [
    "A1_SU-35", "A2_C-130", "A3_C-17", "A4_C-5", "A5_F-16",
    "A6_TU-160", "A7_E-3", "A8_B-52", "A9_P-3C", "A10_B-1B",
    "A11_E-8", "A12_TU-22", "A13_F-15", "A14_KC-135", "A15_F-22",
    "A16_FA-18", "A17_TU-95", "A18_KC-10", "A19_SU-34", "A20_SU-24",
]
NAME_TO_CLASS = {name: index for index, name in enumerate(CLASS_NAMES)}


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.cvat_tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))


def rotate_point(cx, cy, dx, dy, angle_radians):
    cosine = math.cos(angle_radians)
    sine = math.sin(angle_radians)
    return (
        cx + dx * cosine - dy * sine,
        cy + dx * sine + dy * cosine,
    )


def shape_signature(shape):
    return (
        int(shape.get("id", -1)),
        int(shape["label_id"]),
        str(shape["type"]),
        round(float(shape.get("rotation") or 0), 6),
        tuple(round(float(value), 5) for value in (shape.get("points") or [])),
    )


def frame_digest(shapes):
    payload = json.dumps(
        sorted(shape_signature(shape) for shape in shapes),
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def shapes_to_yolo(frame, shapes, label_id_to_class):
    width = int(frame["width"])
    height = int(frame["height"])
    output = []

    for shape in sorted(shapes, key=lambda item: int(item.get("id", -1))):
        if str(shape["type"]) != "rectangle":
            raise RuntimeError(
                f"{frame['name']}: unsupported shape type {shape['type']!r}"
            )

        label_id = int(shape["label_id"])
        if label_id not in label_id_to_class:
            raise RuntimeError(f"{frame['name']}: unknown label_id={label_id}")

        points = [float(value) for value in shape["points"]]
        if len(points) != 4:
            raise RuntimeError(f"{frame['name']}: rectangle points must contain 4 values")

        xtl, ytl, xbr, ybr = points
        box_width = xbr - xtl
        box_height = ybr - ytl
        if box_width <= 0 or box_height <= 0:
            raise RuntimeError(f"{frame['name']}: non-positive rectangle dimensions")

        cx = (xtl + xbr) / 2.0
        cy = (ytl + ybr) / 2.0
        angle = math.radians(float(shape.get("rotation") or 0))
        offsets = (
            (-box_width / 2, -box_height / 2),
            (box_width / 2, -box_height / 2),
            (box_width / 2, box_height / 2),
            (-box_width / 2, box_height / 2),
        )

        coordinates = []
        for dx, dy in offsets:
            x, y = rotate_point(cx, cy, dx, dy, angle)
            tolerance = 1e-4
            if not (-tolerance <= x <= width + tolerance):
                raise RuntimeError(f"{frame['name']}: x={x} is outside [0,{width}]")
            if not (-tolerance <= y <= height + tolerance):
                raise RuntimeError(f"{frame['name']}: y={y} is outside [0,{height}]")
            coordinates.extend((min(1.0, max(0.0, x / width)), min(1.0, max(0.0, y / height))))

        output.append(
            f"{label_id_to_class[label_id]} "
            + " ".join(f"{value:.8f}" for value in coordinates)
        )

    return "\n".join(output) + ("\n" if output else "")


def read_snapshot():
    close_old_connections()
    tasks = list(Task.objects.filter(name=TASK_NAME).order_by("id"))
    if not tasks:
        return None
    if len(tasks) != 1:
        raise RuntimeError(f"Expected exactly one task named {TASK_NAME!r}, found {len(tasks)}")

    task = tasks[0]
    labels = list(task.get_labels().values_list("id", "name"))
    found_names = {name for _, name in labels}
    if found_names != set(CLASS_NAMES):
        raise RuntimeError(
            f"Task labels mismatch. missing={sorted(set(CLASS_NAMES)-found_names)} "
            f"extra={sorted(found_names-set(CLASS_NAMES))}"
        )
    label_id_to_class = {int(label_id): NAME_TO_CLASS[name] for label_id, name in labels}

    frames = [
        {
            "frame": int(frame),
            "name": Path(path).name,
            "width": int(width),
            "height": int(height),
        }
        for frame, path, width, height in task.data.images.order_by("frame").values_list(
            "frame", "path", "width", "height"
        )
    ]
    if len(frames) != 200:
        raise RuntimeError(f"Task must contain 200 frames, found {len(frames)}")

    # CVAT v2.72 takes row locks while materializing task annotations, so the
    # native model API requires an explicit transaction even for a read-only
    # snapshot. Copy the data while the transaction is active, then release it
    # before converting/writing any label files.
    with transaction.atomic():
        annotation = TaskAnnotation(task.id)
        annotation.init_from_db(streaming=False)
        data = annotation.data
    tracks = list(data.get("tracks") or [])
    if tracks:
        raise RuntimeError("Tracks are not supported by the MAR20 live label sync")

    grouped = [[] for _ in frames]
    for shape in list(data.get("shapes") or []):
        frame_id = int(shape["frame"])
        if not 0 <= frame_id < len(frames):
            raise RuntimeError(f"Shape references invalid frame {frame_id}")
        grouped[frame_id].append(shape)

    return task.id, frames, grouped, label_id_to_class


def load_state(task_id, frame_count):
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        digests = payload["digests"]
        if payload["task_id"] == task_id and len(digests) == frame_count:
            return digests
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return None


def save_state(task_id, digests, shape_count):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": task_id,
        "task_name": TASK_NAME,
        "frame_count": len(digests),
        "shape_count": shape_count,
        "digests": digests,
        "updated_at_unix": time.time(),
    }
    atomic_write(STATE_FILE, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def main():
    LABEL_DIR.mkdir(parents=True, exist_ok=True)
    previous = None
    active_task_id = None
    print(f"[START] waiting for task {TASK_NAME!r}; poll={POLL_SECONDS}s", flush=True)

    while True:
        try:
            snapshot = read_snapshot()
            if snapshot is None:
                print(f"[WAIT] task {TASK_NAME!r} does not exist yet", flush=True)
                time.sleep(POLL_SECONDS)
                continue

            task_id, frames, grouped, label_map = snapshot
            if active_task_id != task_id:
                active_task_id = task_id
                previous = load_state(task_id, len(frames))
                print(f"[TASK] id={task_id} frames={len(frames)}", flush=True)

            current = [frame_digest(shapes) for shapes in grouped]
            changed = list(range(len(frames))) if previous is None else [
                index for index, digest in enumerate(current) if digest != previous[index]
            ]

            for frame_id in changed:
                frame = frames[frame_id]
                label_path = LABEL_DIR / f"{Path(frame['name']).stem}.txt"
                atomic_write(label_path, shapes_to_yolo(frame, grouped[frame_id], label_map))
                print(
                    f"[SYNC] frame={frame_id} file={label_path.name} "
                    f"objects={len(grouped[frame_id])}",
                    flush=True,
                )

            if changed:
                previous = current
                save_state(task_id, current, sum(len(items) for items in grouped))
                print(
                    f"[READY] changed={len(changed)} labels={len(current)} "
                    f"shapes={sum(len(items) for items in grouped)}",
                    flush=True,
                )

        except KeyboardInterrupt:
            return
        except Exception as exc:
            print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            close_old_connections()

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
