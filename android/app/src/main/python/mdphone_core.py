# -*- coding: utf-8 -*-
"""手机端可复用的纯逻辑（由 _gen_mdphone_core.py 从 server.py 自动提取，别手改）。

只依赖标准库：urllib / json / re / time / hashlib / pathlib。
没有 ffmpeg、没有 yt-dlp、没有 Windows API —— 所以能在 Android 的 CPython 里跑。
"""
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

AUDIO_MAGIC = (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xf1",
               b"\xff\xf9", b"\xff\xfa", b"OggS", b"RIFF", b"fLaC")
WS = re.compile(r"\s+")
BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
BAD_TITLE = re.compile(r"(翻唱|cover|伴奏|inst\.?|karaoke|dj|remix|钢琴版|纯音乐|吉他版|抖音|铃声|片段|live|现场|加速版|慢速版|女声版|男声版|童声|口琴|八音盒)", re.I)
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

# ---------------------------------------------------------------- HTTP 取流
# 手机端没有设置文件：代理留空即可（桌面版的 settings 在这里用默认值）
settings: dict = {"proxy": "", "browser_cookies": ""}


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

def _norm(s: str) -> str:
    return re.sub(r"[\s\-_·.,'\"()（）\[\]【】]", "", (s or "").lower())

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

def clean_query(q: str) -> str:
    q = q.strip()
    q = re.sub(r"(?i)\bhttps?://\S+", " ", q)
    q = re.sub(r"(帮我|请帮我|麻烦|给我|我要|下载一下|下载|找一下|找下|找|搜索|搜一下|搜)\s*", " ", q)
    q = re.sub(r"(?i)\.?(mp3|m4a|flac|wav)\b", " ", q)
    q = re.sub(r"(?i)\b(mp3|320k|无损|高音质|音质|完整版|原版|超高音质)\b", " ", q)
    q = WS.sub(" ", q).strip(" ,，。、-")
    return q or q.strip()
