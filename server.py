#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
音乐下载台 · 后端
=================
把「链接 / 歌名描述 / 本地音频文件」统一变成 mp3，落到 downloads/ 目录。

启动:  python server.py [--port 8899] [--host 127.0.0.1]
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import platforms as PLT
import updater as UPD

_UPDATE_STAGE: dict = {}


def _delayed_exit(delay: float = 1.2) -> None:
    """给 HTTP 响应留出时间，再退出进程让 updater.bat 接手替换文件。"""
    time.sleep(delay)
    os._exit(0)

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _suppress_console_windows() -> None:
    """--windowed 打包后没有控制台，子进程（ffmpeg / powershell / edge）每起一个
    就弹一个黑色 cmd 窗口。统一给 subprocess.Popen 补 CREATE_NO_WINDOW + 隐藏的
    STARTUPINFO，连 yt-dlp 内部调用 ffmpeg 也一并盖住。"""
    if os.name != "nt" or getattr(subprocess.Popen.__init__, "_md_patched", False):
        return
    _flag = 0x08000000  # CREATE_NO_WINDOW
    _orig = subprocess.Popen.__init__

    def _patched(self, *args, **kwargs):
        kwargs["creationflags"] = int(kwargs.get("creationflags") or 0) | _flag
        return _orig(self, *args, **kwargs)

    _patched._md_patched = True
    subprocess.Popen.__init__ = _patched


_suppress_console_windows()

# ---------------------------------------------------------------- 目录 / 常量
if getattr(sys, "frozen", False):
    # PyInstaller 打包后：数据目录跟着 exe 走，静态资源在解压包里
    BASE = Path(sys.executable).resolve().parent
    WEB = Path(getattr(sys, "_MEIPASS", BASE)) / "web"
else:
    BASE = Path(__file__).resolve().parent
    WEB = BASE / "web"
DOWNLOADS = BASE / "downloads"
DATA = BASE / "data"
TMP = DOWNLOADS / ".tmp"
for _d in (WEB, DOWNLOADS, DATA, TMP):
    _d.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus",
             ".wma", ".aiff", ".ape", ".webm", ".mp4", ".m4s"}
MP3_EXT = {".mp3"}
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}
VIDEO_SITE = {"douyin", "kuaishou"}


# ---------------------------------------------------------------- ffmpeg 定位
def find_ffmpeg() -> str:
    env = os.environ.get("FFMPEG_PATH")
    if env and Path(env).exists():
        return env
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
    w = shutil.which("ffmpeg")
    if w:
        return w
    for c in (r"C:\ffmpeg\bin\ffmpeg.exe",
              r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
              r"C:\ProgramData\chocolatey\bin\ffmpeg.exe"):
        if Path(c).exists():
            return c
    return ""


FFMPEG = find_ffmpeg()


# ---------------------------------------------------------------- 小工具
def now_ts() -> float:
    return time.time()


def human_size(n) -> str:
    try:
        n = float(n)
    except Exception:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024.0
    return ""


def human_speed(bps) -> str:
    try:
        return human_size(bps) + "/s"
    except Exception:
        return ""


BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WS = re.compile(r"\s+")
MARKETING = re.compile(
    r"(?i)(official\s*(video|audio|music\s*video|mv)|lyric\s*video|动态歌词|歌词版|"
    r"hd|hi-?res|无损|高清|完整版|超清|4k|8k|2160p|1080p|mv|现场版|翻自|"
    r"audio|visualizer|lyrics?)")


def pretty_title(t: str) -> str:
    """把平台标题清洗成人看得懂的名字。"""
    s = (t or "").strip()
    if not s:
        return ""
    # 书名号优先
    m = re.search(r"《([^》]{1,60})》", s)
    if m:
        return WS.sub(" ", m.group(1)).strip() or s
    # 去掉抖音/快手的 #话题 尾巴（整条都是话题时保留原样）
    no_tag = re.sub(r"\s*#[^\s#]{1,40}", "", s).strip()
    no_tag = re.sub(r"^[\s\-–—_|｜·、,，.]+", "", no_tag)
    no_tag = re.sub(r"[\s\-–—_|｜·、,，.]+$", "", no_tag).strip()
    if no_tag and len(no_tag) >= 4:
        s2 = no_tag
    else:
        s2 = s
    if not re.search(r"[【】\[\]（）()]", s2):
        return s2
    s = re.sub(r"【[^】]{1,40}】", " ", s2)
    s = re.sub(r"\[[^\]]{1,40}\]", " ", s)
    s = re.sub(r"（[^）]{1,30}）|\([^)]{1,30}\)", " ", s)
    s = MARKETING.sub(" ", s)
    s = re.sub(r"^[\s\-–—_|｜·、,，.]+", "", s)
    s = re.sub(r"[\s\-–—_|｜·、,，.]+$", "", s)
    s = WS.sub(" ", s).strip()
    return s or s2 or (t or "").strip()


def build_filename(title: str, artist: str, source: str) -> str:
    t = pretty_title(title)
    src = (source or "").lower()
    is_ugc = "bilibili.com" in src or "b23.tv" in src or "youtube.com" in src or "youtu.be" in src
    if artist and not is_ugc and _norm(artist) and _norm(artist) not in _norm(t):
        return clean_name(f"{artist} - {t}.mp3")
    return clean_name(f"{t}.mp3")


def clean_name(name: str, maxlen: int = 110) -> str:
    name = BAD_CHARS.sub("", name or "")
    name = WS.sub(" ", name).strip().strip(".")
    if not name:
        name = "untitled"
    if len(name) > maxlen:
        name = name[:maxlen].rstrip()
    return name


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem, suf, i = p.stem, p.suffix, 2
    while True:
        cand = p.with_name(f"{stem} ({i}){suf}")
        if not cand.exists():
            return cand
        i += 1


def load_json(path: Path, default: dict) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        out = dict(default)
        if isinstance(raw, dict):
            out.update(raw)
        return out
    except Exception:
        return dict(default)


def save_json(path: Path, obj) -> None:
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 设置
SETTINGS_FILE = DATA / "settings.json"
SETTINGS_DEFAULTS = {
    "quality": "320",          # mp3 比特率 kbps
    "concurrency": 3,          # 并行任务数
    "write_tags": True,        # 写 ID3 标签
    "embed_cover": True,       # 嵌入封面
    "proxy": "",               # http://127.0.0.1:7890
    "browser_cookies": "",     # chrome / edge / firefox / 空
    "keep_source": False,      # 保留原始文件
    "download_dir": "",        # 下载目录（空 = 程序目录下的 downloads）
    "autohide_done": False,    # 任务完成后自动从列表隐藏
    "purge_days": 3,           # 已完成任务隐藏 N 天后彻底删除（0 = 永不删除）
    "update_source": "",       # 更新发布点：本地目录 / http(s) 地址；空 = 自动探测 OneDrive
    "allow_lan": False,        # 允许同一局域网内的手机访问（改了要重启生效）
    "move_on_dir_change": False,  # 换下载目录时，把已有文件一起搬过去
}
settings = load_json(SETTINGS_FILE, SETTINGS_DEFAULTS)


def apply_download_dir() -> Path:
    """按设置切换下载目录；空值 = 程序目录下的 downloads。"""
    global DOWNLOADS
    want = str(settings.get("download_dir") or "").strip()
    target = None
    if want:
        try:
            p = Path(want).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            target = p
        except Exception as e:
            print(f"[warn] 下载目录不可用（{want}）: {e}")
    if target is None:
        target = BASE / "downloads"
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    DOWNLOADS = target
    return DOWNLOADS


apply_download_dir()


def save_settings(new: dict) -> dict:
    global settings
    merged = dict(settings)
    for k, v in (new or {}).items():
        if k in SETTINGS_DEFAULTS:
            merged[k] = v
    if str(merged.get("quality")) not in ("128", "192", "256", "320"):
        merged["quality"] = "320"
    try:
        merged["concurrency"] = max(1, min(6, int(merged.get("concurrency") or 3)))
    except Exception:
        merged["concurrency"] = 3
    try:
        merged["purge_days"] = max(0, min(365, int(merged.get("purge_days") or 0)))
    except Exception:
        merged["purge_days"] = 3
    settings = merged
    save_json(SETTINGS_FILE, settings)
    apply_download_dir()
    rebuild_pool()
    return settings


# ---------------------------------------------------------------- 任务
JOBS_FILE = DATA / "jobs.json"
_jobs_lock = threading.RLock()
jobs: dict[str, dict] = {}
_pool: ThreadPoolExecutor | None = None


def rebuild_pool():
    global _pool
    try:
        if _pool:
            _pool.shutdown(wait=False)
    except Exception:
        pass
    _pool = ThreadPoolExecutor(max_workers=int(settings.get("concurrency") or 3),
                               thread_name_prefix="dl")


rebuild_pool()


def load_jobs():
    raw = load_json(JOBS_FILE, {"jobs": []})
    items = raw.get("jobs") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return
    keep = []
    for j in items[-200:]:
        if not isinstance(j, dict) or not j.get("id"):
            continue
        if j.get("status") in ("queued", "running"):
            j["status"] = "error"
            j["error"] = j.get("error") or "服务重启，任务中断"
        keep.append(j)
    with _jobs_lock:
        for j in keep:
            jobs[j["id"]] = j


def persist_jobs():
    with _jobs_lock:
        snapshot = {"jobs": [jobs[k] for k in list(jobs.keys())[-200:]]}
    try:
        save_json(JOBS_FILE, snapshot)
    except Exception:
        pass


def public_job(j: dict) -> dict:
    keys = ("id", "title", "artist", "album", "status", "stage", "progress",
            "speed", "eta", "engine", "source", "filename", "error",
            "created", "finished", "thumb", "duration", "out_size", "platform")
    return {k: j.get(k) for k in keys}


def update_job(jid: str, **kw):
    with _jobs_lock:
        j = jobs.get(jid)
        if not j:
            return
        j.update(kw)
        if kw.get("_touch"):
            j["updated"] = now_ts()


# ---------------------------------------------------------------- HTTP 取流
def make_opener():
    proxy = (settings.get("proxy") or "").strip()
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def http_open(url: str, headers: dict | None = None, timeout: int = 30, data: bytes | None = None):
    h = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "close",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    return make_opener().open(req, timeout=timeout)


AUDIO_MAGIC = (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xf1",
               b"\xff\xf9", b"\xff\xfa", b"OggS", b"RIFF", b"fLaC")


