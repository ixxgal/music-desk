# -*- coding: utf-8 -*-
"""用登录态从快手热榜接口取真实作品，验证 fetch_video_info 全链路。"""
import json
import pathlib
import re
import time

import browser_cdp as B

PROFILE = str(pathlib.Path(__file__).resolve().parent / "data" / "cdp-kuaishou")
s = B.CDPSession(headless=False, persist_dir=PROFILE, timeout=60)
feed_body = ""
try:
    s.start()
    pid, ws = s.new_page("about:blank")
    s.connect(ws)
    s.send("Network.enable")
    s.send("Page.enable")
    s.send("Page.navigate", {"url": "https://www.kuaishou.com/"})
    time.sleep(16)
    for e in s.events(0):
        if e.get("method") != "Network.responseReceived":
            continue
        u = (e.get("params", {}).get("response", {}) or {}).get("url", "")
        if "/rest/v/feed/hot" in u:
            try:
                feed_body = s.send("Network.getResponseBody", {"requestId": e["params"]["requestId"]}).get("result", {}).get("body", "")
            except Exception:
                pass
finally:
    s.close()

print("feed body len:", len(feed_body))
if not feed_body:
    raise SystemExit("没抓到热榜接口")

data = json.loads(feed_body)
feeds = data.get("feeds") or []
print("feeds:", len(feeds))
picked = []
for f in feeds:
    ph = f.get("photo") or {}
    pid = ph.get("id") or f.get("photoId")
    cap = ph.get("caption") or ""
    dur = ph.get("duration")
    if pid:
        picked.append((pid, cap, dur))
print("可用作品 %d 条，前 5 条：" % len(picked))
for pid, cap, dur in picked[:5]:
    print("  %-22s %-45s %sms" % (pid, cap[:40], dur))

if not picked:
    raise SystemExit("热榜里没有 photo.id")

target = picked[0][0]
print("\n=== 用登录态解析热榜第一条 %s ===" % target)
info = B.fetch_video_info("https://www.kuaishou.com/short-video/%s" % target)
shown = dict(info)
shown["url"] = (shown.get("url") or "")[:100]
print(json.dumps(shown, ensure_ascii=False, indent=2))
print("\nRESULT: ok=%s exact=%s title_match=%s" % (
    info.get("ok"), info.get("exact"),
    (info.get("title") or "")[:14] == (picked[0][1] or "")[:14]))