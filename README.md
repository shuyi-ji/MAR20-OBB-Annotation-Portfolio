# MAR20 OBB Annotation Portfolio

这是 CVAT Task #25 `MAR20_Portfolio_OBB_Review_200` 的独立公开作品集仓库。项目展示公开 MAR20 遥感飞机数据的确定性抽样、官方四顶点 OBB 到 CVAT 旋转框的严格转换、人工复核流程、YOLO-OBB 实时同步以及全量质量验证。

> 准确表述：**官方标注导入 + 人工复核、修正与质量控制**，不是 200 张图像全部从零手工标注。

## 关键结果

| 指标 | 结果 |
| --- | ---: |
| 精选图像 | 200 |
| 飞机类别 | 20（A1–A20） |
| 旋转框实例 | 1,534 |
| CVAT Jobs | 4 × 50 帧 |
| 全量几何比较 | 1,534 / 1,534 匹配 |
| 最大四点重建误差 | 0.000990 px（阈值 ≤ 1 px） |
| 临时编辑写入标签 | 3.005 s |
| 恢复写入标签 | 2.753 s |

详细结果见 [`data/VERIFICATION_REPORT.json`](data/VERIFICATION_REPORT.json)。

## 架构

```text
MAR20 public train split
        │ deterministic sampling / SHA-256 manifest
        ▼
mar20_portfolio_images (read-only)
        │
        ▼
CVAT Task #25 ── API poll (3 s) ──► label synchronizer
                                        │
                                        ▼
                              YOLO-OBB label volume (read/write)
```

图片由原生 CVAT 备份自包含保存；标签由独立同步服务写入持久化目录。运行环境、同步状态和原生任务备份分开管理。

## 仓库内容与边界

- `index.html` / `styles.css` / `script.js`：静态作品页，可直接由 GitHub Pages 托管。
- `assets/`：少量真实视觉质检样例，不含完整 200 张影像。
- `data/`：抽样清单、类别分布和机器可读验证证据。
- `data/sample-labels/`：仅与公开样例对应的少量初始 YOLO-OBB 标签。
- `scripts/`：数据准备、CVAT 创建、同步、可逆测试与验证脚本。
- `recovery/`：锁定 CVAT 版本的一键恢复、备份校验、恢复后验收与标签同步环境。

Git 历史明确不包含：其他 CVAT 任务、Docker 数据卷、账号密码、Token、`.env`、服务器地址或实验室目录。Task #25 的原生备份以 GitHub Release 资产单独保存，不进入 Git 提交历史。

## 交互式 CVAT 灾难恢复

静态作品页用于随时在线展示；交互式 CVAT 由可下载的原生任务备份和锁定版本的 Docker 环境重建。即使原电脑、Docker、Ubuntu/WSL 和 Docker 数据卷全部丢失，也可以在重装运行平台后，从 GitHub 恢复 200 张图片、20 类、4 个 Jobs 和已存档的全部框。

- [恢复说明](recovery/README.md)
- [全新电脑恢复步骤](recovery/FRESH_MACHINE.md)
- [Task #25 恢复资产](https://github.com/shuyi-ji/MAR20-OBB-Annotation-Portfolio/releases/tag/task25-recovery-v1)
- [最新工作存档](https://github.com/shuyi-ji/MAR20-OBB-Annotation-Portfolio/releases/tag/task25-checkpoint-latest)
- CVAT：`v2.72.0`
- 官方源码提交：`0445d0336f9bc537dd8227b5f9be69c9ad5d11b0`
- 原生备份 SHA-256：`7e42b43f213e4e97333b8e8971d4085533bb80edefa12e3987822bff23c730e6`

Windows + WSL：

```powershell
powershell -ExecutionPolicy Bypass -File .\recovery\restore.ps1
```

Linux/WSL：

```bash
bash recovery/bootstrap-and-restore.sh
```

密码仅交互输入，不写入脚本、仓库或日志。恢复成功后，YOLO-OBB 标签持续写入 `.runtime/mar20-cvat/labels/`。

每轮标注完成后执行以下命令，才会把新修改连同原生 Task 备份和 200 个标签文件更新到云端最新存档：

```powershell
powershell -ExecutionPolicy Bypass -File .\recovery\checkpoint-and-publish.ps1
```

因此，初始发布版是不可变的恢复根；`task25-checkpoint-latest` 是可滚动更新的工作存档。未执行存档命令的新修改仍只在本机，不能承诺在整机删除后恢复。

## 本地预览

在仓库根目录运行任意静态文件服务器，例如：

```bash
python -m http.server 4173
```

然后打开 `http://127.0.0.1:4173/`。直接双击 `index.html` 时，浏览器可能禁止读取本地 CSV，类别图表需要通过 HTTP 预览。

## 数据来源与许可

MAR20 官方页面：<https://gcheng-nwpu.github.io/#Datasets>

作者页面说明 MAR20 包含 3,842 张图像、20 类和 22,341 个飞机实例，并采用 **CC BY-NC 4.0**。本仓库中的 MAR20 衍生视觉样例和标签仅用于非商业实习/求职展示；数据集权利与许可仍归原作者及原许可约束。仓库中的原创代码采用 MIT License，详见 [`LICENSE.md`](LICENSE.md)。

引用：

> Wenqi Yu, Gong Cheng, Meijun Wang, Yanqing Yao, Xingxing Xie, Xiwen Yao, and Junwei Han. “MAR20: A Benchmark for Military Aircraft Recognition in Remote Sensing Images.” *Journal of Remote Sensing (Chinese)*, 2022.

## 隐私与安全

如果发现凭据、私有路径或不应公开的数据，请不要提交公开 Issue；处理原则见 [`SECURITY.md`](SECURITY.md)。
