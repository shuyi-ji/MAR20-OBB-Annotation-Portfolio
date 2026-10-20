# Task #25 interactive recovery asset

This release contains the native CVAT backup for
`MAR20_Portfolio_OBB_Review_200`.

- 200 embedded images (no SSHFS, server mount or original Docker volume required)
- 20 MAR20 aircraft classes
- four jobs of 50 frames
- 1,534 OBB rectangle shapes
- expected SHA-256:
  `7e42b43f213e4e97333b8e8971d4085533bb80edefa12e3987822bff23c730e6`

Use `recovery/restore.ps1` on Windows + WSL or
`recovery/bootstrap-and-restore.sh` on Linux/WSL. Both paths verify the archive before import.

## Data license and attribution

The embedded 200-image subset is derived from the public MAR20 dataset and is released here
solely for non-commercial portfolio reproducibility under CC BY-NC 4.0.

Wenqi Yu, Gong Cheng, Meijun Wang, Yanqing Yao, Xingxing Xie, Xiwen Yao, and Junwei Han.
“MAR20: A Benchmark for Military Aircraft Recognition in Remote Sensing Images.”
*Journal of Remote Sensing (Chinese)*, 2022.

Official dataset page: https://gcheng-nwpu.github.io/#Datasets
