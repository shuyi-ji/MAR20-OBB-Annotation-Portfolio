# Interactive CVAT recovery

This directory reconstructs the interactive Task #25 environment after the original computer,
Docker installation, Docker volumes, or local CVAT database are gone.

## What is preserved

- CVAT `v2.72.0`, pinned to official source commit
  `0445d0336f9bc537dd8227b5f9be69c9ad5d11b0`;
- an immutable baseline CVAT backup with all 200 images, 20 labels, four jobs and 1,534 shapes;
- a rolling latest checkpoint containing the current Task state and all 200 YOLO-OBB labels;
- SHA-256 integrity gate before any import;
- an idempotent import and post-restore API verifier;
- the live YOLO-OBB label synchronizer, writing to `.runtime/mar20-cvat/labels/`;
- no saved CVAT password, GitHub token, server address or laboratory path.

The large backups are GitHub Release assets rather than Git objects. This keeps Git history
small while preserving downloadable recovery artifacts. `task25-recovery-v1` is the immutable
baseline; `task25-checkpoint-latest` is replaced only when the checkpoint command succeeds.

## Requirements

- Docker Desktop with WSL integration, or Docker Engine plus Compose v2;
- Git, curl and Python 3 in the Linux/WSL shell;
- enough disk space for CVAT images and the 69.8 MB task backup.

## Windows + WSL restore

Clone the public repository, open PowerShell in it, and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\recovery\restore.ps1
```

The script prefers and verifies the newest published checkpoint, falling back to the immutable
baseline when no checkpoint exists. It starts only the CVAT services needed for
annotation, prompts for a local CVAT account, imports or reuses the task, verifies the restored
counts and starts live label synchronization.

## Linux/WSL restore

```bash
bash recovery/bootstrap-and-restore.sh
```

To restore from a previously downloaded backup instead of GitHub:

```bash
bash recovery/bootstrap-and-restore.sh /absolute/path/to/task_25_MAR20_Portfolio_OBB_Review_200.zip
```

## Acceptance gate

The script exits successfully only when all of the following hold:

- the baseline SHA-256 equals `7e42b43f213e4e97333b8e8971d4085533bb80edefa12e3987822bff23c730e6`,
  or the rolling checkpoint equals its manifest-recorded SHA-256 and byte count;
- the archive contains 200 local images, 20 labels and four jobs; the baseline has 1,534 shapes,
  while a later checkpoint records and verifies its current shape count;
- live CVAT returns the same counts after restoration;
- the first and last original frames can be fetched;
- the synchronizer writes exactly 200 YOLO-OBB label files.

The generated restore certificate is saved at
`.runtime/mar20-cvat/reports/restore_report.json`.

## Save work completed after restoration

Live YOLO files are durable across ordinary container restarts, but they are still local files.
After each annotation session, publish a complete new disaster-recovery checkpoint:

```powershell
powershell -ExecutionPolicy Bypass -File .\recovery\checkpoint-and-publish.ps1
```

The command asks for the CVAT password without saving it, exports a fresh native task backup,
compares the CVAT shape count with all synchronized label rows, verifies hashes, and then uploads
the three checkpoint assets through the already authenticated GitHub CLI. The GitHub token never
enters the CVAT or recovery containers. See [`FRESH_MACHINE.md`](FRESH_MACHINE.md) for the full
reinstallation procedure.

The same path is exercised on a disposable GitHub-hosted Linux runner by
`.github/workflows/recovery-smoke-test.yml`. The workflow downloads the public Release asset,
starts the pinned CVAT stack, performs a real import, verifies all 200 generated labels, exports a
fresh checkpoint and validates that backup against its manifest. It publishes the restore
certificate and checkpoint as short-lived workflow artifacts. Its administrator password is
generated per run, masked and discarded with the runner.

## Storage and license

The release asset contains a 200-image subset derived from the public MAR20 dataset and is for
non-commercial portfolio and reproducibility use under CC BY-NC 4.0. Cite the MAR20 paper and
retain the attribution in the repository root. The task backup contains no login credential.