def looks_like_audio(head: bytes) -> bool:
    """按文件头判断是不是音频。网易云/腾讯等外链失效时会返回 200 + HTML。"""
    if not head:
        return False
    if head[4:8] == b"ftyp":                 # m4a / mp4
        return True
    if head[:4] in (b"\x30\x26\xb2\x75", b"MAC "):   # ape / aac-adts 变种
        return True
    return any(head.startswith(m) for m in AUDIO_MAGIC)


def check_media_type(ctype: str) -> None:
    """响应头是 html/json 的就别往下写了 —— 直接当链接失效。"""
    c = (ctype or "").lower()
    if c.startswith(("text/html", "text/plain", "application/json",
                     "application/xml", "text/xml")):
        raise RuntimeError(f"该链接返回的是 {c.split(';')[0]} 而不是音频（多半无版权 / 需会员 / 已失效）")


def stream_download(url: str, dest: Path, job: dict | None = None,
                    headers: dict | None = None, timeout: int = 60,
                    max_bytes: int = 600 * 1024 * 1024,
                    verify_audio: bool = False) -> Path:
    resp = http_open(url, headers, timeout=timeout)
    total = 0
    try:
        total = int(resp.headers.get("Content-Length") or 0)
    except Exception:
        total = 0
    ctype = (resp.headers.get("Content-Type") or "").lower()
    check_media_type(ctype)
    part = dest.with_suffix(dest.suffix + ".part")
    done = 0
    t0 = time.time()
    try:
        with open(part, "wb") as f:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if done > max_bytes:
                    raise RuntimeError("文件超过大小上限")
                if job is not None:
                    if total > 0:
                        job["progress"] = round(min(99.0, done * 100.0 / total), 1)
                    el = max(0.001, time.time() - t0)
                    job["speed"] = human_speed(done / el)
                    job["stage"] = "下载中"
    finally:
        try:
            resp.close()
        except Exception:
            pass
    if total and 0 < done < total * 0.98:
        try:
            part.unlink()
        except Exception:
            pass
        raise RuntimeError(f"下载不完整 ({human_size(done)}/{human_size(total)})")
    if done == 0:
        try:
            part.unlink()
        except Exception:
            pass
        raise RuntimeError("服务端返回空内容")
    os.replace(part, dest)
    if verify_audio:
        with open(dest, "rb") as f:
            head = f.read(16)
        if not looks_like_audio(head):
            try:
                dest.unlink()
            except Exception:
                pass
            raise RuntimeError("下载到的不是音频文件（多半是失效页面），换其他源试试")
    return dest


# ---------------------------------------------------------------- 音频元信息
def audio_meta(p: Path) -> dict:
    info = {}
    try:
        from mutagen import File as MFile
        f = MFile(str(p))
        if f is not None and getattr(f, "info", None):
            info["duration"] = int(round(getattr(f.info, "length", 0) or 0))
            br = getattr(f.info, "bitrate", 0) or 0
            info["bitrate"] = int(round(br / 1000)) if br else 0
    except Exception:
        pass
    try:
        info["size"] = p.stat().st_size
        info["mtime"] = p.stat().st_mtime
    except Exception:
        pass
    return info


def video_meta(p: Path) -> dict:
    """视频文件基本信息。

    优先用 mutagen 纯 Python 解析（mp4/m4v/mov 支持良好）——**不启动 ffmpeg**。
    因为 --windowed 打包后每次起 ffmpeg 都会闪一个黑色控制台窗口，刷新曲库时
    每个视频文件闪一个，体验极差。纯 Python 解析拿全了就直接返回；只有解析
    不了（.webm/.mkv 等）才回退 ffmpeg，且那条路径也带了 CREATE_NO_WINDOW。
    """
    info = {}
    try:
        info["size"] = p.stat().st_size
        info["mtime"] = p.stat().st_mtime
    except Exception:
        pass

    # ---- 1) 纯 Python：mutagen ----
    if p.suffix.lower() in (".mp4", ".m4v", ".mov"):
        try:
            from mutagen.mp4 import MP4
            f = MP4(str(p))
            dur = getattr(f.info, "length", 0) or 0
            w = getattr(f.info, "width", 0) or 0
            h = getattr(f.info, "height", 0) or 0
            if dur:
                info["duration"] = int(round(dur))
            if w:
                info["width"] = int(w)
            if h:
                info["height"] = int(h)
            br = getattr(f.info, "bitrate", 0) or 0
            if br:
                info["bitrate"] = int(round(br / 1000))
            if info.get("duration"):
                return info          # 时长拿到了就够用，绝不碰 ffmpeg
        except Exception:
            pass

    # ---- 2) 兜底：ffmpeg（不弹窗）----
    if not FFMPEG:
        return info
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-i", str(p)],
                             capture_output=True, text=True, errors="replace",
                             timeout=60,
                             creationflags=0x08000000 if os.name == "nt" else 0).stderr
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
        if m:
            hh, mm, ss = m.groups()
            info["duration"] = int(int(hh) * 3600 + int(mm) * 60 + float(ss))
        m = re.search(r"Stream #0:0.*?(\d{2,5})x(\d{2,5})", out)
        if m:
            info["width"] = int(m.group(1))
            info["height"] = int(m.group(2))
        m = re.search(r"Video:\s*(\w+)", out)
        if m:
            info["vcodec"] = m.group(1)
        m = re.search(r"bitrate:\s*(\d+)\s*kb/s", out)
        if m:
            info["bitrate"] = int(m.group(1))
    except Exception:
        pass
    return info


def write_tags(path: Path, title: str, artist: str = "", album: str = "",
               cover: bytes | None = None) -> None:
    if not settings.get("write_tags"):
        return
    try:
        from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, APIC, TCON
        try:
            tags = ID3(str(path))
        except ID3NoHeaderError:
            tags = ID3()
        if title:
            tags.delall("TIT2")
            tags.add(TIT2(encoding=3, text=[title]))
        if artist:
            tags.delall("TPE1")
            tags.add(TPE1(encoding=3, text=[artist]))
        if album:
            tags.delall("TALB")
            tags.add(TALB(encoding=3, text=[album]))
        if cover and settings.get("embed_cover"):
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3,
                          desc="Cover", data=cover))
        tags.save(str(path), v2_version=3)
    except Exception:
        pass


def cover_url(url: str, size: int = 800) -> str:
    """让 CDN 直接吐缩略图，避免把 5906x5906 的原图塞进 ID3。"""
    if not url:
        return url
    if "hdslb.com" in url and "@" not in url:
        return f"{url}@{size}w_{size}h_1c.jpg"
    if ("126.net" in url or "music.163.com" in url) and "?" not in url:
        return f"{url}?param={size}y{size}"
    return url


def shrink_cover(data: bytes, max_side: int = 800, limit: int = 200 * 1024) -> bytes:
    """兜底：图片仍然过大就用 ffmpeg 缩到 max_side 以内。"""
    if not data or len(data) <= limit or not FFMPEG:
        return data
    tmpd = TMP / ("cover-" + uuid.uuid4().hex[:8])
    try:
        tmpd.mkdir(parents=True, exist_ok=True)
        src = tmpd / "in.jpg"
        src.write_bytes(data)
        dst = tmpd / "out.jpg"
        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
             "-vf", f"scale='min({max_side},iw)':'min({max_side},ih)'"
                    ":force_original_aspect_ratio=decrease",
             "-q:v", "4", str(dst)],
            capture_output=True, creationflags=0x08000000 if os.name == "nt" else 0)
        if dst.exists() and dst.stat().st_size > 512:
            return dst.read_bytes()
        return data
    except Exception:
        return data
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def fetch_cover(url: str) -> bytes | None:
    if not url or not settings.get("embed_cover"):
        return None
    try:
        ref = "https://www.bilibili.com/" if "hdslb.com" in url else "https://music.163.com/"
        resp = http_open(cover_url(url), {"Referer": ref, "User-Agent": UA}, timeout=15)
        data = resp.read(3 * 1024 * 1024)
        resp.close()
        if len(data) < 512:
            return None
        return shrink_cover(data)
    except Exception:
        return None


def transcode_to_mp3(src: Path, dest: Path, quality: str = "320") -> Path:
    if not FFMPEG:
        raise RuntimeError("找不到 ffmpeg，无法转码（请设置 FFMPEG_PATH 环境变量）")
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(src), "-vn", "-map", "0:a:0",
           "-c:a", "libmp3lame", "-b:a", f"{quality}k",
           "-id3v2_version", "3", "-f", "mp3", str(dest)]
    p = subprocess.run(cmd, capture_output=True, creationflags=0x08000000
                       if os.name == "nt" else 0)
    if p.returncode != 0 or not dest.exists():
        err = (p.stderr or b"").decode("utf-8", "ignore")[-500:]
        raise RuntimeError("转码失败: " + (err.strip() or f"ffmpeg 退出码 {p.returncode}"))
    return dest


# ---------------------------------------------------------------- 搜索：网易云
def netease_details(ids: list) -> dict:
    """批量取详情（封面 / 收费状态 / 专辑），一次请求。"""
    if not ids:
        return {}
    ids_json = json.dumps([int(i) for i in ids], separators=(",", ":"))
    url = "https://music.163.com/api/song/detail/?ids=" + urllib.parse.quote(ids_json)
    raw = http_open(url, {"Referer": "https://music.163.com/"}, timeout=15).read()
    j = json.loads(raw.decode("utf-8", "ignore"))
    out = {}
    for s in (j.get("songs") or []):
        album = s.get("album") or {}
        out[s.get("id")] = {
            "album": album.get("name") or "",
            "thumb": (album.get("picUrl") or "").replace("http://", "https://"),
            "fee": s.get("fee", 0),
            "duration": int(round((s.get("duration") or 0) / 1000)),
            "title": s.get("name") or "",
            "artist": "/".join(a.get("name", "") for a in (s.get("artists") or [])),
        }
    return out


