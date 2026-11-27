"""
音乐下载台 · 发版脚本

用法：
    python make_release.py                # 用 updater.APP_VERSION 打 zip + 写 version.json
    python make_release.py --notes "修了xxx"  # 顺便写更新说明
    python make_release.py --build         # 先跑 PyInstaller 再打包

产物（release/ 目录，放在 OneDrive 里会自动同步到其它电脑）：
    release/MusicDesk-1.1.0.zip
    release/version.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "MusicDesk"

sys.path.insert(0, str(ROOT))
import updater as UPD  # noqa: E402


def _primary_release() -> Path:
    """发布点：OneDrive 根下的 MusicDesk-release（跨电脑固定位置）。

    放在这里而不是项目目录里，是因为另一台电脑的工作区未必同步在 OneDrive，
    但 OneDrive 根目录一定是同步的。
    """
    for key in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        v = os.environ.get(key)
        if not v:
            continue
        p = Path(v) / UPD.RELEASE_DIRNAME
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            continue
    p = ROOT / "release"
    p.mkdir(parents=True, exist_ok=True)
    return p


RELEASE = _primary_release()

# 打包时跳过的东西：用户数据、日志、临时文件
SKIP_DIRS = {"data", "downloads", "__pycache__", "build"}
SKIP_SUFFIX = {".log", ".tmp", ".pyc"}
SKIP_FILES = {"jobs.json", "settings.json", "history.json"}


def _should_skip(rel: Path) -> bool:
    parts = {p.lower() for p in rel.parts}
    if parts & SKIP_DIRS:
        return True
    if rel.name.lower() in SKIP_FILES:
        return True
    if rel.suffix.lower() in SKIP_SUFFIX:
        return True
    return False


def build_exe() -> None:
    print("→ PyInstaller 打包中…")
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onedir", "--windowed", "--name", "MusicDesk",
        "--icon", "icon.ico", "--splash", "splash.png",
        "--add-data", "web;web", "--add-data", "icon.ico;.",
        "--hidden-import", "browser_cdp",
        "--hidden-import", "platforms",
        "--hidden-import", "video_search",
        "--hidden-import", "updater",
        "--collect-all", "webview", "--collect-all", "clr_loader",
        "--hidden-import", "clr",
        "--exclude-module", "tkinter", "--exclude-module", "matplotlib",
        "--exclude-module", "pandas", "--exclude-module", "scipy",
        "desktop.py",
    ]
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit(f"打包失败（exit {r.returncode}）")
    copy_apk()


def copy_apk() -> None:
    """把构建好的手机 App 一起塞进 dist，用户下 zip 就有 APK 可传给手机。"""
    src = ROOT / "android" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
    if not src.is_file():
        print("  （没有 Android App，跳过 —— 需要先 gradle assembleDebug）")
        return
    dst = DIST / "音乐下载台-手机.apk"
    shutil.copy2(src, dst)
    print(f"→ 手机 App 已附上：{dst.name}  {dst.stat().st_size / 1024:.1f} KB")


def make_zip(version: str) -> Path:
    if not (DIST / "MusicDesk.exe").is_file():
        raise SystemExit(f"找不到 {DIST / 'MusicDesk.exe'}，先跑 --build 或手动打包")

    RELEASE.mkdir(parents=True, exist_ok=True)
    zpath = RELEASE / f"MusicDesk-{version}.zip"
    if zpath.exists():
        zpath.unlink()

    print(f"→ 压缩 {zpath.name} …")
    n = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(DIST.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(DIST)
            if _should_skip(rel):
                continue
            z.write(p, Path("MusicDesk") / rel)      # 统一带一层 MusicDesk/
            n += 1
            if n % 400 == 0:
                print(f"    {n} 个文件…", end="\r")
    print(f"    {n} 个文件，{zpath.stat().st_size / 1048576:.1f} MB")
    return zpath


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(zpath: Path, version: str, notes: str) -> Path:
    mpath = RELEASE / "version.json"
    old = {}
    if mpath.is_file():
        try:
            old = json.loads(mpath.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    data = {
        "version": version,
        "notes": notes or old.get("notes") or "",
        "zip": zpath.name,
        "size": zpath.stat().st_size,
        "sha256": sha256_of(zpath),
        "published_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "app": UPD.APP_NAME,
    }
    mpath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ 清单已写：{mpath}")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return mpath


def prune_old(keep: str) -> None:
    """只保留最新一个 zip，免得 OneDrive 里堆一堆几百 MB 的老版本。"""
    for z in RELEASE.glob("MusicDesk-*.zip"):
        if z.name != keep:
            print(f"→ 删掉旧包 {z.name}")
            z.unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="音乐下载台 发版")
    ap.add_argument("--notes", default="", help="本次更新说明")
    ap.add_argument("--version", default="", help="覆盖版本号（默认取 updater.APP_VERSION）")
    ap.add_argument("--build", action="store_true", help="先跑 PyInstaller")
    ap.add_argument("--keep-old", action="store_true", help="保留历史 zip（默认只留最新）")
    a = ap.parse_args()

    version = a.version.strip() or UPD.APP_VERSION
    print(f"→ 版本 {version}")

    if a.build:
        build_exe()

    z = make_zip(version)
    write_manifest(z, version, a.notes)
    if not a.keep_old:
        prune_old(z.name)

    print()
    print("✓ 发版完成。其它电脑打开软件后会在顶部看到「有新版本」提示。")
    print(f"  发布点：{RELEASE}")
    print(f"  如果那台电脑没同步到，在「设置 → 更新源」里填：{RELEASE}")


if __name__ == "__main__":
    main()