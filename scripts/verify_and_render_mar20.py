#!/usr/bin/env python3
"""Verify the full OBB round trip and render a deterministic 10-image QA sheet."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageDraw


COLORS = [
    "#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231",
    "#911eb4", "#46f0f0", "#f032e6", "#bcf60c", "#fabebe",
    "#008080", "#e6beff", "#9a6324", "#fffac8", "#800000",
    "#aaffc3", "#808000", "#ffd8b1", "#000075", "#808080",
]


def read_labels(path: Path) -> list[tuple[int, list[tuple[float, float]]]]:
    result = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        fields = line.split()
        if len(fields) != 9:
            raise RuntimeError(f"{path.name}:{number}: expected 9 fields, got {len(fields)}")
        class_id = int(fields[0])
        values = [float(value) for value in fields[1:]]
        result.append((class_id, list(zip(values[0::2], values[1::2]))))
    return result


def equivalent_orders(points: list[tuple[float, float]]):
    for direction in (points, list(reversed(points))):
        for offset in range(4):
            yield direction[offset:] + direction[:offset]


def pixel_error(
    official: list[tuple[float, float]],
    current: list[tuple[float, float]],
    width: int,
    height: int,
) -> float:
    return min(
        max(
            math.hypot((left[0] - right[0]) * width, (left[1] - right[1]) * height)
            for left, right in zip(official, ordered)
        )
        for ordered in equivalent_orders(current)
    )


def verify_file(official_path: Path, current_path: Path, width: int, height: int) -> tuple[int, float]:
    official = read_labels(official_path)
    current = read_labels(current_path)
    if len(official) != len(current):
        raise RuntimeError(f"{official_path.name}: count {len(official)} != {len(current)}")

    official_by_class: dict[int, list[list[tuple[float, float]]]] = defaultdict(list)
    current_by_class: dict[int, list[list[tuple[float, float]]]] = defaultdict(list)
    for class_id, points in official:
        official_by_class[class_id].append(points)
    for class_id, points in current:
        current_by_class[class_id].append(points)
    if {key: len(value) for key, value in official_by_class.items()} != {
        key: len(value) for key, value in current_by_class.items()
    }:
        raise RuntimeError(f"{official_path.name}: per-class counts differ")

    maximum = 0.0
    for class_id, official_shapes in official_by_class.items():
        remaining = list(current_by_class[class_id])
        for official_points in official_shapes:
            candidates = [
                (pixel_error(official_points, points, width, height), index)
                for index, points in enumerate(remaining)
            ]
            error, index = min(candidates)
            maximum = max(maximum, error)
            remaining.pop(index)
    return len(official), maximum


def render_overlay(image_path: Path, label_path: Path, destination: Path) -> None:
    image = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    for class_id, normalized in read_labels(label_path):
        points = [(x * width, y * height) for x, y in normalized]
        draw.line(points + [points[0]], fill=COLORS[class_id], width=3, joint="curve")
        x, y = points[0]
        text = f"A{class_id + 1}"
        draw.rectangle((x, max(0, y - 14), x + 8 + 7 * len(text), y), fill="#000000")
        draw.text((x + 3, max(0, y - 13)), text, fill=COLORS[class_id])
    image.save(destination, quality=94)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--images", default="/images")
    parser.add_argument("--official", default="/official")
    parser.add_argument("--current", default="/current")
    parser.add_argument("--manifest", default="/metadata/selection_manifest.csv")
    parser.add_argument("--output", default="/output")
    args = parser.parse_args()

    images = Path(args.images)
    official = Path(args.official)
    current = Path(args.current)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    with Path(args.manifest).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != 200:
        raise RuntimeError(f"manifest contains {len(rows)} rows instead of 200")

    expected_names = {Path(row["filename"]).stem + ".txt" for row in rows}
    official_names = {path.name for path in official.glob("*.txt")}
    current_names = {path.name for path in current.glob("*.txt")}
    if expected_names != official_names or expected_names != current_names:
        raise RuntimeError("official/current label file sets do not match the selection manifest")

    instance_count = 0
    maximum_error = 0.0
    for row in rows:
        image_path = images / row["filename"]
        with Image.open(image_path) as image:
            width, height = image.size
        label_name = image_path.stem + ".txt"
        count, error = verify_file(official / label_name, current / label_name, width, height)
        instance_count += count
        maximum_error = max(maximum_error, error)
    if instance_count != 1534:
        raise RuntimeError(f"instance total is {instance_count}, expected 1534")
    if maximum_error > 1.0:
        raise RuntimeError(f"maximum round-trip point error is {maximum_error:.6f}px (>1px)")

    overlay_paths = []
    for row in rows[:10]:
        image_path = images / row["filename"]
        destination = output / f"frame_{int(row['frame']):03d}_{image_path.stem}_overlay.jpg"
        render_overlay(image_path, current / (image_path.stem + ".txt"), destination)
        overlay_paths.append(destination)

    tile_width, tile_height = 300, 320
    sheet = Image.new("RGB", (tile_width * 5, tile_height * 2), "white")
    draw = ImageDraw.Draw(sheet)
    for index, path in enumerate(overlay_paths):
        image = Image.open(path).convert("RGB")
        image.thumbnail((tile_width, tile_height - 20), Image.Resampling.LANCZOS)
        left = (index % 5) * tile_width + (tile_width - image.width) // 2
        top = (index // 5) * tile_height + 20
        sheet.paste(image, (left, top))
        draw.text(((index % 5) * tile_width + 5, (index // 5) * tile_height + 3), path.stem, fill="black")
    sheet_path = output / "MAR20_visual_QA_10_images.jpg"
    sheet.save(sheet_path, quality=92)

    report = {
        "status": "PASS",
        "images_compared": 200,
        "instances_compared": instance_count,
        "maximum_roundtrip_point_error_px": maximum_error,
        "acceptance_threshold_px": 1.0,
        "visual_qa_images": [path.name for path in overlay_paths],
        "visual_qa_sheet": sheet_path.name,
    }
    (output / "geometry_verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
