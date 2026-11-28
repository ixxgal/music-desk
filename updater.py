"""
音乐下载台 · 版本检查与自动更新

思路
----
发布点就是一个目录（或 http 地址），里面放两样东西：

    release/
      version.json          ← 清单：版本号、说明、zip 文件名、sha256
      MusicDesk-1.1.0.zip   ← 完整程序包（onedir 的内容）

程序启动后（以及每 6 小时）读一次清单，比自己新就在界面顶部挂个横幅。
用户点「更新」→ 下载 zip → 解压到临时目录 → 写一个 updater.bat →
主进程退出 → bat 等进程消失 → robocopy /MIR 覆盖 → 重新启动。

为什么用 OneDrive 当发布点：本仓库本来就在 D:\\OneDrive 下，新版丢进去会自动
同步到其它电脑的 OneDrive，那边读本地文件就知道有更新了 —— 零服务器、零端口。
也支持 http(s) 地址，将来想放网盘/对象存储直接改设置里的「更新源」即可。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

# ⬆️ 每次发版改这里，并同步 make_release.py 里的版本号
APP_VERSION = "1.2.6"
APP_NAME = "音乐下载台"
MANIFEST_NAME = "version.json"

# OneDrive 根目录下固定用这个文件夹当发布点，跟项目路径无关，
# 另一台电脑哪怕工作区不在 OneDrive 里也能找到
RELEASE_DIRNAME = "MusicDesk-release"

# 安装目录里更新时要保住的东西（用户数据）
KEEP_DIRS = {"data", "downloads"}
KEEP_FILES = {"settings.json", "jobs.json", "history.json"}

UA = "MusicDesk-Updater/1.0"


# ---------------------------------------------------------------- 版本比较
def parse_version(s: str) -> tuple:
    """'1.2.3' → (1, 2, 3)。非数字段落一律当 0，保证不抛异常。"""
    out = []
    for part in str(s or "").strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:4])


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


# ---------------------------------------------------------------- 发布点定位
def _onedrive_roots() -> list[Path]:
    roots = []
    for key in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        v = os.environ.get(key)
        if v:
            roots.append(Path(v))
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    for cand in (home / "OneDrive", Path("D:/OneDrive"), Path("C:/OneDrive")):
        roots.append(cand)
    seen, out = set(), []
    for r in roots:
        try:
            key = str(r.resolve()).lower()
        except Exception:
            key = str(r).lower()
        if key not in seen and r.exists():
            seen.add(key)
            out.append(r)
    return out


def default_sources() -> list[str]:
    """默认去哪些地方找 version.json（按优先级）。"""
    out = []
    for root in _onedrive_roots():
        # 首选固定发布点：OneDrive 根下直接一个 MusicDesk-release，
        # 跟项目在不在 OneDrive 里、另一台电脑的工作区长什么样都无关
        for rel in (
            RELEASE_DIRNAME,
            "音乐下载台/release",
            "Documents/deepseek-harness/default-workspace/music-desk/release",
            "deepseek-harness/default-workspace/music-desk/release",
        ):
            p = root / rel
            if p.is_dir():
                out.append(str(p))
    # 同目录下的 release/（开发机直接跑源码时）
    out.append(str(Path(__file__).resolve().parent / "release"))
    return out


def resolve_source(explicit: str = "") -> str:
    """显式设置优先；否则自动探测；都找不到就返回第一个候选（用于报错提示）。"""
    src = str(explicit or "").strip()
    if src:
        return src
    found = default_sources()
    for s in found:
        if (Path(s) / MANIFEST_NAME).is_file():
            return s
    return found[0] if found else ""


# ---------------------------------------------------------------- 读清单
def _is_url(s: str) -> bool:
    return s.lower().startswith(("http://", "https://"))


def fetch_manifest(source: str, timeout: int = 15) -> dict:
    """读版本清单。source 可以是本地目录 / 本地 json 文件 / http(s) 目录或地址。"""
    src = (source or "").strip()
    if not src:
        return {"ok": False, "error": "还没设置更新源"}

    if _is_url(src):
        url = src if src.lower().endswith(".json") else src.rstrip("/") + "/" + MANIFEST_NAME
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            return {"ok": False, "error": f"取不到更新清单：{e}"}
    else:
        p = Path(src)
        if p.is_dir():
            p = p / MANIFEST_NAME
        if not p.is_file():
            return {"ok": False, "error": f"更新清单不存在：{p}"}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": f"更新清单读不出来：{e}"}

    if not isinstance(data, dict) or not data.get("version"):
        return {"ok": False, "error": "更新清单格式不对（缺 version 字段）"}
    return {"ok": True, "manifest": data, "source": src}


def check_update(source: str = "") -> dict:
    """返回给前端的状态对象。"""
    src = resolve_source(source)
    base = {
        "current": APP_VERSION,
        "source": src,
        "has_update": False,
        "latest": "",
        "notes": "",
        "size": 0,
        "published_at": "",
        "error": "",
        "auto_source": not bool(str(source or "").strip()),
    }
    got = fetch_manifest(src)
    if not got.get("ok"):
        base["error"] = got.get("error") or "检查失败"
        return base

    m = got["manifest"]
    latest = str(m.get("version") or "")
    base.update({
        "latest": latest,
        "notes": str(m.get("notes") or ""),
        "size": int(m.get("size") or 0),
        "published_at": str(m.get("published_at") or ""),
        "zip": str(m.get("zip") or ""),
        "sha256": str(m.get("sha256") or ""),
        "has_update": is_newer(latest, APP_VERSION),
    })
    return base


# ---------------------------------------------------------------- 下载 / 解压
def _download_zip(source: str, manifest: dict, dest_dir: Path,
                  progress_cb=None) -> Path:
    zip_name = str(manifest.get("zip") or "").strip()
    if not zip_name:
        raise RuntimeError("更新清单里没写 zip 文件名")

    dest = dest_dir / zip_name
    if _is_url(source):
        url = zip_name if zip_name.lower().startswith("http") else source.rstrip("/") + "/" + zip_name
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_cb and total:
                    progress_cb(min(0.99, done / total))
    else:
        src = Path(source)
        if src.is_file():
            src = src.parent
        cand = src / zip_name
        if not cand.is_file():
            raise RuntimeError(f"更新包里找不到 {zip_name}")
        total = cand.stat().st_size
        done = 0
        with open(cand, "rb") as fi, open(dest, "wb") as fo:
            while True:
                chunk = fi.read(262144)
                if not chunk:
                    break
                fo.write(chunk)
                done += len(chunk)
                if progress_cb and total:
                    progress_cb(min(0.99, done / total))

    want = str(manifest.get("sha256") or "").strip().lower()
    if want:
        h = hashlib.sha256()
        with open(dest, "rb") as f:
            for chunk in iter(lambda: f.read(1048576), b""):
                h.update(chunk)
        if h.hexdigest().lower() != want:
            raise RuntimeError("更新包校验失败（sha256 对不上），已丢弃")
    return dest


def stage_update(source: str = "", progress_cb=None) -> dict:
    """下载并解压到临时目录，返回 stage 信息。"""
    info = check_update(source)
    if not info.get("has_update"):
        return {"ok": False, "error": info.get("error") or f"已经是最新版（{APP_VERSION}）"}

    got = fetch_manifest(info["source"])
    manifest = got.get("manifest") or {}

    stage = Path(tempfile.mkdtemp(prefix="mdesk-update-"))
    try:
        if progress_cb:
            progress_cb(0.02)
        zpath = _download_zip(info["source"], manifest, stage, progress_cb)
        if progress_cb:
            progress_cb(0.99)
        extract = stage / "x"
        extract.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(extract)

        # zip 里可能带一层 MusicDesk/ 目录，也可能直接是内容 → 找到含 exe 的那层
        root = None
        for cand in [extract] + [p for p in extract.iterdir() if p.is_dir()]:
            if (cand / "MusicDesk.exe").is_file():
                root = cand
                break
        if root is None:
            for p in extract.rglob("MusicDesk.exe"):
                root = p.parent
                break
        if root is None:
            raise RuntimeError("更新包里没找到 MusicDesk.exe")

        return {
            "ok": True,
            "stage": str(root),
            "zip": str(zpath),
            "version": str(manifest.get("version") or ""),
            "notes": str(manifest.get("notes") or ""),
        }
    except Exception as e:
        shutil.rmtree(stage, ignore_errors=True)
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------- 写 updater
_BAT = r"""@echo off
setlocal enabledelayedexpansion
set SRC=%~1
set DST=%~2
set LOG=%DST%\data\update.log
set /a n=0