BAD_TITLE = re.compile(r"(翻唱|cover|伴奏|inst\.?|karaoke|dj|remix|钢琴版|纯音乐|吉他版|抖音|铃声|片段|live|现场|加速版|慢速版|女声版|男声版|童声|口琴|八音盒)", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[\s\-_·.,'\"()（）\[\]【】]", "", (s or "").lower())


SEARCH_SOURCES = ("bilibili", "netease", "ximalaya")


def search_all(q: str, limit: int = 20) -> list[dict]:
    """综合搜索：并行问 B站 / 网易云 / 喜马拉雅，按相关度混排后合并。

    原则：能直接下载的源优先（B站 权重最高），VIP 降权，
    同一首歌只留分数最高的那条，避免三个平台的同名结果刷屏。
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(src: str) -> list[dict]:
        try:
            if src == "bilibili":
                return bilibili_search(q, max(8, limit // 2))
            if src == "netease":
                return netease_search(q, max(6, limit // 3))
            return PLT.search("ximalaya", q, max(4, limit // 4))
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=3) as ex:
        chunks = list(ex.map(one, SEARCH_SOURCES))

    merged: list[dict] = []
    for src, items in zip(SEARCH_SOURCES, chunks):
        for it in items:
            it = dict(it)
            if not it.get("platform"):
                it["platform"] = src
            s = score_hit(it, q)
            if s < 25:
                continue          # 与关键词无关的结果（喜马拉雅尤其容易混进来）直接丢掉
            if src == "bilibili":
                s += 30          # 可直接下、音质有保证，明显优先
            elif src == "ximalaya":
                s -= 40          # 电台/有声书居多，别抢到音乐前面
            if it.get("vip"):
                s -= 15
            it["_mix"] = s
            merged.append(it)

    merged.sort(key=lambda x: x.get("_mix", 0), reverse=True)

    seen: dict[str, float] = {}
    out: list[dict] = []
    for it in merged:
        key = _norm(f"{it.get('title','')}|{it.get('artist','')}") or f"_{id(it)}"
        prev = seen.get(key)
        if prev is not None and prev >= it.get("_mix", 0):
            continue
        seen[key] = it.get("_mix", 0)
        out.append(it)
        if len(out) >= limit:
            break
    for it in out:
        it.pop("_mix", None)
    return out


def score_hit(item: dict, q: str) -> float:
    nq = _norm(q)
    nt = _norm(item.get("title"))
    na = _norm(item.get("artist"))
    score = 0.0
    if nq and nq == nt:
        score += 120
    elif nq and nq in nt:
        score += 55
    if nq and nq and nq in na:
        score += 35
    for w in re.split(r"[\s]+", q.strip()):
        nw = _norm(w)
        if not nw:
            continue
        if nw in nt:
            score += 14
        if nw in na:
            score += 20
    title_raw = item.get("title") or ""
    if BAD_TITLE.search(title_raw):
        score -= 55
    if item.get("fee") == 1:
        score -= 12
    d = item.get("duration") or 0
    if d and d < 60:
        score -= 35
    elif 120 <= d <= 420:
        score += 8
    return score


def netease_search(q: str, limit: int = 20) -> list[dict]:
    url = "https://music.163.com/api/search/get/web"
    body = urllib.parse.urlencode({
        "s": q, "type": "1", "offset": "0", "total": "true", "limit": str(limit),
    }).encode()
    data = http_open(url, {
        "Referer": "https://music.163.com/",
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": "appver=2.0.2; os=pc; channel=netease;",
    }, timeout=15, data=body).read()
    j = json.loads(data.decode("utf-8", "ignore"))
    songs = ((j.get("result") or {}).get("songs")) or []
    if not songs:
        return []

    ids = [s.get("id") for s in songs if s.get("id")]
    details = {}
    try:
        details = netease_details(ids)
    except Exception:
        details = {}

    out = []
    for s in songs:
        sid = s.get("id")
        d = details.get(sid, {})
        artists = "/".join(a.get("name", "") for a in (s.get("artists") or []) if a.get("name"))
        album = s.get("album") or {}
        fee = d.get("fee", s.get("fee", 0))
        item = {
            "uid": f"ne:{sid}",
            "engine": "netease",
            "id": sid,
            "title": s.get("name") or d.get("title") or "",
            "artist": artists or d.get("artist", ""),
            "album": d.get("album") or album.get("name") or "",
            "thumb": d.get("thumb") or (album.get("picUrl") or "").replace("http://", "https://"),
            "duration": d.get("duration") or int(round((s.get("duration") or 0) / 1000)),
            "source": f"https://music.163.com/song?id={sid}",
            "fee": fee,
            "vip": fee == 1,
            "note": "VIP" if fee == 1 else "网易云",
        }
        item["_score"] = score_hit(item, q)
        out.append(item)

    out.sort(key=lambda x: -x["_score"])
    # 去重：同标题 + 同歌手
    seen, uniq = set(), []
    for it in out:
        k = (_norm(it["title"]), _norm(it["artist"]))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    for it in uniq:
        it.pop("_score", None)
    return uniq


def netease_detail(song_id: str) -> dict:
    ids_json = urllib.parse.quote(json.dumps([int(song_id)], separators=(",", ":")))
    url = "https://music.163.com/api/song/detail/?ids=" + ids_json
    raw = http_open(url, {"Referer": "https://music.163.com/"}, timeout=15).read()
    j = json.loads(raw.decode("utf-8", "ignore"))
    songs = j.get("songs") or []
    if not songs:
        return {}
    s = songs[0]
    artists = "/".join(a.get("name", "") for a in (s.get("artists") or []))
    album = s.get("album") or {}
    return {
        "title": s.get("name") or "",
        "artist": artists,
        "album": album.get("name") or "",
        "thumb": (album.get("picUrl") or "").replace("http://", "https://"),
        "duration": int(round((s.get("duration") or 0) / 1000)),
        "fee": s.get("fee", 0),
    }


# ---------------------------------------------------------------- 搜索：B 站
BILI_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]
_bili_cache = {"ts": 0.0, "cookie": "", "mixin": ""}


def bili_prepare(force: bool = False) -> tuple[str, str]:
    """取 buvid3 cookie 与 wbi mixin key（缓存 8 分钟）。"""
    if not force and time.time() - _bili_cache["ts"] < 480 and _bili_cache["mixin"]:
        return _bili_cache["cookie"], _bili_cache["mixin"]
    cookies = {}
    try:
        r = http_open("https://www.bilibili.com/", timeout=15)
        for sc in (r.headers.get_all("Set-Cookie") or []):
            part = sc.split(";")[0]
            if "=" in part:
                k, v = part.split("=", 1)
                cookies[k.strip()] = v.strip()
        r.close()
    except Exception:
        pass
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    mixin = ""
    try:
        r = http_open("https://api.bilibili.com/x/web-interface/nav",
                      {"Cookie": cookie_str, "Referer": "https://www.bilibili.com/"}, timeout=15)
        j = json.loads(r.read().decode("utf-8", "ignore"))
        r.close()
        wbi = ((j.get("data") or {}).get("wbi_img")) or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if img and sub:
            raw = img + sub
            mixin = "".join(raw[i] for i in BILI_MIXIN_TAB if i < len(raw))[:32]
    except Exception:
        pass
    _bili_cache.update({"ts": time.time(), "cookie": cookie_str, "mixin": mixin})
    return cookie_str, mixin


def bili_sign(params: dict, mixin: str) -> str:
    import hashlib
    p = dict(params)
    p["wts"] = int(time.time())
    items = sorted(p.items())
    query = urllib.parse.urlencode(items, quote_via=urllib.parse.quote)
    if mixin:
        w_rid = hashlib.md5((query + mixin).encode()).hexdigest()
        return f"{query}&w_rid={w_rid}"
    return query


def _parse_bili_dur(s: str) -> int:
    try:
        parts = [int(x) for x in str(s).split(":")]
        t = 0
        for x in parts:
            t = t * 60 + x
        return t
    except Exception:
        return 0


def bilibili_search(q: str, limit: int = 20) -> list[dict]:
    cookie, mixin = bili_prepare()
    params = {
        "search_type": "video",
        "keyword": q,
        "page": 1,
        "page_size": max(10, limit),
        "order": "totalrank",
    }
    url = "https://api.bilibili.com/x/web-interface/wbi/search/type?" + bili_sign(params, mixin)
    raw = http_open(url, {
        "Cookie": cookie,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }, timeout=20).read()
    j = json.loads(raw.decode("utf-8", "ignore"))
    code = j.get("code")
    if code != 0:
        # 风控时刷新一次 cookie 重试
        cookie, mixin = bili_prepare(force=True)
        url = "https://api.bilibili.com/x/web-interface/wbi/search/type?" + bili_sign(params, mixin)
        raw = http_open(url, {"Cookie": cookie, "Referer": "https://www.bilibili.com/"}, timeout=20).read()
        j = json.loads(raw.decode("utf-8", "ignore"))
        if j.get("code") != 0:
            raise RuntimeError(f"B站搜索失败 code={j.get('code')} {j.get('message')}")

    rows = ((j.get("data") or {}).get("result")) or []
    out = []
    for v in rows:
        if v.get("type") and v.get("type") != "video":
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", v.get("title") or "")).strip()
        pic = v.get("pic") or ""
        if pic.startswith("//"):
            pic = "https:" + pic
        bvid = v.get("bvid") or ""
        out.append({
            "uid": f"bv:{bvid}",
            "engine": "ytdlp",
            "id": bvid,
            "title": title,
            "artist": v.get("author") or "",
            "album": "",
            "thumb": pic,
            "duration": _parse_bili_dur(v.get("duration")),
            "source": v.get("arcurl") or f"https://www.bilibili.com/video/{bvid}",
            "note": "B站",
            "plays": v.get("play") or 0,
        })
    return out


# ---------------------------------------------------------------- 搜索：网络视频源
def web_search(q: str, limit: int = 12) -> list[dict]:
    import yt_dlp
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "skip_download": True, "extract_flat": "in_playlist",
        "noplaylist": True, "socket_timeout": 12,
    }
    if settings.get("proxy"):
        opts["proxy"] = settings["proxy"]
    out = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{q} audio", download=False)
    for e in (info.get("entries") or []):
        if not e:
            continue
        dur = e.get("duration") or 0
        url = e.get("url") or e.get("webpage_url") or ""
        if url and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={e.get('id')}"
        out.append({
            "uid": f"yt:{e.get('id')}",
            "engine": "ytdlp",
            "id": e.get("id"),
            "title": e.get("title") or "",
            "artist": e.get("uploader") or e.get("channel") or "",
            "album": "",
            "thumb": e.get("thumbnails")[-1]["url"] if e.get("thumbnails") else "",
            "duration": int(dur) if dur else 0,
            "source": url,
            "note": "网络视频源",
        })
    return out


def clean_query(q: str) -> str:
    q = q.strip()
    q = re.sub(r"(?i)\bhttps?://\S+", " ", q)
    q = re.sub(r"(帮我|请帮我|麻烦|给我|我要|下载一下|下载|找一下|找下|找|搜索|搜一下|搜)\s*", " ", q)
    q = re.sub(r"(?i)\.?(mp3|m4a|flac|wav)\b", " ", q)
    q = re.sub(r"(?i)\b(mp3|320k|无损|高音质|音质|完整版|原版|超高音质)\b", " ", q)
    q = WS.sub(" ", q).strip(" ,，。、-")
    return q or q.strip()


# ---------------------------------------------------------------- 探测 / 解析
NETEASE_ID = re.compile(r"(?:music\.163\.com|163cn\.tv).*?id=(\d+)", re.I)
NETEASE_SHORT = re.compile(r"https?://163cn\.tv/\w+", re.I)
DIRECT_AUDIO = re.compile(r"\.(mp3|m4a|flac|wav|aac|ogg|opus|wma)(\?|#|$)", re.I)


def classify(text: str) -> str:
    t = (text or "").strip()
    if re.match(r"^https?://", t, re.I):
        return "url"
    if re.match(r"^[a-zA-Z]:[\\/]", t) or t.startswith("\\\\"):
        return "path"
    return "query"


SHORT_HOSTS = ("v.douyin.com", "b23.tv", "k.kuaishou.com", "v.kuaishou.com",
               "163cn.tv", "xhslink.com", "v.qq.com", "t.cn")


def resolve_short(url: str, timeout: int = 12) -> str:
    """短链跟跳到真实地址：v.douyin.com / b23.tv 这类分享链接里的 id 藏在跳转后。"""
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return url
    if not any(h in host for h in SHORT_HOSTS):
        return url
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            final = r.geturl() or url
        return final
    except Exception:
        return url


def guess_platform(url: str) -> str:
    u = url.lower()
    if "music.163.com" in u or "163cn.tv" in u:
        return "netease"
    if "bilibili.com" in u or "b23.tv" in u:
        return "bilibili"
    if "ximalaya.com" in u or "xmcdn.com" in u:
        return "ximalaya"
    if "douyin.com" in u or "iesdouyin.com" in u:
        return "douyin"
    if "kuaishou.com" in u or "kwaicdn.com" in u or "chenzhongtech.com" in u:
        return "kuaishou"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "qq.com" in u and "song" in u:
        return "qq"
    if "kugou.com" in u:
        return "kugou"
    if "kuwo.cn" in u:
        return "kuwo"
    if DIRECT_AUDIO.search(u):
        return "direct"
    return "generic"


def probe_url(url: str) -> dict:
    url = resolve_short(url)
    plat = guess_platform(url)
    out = {"kind": "url", "platform": plat, "url": url,
           "title": "", "artist": "", "thumb": "", "duration": 0,
           "ok": True, "message": ""}
    if plat == "netease":
        m = NETEASE_ID.search(url)
        if not m:
            out["ok"] = False
            out["message"] = "无法从链接里识别歌曲 ID"
            return out
        sid = m.group(1)
        out["song_id"] = sid
        try:
            d = netease_detail(sid)
            out.update({k: v for k, v in d.items() if v})
        except Exception:
            pass
        ok, msg = netease_probe(sid)
        out["ok"] = ok
        out["message"] = msg
        if not out["title"]:
            out["title"] = f"网易云歌曲 {sid}"
        return out
    if plat in ("qq", "kugou"):
        name = "QQ音乐" if plat == "qq" else "酷狗"
        out["ok"] = False
        out["platform"] = plat
        out["platform_id"] = PLT.extract_id(plat, url)
        out["message"] = (f"{name} 的音频流需要登录会员，本工具会改用「歌名 + 歌手」"
                          f"去 B站 / 网易云匹配同一首歌的高音质版本下载")
        return out
    if plat in VIDEO_SITE:
        out["kind"] = "video"
        out["ok"] = True
        name = "抖音" if plat == "douyin" else "快手"
        out["message"] = (f"{name} 视频：会开一个后台浏览器内核解析播放地址，"
                          f"下到的是去水印 mp4（不是音频）")
        return out
    if plat == "ximalaya":
        tid = PLT.extract_id("ximalaya", url)
        out["song_id"] = tid
        out["platform_id"] = tid
        if not tid:
            out["ok"] = False
            out["message"] = "无法从链接里识别喜马拉雅声音 ID"
            return out
        try:
            info = PLT.ximalaya_track(tid)
        except Exception as e:
            info = {"ok": False, "error": str(e)}
        out["title"] = info.get("title") or f"喜马拉雅音频 {tid}"
        out["artist"] = info.get("artist") or ""
        out["album"] = info.get("album") or ""
        out["thumb"] = info.get("cover") or ""
        out["duration"] = info.get("duration") or 0
        if info.get("ok"):
            out["message"] = "免费音频，可直接下载"
        else:
            out["ok"] = False
            out["message"] = info.get("error") or "取不到音频直链"
        return out
    if plat == "direct":
        out["title"] = Path(urllib.parse.urlparse(url).path).stem or "直链音频"
        try:
            resp = http_open(url, timeout=20)
            ctype = (resp.headers.get("Content-Type") or "")
            clen = resp.headers.get("Content-Length") or "?"
            resp.close()
            out["message"] = f"直链 · {ctype or '未知类型'} · {clen} bytes"
        except Exception as e:
            out["ok"] = False
            out["message"] = f"直链不可访问: {e}"
        return out
    # 其他站点交给 yt-dlp 探测
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "noprogress": True,
            "skip_download": True, "noplaylist": True, "socket_timeout": 15}
    if settings.get("proxy"):
        opts["proxy"] = settings["proxy"]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            if entries:
                info = entries[0]
        out["title"] = info.get("title") or ""
        out["artist"] = info.get("artist") or info.get("creator") or info.get("uploader") or ""
        out["duration"] = int(info.get("duration") or 0)
        out["thumb"] = info.get("thumbnail") or ""
        out["message"] = f"{plat} · 可解析"
    except Exception as e:
        out["ok"] = False
        msg = str(e)
        msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)
        out["message"] = msg.split("\n")[0][:220]
    return out


def netease_probe(song_id: str) -> tuple[bool, str]:
    """检查网易云外链是否真的能拿到音频。"""
    url = f"https://music.163.com/song/media/outer/url?id={song_id}.mp3"
    try:
        r = http_open(url, {"Referer": "https://music.163.com/"}, timeout=15)
        final = r.geturl()
        ctype = (r.headers.get("Content-Type") or "").lower()
        try:
            clen = int(r.headers.get("Content-Length") or 0)
        except Exception:
            clen = 0
        r.close()
    except urllib.error.HTTPError as he:
        return False, f"外链 HTTP {he.code}，该曲目可能无版权 / 需会员"
    except Exception as e:
        return False, f"外链检查失败: {e}"
    if "404" in final or "/404" in final:
        return False, "该曲目无版权 / 需会员，外链不可用"
    if clen and clen < 100 * 1024:
        return False, "外链返回内容过小，可能不可用"
    if ctype and "audio" not in ctype and "octet-stream" not in ctype:
        return False, f"外链返回类型 {ctype}，可能不可用"
    return True, "外链可下载" + (f" · {human_size(clen)}" if clen else "")


# ---------------------------------------------------------------- 下载引擎
def engine_netease(job: dict) -> tuple[Path, dict]:
    sid = job.get("song_id") or (NETEASE_ID.search(job.get("source", "")) or [None, ""])[1]
    if not sid:
        raise RuntimeError("缺少网易云歌曲 ID")
    meta = {}
    try:
        meta = netease_detail(str(sid))
    except Exception:
        pass
    url = f"https://music.163.com/song/media/outer/url?id={sid}.mp3"
    tmp = TMP / job["id"]
    tmp.mkdir(parents=True, exist_ok=True)
    raw = tmp / "source.mp3"
    try:
        stream_download(url, raw, job, {"Referer": "https://music.163.com/"},
                        max_bytes=80 * 1024 * 1024, verify_audio=True)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"网易云外链 HTTP {e.code}：该曲目可能无版权或需会员，换 B站 源试试")
    except RuntimeError as e:
        raise RuntimeError(f"{e}｜建议切到 B站 源重新下载")
    size = raw.stat().st_size
    if size < 100 * 1024:
        raise RuntimeError("下载内容过小（<100KB），该曲目大概率无版权 / 需会员，换 B站 源试试")
    title = job.get("title") or meta.get("title") or f"网易云 {sid}"
    artist = job.get("artist") or meta.get("artist") or ""
    album = meta.get("album") or ""
    job["stage"] = "写入中"
    job["progress"] = 99.0
    final = unique_path(DOWNLOADS / build_filename(title, artist, job.get("source", "")))
    if raw.suffix.lower() in MP3_EXT:
        shutil.copyfile(raw, final)
    else:
        transcode_to_mp3(raw, final, settings.get("quality", "320"))
    cover = fetch_cover(meta.get("thumb") or job.get("thumb") or "")
    write_tags(final, title, artist, album, cover)
    shutil.rmtree(tmp, ignore_errors=True)
    return final, {"title": title, "artist": artist, "album": album}


def engine_direct(job: dict) -> tuple[Path, dict]:
    url = job["source"]
    tmp = TMP / job["id"]
    tmp.mkdir(parents=True, exist_ok=True)
    name = Path(urllib.parse.urlparse(url).path).name or "audio"
    ext = Path(name).suffix.lower() or ".bin"
    raw = tmp / ("source" + ext)
    stream_download(url, raw, job, verify_audio=True)
    job["stage"] = "处理中"
    job["progress"] = 96.0
    final = unique_path(DOWNLOADS / build_filename(job.get("title") or Path(name).stem,
                                                   job.get("artist", ""), job.get("source", "")))
    if ext in MP3_EXT:
        shutil.copyfile(raw, final)
    else:
        job["stage"] = "转码中"
        transcode_to_mp3(raw, final, settings.get("quality", "320"))
    write_tags(final, job.get("title", ""), job.get("artist", ""))
    shutil.rmtree(tmp, ignore_errors=True)
    return final, {}


def _yt_progress(d: dict, job: dict):
    st = d.get("status")
    if st == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        got = d.get("downloaded_bytes") or 0
        job["stage"] = "下载中"
        if total:
            job["progress"] = round(min(95.0, got * 100.0 / total), 1)
        sp = d.get("speed")
        if sp:
            job["speed"] = human_speed(sp)
        eta = d.get("eta")
        if eta:
            job["eta"] = int(eta)
    elif st == "finished":
        job["progress"] = 96.0
        job["stage"] = "转码中"
        job["speed"] = ""


def _yt_pp(d: dict, job: dict):
    if d.get("status") == "started":
        name = (d.get("postprocessor") or "").replace("FFmpeg", "")
        job["stage"] = {"ExtractAudio": "转码中", "Metadata": "写标签",
                        "EmbedThumbnail": "嵌封面"}.get(name, "处理中")
        job["progress"] = 97.0


def ytdlp_options(job: dict, outdir: Path) -> dict:
    q = str(settings.get("quality") or "320")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(outdir / "%(title).80s.%(ext)s"),
        "ffmpeg_location": FFMPEG,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "windowsfilenames": True,
        "concurrent_fragment_downloads": 4,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 25,
        "progress_hooks": [lambda d: _yt_progress(d, job)],
        "postprocessor_hooks": [lambda d: _yt_pp(d, job)],
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": q},
        ],
    }
    if settings.get("write_tags"):
        opts["postprocessors"].append({"key": "FFmpegMetadata", "add_metadata": True,
                                       "add_chapters": False})
    if settings.get("embed_cover"):
        opts["writethumbnail"] = True
        opts["postprocessors"].append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    if settings.get("proxy"):
        opts["proxy"] = settings["proxy"]
    bc = (settings.get("browser_cookies") or "").strip()
    if bc:
        opts["cookiesfrombrowser"] = (bc,)
    return opts


def engine_ytdlp(job: dict) -> tuple[Path, dict]:
    import yt_dlp
    tmp = TMP / job["id"]
    tmp.mkdir(parents=True, exist_ok=True)
    opts = ytdlp_options(job, tmp)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(job["source"], download=True)
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("链接里没有可下载的音频")
        info = entries[0]
    mp3s = sorted(tmp.glob("*.mp3"), key=lambda p: p.stat().st_size, reverse=True)
    if not mp3s:
        cands = [p for p in tmp.iterdir()
                 if p.is_file() and p.suffix.lower() in AUDIO_EXT]
        if not cands:
            raise RuntimeError("下载完成但没有找到音频文件")
        src = max(cands, key=lambda p: p.stat().st_size)
        dest = unique_path(DOWNLOADS / clean_name(f"{job.get('artist','')} - {job.get('title','')}.mp3"
                                                 if job.get("artist") else f"{src.stem}.mp3"))
        transcode_to_mp3(src, dest, settings.get("quality", "320"))
        final = dest
    else:
        src = mp3s[0]
        title = job.get("title") or info.get("title") or src.stem
        artist = job.get("artist") or info.get("artist") or info.get("creator") or ""
        final = unique_path(DOWNLOADS / build_filename(title, artist, job.get("source", "")))
        shutil.move(str(src), str(final))
    title = job.get("title") or info.get("title") or final.stem
    artist = job.get("artist") or info.get("artist") or info.get("creator") or ""
    shutil.rmtree(tmp, ignore_errors=True)
    return final, {"title": title, "artist": artist,
                   "duration": int(info.get("duration") or 0),
                   "thumb": info.get("thumbnail") or ""}


def engine_ximalaya(job: dict) -> tuple[Path, dict]:
    """喜马拉雅：取音频直链（多为 m4a）→ 转 mp3。VIP 付费内容在这里报错。"""
    tid = (str(job.get("song_id") or job.get("platform_id") or "").strip()
           or PLT.extract_id("ximalaya", job.get("source") or ""))
    if not tid:
        raise RuntimeError("缺少喜马拉雅 track id")
    update_job(job["id"], stage="解析音频地址", progress=3.0)
    info = PLT.ximalaya_track(tid)
    if not info.get("ok"):
        raise RuntimeError(info.get("error") or "取不到音频直链")
    url = info["url"]
    # 用接口返回的真实标题补全元数据（用户可能只传了 id）
    if info.get("title") and not job.get("title"):
        job["title"] = info["title"]
    if info.get("duration") and not job.get("duration"):
        job["duration"] = info["duration"]
    if info.get("cover") and not job.get("thumb"):
        job["thumb"] = info["cover"]
    tmpdir = TMP / job["id"]
    tmpdir.mkdir(parents=True, exist_ok=True)
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower() or ".m4a"
    raw = tmpdir / ("source" + ext)
    stream_download(url, raw, job, {"Referer": "https://www.ximalaya.com/"},
                    max_bytes=500 * 1024 * 1024, verify_audio=True)
    base = job.get("title") or Path(raw).stem
    final = unique_path(DOWNLOADS / clean_name(base + ".mp3"))
    if raw.suffix.lower() in MP3_EXT:
        shutil.copyfile(raw, final)
    else:
        transcode_to_mp3(raw, final)
    return final, {"title": base, "artist": job.get("artist", ""),
                   "ximalaya_id": tid}


def engine_video(job: dict) -> tuple[Path, dict]:
    """抖音 / 快手：用真实浏览器（CDP）抓播放直链，原样存 mp4。

    这两个站点的接口都要前端 JS 生成的设备指纹，纯 HTTP 打不通，
    所以交给 browser_cdp 开一个 Edge 把页面跑起来，从网络响应里取地址。
    """
    import browser_cdp

    src_url = job.get("source") or ""
    update_job(job["id"], stage="启动浏览器解析", progress=4.0)
    info = browser_cdp.fetch_video_info(src_url)
    # 浏览器解析偶尔抓空（多实例抢资源 / 页面还没渲染完）→ 自动重试两轮，别让用户看到无谓的失败
    if not info.get("ok"):
        for _ in range(2):
            update_job(job["id"], stage="重试解析（浏览器没抓到）", progress=5.0)
            again = browser_cdp.fetch_video_info(src_url)
            if again.get("ok"):
                info = again
                break
    # 快手未登录时详情接口可能只吐推荐流里的别的视频 —— 宁可重试/报错也不下错片
    if info.get("platform") == "kuaishou" and not info.get("exact"):
        for _ in range(2):
            update_job(job["id"], stage="重试解析（校验作品 id）", progress=5.0)
            again = browser_cdp.fetch_video_info(src_url)
            if again.get("ok") and again.get("exact"):
                info = again
                break
    if not info.get("ok"):
        raise RuntimeError(info.get("error") or "解析视频地址失败")
    if info.get("platform") == "kuaishou" and not info.get("exact"):
        raise RuntimeError("快手没返回这条作品的数据（多为未登录 / 仅粉丝可见 / 已删除）。"
                           "为免下错成推荐流里的其他视频，已中止。可重试，或先在浏览器里登录快手再试")

    if info.get("title"):
        job["title"] = info["title"]
    if info.get("duration") and not job.get("duration"):
        job["duration"] = info["duration"]

    tmpdir = TMP / job["id"]
    tmpdir.mkdir(parents=True, exist_ok=True)
    raw = tmpdir / "video.mp4"

    update_job(job["id"], stage="下载视频", progress=8.0)
    stream_download(
        info["url"], raw, job,
        {"Referer": src_url, "User-Agent": browser_cdp.UA},
        max_bytes=1500 * 1024 * 1024,
    )

    base = pretty_title(job.get("title") or "") or info.get("platform") or "video"
    final = unique_path(DOWNLOADS / clean_name(base + ".mp4"))
    shutil.move(str(raw), str(final))

    meta = video_meta(final)
    if not meta.get("duration"):
        final.unlink(missing_ok=True)
        raise RuntimeError("下到的文件里没有视频数据，已删除")

    return final, {"title": base, "artist": job.get("artist", ""),
                   "platform": info.get("platform", ""),
                   "video_id": info.get("id", "")}


def engine_local(job: dict) -> tuple[Path, dict]:
    src = Path(job["source"])
    if not src.exists():
        raise RuntimeError("本地文件不存在")
    # 上传时用的原始文件名放在 title 里，优先用它
    base = (job.get("title") or "").strip() or src.stem
    base = Path(base).stem or src.stem
    final = unique_path(DOWNLOADS / clean_name(base + ".mp3"))
    if src.suffix.lower() == ".mp3":
        shutil.copyfile(src, final)
    else:
        job["stage"] = "转码中"
        job["progress"] = 40.0
        transcode_to_mp3(src, final, settings.get("quality", "320"))
    return final, {"title": base}


# 换源严格度：宁可下不成，也不下错
MIN_FALLBACK_SCORE = 60.0   # 低于此分视为不是同一首
MAX_DURATION_GAP = 0.10     # 时长容差 10%（且至少 12 秒）
MIN_DURATION_TOL = 12.0


def _judge_hit(hit: dict, nt_title: str, artist_n: str, want: float):
    """严格判定候选是不是同一首。返回 (score, reason)：score<=0 即拒绝。"""
    t = hit.get("title") or ""
    nt = _norm(t)
    d = float(hit.get("duration") or 0)
    if not nt:
        return 0.0, "标题为空"
    if BAD_TITLE.search(t):
        return 0.0, "翻唱/伴奏/剪辑版「%s」" % t[:36]
    if want and d:
        tol = max(MIN_DURATION_TOL, want * MAX_DURATION_GAP)
        if abs(d - want) > tol:
            return 0.0, "时长不符 %ds≠%ds「%s」" % (round(d), round(want), t[:28])
    exact = bool(nt_title) and nt == nt_title
    contains = bool(nt_title) and len(nt_title) >= 2 and (nt_title in nt)
    artist_ok = bool(artist_n) and artist_n in nt
    if not (exact or contains or artist_ok):
        return 0.0, "名字对不上「%s」" % t[:36]
    s = 0.0
    if exact:
        s += 100.0
    elif contains:
        s += 55.0
    if artist_ok:
        s += 35.0
    if want and d:
        s += max(0.0, 20.0 - abs(d - want) * 2.0)
    if s < MIN_FALLBACK_SCORE:
        return 0.0, "相关度太低(%d)「%s」" % (round(s), t[:36])
    return s, ""


def fallback_to_bilibili(job: dict) -> bool:
    """版权平台 / 失效链接的统一兜底：先 B 站，再网易云免费曲。

    **严格模式**：标题/歌手对不上、时长跑偏、翻唱剪辑版一律拒绝——
    宁可报失败，也不给用户下错歌。命中后改 job 的 engine/source，返回 True；
    全部拒绝时把原因写进 job["_fallback_reject"]。
    """
    title = (job.get("title") or "").strip()
    if not title:
        job["_fallback_reject"] = ["没有歌名，无法匹配"]
        return False
    artist = (job.get("artist") or "").replace("、", "/").replace("&", "/").split("/")[0].strip()
    q = f"{title} {artist}".strip()
    want = float(job.get("duration") or 0)
    nt_title = _norm(title)
    artist_n = _norm(artist)
    rejects = []

    def pick(hits, bonus=None, tag=""):
        best, best_score, why_first = None, 0.0, ""
        for h in hits:
            d = h.get("duration") or 0
            if not d or d < 20:
                continue
            s, why = _judge_hit(h, nt_title, artist_n, want)
            if s <= 0:
                if why and not why_first:
                    why_first = why
                continue
            if bonus:
                s += bonus(h)
            if s > best_score:
                best_score, best = s, h
        if not best and why_first:
            rejects.append("%s %s" % (tag, why_first))
        return best

    # 1) B 站（覆盖面最大，音质好）
    try:
        best = pick(bilibili_search(q, 10),
                    lambda h: min((h.get("plays") or 0) / 1e6, 5), "B站")
        if best and best.get("source"):
            job["_fallback_from"] = job.get("_fallback_from") or job.get("engine") or ""
            job["_fallback_via"] = "B站"
            job["engine"] = "ytdlp"
            job["source"] = best["source"]
            job["thumb"] = job.get("thumb") or best.get("thumb") or ""
            return True
    except Exception as e:
        rejects.append("B站搜索失败 %s" % str(e)[:60])

    # 2) 网易云免费曲（外链可直取）
    try:
        hits = [h for h in netease_search(q, 12) if not h.get("vip")]
        best = pick(hits, None, "网易云")
        if best and best.get("source"):
            job["_fallback_from"] = job.get("_fallback_from") or job.get("engine") or ""
            job["_fallback_via"] = "网易云"
            job["engine"] = "netease"
            job["source"] = best["source"]
            job["song_id"] = str(best.get("id") or "")
            job["thumb"] = job.get("thumb") or best.get("thumb") or ""
            return True
    except Exception as e:
        rejects.append("网易云搜索失败 %s" % str(e)[:60])

    job["_fallback_reject"] = rejects or ["没有找到任何候选"]
    return False


def run_job(jid: str):
    with _jobs_lock:
        job = jobs.get(jid)
        if not job:
            return
        job["status"] = "running"
        job["stage"] = "准备中"
        job["progress"] = 0.0
        job["started"] = now_ts()
        snapshot = dict(job)
    persist_jobs()
    engine = snapshot.get("engine") or "ytdlp"
    fell_back = False
    # 版权平台（QQ音乐 / 酷狗）只提供元数据，创建时标记 fallback：直接换源，不做无用下载
    if engine == "fallback":
        update_job(jid, stage="匹配可用音源", progress=1.0)
        if fallback_to_bilibili(job):
            engine = job.get("engine") or "ytdlp"
            fell_back = True
            update_job(jid, engine=engine, source=job.get("source"),
                       song_id=job.get("song_id") or "", thumb=job.get("thumb"),
                       stage="已从 %s 获取同曲" % (job.get("_fallback_via") or "其他源"),
                       progress=0.0, error="")
            print("[FB] %s 版权源 → %s：%s" % (jid, job.get("_fallback_via"), job.get("source")))
        else:
            why = "；".join((job.get("_fallback_reject") or [])[:2])
            update_job(jid, status="error", stage="失败", finished=now_ts(),
                       speed="", eta=0,
                       error="该曲目在版权平台无法直接获取，也没找到可信的同曲音源"
                             + ("（%s）" % why if why else "")
                             + "。宁可不下也不下错——换个更准确的歌名，或直接切到「B站」源搜索")
            record_history({
                "id": jid, "title": job.get("title") or "",
                "artist": job.get("artist") or "",
                "filename": job.get("filename") or "",
                "platform": job.get("platform") or job.get("engine") or "",
                "source": job.get("source") or "",
                "status": "error",
                "error": ("换源无可信匹配：" + why) if why else "版权平台无可用音源",
                "size": 0, "duration": job.get("duration") or 0,
                "finished": now_ts(),
            })
            persist_jobs()
            return
    while True:
        try:
            if engine == "netease":
                final, meta = engine_netease(job)
            elif engine == "direct":
                final, meta = engine_direct(job)
            elif engine == "ximalaya":
                final, meta = engine_ximalaya(job)
            elif engine == "video":
                final, meta = engine_video(job)
            elif engine == "local":
                final, meta = engine_local(job)
            else:
                final, meta = engine_ytdlp(job)
            is_video = final.suffix.lower() in VIDEO_EXT
            if is_video:
                am = video_meta(final)
                if not am.get("duration"):
                    try:
                        final.unlink()
                    except Exception:
                        pass
                    raise RuntimeError("下到的文件里没有视频数据，已删除")
                update_job(jid, status="done", progress=100.0, stage="完成",
                           filename=final.name, finished=now_ts(), speed="", eta=0,
                           out_size=am.get("size"),
                           duration=am.get("duration") or job.get("duration"))
                print(f"[OK] {final.name}  ({human_size(am.get('size'))}) 视频")
                record_history({
                    "id": jid, "title": job.get("title") or "",
                    "artist": job.get("artist") or "", "filename": final.name,
                    "platform": job.get("platform") or job.get("engine") or "",
                    "source": job.get("source") or "", "status": "done",
                    "size": am.get("size") or 0,
                    "duration": am.get("duration") or job.get("duration") or 0,
                    "finished": now_ts(),
                })
                break
            # 用任务里的歌名/歌手覆盖平台标签，避免 UP 主/营销标题污染 ID3
            if settings.get("write_tags"):
                t_title = pretty_title(job.get("title") or meta.get("title") or "")
                t_artist = (job.get("artist") or meta.get("artist") or "").strip()
                t_album = (job.get("album") or "").strip()
                cover = fetch_cover(job.get("thumb") or meta.get("thumb") or "")
                write_tags(final, t_title, t_artist, t_album, cover=cover)
            am = audio_meta(final)
            # 兜底：产物解析不出时长 = 里面没有可播的音频帧（常见于外链返回了失效页面）
            if not am.get("duration"):
                try:
                    final.unlink()
                except Exception:
                    pass
                raise RuntimeError("产物里没有音频数据（文件只剩封面），已删除，换其他源重试")
            update_job(jid, status="done", progress=100.0, stage="完成",
                       filename=final.name, finished=now_ts(), speed="", eta=0,
                       out_size=am.get("size"),
                       duration=am.get("duration") or job.get("duration"))
            print(f"[OK] {final.name}  ({human_size(am.get('size'))})")
            record_history({
                "id": jid, "title": job.get("title") or "",
                "artist": job.get("artist") or "", "filename": final.name,
                "platform": job.get("platform") or job.get("engine") or "",
                "source": job.get("source") or "", "status": "done",
                "size": am.get("size") or 0,
                "duration": am.get("duration") or job.get("duration") or 0,
                "finished": now_ts(),
            })
            break
        except Exception as e:
            msg = str(e)
            msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)
            # 网易云 / 直链 / 喜马拉雅失效 → 自动去 B 站捞一条同曲重试，用户不用手动换源
            if not fell_back and engine in ("netease", "direct", "ximalaya"):
                if fallback_to_bilibili(job):
                    engine = job.get("engine") or "ytdlp"
                    fell_back = True
                    update_job(jid, engine=engine, source=job.get("source"),
                               song_id=job.get("song_id") or "",
                               thumb=job.get("thumb"),
                               stage="换 %s 源重试" % (job.get("_fallback_via") or "B站"),
                               progress=0.0, error="")
                    print("[FB] %s 换 %s 源：%s" % (jid, job.get("_fallback_via"), job.get("source")))
                    continue
                else:
                    why = "；".join((job.get("_fallback_reject") or [])[:2])
                    if why:
                        msg += "｜自动换源也放弃了（%s）——宁可不下也不下错" % why
            low = msg.lower()
            if "sign in" in low or "login" in low or "cookie" in low or "private" in low:
                msg += "  （可在设置里开启「浏览器 Cookie」）"
            elif "timed out" in low or "timeout" in low:
                msg += "  （网络超时，可在设置里配置代理）"
            elif "unable to download" in low and "403" in msg:
                msg += "  （403 拒绝，可能需要在设置里配置 Cookie 或代理）"
            update_job(jid, status="error", error=msg[:600], stage="失败",
                       finished=now_ts(), speed="", eta=0)
            record_history({
                "id": jid, "title": job.get("title") or "",
                "artist": job.get("artist") or "",
                "filename": job.get("filename") or "",
                "platform": job.get("platform") or job.get("engine") or "",
                "source": job.get("source") or "",
                "status": "error",
                "error": msg[:200],
                "size": 0, "duration": job.get("duration") or 0,
                "finished": now_ts(),
            })
            print(f"[ERR] {jid} {msg[:200]}")
            try:
                shutil.rmtree(TMP / jid, ignore_errors=True)
            except Exception:
                pass
            break
    with _jobs_lock:
        if jid in jobs:
            jobs[jid]["speed"] = ""
            if jobs[jid].get("status") == "running":
                jobs[jid]["status"] = "error"
                jobs[jid]["error"] = jobs[jid].get("error") or "任务异常结束"
    persist_jobs()


def create_job(payload: dict) -> dict:
    src = (payload.get("source") or payload.get("url") or "").strip()
    if not src:
        raise ValueError("缺少 source")
    # 分享短链（v.douyin.com / b23.tv / 163cn.tv）先跟跳到真实地址
    if classify(src) == "url":
        src = resolve_short(src)
    engine = payload.get("engine") or "auto"
    plat_hint = (payload.get("platform") or "").strip().lower()
    if engine == "auto":
        if plat_hint == "ximalaya":
            engine = "ximalaya"
        elif plat_hint in ("qq", "kugou"):
            # 版权平台：只拿元数据，下载阶段自动换源
            engine = "fallback"
        elif plat_hint == "netease":
            engine = "netease"
        elif plat_hint in VIDEO_SITE:
            engine = "video"
        else:
            plat = guess_platform(src) if classify(src) == "url" else "local"
            engine = {"netease": "netease", "direct": "direct",
                      "ximalaya": "ximalaya",
                      "douyin": "video", "kuaishou": "video"}.get(plat, "ytdlp")
            if classify(src) == "path":
                engine = "local"
    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid,
        "source": src,
        "engine": engine,
        "platform": plat_hint or (guess_platform(src) if classify(src) == "url" else ""),
        "platform_id": payload.get("platform_id") or "",
        "song_id": (payload.get("platform_id") or payload.get("song_id") or ""),
        "title": payload.get("title") or "",
        "artist": payload.get("artist") or "",
        "album": payload.get("album") or "",
        "thumb": payload.get("thumb") or "",
        "duration": int(payload.get("duration") or 0),
        "status": "queued",
        "stage": "排队中",
        "progress": 0.0,
        "speed": "",
        "eta": 0,
        "filename": "",
        "error": "",
        "created": now_ts(),
        "finished": 0,
    }
    with _jobs_lock:
        jobs[jid] = job
    persist_jobs()
    _pool.submit(run_job, jid)
    return job


# ---------------------------------------------------------------- 文件库
HISTORY_FILE = DATA / "history.json"
_history_lock = threading.Lock()


def record_history(entry: dict) -> None:
    """把一次结束（成功/失败）记进 data/history.json，最多留 500 条。"""
    with _history_lock:
        raw = load_json(HISTORY_FILE, {"items": []})
        items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            items = []
        items.insert(0, entry)
        save_json(HISTORY_FILE, {"items": items[:500]})


def read_history(limit: int = 200) -> list[dict]:
    raw = load_json(HISTORY_FILE, {"items": []})
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    return items[:limit]


def purge_finished_jobs() -> int:
    """已完成/已失败的任务，超过 purge_days 天后从任务列表彻底删除。

    purge_days = 0 表示永不删除。只删任务记录，不动磁盘上的文件和下载记录。
    """
    try:
        days = int(settings.get("purge_days") or 0)
    except Exception:
        days = 0
    if days <= 0:
        return 0
    cutoff = now_ts() - days * 86400
    removed = 0
    with _jobs_lock:
        for jid, j in list(jobs.items()):
            if j.get("status") in ("running", "queued", "pending"):
                continue
            ts = j.get("finished") or j.get("created") or 0
            try:
                ts = float(ts)
            except Exception:
                continue
            if ts and ts < cutoff:
                jobs.pop(jid, None)
                removed += 1
    if removed:
        persist_jobs()
        print(f"[purge] 清理 {removed} 个超过 {days} 天的历史任务")
    return removed


def move_files_to(target: str, names=None) -> dict:
    """把当前下载目录里的成品搬到 target；names 为空则全搬。"""
    tgt = Path(str(target)).expanduser()
    tgt.mkdir(parents=True, exist_ok=True)
    src = DOWNLOADS
    if tgt.resolve() == src.resolve():
        return {"ok": False, "error": "目标目录和当前下载目录是同一个"}
    want = set(names or [])
    files = []
    try:
        for p in src.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lower() not in (MP3_EXT | VIDEO_EXT):
                continue
            if want and p.name not in want:
                continue
            files.append(p)
    except Exception as e:
        return {"ok": False, "error": f"读不到源目录: {e}"}
    moved, failed = [], []
    for p in files:
        dst = tgt / p.name
        if dst.exists():
            stem, suf, i = p.stem, p.suffix, 2
            while dst.exists():
                dst = tgt / f"{stem} ({i}){suf}"
                i += 1
        try:
            shutil.move(str(p), str(dst))
            moved.append(p.name)
        except Exception as e:
            failed.append(f"{p.name}: {e}")
    return {"ok": True, "moved": len(moved), "names": moved,
            "failed": failed, "target": str(tgt)}


def pick_folder(initial: str = "") -> str:
    """弹系统选择目录对话框（PowerShell + WinForms）。

    注意：对话框标题/描述用英文，避免 PowerShell 子进程按 GBK 解释 UTF-8 中文导致乱码。
    """
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "[System.Windows.Forms.Application]::EnableVisualStyles();"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description = 'Select download folder';"
        "$d.UseDescriptionForTitle = $true;"
        "$d.ShowNewFolderButton = $true;"
        + (f"$d.SelectedPath = '{initial}';" if initial else "") +
        "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True, text=True, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (r.stdout or "").strip()
    except Exception as e:
        print(f"[warn] 打开目录选择器失败: {e}")
        return ""


def list_files() -> list[dict]:
    out = []
    try:
        for p in sorted(DOWNLOADS.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            ext = p.suffix.lower()
            if not p.is_file() or (ext not in MP3_EXT and ext not in VIDEO_EXT):
                continue
            if ext in VIDEO_EXT:
                vm = video_meta(p)
                out.append({
                    "name": p.name,
                    "size": vm.get("size", 0),
                    "size_h": human_size(vm.get("size", 0)),
                    "duration": vm.get("duration", 0),
                    "bitrate": vm.get("bitrate", 0),
                    "mtime": vm.get("mtime", 0),
                    "width": vm.get("width", 0),
                    "height": vm.get("height", 0),
                    "kind": "video",
                    "title": p.stem,
                    "artist": "",
                })
                continue
            info = audio_meta(p)
            tags_title, tags_artist = "", ""
            try:
                from mutagen import File as MFile
                f = MFile(str(p))
                if f and f.tags:
                    t = f.tags.get("TIT2") or f.tags.get("title")
                    a = f.tags.get("TPE1") or f.tags.get("artist")
                    tags_title = str(t[0]) if isinstance(t, list) else (str(t) if t else "")
                    tags_artist = str(a[0]) if isinstance(a, list) else (str(a) if a else "")
            except Exception:
                pass
            out.append({
                "name": p.name,
                "size": info.get("size", 0),
                "size_h": human_size(info.get("size", 0)),
                "duration": info.get("duration", 0),
                "bitrate": info.get("bitrate", 0),
                "mtime": info.get("mtime", 0),
                "title": tags_title or p.stem,
                "artist": tags_artist,
                "kind": "audio",
            })
    except Exception:
        pass
    return out


def safe_media_path(name: str) -> Path | None:
    name = os.path.basename(name or "")
    if not name or name.startswith("."):
        return None
    p = (DOWNLOADS / name).resolve()
    try:
        p.relative_to(DOWNLOADS.resolve())
    except Exception:
        return None
    return p if p.exists() and p.is_file() else None


# ---------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    server_version = "MusicDesk/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 基础响应
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8",
              extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def err(self, msg, code=400):
        self.json({"ok": False, "error": str(msg)}, code)

    def read_body(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        return self.rfile.read(n) if n > 0 else b""

    def read_json(self) -> dict:
        raw = self.read_body()
        if not raw:
            return {}
        try:
            v = json.loads(raw.decode("utf-8"))
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}

    # ---- GET
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, qs = u.path, urllib.parse.parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                return self.serve_web("index.html")
            if path in ("/m", "/m/", "/m.html"):
                return self.serve_web("m.html")
            if path.startswith("/static/"):
                return self.serve_web(path[len("/static/"):])
            if path == "/api/ping":
                return self.json({"ok": True, "ffmpeg": bool(FFMPEG), "version": UPD.APP_VERSION})
            if path == "/api/settings":
                return self.json({"ok": True, "settings": settings, "ffmpeg": FFMPEG})
            if path == "/api/jobs":
                purge_finished_jobs()
                with _jobs_lock:
                    items = [public_job(jobs[k]) for k in
                             sorted(jobs, key=lambda x: jobs[x].get("created", 0), reverse=True)]
                return self.json({"ok": True, "jobs": items})
            if path.startswith("/api/jobs/"):
                jid = path[len("/api/jobs/"):].split("/")[0]
                with _jobs_lock:
                    j = jobs.get(jid)
                if not j:
                    return self.err("任务不存在", 404)
                return self.json({"ok": True, "job": public_job(j)})
            if path == "/api/search":
                q = (qs.get("q") or [""])[0]
                src = (qs.get("src") or ["netease"])[0]
                limit = int((qs.get("limit") or ["18"])[0])
                if not q.strip():
                    return self.err("缺少关键词")
                try:
                    if src == "web":
                        items = web_search(clean_query(q), limit)
                    elif src == "auto":
                        items = search_all(clean_query(q), limit)
                    elif src == "bilibili":
                        items = bilibili_search(clean_query(q), limit)
                    elif src in ("douyin", "kuaishou"):
                        import video_search as VS
                        fn = VS.kuaishou_search if src == "kuaishou" else VS.douyin_search
                        try:
                            items = fn(clean_query(q), limit) or []
                        except Exception:
                            items = []
                        if not items:
                            plat = "抖音" if src == "douyin" else "快手"
                            return self.json({
                                "ok": True, "items": [], "src": src,
                                "notice": (
                                    f"{plat}的网页搜索要过签名和设备指纹，网页端拿不到稳定结果。"
                                    f"把手机 App 里的分享文案整段粘到上方输入框就行 —— "
                                    f"形如「复制打开{plat}… https://v.…」，会自动识别平台并下成 mp4。"
                                ),
                            })
                    elif src in ("qq", "kugou", "ximalaya"):
                        items = PLT.search(src, clean_query(q), limit)
                    else:
                        items = netease_search(q.strip(), limit)
                except Exception as e:
                    return self.json({"ok": False, "error": f"{src} 搜索失败: {e}",
                                      "items": []})
                return self.json({"ok": True, "items": items, "src": src})
            if path == "/api/thumb":
                return self.serve_thumb((qs.get("u") or [""])[0])
            if path == "/api/probe":
                url = (qs.get("url") or [""])[0]
                if not url:
                    return self.err("缺少 url")
                return self.json({"ok": True, "probe": probe_url(url)})
            if path == "/api/history":
                limit = int((qs.get("limit") or ["200"])[0])
                return self.json({"ok": True, "items": read_history(limit)})
            if path == "/api/update":
                src = str(settings.get("update_source") or "")
                return self.json({"ok": True, **UPD.check_update(src),
                                  "version": UPD.APP_VERSION})
            if path == "/api/net":
                h, p = self.server.server_address[:2]
                ip = lan_ip()
                lan_on = str(h) not in ("127.0.0.1", "localhost", "")
                return self.json({
                    "ok": True, "host": str(h), "port": int(p), "ip": ip,
                    "lan_enabled": lan_on,
                    "mobile_url": f"http://{ip}:{p}/m" if (ip and lan_on) else "",
                    "local_url": f"http://127.0.0.1:{p}/",
                })
            if path == "/api/qr":
                txt = (qs.get("t") or [""])[0].strip()
                if not txt:
                    h, p = self.server.server_address[:2]
                    ip = lan_ip()
                    txt = f"http://{ip}:{p}/m" if ip else f"http://127.0.0.1:{p}/"
                try:
                    svg = qr_svg(txt)
                except Exception as e:
                    return self.err(f"二维码生成失败: {e}", 500)
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(svg)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(svg)
                return
            if path == "/api/files":
                return self.json({"ok": True, "files": list_files()})
            if path == "/api/stats":
                with _jobs_lock:
                    running = sum(1 for j in jobs.values() if j.get("status") == "running")
                    queued = sum(1 for j in jobs.values() if j.get("status") == "queued")
                    done = sum(1 for j in jobs.values() if j.get("status") == "done")
                files = list_files()
                total = sum(f.get("size") or 0 for f in files)
                return self.json({"ok": True, "running": running, "queued": queued,
                                  "done": done, "files": len(files),
                                  "total_size": total, "total_size_h": human_size(total)})
            if path.startswith("/media/"):
                name = urllib.parse.unquote(path[len("/media/"):])
                return self.serve_media(name, dl=("1" in (qs.get("dl") or [])))
            return self.err("Not Found", 404)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except Exception as e:
            traceback.print_exc()
            return self.err(f"服务端异常: {e}", 500)

    # ---- POST
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        path, qs = u.path, urllib.parse.parse_qs(u.query)
        try:
            if path == "/api/jobs":
                data = self.read_json()
                items = data.get("items")
                created = []
                if isinstance(items, list) and items:
                    for it in items:
                        try:
                            created.append(public_job(create_job(it)))
                        except Exception as e:
                            created.append({"error": str(e)})
                else:
                    created.append(public_job(create_job(data)))
                return self.json({"ok": True, "jobs": created})
            if path == "/api/update/apply":
                self.read_json()
                src = str(settings.get("update_source") or "")
                stage = UPD.stage_update(src)
                if not stage.get("ok"):
                    return self.json({"ok": False, "error": stage.get("error")})
                _UPDATE_STAGE.clear()
                _UPDATE_STAGE.update(stage)
                return self.json({"ok": True, "version": stage.get("version"),
                                  "notes": stage.get("notes"),
                                  "message": "更新包已就绪，点「重启并安装」"})
            if path == "/api/update/install":
                self.read_json()
                if not _UPDATE_STAGE.get("ok"):
                    return self.json({"ok": False, "error": "还没有下载好的更新包"})
                res = UPD.apply_update(dict(_UPDATE_STAGE))
                if res.get("ok"):
                    threading.Thread(target=_delayed_exit, daemon=True).start()
                return self.json(res)
            if path == "/api/settings":
                data = self.read_json()
                s = save_settings(data)
                return self.json({"ok": True, "settings": s})
            if path == "/api/files/move":
                data = self.read_json() or {}
                target = str(data.get("target") or "").strip()
                if not target:
                    return self.json({"ok": False, "error": "缺少 target 目录"})
                try:
                    res = move_files_to(target, data.get("names"))
                except Exception as e:
                    return self.json({"ok": False, "error": f"搬移失败: {e}"})
                return self.json(res)
            if path == "/api/pick-folder":
                data = self.read_json() or {}
                got = pick_folder(str(data.get("initial") or "").strip())
                return self.json({"ok": bool(got), "path": got})
            if path == "/api/open-folder":
                try:
                    if os.name == "nt":
                        os.startfile(str(DOWNLOADS))  # noqa: S606
                    else:
                        subprocess.Popen(["xdg-open", str(DOWNLOADS)])
                except Exception as e:
                    return self.err(f"无法打开目录: {e}")
                return self.json({"ok": True, "path": str(DOWNLOADS)})
            if path == "/api/files/rename":
                data = self.read_json()
                old = os.path.basename(data.get("name") or "")
                new = clean_name(data.get("new") or "")
                src = safe_media_path(old)
                if not src:
                    return self.err("文件不存在", 404)
                if not new:
                    return self.err("新名字为空")
                if not new.lower().endswith(".mp3"):
                    new += ".mp3"
                dest = unique_path(DOWNLOADS / new)
                os.replace(src, dest)
                return self.json({"ok": True, "name": dest.name})
            if path == "/api/upload":
                name = urllib.parse.unquote((qs.get("name") or ["upload.bin"])[0])
                name = os.path.basename(name)
                ext = Path(name).suffix.lower()
                if ext and ext not in AUDIO_EXT:
                    return self.err(f"不支持的格式 {ext}")
                raw = self.read_body()
                if not raw:
                    return self.err("文件为空")
                TMP.mkdir(parents=True, exist_ok=True)
                up = TMP / ("upload_" + uuid.uuid4().hex[:8] + (ext or ".bin"))
                with open(up, "wb") as f:
                    f.write(raw)
                job = create_job({"source": str(up), "engine": "local",
                                  "title": Path(name).stem})
                return self.json({"ok": True, "job": public_job(job)})
            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                jid = path[len("/api/jobs/"):].split("/")[0]
                with _jobs_lock:
                    j = jobs.get(jid)
                    if j and j.get("status") in ("queued", "running"):
                        j["cancel"] = True
                        j["status"] = "error"
                        j["error"] = "已取消"
                        j["stage"] = "已取消"
                persist_jobs()
                return self.json({"ok": True})
            return self.err("Not Found", 404)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except Exception as e:
            traceback.print_exc()
            return self.err(f"服务端异常: {e}", 500)

    def do_DELETE(self):
        u = urllib.parse.urlparse(self.path)
        path, qs = u.path, urllib.parse.parse_qs(u.query)
        try:
            if path == "/api/history":
                save_json(HISTORY_FILE, {"items": []})
                return self.json({"ok": True, "cleared": True})  # DELETE_HISTORY_MARK
            if path == "/api/files":
                name = (qs.get("name") or [""])[0]
                p = safe_media_path(name)
                if not p:
                    return self.err("文件不存在", 404)
                p.unlink()
                return self.json({"ok": True})
            if path.startswith("/api/jobs/"):
                jid = path[len("/api/jobs/"):]
                with _jobs_lock:
                    jobs.pop(jid, None)
                persist_jobs()
                return self.json({"ok": True})
            return self.err("Not Found", 404)
        except Exception as e:
            return self.err(str(e), 500)

    # ---- 静态资源
    def serve_web(self, rel: str):
        rel = rel.split("?")[0].strip("/")
        if not rel:
            rel = "index.html"
        p = (WEB / rel).resolve()
        try:
            p.relative_to(WEB.resolve())
        except Exception:
            return self.err("Forbidden", 403)
        if not p.exists() or not p.is_file():
            return self.err("Not Found", 404)
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if p.suffix in (".js",):
            ctype = "application/javascript; charset=utf-8"
        elif p.suffix in (".css",):
            ctype = "text/css; charset=utf-8"
        elif p.suffix in (".html",):
            ctype = "text/html; charset=utf-8"
        self._send(200, p.read_bytes(), ctype)

    # ---- 音频（Range）
    def serve_thumb(self, url: str):
        """代理平台封面图（B站/网易云/QQ/酷狗/喜马拉雅都有防盗链，浏览器直连会 403）。"""
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        allowed = ("hdslb.com", "bilibili.com", "126.net", "music.163.com", "163.com",
                   "y.qq.com", "qq.com", "gtimg.com", "kugou.com",
                   "xmcdn.com", "ximalaya.com")
        if not url or not any(host.endswith(d) for d in allowed):
            return self.err("不允许的图片源", 403)
        if "hdslb" in host or "bili" in host:
            ref = "https://www.bilibili.com/"
        elif "qq.com" in host or "gtimg" in host:
            ref = "https://y.qq.com/"
        elif "kugou" in host:
            ref = "https://www.kugou.com/"
        elif "xmcdn" in host or "ximalaya" in host:
            ref = "https://www.ximalaya.com/"
        else:
            ref = "https://music.163.com/"
        try:
            resp = http_open(url, {"Referer": ref, "User-Agent": UA}, timeout=15)
            data = resp.read(3 * 1024 * 1024)
            resp.close()
        except Exception as e:
            return self.err(f"封面获取失败: {e}", 502)
        self.send_response(200)
        self.send_header("Content-Type", resp.headers.get("Content-Type") or "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def serve_media(self, name: str, dl: bool = False):
        p = safe_media_path(name)
        if not p:
            return self.err("文件不存在", 404)
        size = p.stat().st_size
        ctype = mimetypes.guess_type(str(p))[0] or "audio/mpeg"
        if ctype == "audio/mp3":
            ctype = "audio/mpeg"
        if p.suffix.lower() in (".mp4", ".m4v"):
            ctype = "video/mp4"
        rng = self.headers.get("Range") or ""
        start, end, partial = 0, size - 1, False
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                partial = True
        length = max(0, end - start + 1)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if dl:
            fn = urllib.parse.quote(p.name)
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{fn}")
        self.end_headers()
        try:
            with open(p, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(64 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass


# ---------------------------------------------------------------- 启动
def lan_ip() -> str:
    """本机在局域网里的 IPv4（手机连它用）。拿不到就返回空字符串。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.4)
        # 不会真的发包，只是让系统挑一张出口网卡，从而拿到它的地址
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip if ip and not ip.startswith("127.") else ""
    except Exception:
        return ""


def qr_svg(text: str) -> bytes:
    """把一段文本画成二维码 SVG，手机扫一下就能打开。"""
    import io

    import qrcode
    import qrcode.image.svg

    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage,
                      box_size=10, border=1)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


