# -*- coding: utf-8 -*-
"""多平台音乐搜索适配层。

统一返回结构（每个平台一个 dict 列表）：
    platform      qq / kugou / ximalaya / netease / bilibili
    platform_name 中文名
    id            平台内 id
    title         歌名
    artist        歌手
    album         专辑
    duration      秒
    thumb         封面 URL
    url           可回传的链接
    engine        建议下载引擎
    downloadable  是否可直接取到音频流
    note          限制说明（VIP / 付费 / 仅元数据）

设计原则：
    能搜到的平台尽量多，能直接下到音频流的平台诚实标注。
    版权平台（QQ/酷狗）只提供精确元数据，下载时由 server 侧自动换源匹配同曲。
"""

import json
import re
import ssl
import time
import html as _html
import urllib.parse
import urllib.request

UA_PC = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
UA_IOS = "ting_v9.0.0_c5(market_ios)/iPhone"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

PLATFORMS = {
    "bilibili": "B站",
    "netease": "网易云",
    "qq": "QQ音乐",
    "kugou": "酷狗",
    "ximalaya": "喜马拉雅",
    "web": "网络源",
}

# 可直接取到音频流的平台
STREAMABLE = {"bilibili", "netease", "web", "ximalaya_free"}


class PlatformError(RuntimeError):
    pass


def _get(url, headers=None, timeout=15, referer=None):
    h = {"User-Agent": UA_PC, "Accept": "application/json, text/plain, */*"}
    if referer:
        h["Referer"] = referer
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise PlatformError(f"HTTP {e.code}") from None
    except Exception as e:
        raise PlatformError(type(e).__name__) from None


def _json(raw):
    return json.loads(raw.decode("utf-8", "replace"))


def _q(s):
    return urllib.parse.quote(str(s or ""), safe="")


# --------------------------------------------------------------------------
# QQ 音乐
# --------------------------------------------------------------------------
def qq_search(q, limit=20):
    """QQ 音乐搜索。实测可用，返回精确的原曲元数据。"""
    url = ("https://c.y.qq.com/soso/fcgi-bin/client_search_cp?" +
           urllib.parse.urlencode({
               "p": 1, "n": max(1, min(int(limit), 50)), "w": q,
               "format": "json", "cr": 1, "new_json": 1,
               "t": 0, "aggr": 1, "lossless": 0, "catZhida": 1,
           }))
    d = _json(_get(url, {"Referer": "https://y.qq.com/"}))
    songs = (d.get("data") or {}).get("song", {}).get("list") or []
    out = []
    for s in songs:
        mid = s.get("mid") or s.get("songmid") or ""
        album = s.get("album") or {}
        singers = s.get("singer") or []
        pay = s.get("pay") or {}
        # pay_play=1 表示需要付费/会员才能播
        vip = bool(pay.get("pay_play")) or int(pay.get("pay_down") or 0) > 0
        cover = ""
        if album.get("mid"):
            cover = ("https://y.qq.com/music/photo_new/T002R300x300M000%s.jpg"
                     % album["mid"])
        out.append({
            "platform": "qq",
            "platform_name": "QQ音乐",
            "id": mid,
            "title": _html.unescape(s.get("title") or s.get("name") or ""),
            "artist": "、".join(x.get("name", "") for x in singers if x.get("name")),
            "album": _html.unescape(album.get("name") or ""),
            "duration": int(s.get("interval") or 0),
            "thumb": cover,
            "url": "https://y.qq.com/n/ryqq/songDetail/%s" % mid if mid else "",
            "engine": "ytdlp",
            "downloadable": False,
            "note": "会员/付费曲目，下载时自动换源" if vip else "版权曲目，下载时自动换源",
        })
    return out


# --------------------------------------------------------------------------
# 酷狗音乐
# --------------------------------------------------------------------------
def _kugou_cover(item):
    """酷狗把封面模板塞在 trans_param 里，形如 http://imge.kugou.com/stdmusic/{size}/..."""
    tp = item.get("trans_param")
    raw = tp if isinstance(tp, str) else json.dumps(tp or {}, ensure_ascii=False)
    m = re.search(r"https?://imge\.kugou\.com/stdmusic/\{size\}[^'\",}\s]+", raw)
    if not m:
        return ""
    return m.group(0).replace("{size}", "240").replace("http://", "https://")


