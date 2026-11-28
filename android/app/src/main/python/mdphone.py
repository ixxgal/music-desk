"""音乐下载台 · 手机端内置后端。

在手机里跑 CPython（Chaquopy），自己监听 127.0.0.1，WebView 指向它。
不依赖电脑、不依赖 yt-dlp、不依赖 ffmpeg —— 纯标准库 + urllib。

下载什么：
    B站    → view 拿 cid → playurl（wbi 签名）→ dash.audio 最大码率 → 存 m4a
    网易云  → 外链直下（没版权会返回 HTML，靠魔数拦掉）
    直链    → 直接抓

没有转码。m4a/mp3 都能被 Android 原生播，比装个 40MB 的 ffmpeg 划算。
"""
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mdphone_core as C

VERSION = "1.3.1"
UA = C.UA

DATA_DIR = ""
DOWNLOADS = None  # type: Path
PORT = 8899

_jobs = {}
_jobs_lock = threading.Lock()
_history = []

MP3_EXT = {".mp3"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wav"}

# 手机端能处理的源（其余平台走换源：拿歌名歌手去 B 站找同曲）
REPO = "ixxgal/music-desk"
GH_LATEST = "https://api.github.com/repos/%s/releases/latest" % REPO

SRC_LABEL = {
    "bilibili": "B站",
    "netease": "网易云",
    "direct": "直链",
    # 下面这些手机上没解析器，统一按「换源」处理，只是显示用
    "qq": "QQ音乐",
    "kugou": "酷狗",
    "ximalaya": "喜马拉雅",
    "douyin": "抖音",
    "kuaishou": "快手",
    "youtube": "YouTube",
}


# ────────────────────────── 工具 ──────────────────────────

def now_ts() -> float:
    return time.time()


def human_size(n) -> str:
    try:
        n = float(n or 0)
    except Exception:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.1f %s" % (n, unit)) if unit != "B" else ("%d B" % n)
        n /= 1024
    return ""


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem, suf, i = p.stem, p.suffix, 2
    while True:
        q = p.with_name("%s (%d)%s" % (stem, i, suf))
        if not q.exists():
            return q
        i += 1


def classify(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return "empty"
    if re.match(r"^https?://", t, re.I):
        return "url"
    return "text"


def guess_platform(url: str) -> str:
    u = (url or "").lower()
    if "bilibili.com" in u or "b23.tv" in u:
        return "bilibili"
    if "music.163.com" in u or "163cn.tv" in u:
        return "netease"
    return "direct"


def parse_bili_id(url: str):
    """从各种 B站链接里抠出 (kind, value)。"""
    m = re.search(r"(BV[0-9A-Za-z]{10})", url or "")
    if m:
        return "bvid", m.group(1)
    m = re.search(r"/av(\d+)", url or "")
    if m:
        return "aid", m.group(1)
    m = re.search(r"[?&]aid=(\d+)", url or "")
    if m:
        return "aid", m.group(1)
    return "", ""


def audio_meta(p: Path) -> dict:
    """手机端不引 mutagen：只给文件名/大小，时长交给前端 <audio> 自己算。"""
    out = {"size": p.stat().st_size if p.exists() else 0}
    return out


def _ver_tuple(s: str) -> tuple:
    out = []
    for part in (s or "").lstrip("vV").split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def app_update_info() -> dict:
    """查 GitHub 最新 Release，返回有没有新版、APK 直链。失败不抛，返回 error。"""
    out = {"ok": False, "current": VERSION, "latest": "", "has_update": False,
           "apk_url": "", "apk_size": 0, "notes": "", "error": ""}
    try:
        req = urllib.request.Request(GH_LATEST, headers={
            "User-Agent": C.UA,
            "Accept": "application/vnd.github+json",
        })
        with C.make_opener().open(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        out["error"] = "查不到更新信息（%s）" % e
        return out

    latest = (data.get("tag_name") or "").strip()
    out["latest"] = latest
    out["notes"] = (data.get("body") or "").strip()[:600]
    for a in data.get("assets") or []:
        name = (a.get("name") or "").lower()
        if name.endswith(".apk"):
            out["apk_url"] = a.get("browser_download_url") or ""
            out["apk_size"] = int(a.get("size") or 0)
            break
    out["ok"] = True
    out["has_update"] = bool(latest) and _ver_tuple(latest) > _ver_tuple(VERSION)
    return out


def list_files() -> list:
    out = []
    if DOWNLOADS is None or not DOWNLOADS.exists():
        return out
    for p in sorted(DOWNLOADS.iterdir(), key=lambda x: -x.stat().st_mtime if x.is_file() else 0):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXT:
            continue
        st = p.stat()
        out.append({
            "name": p.name,
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "kind": "audio",
            "platform": "",
            "duration": 0,
        })
    return out


# ────────────────────────── 下载引擎 ──────────────────────────

def bili_audio_url(src: str) -> tuple:
    """返回 (直链, 标题, 时长秒)。纯 HTTP，不用 yt-dlp。"""
    kind, val = parse_bili_id(src)
    if not kind:
        raise RuntimeError("这不是 B站 视频链接")

    cookie, mixin = C.bili_prepare()
    hdr = {"Cookie": cookie, "Referer": "https://www.bilibili.com/", "User-Agent": UA}

    def get(url):
        r = C.http_open(url, hdr, timeout=20)
        try:
            return json.loads(r.read().decode("utf-8", "ignore"))
        finally:
            r.close()

    v = get("https://api.bilibili.com/x/web-interface/view?%s=%s" % (kind, val))
    if v.get("code") != 0:
        raise RuntimeError("取视频信息失败：%s" % (v.get("message") or v.get("code")))
    d = v.get("data") or {}
    bvid, cid = d.get("bvid") or val, d.get("cid")
    title = d.get("title") or ""
    dur = int(d.get("duration") or 0)
    if not cid:
        raise RuntimeError("这条视频没有 cid（可能是番剧/直播）")

    q = C.bili_sign({"bvid": bvid, "cid": cid, "fnval": 16, "fnver": 0, "fourk": 1}, mixin)
    p = get("https://api.bilibili.com/x/player/wbi/playurl?" + q)
    if p.get("code") != 0:
        raise RuntimeError("取播放地址失败：%s" % (p.get("message") or p.get("code")))
    audios = ((p.get("data") or {}).get("dash") or {}).get("audio") or []
    if not audios:
        raise RuntimeError("这条视频没有独立音轨（可能是纯音乐投稿，试试别的）")
    best = max(audios, key=lambda x: x.get("bandwidth", 0))
    return best["baseUrl"], title, dur


def stream_download(url: str, dest: Path, job: dict, headers: dict = None,
                    timeout: int = 60, max_bytes: int = 300 * 1024 * 1024) -> Path:
    part = dest.with_suffix(dest.suffix + ".part")
    hdr = dict(headers or {})
    hdr.setdefault("User-Agent", UA)
    req = urllib.request.Request(url, headers=hdr)
    got = 0
    t0 = time.time()
    last = 0.0
    first = b""
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = (r.headers.get("Content-Type") or "").lower()
        C.check_media_type(ctype)
        total = int(r.headers.get("Content-Length") or 0)
        with open(part, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                if not first:
                    first = chunk[:16]
                f.write(chunk)
                got += len(chunk)
                if got > max_bytes:
                    raise RuntimeError("文件超过 %s，已中止" % human_size(max_bytes))
                now = time.time()
                if now - last > 0.4:
                    last = now
                    _update_job(job["id"],
                                progress=round(min(got / total, 0.99), 3) if total else 0.5,
                                stage="下载中",
                                speed=int(got / max(now - t0, 0.1)),
                                out_size=got)
    if first and not C.looks_like_audio(first):
        part.unlink(missing_ok=True)
        raise RuntimeError("下到的不是音频（多半是失效页面）")
    os.replace(part, dest)
    return dest


# ────────────────────── 换源：VIP / 无版权的唯一出路 ──────────────────────
# 网易云外链对 VIP 曲目返回的是 HTML 404，QQ音乐 / 酷狗 / 喜马拉雅在手机上根本没解析器。
# 这三类都走同一条路：拿「歌名 + 歌手」去 B 站搜，**严格判定**通过才下。
# 判不中就报失败 —— 宁可不下，也不给用户下错歌。

MIN_FALLBACK_SCORE = 60.0
MAX_DURATION_GAP = 0.10
MIN_DURATION_TOL = 12.0


def judge_hit(hit: dict, nt_title: str, artist_n: str, want: float):
    """候选是不是同一首。返回 (score, reason)，score<=0 即拒绝。"""
    t = hit.get("title") or ""
    nt = C._norm(t)
    d = float(hit.get("duration") or 0)
    if not nt:
        return 0.0, "标题为空"
    if C.BAD_TITLE.search(t):
        return 0.0, "翻唱/伴奏/剪辑版「%s」" % t[:32]
    if want and d:
        tol = max(MIN_DURATION_TOL, want * MAX_DURATION_GAP)
        if abs(d - want) > tol:
            return 0.0, "时长不符 %ds≠%ds「%s」" % (round(d), round(want), t[:24])
    exact = bool(nt_title) and nt == nt_title
    contains = bool(nt_title) and len(nt_title) >= 2 and (nt_title in nt)
    artist_ok = bool(artist_n) and artist_n in nt
    if not (exact or contains or artist_ok):
        return 0.0, "名字对不上「%s」" % t[:32]
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
        return 0.0, "相关度太低(%d)「%s」" % (round(s), t[:32])
    return s, ""


def fallback_to_bilibili(job: dict) -> bool:
    """用 job 里的歌名/歌手去 B 站找同曲，严格判定通过才改 job["source"]。"""
    title = (job.get("title") or "").strip()
    if not title:
        job["_fb_reject"] = ["没有歌名，没法匹配"]
        return False
    artist = (job.get("artist") or "").replace("、", "/").replace("&", "/").split("/")[0].strip()
    q = ("%s %s" % (title, artist)).strip()
    want = float(job.get("duration") or 0)
    nt_title = C._norm(title)
    artist_n = C._norm(artist)
    rejects = []

    try:
        C.bili_prepare()   # 不先取 cookie + mixin，搜索会直接返回空
    except Exception as e:
        rejects.append("B站准备失败 %s" % str(e)[:40])
    hits = []
    try:
        hits = C.bilibili_search(q, 10)
    except Exception as e:
        rejects.append("B站搜索失败 %s" % str(e)[:40])

    best, best_score, why_first = None, 0.0, ""
    for h in hits:
        d = h.get("duration") or 0
        if not d or d < 20:
            continue
        s, why = judge_hit(h, nt_title, artist_n, want)
        if s <= 0:
            if why and not why_first:
                why_first = why
            continue
        s += min((h.get("plays") or 0) / 1e6, 5.0)
        if s > best_score:
            best_score, best = s, h

    if not best:
        if why_first:
            rejects.append("B站 " + why_first)
        job["_fb_reject"] = rejects or ["没找到任何候选"]
        return False

    job["_fb_from"] = job.get("platform") or ""
    job["_fb_bili_title"] = (best.get("title") or "")[:80]
    job["source"] = best["source"]
    job["thumb"] = job.get("thumb") or best.get("thumb") or ""
    if not job.get("duration"):
        job["duration"] = best.get("duration") or 0
    return True


def engine_fallback(job: dict) -> tuple:
    """换源下载：匹配 → 成功后走 B 站引擎。匹配不到就抛错，绝不硬下。"""
    _update_job(job["id"], stage="匹配可用音源", progress=0.05)
    if not fallback_to_bilibili(job):
        why = "；".join((job.get("_fb_reject") or [])[:2])
        raise RuntimeError(
            "没找到可信的同曲音源（%s）。宁可不下也不下错 —— "
            "换个更准确的「歌手 歌名」再试" % why
        )
    _update_job(job["id"], stage="换源下载中", progress=0.08)
    final, meta = engine_bilibili(job)
    if job.get("_fb_from"):
        meta["switched_from"] = job["_fb_from"]
    return final, meta


def engine_bilibili(job: dict) -> tuple:
    url, title, dur = bili_audio_url(job["source"])
    if not job.get("title") and title:
        _update_job(job["id"], title=C.pretty_title(title))
    base = C.pretty_title(job.get("title") or title or "音频")
    dest = unique_path(DOWNLOADS / C.clean_name(base + ".m4a"))
    _update_job(job["id"], stage="下载中", progress=0.05)
    stream_download(url, dest, job, {
        "Referer": "https://www.bilibili.com/",
        "User-Agent": UA,
    })
    return dest, {"title": base, "duration": dur or 0}


def engine_netease(job: dict) -> tuple:
    m = re.search(r"[?&]id=(\d+)", job["source"] or "")
    if not m:
        raise RuntimeError("这个网易云链接里没有歌曲 id")
    sid = m.group(1)
    # 先把元数据拿到（标题/时长），失败不致命
    meta = {}
    try:
        meta = C.netease_details([int(sid)]).get(int(sid)) or C.netease_details([sid]).get(sid) or {}
    except Exception:
        meta = {}
    title = job.get("title") or meta.get("title") or ""
    url = "https://music.163.com/song/media/outer/url?id=%s.mp3" % sid
    base = C.pretty_title(title or ("网易云 " + sid))
    dest = unique_path(DOWNLOADS / C.clean_name(base + ".mp3"))
    stream_download(url, dest, job, {"Referer": "https://music.163.com/"})
    return dest, {"title": base, "duration": int(meta.get("duration") or 0)}


def engine_direct(job: dict) -> tuple:
    url = job["source"]
    name = urllib.parse.unquote(os.path.basename(urllib.parse.urlsplit(url).path)) or "音频"
    base = C.pretty_title(job.get("title") or Path(name).stem or "音频")
    ext = Path(name).suffix.lower()
    if ext not in AUDIO_EXT:
        ext = ".mp3"
    dest = unique_path(DOWNLOADS / C.clean_name(base + ext))
    stream_download(url, dest, job, {"Referer": url})
    return dest, {"title": base}


# ────────────────────────── 任务 ──────────────────────────

def _update_job(jid: str, **kw):
    with _jobs_lock:
        j = _jobs.get(jid)
        if not j:
            return
        j.update(kw)
        j["updated"] = now_ts()


def public_job(j: dict) -> dict:
    keys = ("id", "title", "artist", "status", "stage", "progress", "speed",
            "out_size", "filename", "error", "platform", "engine", "source",
            "created", "finished")
    return {k: j.get(k) for k in keys}


def pick_engine(plat: str) -> str:
    """手机端只认三种直连来源；其余（QQ音乐/酷狗/喜马拉雅/抖音/快手…）一律换源。"""
    if plat == "bilibili":
        return "bilibili"
    if plat == "netease":
        return "netease"
    if plat in ("direct", ""):
        return "direct"
    return "fallback"


def create_job(payload: dict) -> dict:
    src = (payload.get("source") or "").strip()
    if not src:
        raise ValueError("没有链接")
    plat = payload.get("platform") or guess_platform(src) or "direct"
    engine = pick_engine(plat)
    jid = "%012x" % int(time.time() * 1000 % (1 << 48))
    with _jobs_lock:
        if jid in _jobs:
            jid = jid + "a"
        _jobs[jid] = {
            "id": jid,
            "title": (payload.get("title") or "").strip(),
            "artist": (payload.get("artist") or "").strip(),
            "source": src,
            "platform": plat,
            "engine": engine,
            "duration": payload.get("duration") or 0,
            "thumb": payload.get("thumb") or "",
            "status": "queued",
            "stage": "排队中",
            "progress": 0.0,
            "speed": 0,
            "out_size": 0,
            "filename": "",
            "error": "",
            "created": now_ts(),
            "finished": 0,
        }
    threading.Thread(target=run_job, args=(jid,), daemon=True).start()
    return public_job(_jobs[jid])


def run_job(jid: str):
    with _jobs_lock:
        job = dict(_jobs.get(jid) or {})
    if not job:
        return
    _update_job(jid, status="running", stage="准备中", progress=0.02)
    t0 = time.time()
    try:
        plat = job["platform"]
        engine = job.get("engine") or pick_engine(plat)
        if engine == "bilibili":
            final, meta = engine_bilibili(job)
        elif engine == "netease":
            try:
                final, meta = engine_netease(job)
            except Exception as e:
                # VIP / 无版权：外链返回的是 HTML 404，改走换源
                _update_job(jid, stage="原链接不可用，换源中", progress=0.04,
                            error="")
                print("[job %s] netease 直取失败，转换源: %s" % (jid, e))
                final, meta = engine_fallback(job)
        elif engine == "fallback":
            final, meta = engine_fallback(job)
        else:
            try:
                final, meta = engine_direct(job)
            except Exception as e:
                _update_job(jid, stage="直链不可用，换源中", progress=0.04, error="")
                print("[job %s] 直链失败，转换源: %s" % (jid, e))
                final, meta = engine_fallback(job)
        if meta.get("switched_from"):
            _update_job(jid, platform=(plat + "→B站") if "→" not in plat else plat)

        am = audio_meta(final)
        if not am.get("size"):
            final.unlink(missing_ok=True)
            raise RuntimeError("产物是空的，已删除")

        _update_job(
            jid, status="done", stage="完成", progress=1.0, speed=0,
            filename=final.name, out_size=am["size"],
            title=(meta.get("title") or job.get("title") or final.stem),
            finished=now_ts(), error="",
        )
        with _jobs_lock:
            _history.insert(0, {
                "id": jid, "title": _jobs[jid]["title"], "filename": final.name,
                "platform": plat, "status": "done", "size": am["size"],
                "duration": meta.get("duration") or 0, "finished": now_ts(),
            })
            del _history[200:]
    except Exception as e:
        msg = str(e) or e.__class__.__name__
        _update_job(jid, status="error", stage="失败", error=msg, finished=now_ts())
        with _jobs_lock:
            _history.insert(0, {
                "id": jid, "title": job.get("title") or job["source"], "filename": "",
                "platform": job["platform"], "status": "error", "error": msg,
                "size": 0, "finished": now_ts(),
            })
            del _history[200:]
        try:
            print("[job %s] %s" % (jid, msg))
            traceback.print_exc()
        except Exception:
            pass


# ────────────────────────── 搜索 ──────────────────────────

def search(q: str, src: str = "auto", limit: int = 20) -> list:
    q = C.clean_query(q)
    if not q:
        return []
    items = []
    if src in ("auto", "bilibili"):
        try:
            C.bili_prepare()   # 必须先拿 cookie + mixin，否则搜索直接返回空
        except Exception as e:
            print("bili prepare failed:", e)
        try:
            items += C.bilibili_search(q, max(8, limit // 2) if src == "auto" else limit)
        except Exception as e:
            print("bili search failed:", e)
    if src in ("auto", "netease"):
        try:
            items += C.netease_search(q, max(6, limit // 3) if src == "auto" else limit)
        except Exception as e:
            print("netease search failed:", e)
    for it in items:
        if not it.get("platform"):
            it["platform"] = "bilibili" if "bilibili.com" in (it.get("source") or "") else "netease"
        it["_score"] = C.score_hit(it, q) + (25 if it["platform"] == "bilibili" else 0)
    items.sort(key=lambda x: -x["_score"])
    for it in items:
        it.pop("_score", None)
    return items[:limit]


# ────────────────────────── HTTP ──────────────────────────

PAGE_INDEX = None   # 由 Java 侧或文件提供
WEB_DIR = None      # type: Path

# 已经起过的服务器。Activity 重建、WebView 刷新都会再调一次 start()，
# 要是每次都新开一个监听，第二次就撞 8899 报「Address already in use」。
_srv = None
_srv_port = 0
_srv_data = ""
# start() 必须串行：Activity 重建、onResume 兜底、WebView 刷新可能同时进来。
# 不加这个锁，两个线程会各自 bind 一个端口，起出两个服务，后面谁也说不清在用哪个。
_start_lock = threading.Lock()


def _close_server():
    global _srv
    if _srv is None:
        return
    srv = _srv
    _srv = None
    try:
        srv.shutdown()
    except Exception:
        pass
    try:
        srv.server_close()
    except Exception:
        pass
    # server_close() 在某些 Python 版本上只等 handler 线程，listening socket 要自己再关一次
    try:
        sock = getattr(srv, "socket", None)
        if sock is not None:
            sock.close()
    except Exception:
        pass


def _web(name: str) -> bytes:
    p = (WEB_DIR / name) if WEB_DIR else None
    if p and p.is_file():
        return p.read_bytes()
    return b""


CTYPE = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "MusicDeskPhone/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ---- 底层 ----

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except Exception:
                pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- GET ----

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        qs = urllib.parse.parse_qs(u.query)

        if path == "/api/ping":
            return self._json({"ok": True, "where": "phone", "version": VERSION,
                               "py": sys.version.split()[0], "ffmpeg": False})
        if path == "/api/settings":
            return self._json({"ok": True, "settings": {
                "download_dir": str(DOWNLOADS or ""),
                "engine": "phone",
                "write_tags": False,
                "embed_cover": False,
            }})
        if path == "/api/search":
            q = (qs.get("q") or [""])[0]
            src = (qs.get("src") or ["auto"])[0]
            limit = int((qs.get("limit") or ["20"])[0] or 20)
            try:
                return self._json({"ok": True, "items": search(q, src, limit), "src": src})
            except Exception as e:
                return self._json({"ok": False, "items": [], "error": str(e)})
        if path == "/api/jobs":
            with _jobs_lock:
                arr = [public_job(j) for j in _jobs.values()]
            arr.sort(key=lambda x: -(x.get("created") or 0))
            return self._json({"ok": True, "jobs": arr})
        if path.startswith("/api/jobs/"):
            jid = path.rsplit("/", 1)[-1]
            with _jobs_lock:
                j = _jobs.get(jid)
            if not j:
                return self._json({"ok": False, "error": "任务不存在"}, 404)
            return self._json({"ok": True, "job": public_job(j)})
        if path == "/api/files":
            return self._json({"ok": True, "files": list_files()})
        if path == "/api/history":
            with _jobs_lock:
                return self._json({"ok": True, "items": list(_history)})
        if path == "/api/stats":
            fs = list_files()
            return self._json({"ok": True, "count": len(fs),
                               "size": sum(f["size"] for f in fs)})
        if path == "/api/net":
            return self._json({"ok": True, "host": "127.0.0.1", "port": PORT,
                               "lan_enabled": False, "mobile_url": "/m", "local_url": "/"})

        if path == "/api/app/update":
            return self._json(app_update_info())

        if path.startswith("/media/"):
            return self.serve_media(urllib.parse.unquote(path[len("/media/"):]))

        if path in ("/", "/m", "/m/", "/m.html", "/index.html"):
            body = _web("m.html")
            if not body:
                body = _web("index.html")
            if body:
                return self._send(200, body, CTYPE[".html"])
            return self._send(200, FALLBACK_PAGE, CTYPE[".html"])

        if path.startswith("/static/"):
            name = urllib.parse.unquote(path[len("/static/"):])
            body = _web(name)
            if body:
                return self._send(200, body, CTYPE.get(Path(name).suffix, "application/octet-stream"))
        if path in ("/manifest.webmanifest", "/sw.js"):
            body = _web(path.lstrip("/"))
            if body:
                return self._send(200, body,
                                  CTYPE.get(Path(path).suffix, "application/octet-stream"))

        self._send(404, "not found")

    # ---- POST ----

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        body = self._body()

        if path == "/api/jobs":
            items = body.get("items")
            if items is None:
                items = [body] if body.get("source") else []
            out = []
            for it in items:
                try:
                    out.append(create_job(it))
                except Exception as e:
                    out.append({"error": str(e)})
            return self._json({"ok": True, "jobs": out})
        if path == "/api/settings":
            return self._json({"ok": True, "settings": {}})
        if path.endswith("/cancel"):
            jid = path.split("/")[-2]
            _update_job(jid, status="error", stage="已取消", error="已取消")
            return self._json({"ok": True})
        self._json({"ok": False, "error": "not found"}, 404)

    # ---- DELETE ----

    def do_DELETE(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        qs = urllib.parse.parse_qs(u.query)
        if path == "/api/files":
            name = (qs.get("name") or [""])[0]
            p = (DOWNLOADS / name) if (DOWNLOADS and name) else None
            if p and p.is_file() and p.parent == DOWNLOADS:
                p.unlink()
                return self._json({"ok": True})
            return self._json({"ok": False, "error": "找不到"}, 404)
        if path.startswith("/api/jobs/"):
            jid = path.rsplit("/", 1)[-1]
            with _jobs_lock:
                _jobs.pop(jid, None)
            return self._json({"ok": True})
        self._json({"ok": False}, 404)

    # ---- 媒体（支持 Range，进度条能拖） ----

    def serve_media(self, name: str):
        if not DOWNLOADS:
            return self._send(404, "no dir")
        p = (DOWNLOADS / name)
        try:
            if not p.is_file() or p.parent.resolve() != DOWNLOADS.resolve():
                return self._send(404, "not found")
        except Exception:
            return self._send(404, "not found")

        size = p.stat().st_size
        ctype = CTYPE.get(p.suffix.lower(), "application/octet-stream")
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
        if m:
            start = int(m.group(1) or 0)
            end = int(m.group(2)) if m.group(2) else size - 1
            end = min(end, size - 1)
            if start > end:
                return self._send(416, "range not satisfiable")
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with open(p, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    b = f.read(min(65536, left))
                    if not b:
                        break
                    try:
                        self.wfile.write(b)
                    except Exception:
                        return
                    left -= len(b)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(p, "rb") as f:
            while True:
                b = f.read(65536)
                if not b:
                    break
                try:
                    self.wfile.write(b)
                except Exception:
                    return


FALLBACK_PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>音乐下载台</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#070a10;
color:#8b93a8;font:15px/1.8 -apple-system,'PingFang SC',sans-serif;padding:30px;text-align:center}
b{color:#e6e9f2}a{color:#a78bfa}</style>
<div><h2 style="color:#e6e9f2">后端起来了</h2>
<p>但没找到界面文件（m.html）。<br>
接口是通的：<a href="/api/ping">/api/ping</a></p></div>"""


def start(data_dir: str, port: int = 8899, web_dir: str = "") -> int:
    """Java 侧调用：起服务，返回实际端口。可以重复、可以并发调用。"""
    with _start_lock:
        return _start_locked(data_dir, port, web_dir)


def _start_locked(data_dir: str, port: int = 8899, web_dir: str = "") -> int:
    """start() 的真正实现，调用方必须已经持有 _start_lock。

    可重复调用：数据目录没变就直接返回已起的那个；变了（比如刚拿到存储权限）
    就把旧的关掉重开。端口被占会自动往后找，不会再抛 Address already in use。
    """
    global DATA_DIR, PORT, DOWNLOADS, WEB_DIR, _srv, _srv_port, _srv_data
    d = data_dir or ""
    if _srv is not None and _srv_data == d:
        return _srv_port
    if _srv is not None:
        _close_server()

    DATA_DIR = d
    if DATA_DIR:
        DOWNLOADS = Path(DATA_DIR) / "downloads"
        DOWNLOADS.mkdir(parents=True, exist_ok=True)
    if web_dir:
        WEB_DIR = Path(web_dir)
    elif DATA_DIR:
        # APK 里没有可用的文件路径，界面文件在 webpack 里，解到私有目录
        try:
            import webpack
            WEB_DIR = Path(webpack.extract(os.path.join(DATA_DIR, "web")))
        except Exception as e:
            print("webpack extract failed:", e)

    srv = None
    last = None
    for p in range(port, port + 15):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError as e:
            # 端口被占：Linux/Android 是 Errno 98(EADDRINUSE)，
            # Windows 独占时是 13/10013(EACCES)。一律换下一个端口试。
            last = e
            continue
    if srv is None:
        raise RuntimeError("端口 %d-%d 全被占用（%s）" % (port, port + 14, last))

    _srv = srv
    _srv_port = srv.server_address[1]
    _srv_data = DATA_DIR
    PORT = _srv_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return _srv_port


def stop() -> None:
    """Java 侧调用：彻底关掉服务（换数据目录或退出前用）。"""
    _close_server()


if __name__ == "__main__":
    import tempfile
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    data = sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="mdphone-")
    # 第三个参数可显式指定界面目录；不传就走 webpack 解压（和手机上一致）
    web = sys.argv[3] if len(sys.argv) > 3 else ""
    real = start(data, port, web)
    print("listening on http://127.0.0.1:%d/m" % real, flush=True)
    print("data:", data, flush=True)
    print("web :", WEB_DIR, flush=True)
    threading.Event().wait()