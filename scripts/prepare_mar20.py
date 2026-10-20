#!/usr/bin/env python3
"""Prepare a reproducible 200-image MAR20 portfolio subset.

The author-hosted Baidu and OneDrive links are the authoritative source for the
license, citation, and dataset counts. Because both official transfer endpoints
currently reject automated downloads, the payload is transferred from two
version-pinned public mirrors. The 1.2 GB image ZIP is never downloaded as a
whole: only its central directory and the selected members are fetched with
HTTP Range requests. The script fails closed before producing a READY marker if
any count, split, class, coordinate, or OBB reconstruction check fails.
"""

import argparse
import csv
import hashlib
import io
import json
import math
import os
import struct
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath

import requests


OFFICIAL_PAGE = "https://gcheng-nwpu.github.io/#Datasets"
OFFICIAL_SHARE = "https://pan.baidu.com/s/1VpQGGoSVTdFCtROVnH4s3A?pwd=wye2"
SHARE_SURL = "VpQGGoSVTdFCtROVnH4s3A"
SHARE_PASSWORD = "wye2"
SEED = 20260927

EXPECTED_IMAGES = 3842
EXPECTED_INSTANCES = 22341
EXPECTED_TRAIN = 1331
EXPECTED_TEST = 2511
SELECTION_SIZE = 200
NORMALIZED_PARALLELOGRAM_TOLERANCE = 1e-5
NORMALIZED_ORTHOGONALITY_TOLERANCE = 1e-4

IMAGE_MIRROR_PAGE = "https://www.kaggle.com/datasets/jsxyhelu2019/mar20-1220"
IMAGE_MIRROR_URL = (
    "https://www.kaggle.com/api/v1/datasets/download/"
    "jsxyhelu2019/mar20-1220?datasetVersionNumber=1"
)
IMAGE_MIRROR_DATASET_ID = 2748524
IMAGE_MIRROR_VERSION = 1
IMAGE_MIRROR_SIZE = 1203804831

LABEL_MIRROR_PAGE = "https://www.kaggle.com/datasets/jsxyhelu2019/labels-mar20-obb"
LABEL_MIRROR_URL = (
    "https://www.kaggle.com/api/v1/datasets/download/"
    "jsxyhelu2019/labels-mar20-obb?datasetVersionNumber=1"
)
LABEL_MIRROR_DATASET_ID = 5985528
LABEL_MIRROR_VERSION = 1
LABEL_MIRROR_SIZE = 1214185
LABEL_MIRROR_SHA256 = "dca88f63506da5ccd8a07575fef7d684ebfaca64df9c33d7c368f94cda35f8c1"

ARCHIVES = {
    "Annotations.zip": {"fs_id": 563006781475179, "size": 5254812},
    "ImageSets.zip": {"fs_id": 34232113673096, "size": 9062},
    "JPEGImages.zip": {"fs_id": 602870997006778, "size": 1202024389},
}

CLASS_NAMES = {
    "A1": "A1_SU-35",
    "A2": "A2_C-130",
    "A3": "A3_C-17",
    "A4": "A4_C-5",
    "A5": "A5_F-16",
    "A6": "A6_TU-160",
    "A7": "A7_E-3",
    "A8": "A8_B-52",
    "A9": "A9_P-3C",
    "A10": "A10_B-1B",
    "A11": "A11_E-8",
    "A12": "A12_TU-22",
    "A13": "A13_F-15",
    "A14": "A14_KC-135",
    "A15": "A15_F-22",
    "A16": "A16_FA-18",
    "A17": "A17_TU-95",
    "A18": "A18_KC-10",
    "A19": "A19_SU-34",
    "A20": "A20_SU-24",
}

COLORS = [
    "#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231",
    "#911eb4", "#46f0f0", "#f032e6", "#bcf60c", "#fabebe",
    "#008080", "#e6beff", "#9a6324", "#fffac8", "#800000",
    "#aaffc3", "#808000", "#ffd8b1", "#000075", "#808080",
]


