from __future__ import annotations

import csv
import json
import re
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {"images": 200, "classes": 20, "instances": 1534, "jobs": 4}
FORBIDDEN_PATTERNS = {
    "password literal": re.compile("1234" + "5678"),
    "private email": re.compile("209215" + r"8247\s*@\s*qq\.com", re.I),
    "lab filesystem": re.compile("/" + r"HUBU-[A-Z0-9_-]+", re.I),
    "private server alias": re.compile(r"\b(?:server" + r"2|ai028" + r"-1)\b", re.I),
    "dotenv assignment": re.compile(r"^(?:CVAT_HOST|SMOKESCREEN_OPTS|CVAT_PASS(?:WORD)?)=", re.M),
}


class LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        for key in ("href", "src"):
            value = values.get(key)
            if value and not value.startswith(("#", "http://", "https://", "mailto:", "data:")):
                self.paths.append(value.split("#", 1)[0].split("?", 1)[0])


def main() -> None:
    failures: list[str] = []
    report = json.loads((ROOT / "data" / "VERIFICATION_REPORT.json").read_text(encoding="utf-8"))
    distribution = list(csv.DictReader((ROOT / "data" / "class_distribution.csv").open(encoding="utf-8")))
    selection = list(csv.DictReader((ROOT / "data" / "selection_manifest.csv").open(encoding="utf-8")))

    actual = {
        "images": report["task"]["size"],
        "classes": report["task"]["labels"],
        "instances": report["task"]["instances"],
        "jobs": len(report["jobs"]),
    }
    if actual != EXPECTED:
        failures.append(f"metric mismatch: {actual!r}")
    if report.get("status") != "PASS":
        failures.append("verification report is not PASS")
    if len(distribution) != EXPECTED["classes"]:
        failures.append(f"class rows: {len(distribution)}")
    if sum(int(row["selected_instances"]) for row in distribution) != EXPECTED["instances"]:
        failures.append("class instance total mismatch")
    if len(selection) != EXPECTED["images"]:
        failures.append(f"selection rows: {len(selection)}")

    index = (ROOT / "index.html").read_text(encoding="utf-8")
    collector = LinkCollector()
    collector.feed(index)
    for relative in collector.paths:
        target = ROOT / relative
        if relative.endswith("/"):
            if not target.is_dir():
                failures.append(f"missing directory link: {relative}")
        elif not target.is_file():
            failures.append(f"missing link asset: {relative}")

    excluded_parts = {".git", ".runtime", "__pycache__", ".venv", "venv"}
    public_files = [
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not excluded_parts.intersection(path.parts)
    ]
    if len(list((ROOT / "assets").glob("*.jpg"))) != 7:
        failures.append("expected exactly 7 public QA images")
    if len(list((ROOT / "data" / "sample-labels").glob("*.txt"))) != 6:
        failures.append("expected exactly 6 public sample labels")

    recovery_files = {
        "recovery/versions.env",
        "recovery/Dockerfile.recovery-client",
        "recovery/docker-compose.sync.yml",
        "recovery/verify_backup.py",
        "recovery/import_and_verify.py",
        "recovery/export_snapshot.py",
        "recovery/download-preferred-backup.sh",
        "recovery/bootstrap-and-restore.sh",
        "recovery/restore.ps1",
        "recovery/checkpoint-and-publish.sh",
        "recovery/checkpoint-and-publish.ps1",
        "recovery/FRESH_MACHINE.md",
        "recovery/CHECKPOINT_NOTES.md",
        "recovery/README.md",
        "recovery/SHA256SUMS.txt",
        ".github/workflows/recovery-smoke-test.yml",
    }
    for relative in recovery_files:
        if not (ROOT / relative).is_file():
            failures.append(f"missing recovery file: {relative}")
    if "task25-recovery-v1" not in index:
        failures.append("recovery release is not linked from the portfolio")

    for path in public_files:
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in FORBIDDEN_PATTERNS.items():
            if pattern.search(text):
                failures.append(f"{label} found in {path.relative_to(ROOT)}")

    result = {
        "status": "PASS" if not failures else "FAIL",
        "scope": "CVAT Task #25 only",
        "metrics": actual,
        "checked_files": len(public_files),
        "relative_links_checked": len(collector.paths),
        "public_qa_images": 7,
        "public_sample_labels": 6,
        "failures": failures,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
