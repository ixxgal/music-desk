# -*- coding: utf-8 -*-
"""抖音 / 快手 关键词搜索。

两家都没有公开的搜索 API，页面又是 SPA，卡片节点不是 <a> 标签，
所以走「真实浏览器打开搜索页 → 从网络响应里抽 photoId + caption」这条路。
快手需要登录态（data/cdp-kuaishou）；抖音 headless 会撞验证码，必须开有头窗口。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KS_PROFILE = ROOT / "data" / "cdp-kuaishou"

_KS_ID = re.compile(r'"photoId"\s*:\s*"([0-9a-zA-Z_-]{10,24})"')
_KS_CAP = re.compile(r'"caption"\s*:\s*"((?:[^"\\]|\\.){4,200})"')
_DY_ID = re.compile(r'"aweme_id"\s*:\s*"(\d{15,25})"')
_DY_DESC = re.compile(r'"desc"\s*:\s*"((?:[^"\\]|\\.){2,300})"')
_DY_ID2 = re.compile(r'aweme/v1/(?:play|web/aweme/detail)[^"]*?(?:video_id|aweme_id)=([\w-]+)')


def _unescape(s: str) -> str:
    try:
        return json.loads('"%s"' % s)
    except Exception:
        return s


def _drain_bodies(sess):
    """把目前收到的所有响应体取回来（失败就算了）。"""
    bodies = []
    for ev in sess.events():
        if ev.get("method") != "Network.responseReceived":
            continue
        rid = ev["params"]["response"].get("requestId")
        url = ev["params"]["response"].get("url") or ""
        if not rid:
            continue
        if not any(k in url for k in ("graphql", "/rest/v/", "aweme", "/web/api/", "search")):
            continue
        try:
            r = sess.send("Network.getResponseBody", {"requestId": rid}, timeout=12)
            body = r.get("body") or ""
            if len(body) > 400:
                bodies.append(body)
        except Exception:
            pass
    return bodies


def _open_and_wait(sess, url, settle_wait=6.0, scrolls=3):
    """先开 blank 再 navigate —— 直接在 new_page(url) 里导航会错过早期响应。"""
    sess.send("Network.enable")
    sess.send("Page.enable")
    sess.send("Runtime.enable")
    sess.send("Page.navigate", {"url": url})
    time.sleep(settle_wait)
    for _ in range(scrolls):
        sess.send("Runtime.evaluate", {"expression": "window.scrollBy(0,1200)",
                                      "returnByValue": True})
        time.sleep(1.5)


def _pair(ids, caps, limit):
    """把 id 与 caption 按出现顺序配对（两个正则各自按出现位置，天然对齐）。"""
    out, seen = [], set()
    for i, pid in enumerate(ids):
        if pid in seen:
            continue
        seen.add(pid)
        cap = _unescape(caps[i]) if i < len(caps) else ""
        out.append({"id": pid, "title": cap.strip()[:90]})
        if len(out) >= limit:
            break
    return out


def kuaishou_search(q: str, limit: int = 12) -> list[dict]:
    import browser_cdp as C

    url = "https://www.kuaishou.com/search/video?searchKey=" + urllib.parse.quote(q)
    sess = C.CDPSession(headless=False, timeout=60,
                        persist_dir=str(KS_PROFILE) if KS_PROFILE.exists() else "")
    try:
        sess.start()
        _pid, ws = sess.new_page("about:blank")
        sess.connect(ws)
        _open_and_wait(sess, url, settle_wait=8.0, scrolls=3)
        ids, caps = [], []
        for body in _drain_bodies(sess):
            ids += _KS_ID.findall(body)
            caps += _KS_CAP.findall(body)
        items = _pair(ids, caps, limit)
        return [{
            "platform": "kuaishou",
            "platform_name": "快手",
            "id": it["id"],
            "title": it["title"] or "快手视频",
            "artist": "",
            "album": "",
            "duration": 0,
            "thumb": "",
            "url": "https://www.kuaishou.com/short-video/%s" % it["id"],
            "source": "https://www.kuaishou.com/short-video/%s" % it["id"],
            "engine": "video",
            "downloadable": True,
            "note": "点下载会再解析一次，拿到直链后存成 mp4",
        } for it in items]
    finally:
        sess.close()


def douyin_search(q: str, limit: int = 12) -> list[dict]:
    import browser_cdp as C

    url = "https://www.douyin.com/search/" + urllib.parse.quote(q)
    sess = C.CDPSession(headless=False, timeout=60)   # headless 会撞验证码
    try:
        sess.start()
        _pid, ws = sess.new_page("about:blank")
        sess.connect(ws)
        sess.send("Network.enable")
        sess.send("Page.enable")
        sess.send("Runtime.enable")
        sess.send("Page.navigate", {"url": url})
        # 抖音偶尔弹人机验证，多等几轮看它自己过不过
        for _ in range(6):
            time.sleep(2.5)
            r = sess.send("Runtime.evaluate", {"expression": "document.title",
                                               "returnByValue": True})
            title = (r.get("result", {}).get("result", {}).get("value") or "")
            if "验证" not in title:
                break
        for _ in range(3):
            sess.send("Runtime.evaluate", {"expression": "window.scrollBy(0,1200)",
                                           "returnByValue": True})
            time.sleep(1.5)
        ids, descs = [], []
        for body in _drain_bodies(sess):
            ids += _DY_ID.findall(body)
            descs += _DY_DESC.findall(body)
        items = _pair(ids, descs, limit)
        return [{
            "platform": "douyin",
            "platform_name": "抖音",
            "id": it["id"],
            "title": it["title"] or "抖音视频",
            "artist": "",
            "album": "",
            "duration": 0,
            "thumb": "",
            "url": "https://www.douyin.com/video/%s" % it["id"],
            "source": "https://www.douyin.com/video/%s" % it["id"],
            "engine": "video",
            "downloadable": True,
            "note": "点下载会再解析一次，拿到去水印直链",
        } for it in items]
    finally:
        sess.close()


if __name__ == "__main__":
    import sys
    kw = sys.argv[1] if len(sys.argv) > 1 else "周杰伦"
    for name, fn in (("快手", kuaishou_search), ("抖音", douyin_search)):
        print("=" * 60)
        print(name, kw)
        try:
            got = fn(kw, 10)
            print(" 共", len(got), "条")
            for g in got[:8]:
                print("   %-22s %s" % (g["id"], g["title"][:52]))
        except Exception as e:
            print(" 失败:", type(e).__name__, e)