def kugou_search(q, limit=20):
    """酷狗音乐搜索。实测可用；有 hash / 320hash / sqhash 与封面。"""
    url = ("https://mobilecdn.kugou.com/api/v3/search/song?" +
           urllib.parse.urlencode({
               "format": "json", "keyword": q, "page": 1,
               "pagesize": max(1, min(int(limit), 50)), "showtype": 1,
           }))
    d = _json(_get(url, {"Referer": "https://m.kugou.com/"}))
    info = (d.get("data") or {}).get("info") or []
    out = []
    for it in info:
        h = it.get("hash") or ""
        # privilege 10 / pay_type 3 一般意味着需要会员
        vip = int(it.get("privilege") or 0) >= 10 or int(it.get("pay_type") or 0) == 3
        out.append({
            "platform": "kugou",
            "platform_name": "酷狗",
            "id": h,
            "title": _html.unescape(it.get("songname") or ""),
            "artist": _html.unescape(it.get("singername") or ""),
            "album": _html.unescape(it.get("album_name") or ""),
            "duration": int(it.get("duration") or 0),
            "thumb": _kugou_cover(it),
            "url": "https://www.kugou.com/song/#hash=%s&album_id=%s"
                   % (h, it.get("album_id") or ""),
            "engine": "ytdlp",
            "downloadable": False,
            "note": "会员曲目，下载时自动换源" if vip else "版权曲目，下载时自动换源",
            "bitrate": it.get("bitrate"),
            "size_320": it.get("320filesize"),
        })
    return out


# --------------------------------------------------------------------------
# 喜马拉雅
# --------------------------------------------------------------------------
def ximalaya_search(q, limit=20):
    """喜马拉雅搜索（有声书 / 播客 / 相声 / 电台）。

    走 PC 站 revision/search 接口，必须带 Referer 指向搜索结果页，
    否则返回 risk invalid 风控。
    """
    limit = max(1, min(int(limit), 50))
    ref = "https://www.ximalaya.com/search?kw=" + _q(q)
    url = ("https://www.ximalaya.com/revision/search?" +
           urllib.parse.urlencode({
               "kw": q, "page": 1, "rows": limit,
               "core": "track", "spellchecker": "false", "condition": "relation",
           }))
    raw = _get(url, {
        "Referer": ref,
        "Accept": "application/json, text/plain, */*",
        "x-requested-with": "XMLHttpRequest",
    })
    d = _json(raw)
    docs = (((d.get("data") or {}).get("result") or {})
            .get("response") or {}).get("docs") or []
    out = []
    for it in docs:
        tid = it.get("id")
        cover = it.get("cover_path") or ""
        if cover.startswith("//"):
            cover = "https:" + cover
        paid = bool(it.get("is_paid")) or bool(it.get("is_free") is False)
        out.append({
            "platform": "ximalaya",
            "platform_name": "喜马拉雅",
            "id": str(tid or ""),
            "title": _html.unescape(re.sub(r"<[^>]+>", "", it.get("title") or "")),
            "artist": _html.unescape(it.get("nickname") or ""),
            "album": _html.unescape(it.get("album_title") or ""),
            "duration": int(float(it.get("duration") or 0)),
            "thumb": cover,
            "url": "https://www.ximalaya.com/sound/%s" % tid if tid else "",
            "engine": "ximalaya",
            "downloadable": not paid,
            "note": "VIP 付费内容，无法获取音频" if paid else "免费音频",
            "plays": it.get("count_play"),
        })
    return out


