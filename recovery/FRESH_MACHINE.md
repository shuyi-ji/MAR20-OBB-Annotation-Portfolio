# 从全新 Windows 电脑恢复

即使 Docker Desktop、Ubuntu/WSL 和原电脑上的 Docker 卷都已删除，GitHub Pages、
仓库源码和 GitHub Release 中的原生 CVAT 备份仍然独立存在。交互式标注需要先重新
安装运行平台；没有 Docker 时网页作品仍能查看，但本机 CVAT 服务不能运行。

## 1. 安装基础软件

在管理员 PowerShell 中执行：

```powershell
winget install -e --id Git.Git
winget install -e --id GitHub.cli
winget install -e --id Docker.DockerDesktop
wsl --install -d Ubuntu-24.04
```

按 Windows 提示重启，启动 Docker Desktop，并在 Settings → Resources → WSL
Integration 中启用新安装的 Ubuntu。Docker Desktop 使用 Linux containers。

## 2. 获取作品集仓库

```powershell
git clone https://github.com/shuyi-ji/MAR20-OBB-Annotation-Portfolio.git
cd MAR20-OBB-Annotation-Portfolio
```

也可以在 GitHub 页面下载仓库 ZIP 并解压，不强制依赖 Git 历史。

## 3. 恢复交互式 CVAT

```powershell
powershell -ExecutionPolicy Bypass -File .\recovery\restore.ps1
```

脚本会自动选择已安装的 Ubuntu，优先下载最新云端存档（没有时回退到初始发布版），
校验哈希和内容，启动锁定版本 CVAT，导入 Task，核对 200 张图片、20 个标签、4 个
Jobs 及存档中记录的框数，并启动标签同步。

## 4. 每次标注后的云端存档

完成一轮标注并在 CVAT 中保存后执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\recovery\checkpoint-and-publish.ps1
```

此命令交互读取 CVAT 密码，导出最新原生 Task 备份，打包 200 个实时 YOLO-OBB
标签并上传到 `task25-checkpoint-latest` Release。GitHub 登录由本机 `gh` 使用；Token
不会进入容器、脚本、备份或仓库。

只有已运行这一步的最新修改才具备“本机全部删除后仍可从云端恢复”的保证。
第一次使用 GitHub CLI 时先执行 `gh auth login`；授权保存在本机的 GitHub CLI 中。