def pick_port(preferred: int, host: str = "127.0.0.1") -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit("没有可用端口")


def main():
    ap = argparse.ArgumentParser(description="音乐下载台")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MUSIC_DESK_PORT", 8899)))
    ap.add_argument("--host", default=None,
                    help="监听地址；不填就看设置里的「手机访问」开关，开=0.0.0.0，关=127.0.0.1")
    args = ap.parse_args()

    load_jobs()
    host = args.host or ("0.0.0.0" if settings.get("allow_lan") else "127.0.0.1")
    port = pick_port(args.port, host if host != "0.0.0.0" else "0.0.0.0")
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True

    print("=" * 58)
    print("  音乐下载台 · Music Desk")
    print("=" * 58)
    print(f"  地址     http://127.0.0.1:{port}")
    if host == "0.0.0.0":
        ip = lan_ip()
        if ip:
            print(f"  手机访问 http://{ip}:{port}/m")
    print(f"  ffmpeg   {FFMPEG or '未找到（转码不可用）'}")
    print(f"  下载目录 {DOWNLOADS}")
    print(f"  并行数   {settings.get('concurrency')}   码率 {settings.get('quality')}kbps")
    print("=" * 58)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
        try:
            _pool.shutdown(wait=False)
        except Exception:
            pass


if __name__ == "__main__":
    main()