def ximalaya_track(track_id):
    """取单条喜马拉雅音频信息。

    返回 dict：
        ok        是否拿到可播放直链
        url       音频直链（m4a）
        title / artist / album / duration / cover
        paid      True 表示 VIP 付费
        error     失败原因
    接口：GET /revision/play/tracks?trackIds=ID
    """
    tid = str(track_id or "").strip()
    out = {"ok": False, "url": "", "title": "", "artist": "", "album": "",
           "duration": 0, "cover": "", "paid": False, "error": ""}
    if not tid.isdigit():
        out["error"] = "无效的 track id"
        return out

    url = "https://www.ximalaya.com/revision/play/tracks?trackIds=" + tid
    try:
        d = _json(_get(url, {"Referer": "https://www.ximalaya.com/sound/" + tid}, timeout=12))
    except Exception as e:
        out["error"] = "接口不可达: %s" % e
        return out

    tracks = ((d.get("data") or {}).get("tracksForAudioPlay") or [])
    if not tracks:
        out["error"] = "接口没有返回音轨（可能已下架）"
        return out

    t = tracks[0]
    cover = (t.get("trackCoverPath") or "").strip()
    if cover.startswith("//"):
        cover = "https:" + cover
    out.update({
        "title": _html.unescape(t.get("trackName") or ""),
        "album": _html.unescape(t.get("albumName") or ""),
        "duration": int(float(t.get("duration") or 0)),
        "cover": cover,
        "paid": bool(t.get("isPaid")),
    })
    src = (t.get("src") or "").strip()
    if not src:
        out["error"] = "VIP 付费内容，取不到音频直链"
        return out
    if t.get("canPlay") is False:
        out["error"] = "该音频当前不可播放（可能已下架）"
        return out
    out["url"] = src.replace("http://", "https://")
    out["ok"] = True
    return out


def ximalaya_stream(track_id):
    """兼容旧接口：返回 (url, error)。"""
    info = ximalaya_track(track_id)
    return (info["url"] or None), (None if info["ok"] else info["error"])


# --------------------------------------------------------------------------
# 平台识别
# --------------------------------------------------------------------------
_URL_RULES = [
    ("qq", r"(?:y\.qq\.com|c\.y\.qq\.com|i\.y\.qq\.com)"),
    ("kugou", r"(?:kugou\.com|m\.kugou\.com|kglink\.kugou\.com)"),
    ("ximalaya", r"(?:ximalaya\.com|xmcdn\.com)"),
    ("netease", r"(?:music\.163\.com|163cn\.tv|y\.music\.163\.com)"),
    ("bilibili", r"(?:bilibili\.com|b23\.tv|acg\.tv)"),
]


def detect_platform(url):
    """按 URL 判断属于哪个平台，未知返回空串。"""
    u = (url or "").lower()
    for name, pat in _URL_RULES:
        if re.search(pat, u):
            return name
    return ""


def extract_id(platform, url):
    """从平台链接里抠出 id。"""
    u = url or ""
    if platform == "qq":
        m = re.search(r"songDetail/([0-9A-Za-z]+)", u) or re.search(r"[?&]songmid=([0-9A-Za-z]+)", u)
        return m.group(1) if m else ""
    if platform == "ximalaya":
        m = re.search(r"/sound/(\d+)", u) or re.search(r"[?&]trackId=(\d+)", u)
        return m.group(1) if m else ""
    if platform == "netease":
        m = re.search(r"[?&]id=(\d+)", u)
        return m.group(1) if m else ""
    if platform == "kugou":
        m = re.search(r"[?&#]hash=([0-9a-fA-F]{32})", u)
        return m.group(1) if m else ""
    if platform == "bilibili":
        m = re.search(r"(?:/video/)?(BV[0-9A-Za-z]{10}|av\d+)", u)
        return m.group(1) if m else ""
    return ""


def search(platform, q, limit=20):
    """统一入口。"""
    fn = {
        "qq": qq_search,
        "kugou": kugou_search,
        "ximalaya": ximalaya_search,
    }.get(platform)
    if not fn:
        raise PlatformError("unsupported platform: %s" % platform)
    return fn(q, limit)


if __name__ == "__main__":
    import sys
    kw = sys.argv[1] if len(sys.argv) > 1 else "晴天"
    for p in ("qq", "kugou", "ximalaya"):
        try:
            rows = search(p, kw, 3)
            print(f"=== {PLATFORMS[p]} ({len(rows)}) ===")
            for r in rows:
                print("  %-28s | %-14s | %4ss | thumb=%s"
                      % (r["title"][:28], r["artist"][:14], r["duration"],
                         "Y" if r["thumb"] else "-"))
        except Exception as e:
            print(f"=== {PLATFORMS[p]} FAIL {type(e).__name__}: {e}")