def atomic_write(path, data, binary=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    mode = "wb" if binary else "w"
    kwargs = {} if binary else {"encoding": "utf-8", "newline": "\n"}
    with temporary.open(mode, **kwargs) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def stable_rank(value):
    return hashlib.sha256(f"{SEED}:{value}".encode("utf-8")).hexdigest()


class BaiduPublicShare:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Referer": OFFICIAL_SHARE,
        })
        self.share_id = None
        self.share_uk = None
        self.files = {}
        self.verify()

    def verify(self):
        response = self.session.post(
            "https://pan.baidu.com/share/verify",
            params={
                "channel": "chunlei", "web": 1, "app_id": 250528,
                "clienttype": 0, "surl": SHARE_SURL,
            },
            data={"pwd": SHARE_PASSWORD, "vcode": "", "vcode_str": ""},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errno") != 0 or "BDCLND" not in self.session.cookies:
            raise RuntimeError(f"Official share verification failed: {payload}")

        listing = self.session.get(
            "https://pan.baidu.com/share/list",
            params={
                "uk": 3675151875, "shareid": 11349532332,
                "order": "name", "desc": 0, "showempty": 0,
                "web": 1, "page": 1, "num": 100, "dir": "/MAR20",
                "channel": "chunlei", "app_id": 250528, "clienttype": 0,
            },
            timeout=30,
        )
        listing.raise_for_status()
        payload = listing.json()
        if payload.get("errno") != 0:
            raise RuntimeError(f"Official share listing failed: {payload}")

        self.share_id = int(payload["share_id"])
        self.share_uk = int(payload["uk"])
        self.files = {item["server_filename"]: item for item in payload["list"]}
        if set(self.files) != set(ARCHIVES):
            raise RuntimeError(f"Unexpected official archive list: {sorted(self.files)}")
        for name, expected in ARCHIVES.items():
            item = self.files[name]
            if int(item["fs_id"]) != expected["fs_id"] or int(item["size"]) != expected["size"]:
                raise RuntimeError(f"Official archive metadata changed for {name}: {item}")

    def download_link(self, name):
        if name not in self.files:
            raise KeyError(name)
        tpl = self.session.get(
            "https://pan.baidu.com/share/tplconfig",
            params={
                "fields": "sign,timestamp", "channel": "chunlei", "web": 1,
                "app_id": 250528, "clienttype": 0, "surl": "1" + SHARE_SURL,
            },
            timeout=30,
        )
        tpl.raise_for_status()
        tpl_payload = tpl.json()
        if tpl_payload.get("errno") != 0:
            raise RuntimeError(f"Could not obtain official download signature: {tpl_payload}")

        sekey = urllib.parse.unquote(self.session.cookies["BDCLND"])
        response = self.session.post(
            "https://pan.baidu.com/api/sharedownload",
            params={
                "app_id": 250528, "channel": "chunlei", "clienttype": 0,
                "web": 1, "bdstoken": "null",
                "sign": tpl_payload["data"]["sign"],
                "timestamp": tpl_payload["data"]["timestamp"],
            },
            data={
                "encrypt": 0,
                "extra": json.dumps({"sekey": sekey}, separators=(",", ":")),
                "fid_list": json.dumps([int(self.files[name]["fs_id"])]),
                "primaryid": self.share_id,
                "uk": self.share_uk,
                "product": "share",
                "type": "nolimit",
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errno") != 0 or not payload.get("list"):
            raise RuntimeError(f"Could not obtain download link for {name}: {payload}")
        return payload["list"][0]["dlink"]

    def download_small(self, name, destination):
        destination = Path(destination)
        expected_size = ARCHIVES[name]["size"]
        if destination.exists():
            actual_size = destination.stat().st_size
            if actual_size == expected_size:
                return destination
            raise RuntimeError(
                f"Existing {destination} has {actual_size} bytes; expected {expected_size}. "
                "It was not overwritten."
            )

        response = self.session.get(
            self.download_link(name),
            headers={"User-Agent": "netdisk;P2SP;2.2.60.26"},
            stream=True,
            timeout=90,
        )
        response.raise_for_status()
        temporary = destination.with_name(f".{destination.name}.partial")
        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with temporary.open("wb") as stream:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    stream.write(chunk)
                    written += len(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if written != expected_size:
            raise RuntimeError(f"Downloaded {name} has {written} bytes; expected {expected_size}")
        os.replace(str(temporary), str(destination))
        return destination


class HTTPRangeReader(io.RawIOBase):
    def __init__(self, session, link_provider, expected_size):
        super().__init__()
        self.session = session
        self.link_provider = link_provider
        self.expected_size = int(expected_size)
        self.position = 0
        self.link = self.link_provider()

        probe = self._get_range(0, 0, retry=True)
        if len(probe) != 1:
            raise RuntimeError("Range probe returned an unexpected payload")

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=os.SEEK_SET):
        if whence == os.SEEK_SET:
            position = offset
        elif whence == os.SEEK_CUR:
            position = self.position + offset
        elif whence == os.SEEK_END:
            position = self.expected_size + offset
        else:
            raise ValueError(f"Unsupported whence={whence}")
        if not 0 <= position <= self.expected_size:
            raise ValueError(f"Seek position out of range: {position}")
        self.position = position
        return position

    def read(self, size=-1):
        if self.position >= self.expected_size:
            return b""
        if size is None or size < 0:
            size = self.expected_size - self.position
        size = min(size, self.expected_size - self.position)
        if size == 0:
            return b""
        start = self.position
        end = start + size - 1
        data = self._get_range(start, end, retry=True)
        self.position += len(data)
        return data

    def _get_range(self, start, end, retry):
        response = None
        for attempt in range(5):
            try:
                response = self.session.get(
                    self.link,
                    headers={
                        "Range": f"bytes={start}-{end}",
                        "User-Agent": "netdisk;P2SP;2.2.60.26",
                    },
                    stream=True,
                    timeout=120,
                )
                break
            except requests.RequestException:
                if attempt == 4:
                    raise
                self.link = self.link_provider()
                time.sleep(min(2 ** attempt, 8))
        if response is None:
            raise RuntimeError("Range request did not produce a response")
        if response.status_code in (401, 403) and retry:
            response.close()
            self.link = self.link_provider()
            return self._get_range(start, end, retry=False)
        if response.status_code != 206:
            response.close()
            raise RuntimeError(
                f"Remote ZIP server ignored Range bytes={start}-{end}; "
                f"HTTP {response.status_code}. Full archive download was refused."
            )
        content_range = response.headers.get("Content-Range", "")
        expected_prefix = f"bytes {start}-{end}/"
        if not content_range.startswith(expected_prefix):
            response.close()
            raise RuntimeError(f"Unexpected Content-Range: {content_range!r}")
        total = int(content_range.rsplit("/", 1)[1])
        if total != self.expected_size:
            response.close()
            raise RuntimeError(f"Remote ZIP size changed: {total} != {self.expected_size}")
        self.link = response.url
        data = response.content
        response.close()
        if len(data) != end - start + 1:
            raise RuntimeError(f"Short range read: got {len(data)} bytes")
        return data


def fetch_zip_member_with_single_range(url, expected_size, info):
    start = info.header_offset
    end = min(
        expected_size - 1,
        start + 30 + len(info.filename.encode("utf-8")) + len(info.extra)
        + info.compress_size + 65535,
    )
    response = None
    for attempt in range(5):
        try:
            response = requests.get(
                url,
                headers={
                    "Range": f"bytes={start}-{end}",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=120,
            )
            if response.status_code == 206:
                break
            response.close()
            response = None
        except requests.RequestException:
            response = None
        if attempt == 4:
            raise RuntimeError(f"Could not fetch ZIP member {info.filename}")
        time.sleep(min(2 ** attempt, 8))
    if response is None:
        raise RuntimeError(f"No response for ZIP member {info.filename}")
    content_range = response.headers.get("Content-Range", "")
    if not content_range.startswith(f"bytes {start}-{end}/"):
        response.close()
        raise RuntimeError(f"Unexpected Content-Range for {info.filename}: {content_range}")
    if int(content_range.rsplit("/", 1)[1]) != expected_size:
        response.close()
        raise RuntimeError(f"Archive size changed while fetching {info.filename}")
    block = response.content
    response.close()
    if len(block) < 30 or block[:4] != b"PK\x03\x04":
        raise RuntimeError(f"Invalid local ZIP header for {info.filename}")
    filename_length, extra_length = struct.unpack_from("<HH", block, 26)
    compressed_start = 30 + filename_length + extra_length
    compressed_end = compressed_start + info.compress_size
    if compressed_end > len(block):
        raise RuntimeError(f"Range did not contain all compressed bytes for {info.filename}")
    compressed = block[compressed_start:compressed_end]
    if info.compress_type == zipfile.ZIP_STORED:
        data = compressed
    elif info.compress_type == zipfile.ZIP_DEFLATED:
        data = zlib.decompress(compressed, -15)
    else:
        raise RuntimeError(
            f"Unsupported ZIP compression {info.compress_type} for {info.filename}"
        )
    if len(data) != info.file_size:
        raise RuntimeError(
            f"Uncompressed size mismatch for {info.filename}: "
            f"{len(data)} != {info.file_size}"
        )
    if zlib.crc32(data) & 0xFFFFFFFF != info.CRC:
        raise RuntimeError(f"CRC mismatch for {info.filename}")
    return data


def parse_split(archive, basename):
    matches = [name for name in archive.namelist() if PurePosixPath(name).name == basename]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {basename}, found {matches}")
    text = archive.read(matches[0]).decode("utf-8-sig")
    return [Path(line.strip()).stem for line in text.splitlines() if line.strip()]


def parse_number(node, field):
    child = node.find(field)
    if child is None or child.text is None:
        raise RuntimeError(f"Missing XML field {field}")
    return float(child.text)


def parse_annotation(xml_bytes, member_name):
    root = ET.fromstring(xml_bytes)
    filename = (root.findtext("filename") or f"{Path(member_name).stem}.jpg").strip()
    width = int(float(root.findtext("size/width")))
    height = int(float(root.findtext("size/height")))
    if width <= 0 or height <= 0:
        raise RuntimeError(f"Invalid image size in {member_name}: {width}x{height}")

    objects = []
    for obj in root.findall("object"):
        class_code = (obj.findtext("name") or "").strip()
        if class_code not in CLASS_NAMES:
            raise RuntimeError(f"Unknown class {class_code!r} in {member_name}")
        box = obj.find("robndbox")
        if box is None:
            raise RuntimeError(f"Missing robndbox in {member_name}")
        points = [
            (parse_number(box, "x_left_top"), parse_number(box, "y_left_top")),
            (parse_number(box, "x_right_top"), parse_number(box, "y_right_top")),
            (parse_number(box, "x_right_bottom"), parse_number(box, "y_right_bottom")),
            (parse_number(box, "x_left_bottom"), parse_number(box, "y_left_bottom")),
        ]
        for x, y in points:
            if not (0 <= x <= width and 0 <= y <= height):
                raise RuntimeError(
                    f"Point {(x, y)} is outside {width}x{height} in {member_name}"
                )
        objects.append({"class_code": class_code, "points": points})

    return {
        "filename": filename,
        "width": width,
        "height": height,
        "objects": objects,
        "xml_member": member_name,
    }


def distance(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def obb_to_cvat(points):
    p0, p1, p2, p3 = points
    cx = sum(point[0] for point in points) / 4.0
    cy = sum(point[1] for point in points) / 4.0
    width = distance(p0, p1)
    height = distance(p0, p3)
    if width <= 0 or height <= 0:
        raise RuntimeError("OBB has non-positive dimensions")
    angle = math.degrees(math.atan2(p1[1] - p0[1], p1[0] - p0[0])) % 360.0

    radians = math.radians(angle)
    cosine, sine = math.cos(radians), math.sin(radians)
    width_edge = (p1[0] - p0[0], p1[1] - p0[1])
    height_edge = (p3[0] - p0[0], p3[1] - p0[1])
    winding_sign = 1.0 if (
        width_edge[0] * height_edge[1] - width_edge[1] * height_edge[0]
    ) >= 0 else -1.0
    rebuilt = []
    for dx, dy in (
        (-width / 2, -height / 2),
        (width / 2, -height / 2),
        (width / 2, height / 2),
        (-width / 2, height / 2),
    ):
        dy *= winding_sign
        rebuilt.append((cx + dx * cosine - dy * sine, cy + dx * sine + dy * cosine))

    max_error = max(distance(original, reconstruction) for original, reconstruction in zip(points, rebuilt))
    if max_error > 1.0:
        raise RuntimeError(f"OBB reconstruction error {max_error:.6f}px exceeds 1px")

    return {
        "xtl": cx - width / 2,
        "ytl": cy - height / 2,
        "xbr": cx + width / 2,
        "ybr": cy + height / 2,
        "rotation": angle,
        "max_error": max_error,
        "rebuilt": rebuilt,
    }


def choose_images(train_ids, annotations):
    classes_by_image = {
        image_id: sorted({item["class_code"] for item in annotations[image_id]["objects"]})
        for image_id in train_ids
    }
    by_class = defaultdict(list)
    for image_id, classes in classes_by_image.items():
        for class_code in classes:
            by_class[class_code].append(image_id)

    if set(by_class) != set(CLASS_NAMES):
        raise RuntimeError(f"Train split class coverage mismatch: {sorted(by_class)}")

    selected = set()
    minimums = {code: min(5, len(by_class[code])) for code in CLASS_NAMES}
    for class_code in sorted(CLASS_NAMES, key=lambda code: (len(by_class[code]), code)):
        candidates = sorted(by_class[class_code], key=lambda image_id: stable_rank(f"minimum:{class_code}:{image_id}"))
        current = sum(image_id in selected for image_id in candidates)
        for image_id in candidates:
            if current >= minimums[class_code]:
                break
            if image_id not in selected:
                selected.add(image_id)
                current += 1

    remaining_quota = SELECTION_SIZE - sum(minimums.values())
    total_availability = sum(len(by_class[code]) for code in CLASS_NAMES)
    raw_additions = {
        code: remaining_quota * len(by_class[code]) / total_availability for code in CLASS_NAMES
    }
    additions = {code: int(math.floor(raw_additions[code])) for code in CLASS_NAMES}
    left = remaining_quota - sum(additions.values())
    for code in sorted(
        CLASS_NAMES,
        key=lambda item: (-(raw_additions[item] - additions[item]), item),
    )[:left]:
        additions[code] += 1
    targets = {code: minimums[code] + additions[code] for code in CLASS_NAMES}

    coverage = Counter()
    for image_id in selected:
        coverage.update(classes_by_image[image_id])

    candidates = set(train_ids) - selected
    while len(selected) < SELECTION_SIZE:
        if not candidates:
            raise RuntimeError("Not enough unique train images for the requested selection")

        def score(image_id):
            deficit = sum(
                max(0, targets[code] - coverage[code]) / len(by_class[code])
                for code in classes_by_image[image_id]
            )
            return (-deficit, stable_rank(f"fill:{image_id}"), image_id)

        image_id = min(candidates, key=score)
        candidates.remove(image_id)
        selected.add(image_id)
        coverage.update(classes_by_image[image_id])

    for code, minimum in minimums.items():
        if coverage[code] < minimum:
            raise RuntimeError(f"Selection contains only {coverage[code]} images of {code}")

    return sorted(selected, key=lambda image_id: (stable_rank(f"order:{image_id}"), image_id)), targets


def write_cvat_xml(path, selected_ids, annotations):
    root = ET.Element("annotations")
    ET.SubElement(root, "version").text = "1.1"
    for frame_id, image_id in enumerate(selected_ids):
        annotation = annotations[image_id]
        image = ET.SubElement(
            root,
            "image",
            {
                "id": str(frame_id),
                "name": f"mar20_portfolio/images/{annotation['filename']}",
                "width": str(annotation["width"]),
                "height": str(annotation["height"]),
            },
        )
        for item in annotation["objects"]:
            rectangle = obb_to_cvat(item["points"])
            ET.SubElement(
                image,
                "box",
                {
                    "label": CLASS_NAMES[item["class_code"]],
                    "source": "auto",
                    "occluded": "0",
                    "xtl": f"{rectangle['xtl']:.6f}",
                    "ytl": f"{rectangle['ytl']:.6f}",
                    "xbr": f"{rectangle['xbr']:.6f}",
                    "ybr": f"{rectangle['ybr']:.6f}",
                    "z_order": "0",
                    "rotation": f"{rectangle['rotation']:.6f}",
                },
            )
    tree = ET.ElementTree(root)
    if hasattr(ET, "indent"):
        ET.indent(tree, space="  ")
    buffer = io.BytesIO()
    tree.write(buffer, encoding="utf-8", xml_declaration=True)
    atomic_write(path, buffer.getvalue(), binary=True)


def yolo_text(annotation):
    lines = []
    for item in annotation["objects"]:
        class_id = int(item["class_code"][1:]) - 1
        coords = []
        for x, y in item["points"]:
            coords.extend((x / annotation["width"], y / annotation["height"]))
        lines.append(f"{class_id} " + " ".join(f"{value:.8f}" for value in coords))
    return "\n".join(lines) + ("\n" if lines else "")


def download_versioned_archive(url, destination, expected_size, expected_sha256):
    destination = Path(destination)
    if destination.exists():
        size = destination.stat().st_size
        digest = sha256_bytes(destination.read_bytes()) if size == expected_size else None
        if size == expected_size and digest == expected_sha256:
            return destination
        raise RuntimeError(
            f"Existing {destination} failed the pinned archive check: "
            f"size={size}, sha256={digest}. It was not overwritten."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial")
    if temporary.exists():
        raise RuntimeError(f"Stale partial download exists and was not overwritten: {temporary}")
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        stream=True,
        timeout=120,
        allow_redirects=True,
    )
    response.raise_for_status()
    written = 0
    digest = hashlib.sha256()
    with temporary.open("wb") as stream:
        for chunk in response.iter_content(1024 * 1024):
            if chunk:
                stream.write(chunk)
                digest.update(chunk)
                written += len(chunk)
        stream.flush()
        os.fsync(stream.fileno())
    if written != expected_size or digest.hexdigest() != expected_sha256:
        raise RuntimeError(
            f"Pinned archive mismatch: size={written}, sha256={digest.hexdigest()}"
        )
    os.replace(str(temporary), str(destination))
    return destination


def parse_yolo_obb(text, member_name):
    objects = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 9:
            raise RuntimeError(
                f"{member_name}:{line_number} has {len(fields)} fields; expected 9"
            )
        try:
            class_index = int(fields[0])
            values = [float(value) for value in fields[1:]]
        except ValueError as error:
            raise RuntimeError(f"Invalid numeric label in {member_name}:{line_number}") from error
        if not 0 <= class_index < len(CLASS_NAMES):
            raise RuntimeError(f"Unknown class index {class_index} in {member_name}:{line_number}")
        if any(not math.isfinite(value) for value in values):
            raise RuntimeError(f"Non-finite OBB point in {member_name}:{line_number}")
        points_normalized = list(zip(values[0::2], values[1::2]))
        if len(set(points_normalized)) != 4:
            raise RuntimeError(f"Degenerate OBB in {member_name}:{line_number}")
        parallelogram_residual = max(
            abs(
                points_normalized[0][axis] + points_normalized[2][axis]
                - points_normalized[1][axis] - points_normalized[3][axis]
            )
            for axis in (0, 1)
        )
        edge_a = (
            points_normalized[1][0] - points_normalized[0][0],
            points_normalized[1][1] - points_normalized[0][1],
        )
        edge_b = (
            points_normalized[3][0] - points_normalized[0][0],
            points_normalized[3][1] - points_normalized[0][1],
        )
        edge_norm_product = math.hypot(*edge_a) * math.hypot(*edge_b)
        if edge_norm_product == 0:
            raise RuntimeError(f"Degenerate OBB edge in {member_name}:{line_number}")
        orthogonality_cosine = abs(
            edge_a[0] * edge_b[0] + edge_a[1] * edge_b[1]
        ) / edge_norm_product
        objects.append({
            "class_code": f"A{class_index + 1}",
            "points_normalized": points_normalized,
            "in_bounds": all(0 <= value <= 1 for value in values),
            "parallelogram_residual": parallelogram_residual,
            "orthogonality_cosine": orthogonality_cosine,
        })
    return objects


def jpeg_dimensions(data):
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise RuntimeError("Image is not a JPEG stream")
    offset = 2
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data):
            raise RuntimeError("Malformed JPEG marker length")
        if marker in sof_markers:
            if length < 7:
                raise RuntimeError("Malformed JPEG SOF marker")
            height = int.from_bytes(data[offset + 3:offset + 5], "big")
            width = int.from_bytes(data[offset + 5:offset + 7], "big")
            if width <= 0 or height <= 0:
                raise RuntimeError(f"Invalid JPEG dimensions: {width}x{height}")
            return width, height
        offset += length
    raise RuntimeError("JPEG dimensions were not found")


def image_dimensions_and_extension(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise RuntimeError("Malformed PNG stream")
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        if width <= 0 or height <= 0:
            raise RuntimeError(f"Invalid PNG dimensions: {width}x{height}")
        return width, height, ".png"
    width, height = jpeg_dimensions(data)
    return width, height, ".jpg"


def materialize_annotation_geometry(annotation, width, height):
    annotation["width"] = width
    annotation["height"] = height
    for item in annotation["objects"]:
        points = [
            (x * width, y * height)
            for x, y in item["points_normalized"]
        ]
        for x, y in points:
            if not (0 <= x <= width and 0 <= y <= height):
                raise RuntimeError(
                    f"Point {(x, y)} is outside {width}x{height} in {annotation['label_member']}"
                )
        rectangle = obb_to_cvat(points)
        rebuilt = rectangle["rebuilt"]
        if any(not (0 <= x <= width and 0 <= y <= height) for x, y in rebuilt):
            raise RuntimeError(
                f"Reconstructed OBB leaves the image in {annotation['label_member']}"
            )
        item["points"] = points


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", default="/home/uuu/mar20_portfolio_work")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()

    work_dir = Path(args.work_dir)
    cache_dir = work_dir / "cache"
    output_dir = work_dir / "prepared"
    images_dir = output_dir / "images"
    metadata_dir = output_dir / "metadata"
    initial_labels_dir = metadata_dir / "official_yolo_obb"
    cache_dir.mkdir(parents=True, exist_ok=True)

    share = BaiduPublicShare()
    annotations_zip = share.download_small("Annotations.zip", cache_dir / "Annotations.zip")
    image_sets_zip = share.download_small("ImageSets.zip", cache_dir / "ImageSets.zip")

    with zipfile.ZipFile(image_sets_zip) as archive:
        train_ids = parse_split(archive, "train.txt")
        test_ids = parse_split(archive, "test.txt")
    if len(train_ids) != EXPECTED_TRAIN or len(test_ids) != EXPECTED_TEST:
        raise RuntimeError(f"Official split counts changed: train={len(train_ids)} test={len(test_ids)}")
    if set(train_ids) & set(test_ids) or len(set(train_ids) | set(test_ids)) != EXPECTED_IMAGES:
        raise RuntimeError("Official train/test splits are not disjoint and complete")

    annotations = {}
    total_instances = 0
    out_of_bounds_instances = 0
    non_rectangular_instances = 0
    non_orthogonal_instances = 0
    archive_hashes = {
        "Annotations.zip": sha256_bytes(annotations_zip.read_bytes()),
        "ImageSets.zip": sha256_bytes(image_sets_zip.read_bytes()),
    }
    with zipfile.ZipFile(annotations_zip) as archive:
        oriented_members = [
            name for name in archive.namelist()
            if name.lower().endswith(".xml") and "oriented bounding boxes" in name.lower()
        ]
        if len(oriented_members) != EXPECTED_IMAGES:
            raise RuntimeError(f"Expected {EXPECTED_IMAGES} OBB XML files, found {len(oriented_members)}")
        for member in oriented_members:
            image_id = Path(member).stem
            if image_id in annotations:
                raise RuntimeError(f"Duplicate OBB XML for {image_id}")
            annotations[image_id] = parse_annotation(archive.read(member), member)
            total_instances += len(annotations[image_id]["objects"])
    if total_instances != EXPECTED_INSTANCES:
        raise RuntimeError(f"Expected {EXPECTED_INSTANCES} OBB instances, found {total_instances}")
    if set(annotations) != set(train_ids) | set(test_ids):
        raise RuntimeError("OBB XML set does not match official train/test image set")

    selected_ids, targets = choose_images(train_ids, annotations)
    if len(selected_ids) != SELECTION_SIZE or len(set(selected_ids)) != SELECTION_SIZE:
        raise RuntimeError("Selection is not exactly 200 unique images")

    reader = HTTPRangeReader(
        share.session,
        lambda: share.download_link("JPEGImages.zip"),
        ARCHIVES["JPEGImages.zip"]["size"],
    )
    image_hashes = {}
    with zipfile.ZipFile(reader) as image_archive:
        image_members = {
            Path(name).name: name
            for name in image_archive.namelist()
            if name.lower().endswith((".jpg", ".jpeg", ".png"))
        }
        if len(image_members) != EXPECTED_IMAGES:
            raise RuntimeError(f"Expected {EXPECTED_IMAGES} image members, found {len(image_members)}")
        expected_filenames = {annotations[image_id]["filename"] for image_id in selected_ids}
        missing = expected_filenames - set(image_members)
        if missing:
            raise RuntimeError(f"Selected images missing from official ZIP: {sorted(missing)[:10]}")

        extract_ids = selected_ids[:1] if args.preflight_only else selected_ids
        destination_root = work_dir / "preflight" if args.preflight_only else images_dir
        destination_root.mkdir(parents=True, exist_ok=True)
        for index, image_id in enumerate(extract_ids, 1):
            filename = annotations[image_id]["filename"]
            data = image_archive.read(image_members[filename])
            if not data.startswith(b"\xff\xd8\xff"):
                raise RuntimeError(f"{filename} is not a valid JPEG stream")
            destination = destination_root / filename
            if destination.exists() and destination.read_bytes() != data:
                raise RuntimeError(f"Existing image differs and was not overwritten: {destination}")
            if not destination.exists():
                atomic_write(destination, data, binary=True)
            image_hashes[filename] = sha256_bytes(data)
            print(f"[IMAGE] {index}/{len(extract_ids)} {filename}", flush=True)

    if args.preflight_only:
        first = selected_ids[0]
        errors = [obb_to_cvat(item["points"])["max_error"] for item in annotations[first]["objects"]]
        print(json.dumps({
            "preflight": "PASS",
            "image_id": first,
            "objects": len(errors),
            "max_reconstruction_error_px": max(errors, default=0),
            "official_images": EXPECTED_IMAGES,
            "official_instances": total_instances,
            "selected_images": len(selected_ids),
        }, ensure_ascii=False, indent=2))
        return

    actual_files = {path.name for path in images_dir.iterdir() if path.is_file()}
    expected_files = {annotations[image_id]["filename"] for image_id in selected_ids}
    if actual_files != expected_files:
        raise RuntimeError(
            f"Prepared image directory mismatch. extra={sorted(actual_files-expected_files)[:5]} "
            f"missing={sorted(expected_files-actual_files)[:5]}"
        )

    metadata_dir.mkdir(parents=True, exist_ok=True)
    initial_labels_dir.mkdir(parents=True, exist_ok=True)
    write_cvat_xml(metadata_dir / "cvat_annotations.xml", selected_ids, annotations)
    for image_id in selected_ids:
        atomic_write(initial_labels_dir / f"{Path(annotations[image_id]['filename']).stem}.txt", yolo_text(annotations[image_id]))

    with (metadata_dir / "selection_manifest.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "frame", "image_id", "filename", "split", "width", "height",
            "classes", "instances", "sha256", "source_member",
        ])
        for frame, image_id in enumerate(selected_ids):
            item = annotations[image_id]
            classes = sorted({obj["class_code"] for obj in item["objects"]})
            writer.writerow([
                frame, image_id, item["filename"], "train", item["width"], item["height"],
                ";".join(classes), len(item["objects"]), image_hashes[item["filename"]],
                f"JPEGImages/{item['filename']}",
            ])

    with (metadata_dir / "class_distribution.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "class_code", "label_name", "train_images", "train_instances",
            "target_images", "selected_images", "selected_instances",
        ])
        for code in CLASS_NAMES:
            train_images = sum(code in {o["class_code"] for o in annotations[i]["objects"]} for i in train_ids)
            selected_images = sum(code in {o["class_code"] for o in annotations[i]["objects"]} for i in selected_ids)
            train_instances = sum(sum(o["class_code"] == code for o in annotations[i]["objects"]) for i in train_ids)
            selected_instances = sum(sum(o["class_code"] == code for o in annotations[i]["objects"]) for i in selected_ids)
            writer.writerow([
                code, CLASS_NAMES[code], train_images, train_instances, targets[code],
                selected_images, selected_instances,
            ])

    max_error = 0.0
    selected_instances = 0
    for image_id in selected_ids:
        for obj in annotations[image_id]["objects"]:
            max_error = max(max_error, obb_to_cvat(obj["points"])["max_error"])
            selected_instances += 1

    source = {
        "dataset": "MAR20",
        "official_page": OFFICIAL_PAGE,
        "official_download": OFFICIAL_SHARE,
        "license": "CC BY-NC 4.0 (official author page; non-commercial portfolio use)",
        "citation": (
            "Wenqi Yu, Gong Cheng, Meijun Wang, Yanqing Yao, Xingxing Xie, "
            "Xiwen Yao, Junwei Han. MAR20: A Benchmark for Military Aircraft "
            "Recognition in Remote Sensing Images. Journal of Remote Sensing (Chinese), 2022."
        ),
        "official_counts": {
            "images": EXPECTED_IMAGES, "classes": 20, "instances": EXPECTED_INSTANCES,
            "train_images": EXPECTED_TRAIN, "test_images": EXPECTED_TEST,
        },
        "selection": {
            "seed": SEED, "split": "train", "images": len(selected_ids),
            "instances": selected_instances,
            "minimum_images_per_class": 5,
            "algorithm": "deterministic multi-label deficit-balanced stratified sampling",
        },
        "archive_sha256": archive_hashes,
        "image_zip": {
            "filename": "JPEGImages.zip",
            "official_size": ARCHIVES["JPEGImages.zip"]["size"],
            "transfer": "HTTP Range: central directory plus selected members only",
        },
        "maximum_obb_reconstruction_error_px": max_error,
        "created_at_unix": time.time(),
    }
    atomic_write(metadata_dir / "source_and_reproducibility.json", json.dumps(source, ensure_ascii=False, indent=2) + "\n")
    atomic_write(metadata_dir / "class_names.json", json.dumps(CLASS_NAMES, ensure_ascii=False, indent=2) + "\n")
    ready = {
        "status": "READY",
        "images": len(selected_ids),
        "labels": 20,
        "instances": selected_instances,
        "max_reconstruction_error_px": max_error,
    }
    atomic_write(output_dir / "READY.json", json.dumps(ready, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(ready, ensure_ascii=False, indent=2))


def main_mirror():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", default="/home/uuu/mar20_portfolio_work")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()

    work_dir = Path(args.work_dir)
    cache_dir = work_dir / "cache"
    output_dir = work_dir / "prepared"
    images_dir = output_dir / "images"
    metadata_dir = output_dir / "metadata"
    initial_labels_dir = metadata_dir / "official_yolo_obb"
    cache_dir.mkdir(parents=True, exist_ok=True)

    label_zip_path = download_versioned_archive(
        LABEL_MIRROR_URL,
        cache_dir / "kaggle_labels_mar20_obb_v1.zip",
        LABEL_MIRROR_SIZE,
        LABEL_MIRROR_SHA256,
    )

    annotations = {}
    split_ids = {"train": [], "val": []}
    total_instances = 0
    out_of_bounds_instances = 0
    non_rectangular_instances = 0
    non_orthogonal_instances = 0
    class_instances = Counter()
    with zipfile.ZipFile(label_zip_path) as label_archive:
        label_members = sorted(
            name for name in label_archive.namelist()
            if name.lower().endswith(".txt")
        )
        if len(label_members) != EXPECTED_IMAGES:
            raise RuntimeError(
                f"Expected {EXPECTED_IMAGES} OBB label files, found {len(label_members)}"
            )
        for member in label_members:
            parts = PurePosixPath(member).parts
            if len(parts) != 2 or parts[0] not in split_ids:
                raise RuntimeError(f"Unexpected OBB label member: {member}")
            split = parts[0]
            image_id = Path(member).stem
            if image_id in annotations:
                raise RuntimeError(f"Duplicate OBB label for image {image_id}")
            objects = parse_yolo_obb(label_archive.read(member).decode("utf-8"), member)
            annotations[image_id] = {
                "filename": f"{image_id}.jpg",
                "objects": objects,
                "label_member": member,
                "split": "train" if split == "train" else "test",
                "width": None,
                "height": None,
            }
            split_ids[split].append(image_id)
            total_instances += len(objects)
            out_of_bounds_instances += sum(not item["in_bounds"] for item in objects)
            non_rectangular_instances += sum(
                item["parallelogram_residual"] > NORMALIZED_PARALLELOGRAM_TOLERANCE
                for item in objects
            )
            non_orthogonal_instances += sum(
                item["orthogonality_cosine"] > NORMALIZED_ORTHOGONALITY_TOLERANCE
                for item in objects
            )
            class_instances.update(item["class_code"] for item in objects)

    train_ids = sorted(split_ids["train"])
    test_ids = sorted(split_ids["val"])
    if len(train_ids) != EXPECTED_TRAIN or len(test_ids) != EXPECTED_TEST:
        raise RuntimeError(
            f"Pinned split counts changed: train={len(train_ids)} test={len(test_ids)}"
        )
    if set(train_ids) & set(test_ids) or len(annotations) != EXPECTED_IMAGES:
        raise RuntimeError("Pinned train/test splits are not disjoint and complete")
    if total_instances != EXPECTED_INSTANCES:
        raise RuntimeError(
            f"Expected {EXPECTED_INSTANCES} OBB instances, found {total_instances}"
        )
    if set(class_instances) != set(CLASS_NAMES) or any(
        class_instances[code] <= 0 for code in CLASS_NAMES
    ):
        raise RuntimeError(f"OBB class coverage mismatch: {dict(class_instances)}")

    excluded_out_of_bounds_ids = {
        image_id
        for image_id, annotation in annotations.items()
        if any(not item["in_bounds"] for item in annotation["objects"])
    }
    excluded_non_rectangular_ids = {
        image_id
        for image_id, annotation in annotations.items()
        if any(
            item["parallelogram_residual"] > NORMALIZED_PARALLELOGRAM_TOLERANCE
            for item in annotation["objects"]
        )
    }
    excluded_non_orthogonal_ids = {
        image_id
        for image_id, annotation in annotations.items()
        if any(
            item["orthogonality_cosine"] > NORMALIZED_ORTHOGONALITY_TOLERANCE
            for item in annotation["objects"]
        )
    }
    excluded_from_sampling_ids = (
        excluded_out_of_bounds_ids
        | excluded_non_rectangular_ids
        | excluded_non_orthogonal_ids
    )
    eligible_train_ids = [
        image_id for image_id in train_ids
        if image_id not in excluded_from_sampling_ids
    ]
    selected_ids, targets = choose_images(eligible_train_ids, annotations)
    if len(selected_ids) != SELECTION_SIZE or len(set(selected_ids)) != SELECTION_SIZE:
        raise RuntimeError("Selection is not exactly 200 unique images")

    image_session = requests.Session()
    image_session.headers.update({"User-Agent": "Mozilla/5.0"})
    reader = HTTPRangeReader(
        image_session,
        lambda: IMAGE_MIRROR_URL,
        IMAGE_MIRROR_SIZE,
    )
    image_hashes = {}
    image_members_for_selected = {}
    with zipfile.ZipFile(reader) as image_archive:
        image_members = {}
        image_infos = {}
        image_splits = {"train": set(), "val": set()}
        for info in image_archive.infolist():
            name = info.filename
            if not name.lower().endswith(".jpg"):
                continue
            parts = PurePosixPath(name).parts
            if len(parts) != 3 or parts[0] != "mar20" or parts[1] not in image_splits:
                raise RuntimeError(f"Unexpected image member: {name}")
            image_id = Path(name).stem
            if image_id in image_members:
                raise RuntimeError(f"Duplicate image member for {image_id}")
            image_members[image_id] = name
            image_infos[image_id] = info
            image_splits[parts[1]].add(image_id)

        if len(image_members) != EXPECTED_IMAGES:
            raise RuntimeError(
                f"Expected {EXPECTED_IMAGES} image members, found {len(image_members)}"
            )
        if image_splits["train"] != set(train_ids) or image_splits["val"] != set(test_ids):
            raise RuntimeError("Image mirror split membership does not match OBB labels")
        if set(image_members) != set(annotations):
            raise RuntimeError("Image and OBB label ID sets do not match")

        extract_ids = selected_ids[:1] if args.preflight_only else selected_ids
        destination_root = work_dir / "preflight" if args.preflight_only else images_dir
        destination_root.mkdir(parents=True, exist_ok=True)
        prefetched_data = {}
        if not args.preflight_only:
            uncached_ids = [
                image_id for image_id in extract_ids
                if not (destination_root / f"{image_id}.jpg").exists()
                and not (destination_root / f"{image_id}.png").exists()
            ]
            if uncached_ids:
                with ThreadPoolExecutor(max_workers=6) as executor:
                    futures = {
                        executor.submit(
                            fetch_zip_member_with_single_range,
                            reader.link,
                            IMAGE_MIRROR_SIZE,
                            image_infos[image_id],
                        ): image_id
                        for image_id in uncached_ids
                    }
                    for fetched, future in enumerate(as_completed(futures), 1):
                        image_id = futures[future]
                        prefetched_data[image_id] = future.result()
                        print(
                            f"[FETCH] {fetched}/{len(uncached_ids)} {image_id}",
                            flush=True,
                        )
        for index, image_id in enumerate(extract_ids, 1):
            member = image_members[image_id]
            cached_candidates = [
                path for path in (
                    destination_root / f"{image_id}.jpg",
                    destination_root / f"{image_id}.png",
                )
                if path.exists()
            ]
            if len(cached_candidates) > 1:
                raise RuntimeError(f"Ambiguous cached image files for {image_id}")
            data = (
                cached_candidates[0].read_bytes()
                if cached_candidates
                else prefetched_data.pop(image_id, None)
            )
            if data is None:
                data = image_archive.read(member)
            width, height, extension = image_dimensions_and_extension(data)
            annotation = annotations[image_id]
            annotation["filename"] = f"{image_id}{extension}"
            annotation["image_member"] = member
            materialize_annotation_geometry(annotation, width, height)
            destination = destination_root / annotation["filename"]
            if destination.exists() and destination.read_bytes() != data:
                raise RuntimeError(
                    f"Existing image differs and was not overwritten: {destination}"
                )
            if not destination.exists():
                atomic_write(destination, data, binary=True)
            image_hashes[annotation["filename"]] = sha256_bytes(data)
            image_members_for_selected[image_id] = member
            print(
                f"[IMAGE] {index}/{len(extract_ids)} {annotation['filename']} "
                f"{width}x{height}",
                flush=True,
            )

    if args.preflight_only:
        first = selected_ids[0]
        errors = [
            obb_to_cvat(item["points"])["max_error"]
            for item in annotations[first]["objects"]
        ]
        print(json.dumps({
            "preflight": "PASS",
            "image_id": first,
            "objects": len(errors),
            "max_reconstruction_error_px": max(errors, default=0),
            "dataset_images": len(annotations),
            "dataset_instances": total_instances,
            "train_images": len(train_ids),
            "test_images": len(test_ids),
            "selected_images": len(selected_ids),
            "excluded_out_of_bounds_images": len(excluded_out_of_bounds_ids),
            "excluded_out_of_bounds_instances": out_of_bounds_instances,
            "excluded_non_rectangular_images": len(excluded_non_rectangular_ids),
            "excluded_non_rectangular_instances": non_rectangular_instances,
            "excluded_non_orthogonal_images": len(excluded_non_orthogonal_ids),
            "excluded_non_orthogonal_instances": non_orthogonal_instances,
            "eligible_train_images": len(eligible_train_ids),
            "label_archive_sha256": LABEL_MIRROR_SHA256,
            "image_archive_size": IMAGE_MIRROR_SIZE,
        }, ensure_ascii=False, indent=2))
        return

    actual_files = {path.name for path in images_dir.iterdir() if path.is_file()}
    expected_files = {annotations[image_id]["filename"] for image_id in selected_ids}
    if actual_files != expected_files:
        raise RuntimeError(
            f"Prepared image directory mismatch. "
            f"extra={sorted(actual_files - expected_files)[:5]} "
            f"missing={sorted(expected_files - actual_files)[:5]}"
        )

    metadata_dir.mkdir(parents=True, exist_ok=True)
    initial_labels_dir.mkdir(parents=True, exist_ok=True)
    write_cvat_xml(metadata_dir / "cvat_annotations.xml", selected_ids, annotations)
    for image_id in selected_ids:
        atomic_write(
            initial_labels_dir / f"{image_id}.txt",
            yolo_text(annotations[image_id]),
        )

    with (metadata_dir / "selection_manifest.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "frame", "image_id", "filename", "split", "width", "height",
            "classes", "instances", "sha256", "source_member",
        ])
        for frame, image_id in enumerate(selected_ids):
            item = annotations[image_id]
            classes = sorted({obj["class_code"] for obj in item["objects"]})
            writer.writerow([
                frame, image_id, item["filename"], "train", item["width"],
                item["height"], ";".join(classes), len(item["objects"]),
                image_hashes[item["filename"]], image_members_for_selected[image_id],
            ])

    with (metadata_dir / "class_distribution.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "class_code", "label_name", "train_images", "train_instances",
            "target_images", "selected_images", "selected_instances",
        ])
        for code in CLASS_NAMES:
            train_images = sum(
                code in {obj["class_code"] for obj in annotations[image_id]["objects"]}
                for image_id in train_ids
            )
            selected_images = sum(
                code in {obj["class_code"] for obj in annotations[image_id]["objects"]}
                for image_id in selected_ids
            )
            train_instances = sum(
                sum(obj["class_code"] == code for obj in annotations[image_id]["objects"])
                for image_id in train_ids
            )
            selected_instances_for_class = sum(
                sum(obj["class_code"] == code for obj in annotations[image_id]["objects"])
                for image_id in selected_ids
            )
            writer.writerow([
                code, CLASS_NAMES[code], train_images, train_instances,
                targets[code], selected_images, selected_instances_for_class,
            ])

    max_error = 0.0
    selected_instances = 0
    for image_id in selected_ids:
        for obj in annotations[image_id]["objects"]:
            max_error = max(max_error, obb_to_cvat(obj["points"])["max_error"])
            selected_instances += 1

    source = {
        "dataset": "MAR20",
        "official_page": OFFICIAL_PAGE,
        "official_download": {
            "baidu": OFFICIAL_SHARE,
            "onedrive": (
                "https://1drv.ms/u/s!AmgKYzARBl5ceGUKiVsRzfxZa_4?e=K21Spg"
            ),
        },
        "license": "CC BY-NC 4.0 (official author page; non-commercial portfolio use)",
        "citation": (
            "Wenqi Yu, Gong Cheng, Meijun Wang, Yanqing Yao, Xingxing Xie, "
            "Xiwen Yao, Junwei Han. MAR20: A Benchmark for Military Aircraft "
            "Recognition in Remote Sensing Images. Journal of Remote Sensing "
            "(Chinese), 2022."
        ),
        "official_counts": {
            "images": EXPECTED_IMAGES,
            "classes": 20,
            "instances": EXPECTED_INSTANCES,
            "train_images": EXPECTED_TRAIN,
            "test_images": EXPECTED_TEST,
            "out_of_bounds_images_excluded_from_sampling": len(excluded_out_of_bounds_ids),
            "out_of_bounds_instances_excluded_from_sampling": out_of_bounds_instances,
            "non_rectangular_images_excluded_from_sampling": len(
                excluded_non_rectangular_ids
            ),
            "non_rectangular_instances_excluded_from_sampling": (
                non_rectangular_instances
            ),
            "non_orthogonal_images_excluded_from_sampling": len(
                excluded_non_orthogonal_ids
            ),
            "non_orthogonal_instances_excluded_from_sampling": (
                non_orthogonal_instances
            ),
        },
        "transfer_mirrors": {
            "images": {
                "page": IMAGE_MIRROR_PAGE,
                "kaggle_dataset_id": IMAGE_MIRROR_DATASET_ID,
                "version": IMAGE_MIRROR_VERSION,
                "archive_size": IMAGE_MIRROR_SIZE,
                "members": EXPECTED_IMAGES,
                "transfer": "HTTP Range: central directory plus selected members only",
            },
            "obb_labels": {
                "page": LABEL_MIRROR_PAGE,
                "kaggle_dataset_id": LABEL_MIRROR_DATASET_ID,
                "version": LABEL_MIRROR_VERSION,
                "archive_size": LABEL_MIRROR_SIZE,
                "archive_sha256": LABEL_MIRROR_SHA256,
                "members": EXPECTED_IMAGES,
                "instances": total_instances,
            },
        },
        "selection": {
            "seed": SEED,
            "split": "train",
            "images": len(selected_ids),
            "instances": selected_instances,
            "minimum_images_per_class": 5,
            "eligible_in_bounds_train_images": len(eligible_train_ids),
            "algorithm": "deterministic multi-label deficit-balanced stratified sampling",
        },
        "maximum_obb_reconstruction_error_px": max_error,
        "created_at_unix": time.time(),
    }
    atomic_write(
        metadata_dir / "source_and_reproducibility.json",
        json.dumps(source, ensure_ascii=False, indent=2) + "\n",
    )
    atomic_write(
        metadata_dir / "class_names.json",
        json.dumps(CLASS_NAMES, ensure_ascii=False, indent=2) + "\n",
    )
    ready = {
        "status": "READY",
        "images": len(selected_ids),
        "labels": 20,
        "instances": selected_instances,
        "max_reconstruction_error_px": max_error,
    }
    atomic_write(
        output_dir / "READY.json",
        json.dumps(ready, ensure_ascii=False, indent=2) + "\n",
    )
    print(json.dumps(ready, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main_mirror()