echo [%DATE% %TIME%] updater start > "%LOG%"
echo   src=%SRC% >> "%LOG%"
echo   dst=%DST% >> "%LOG%"

:wait
tasklist /FI "IMAGENAME eq MusicDesk.exe" 2>nul | find /i "MusicDesk.exe" >nul
if errorlevel 1 goto go
set /a n+=1
if !n! geq 90 goto kill
timeout /t 1 /nobreak >nul
goto wait

:kill
taskkill /F /IM MusicDesk.exe >nul 2>&1
timeout /t 2 /nobreak >nul

:go
echo [%DATE% %TIME%] copying files >> "%LOG%"
robocopy "%SRC%" "%DST%" /MIR /XD data downloads /XF *.log /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo [%DATE% %TIME%] robocopy exit=%RC% >> "%LOG%"

if %RC% geq 8 (
  echo [%DATE% %TIME%] UPDATE FAILED >> "%LOG%"
  echo 更新失败，详细日志见 data\update.log > "%DST%\data\update-error.txt"
  start "" "%DST%\MusicDesk.exe"
  goto end
)

echo [%DATE% %TIME%] launching new version >> "%LOG%"
start "" "%DST%\MusicDesk.exe"
rem clean up the extracted package (~200MB) after a successful swap
rem only touch dirs whose path contains mdesk-update-
echo %SRC% | find /i "mdesk-update-" >nul
if not errorlevel 1 (
  rmdir /S /Q "%SRC%" >nul 2>&1
  for %%I in ("%SRC%\..\..") do rmdir /S /Q "%%~fI" >nul 2>&1
)

:end
(goto) 2>nul & del "%~f0"
"""


def write_updater(stage_dir: str, install_dir: str) -> Path:
    """生成 updater.bat（放在临时目录，避免更新时把自己删掉）。"""
    bat = Path(tempfile.gettempdir()) / f"mdesk-updater-{int(time.time())}.bat"
    # cmd 默认按系统 ANSI 解析脚本，全英文内容 + GBK 写入最稳
    bat.write_text(_BAT, encoding="mbcs", errors="replace")
    (bat.parent).mkdir(parents=True, exist_ok=True)
    return bat


def apply_update(stage: dict) -> dict:
    """启动 updater 并让调用方负责退出进程。"""
    stage_dir = str(stage.get("stage") or "")
    if not stage_dir or not Path(stage_dir).is_dir():
        return {"ok": False, "error": "更新文件还没准备好"}

    install_dir = str(Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                      else Path(__file__).resolve().parent)
    bat = write_updater(stage_dir, install_dir)
    try:
        subprocess.Popen(
            ["cmd", "/c", str(bat), stage_dir, install_dir],
            creationflags=0x08000000,           # CREATE_NO_WINDOW，别闪黑窗
            close_fds=True,
        )
    except Exception as e:
        return {"ok": False, "error": f"启动更新程序失败：{e}"}
    return {"ok": True, "bat": str(bat), "install_dir": install_dir,
            "log": str(Path(install_dir) / "data" / "update.log")}


if __name__ == "__main__":
    info = check_update()
    print(json.dumps(info, ensure_ascii=False, indent=2))