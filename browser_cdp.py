"""用本机 Edge 的 CDP 协议抓取需要签名的站点（抖音 / 快手）。

纯 HTTP 打不通的原因：这两个站点的接口都要求前端 JS 生成的设备指纹
（抖音 `s_v_web_id` / `a_bogus`，快手 GraphQL 的 `kuaishou.web.cp.api` 签名）。
与其逆算法（随时失效），不如开一个真实内核跑页面，把网络响应抓回来。

用法：
    from browser_cdp import fetch_video_info
    info = fetch_video_info("https://www.douyin.com/video/7xxxx")
    info["url"]        # 视频直链
    info["title"]      # 标题
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request

try:
    import websocket  # websocket-client
except Exception:  # pragma: no cover
    websocket = None

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def find_browser():
    for p in EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


class CDPError(RuntimeError):
    pass


class CDPSession:
    """最小可用的 CDP 客户端：开浏览器、开标签页、发命令、收事件。"""

    def __init__(self, headless=True, port=0, timeout=45, persist_dir=""):
        self.port = port or self._free_port()
        self.timeout = timeout
        self.proc = None
        self.profile = None
        self.persist_dir = persist_dir
        self._own_profile = not persist_dir
        self.ws = None
        self._id = 0
        self._answers = {}
        self._events = []
        self._lock = threading.Lock()
        self._reader = None
        self.headless = headless

    # ---------- 基础设施 ----------

    @staticmethod
    def _free_port():
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    def start(self):
        exe = find_browser()
        if not exe:
            raise CDPError("没有找到 Edge / Chrome")
        if self.persist_dir:
            os.makedirs(self.persist_dir, exist_ok=True)
            self.profile = self.persist_dir
        else:
            self.profile = tempfile.mkdtemp(prefix="cdp-")
        args = [
            exe,
            "--remote-debugging-port=%d" % self.port,
            "--user-data-dir=%s" % self.profile,
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--mute-audio",
            "--window-size=1280,900",
            "--window-position=-32000,-32000",  # 挪出屏幕，避免闪烁
        ]
        if self.headless:
            args.append("--headless=new")
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        deadline = time.time() + 25
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/json/version" % self.port, timeout=1) as r:
                    json.load(r)
                return self
            except Exception as e:  # 浏览器还在起
                last = e
                time.sleep(0.25)
        raise CDPError("浏览器 CDP 端口没起来: %s" % last)

    def new_page(self, url="about:blank"):
        """开一个新标签页，返回 (targetId, webSocketDebuggerUrl)。"""
        req = urllib.request.Request(
            "http://127.0.0.1:%d/json/new?%s" % (self.port, url),
            method="PUT",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.load(r)
        except Exception:
            # 老版本只支持 GET
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/new?%s" % (self.port, url), timeout=10) as r:
                d = json.load(r)
        return d["id"], d["webSocketDebuggerUrl"]

    def connect(self, ws_url):
        if websocket is None:
            raise CDPError("缺少 websocket-client，执行 pip install websocket-client")
        self.ws = websocket.create_connection(
            ws_url, timeout=self.timeout, suppress_origin=True)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        return self

    def _read_loop(self):
        while True:
            try:
                raw = self.ws.recv()
            except Exception:
                return
            if not raw:
                return
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mid = msg.get("id")
            if mid is not None:
                with self._lock:
                    self._answers[mid] = msg
            else:
                with self._lock:
                    self._events.append(msg)

    def send(self, method, params=None, timeout=20):
        self._id += 1
        mid = self._id
        payload = {"id": mid, "method": method}
        if params:
            payload["params"] = params
        self.ws.send(json.dumps(payload))
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if mid in self._answers:
                    return self._answers.pop(mid)
            time.sleep(0.02)
        raise CDPError("CDP 命令超时: %s" % method)

    def events(self, since=0):
        with self._lock:
            return self._events[since:]

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        try:
            if self.proc:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=6)
                except Exception:
                    self.proc.kill()
        except Exception:
            pass
        if self.profile and self._own_profile:
            shutil.rmtree(self.profile, ignore_errors=True)


# ---------------------------------------------------------------- 抖音

DOUYIN_RE = re.compile(r"douyin\.com/(?:video|note)/(\d+)", re.I)


def _dy_pick_from_json(obj, out):
    """从抖音任意 JSON 结构里挖出无水印 play_addr。"""
    if isinstance(obj, dict):
        # video.play_addr.url_list
        va = obj.get("play_addr") or obj.get("playAddr")
        if isinstance(va, dict):
            lst = va.get("url_list") or va.get("urlList") or []
            if isinstance(lst, list):
                for u in lst:
                    if isinstance(u, str) and u.startswith("http"):
                        out.append(u)
        if isinstance(obj.get("video"), dict):
            _dy_pick_from_json(obj["video"], out)
        for k in ("aweme_detail", "awemeDetail", "data", "item_list", "aweme_list"):
            if k in obj:
                _dy_pick_from_json(obj[k], out)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _dy_pick_from_json(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _dy_pick_from_json(v, out)


def _fetch_douyin(url):
    """返回 {ok,url,title,error}"""
    out = {"ok": False, "url": "", "title": "", "error": ""}
    sess = CDPSession(headless=True)
    try:
        sess.start()
        tid, ws = sess.new_page("about:blank")
        sess.connect(ws)
        sess.send("Network.enable")
        sess.send("Page.enable")
        sess.send("Page.navigate", {"url": url})

        # 等 detail 接口出现
        deadline = time.time() + 30
        seen = 0
        target_id = None
        while time.time() < deadline:
            evs = sess.events(seen)
            seen += len(evs)
            for ev in evs:
                if ev.get("method") != "Network.responseReceived":
                    continue
                p = ev.get("params") or {}
                u = ((p.get("response") or {}).get("url") or "")
                if "aweme/detail" in u or "aweme/v1/web/aweme" in u:
                    target_id = p.get("requestId")
                    break
            if target_id:
                break
            time.sleep(0.3)

        body = ""
        if target_id:
            r = sess.send("Network.getResponseBody", {"requestId": target_id})
            body = ((r.get("result") or {}).get("body") or "")

        if not body:
            # 退路：从 DOM 里直接找 video 标签
            r = sess.send("Runtime.evaluate", {
                "expression": "JSON.stringify([...document.querySelectorAll('video')]"
                              ".map(v=>v.src||v.currentSrc).filter(Boolean))",
                "returnByValue": True,
            })
            raw = ((r.get("result") or {}).get("result") or {}).get("value") or "[]"
            try:
                urls = json.loads(raw)
            except Exception:
                urls = []
            if urls:
                out.update({"ok": True, "url": urls[0]})
                return out
            out["error"] = "没抓到详情接口，也没找到 video 标签"
            return out

        try:
            data = json.loads(body)
        except Exception:
            out["error"] = "详情接口返回的不是 JSON"
            return out

        detail = data.get("aweme_detail") or {}
        out["title"] = (detail.get("desc") or "").strip()
        cands = []
        _dy_pick_from_json(detail, cands)
        cands = [u for u in dict.fromkeys(cands) if u.startswith("http")]
        if not cands:
            out["error"] = "接口里没有 play_addr"
            return out
        # 优先 play（无 watermark），其次 playwm
        cands.sort(key=lambda u: (0 if "/play/" in u else 1 if "playwm" in u else 2))
        out["url"] = cands[0]
        out["ok"] = True
        return out
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out
    finally:
        sess.close()


# ---------------------------------------------------------------- 快手

KUAISHOU_RE = re.compile(r"kuaishou\.com/(?:short-video|photo)/([\w-]+)", re.I)


def _ks_pick_from_json(obj, out):
    """快手：photoUrls 是 [{"cdn":.., "url":"..mp4"}]，另有 manifest 但太复杂。"""
    if isinstance(obj, dict):
        pu = obj.get("photoUrls")
        if isinstance(pu, list):
            for it in pu:
                if isinstance(it, dict):
                    u = it.get("url") or ""
                    if u.startswith("http"):
                        out.append(u)
                elif isinstance(it, str) and it.startswith("http"):
                    out.append(it)
        for k in ("photoUrl", "srcNoMark", "playUrl"):
            v = obj.get(k)
            if isinstance(v, str) and v.startswith("http"):
                out.append(v)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _ks_pick_from_json(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _ks_pick_from_json(v, out)


def _ks_find_photo(obj, target_id, acc, caption_hint=""):
    """在快手 graphql JSON 里定位目标作品，取它的 photoUrl / caption。

    页面里混着推荐流的其它视频，必须精确对齐，否则会下错片。
    两把尺子：优先 photo id，其次页面标题里的 caption 前缀
    （详情页未登录时 id 字段可能不在响应里，但 caption 一定在）。
    """
    if isinstance(obj, dict):
        oid = obj.get("id")
        cap = (obj.get("caption") or "").strip()
        hit = False
        if isinstance(oid, str) and target_id and oid == target_id:
            hit = True
        elif caption_hint and cap and cap[:14] == caption_hint[:14]:
            hit = True
        if hit:
            urls = []
            pu = obj.get("photoUrls")
            if isinstance(pu, list):
                for it in pu:
                    if isinstance(it, dict) and (it.get("url") or "").startswith("http"):
                        urls.append(it["url"])
            for k in ("photoUrl", "srcNoMark", "playUrl"):
                v = obj.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    urls.append(v)
            if urls:
                acc["urls"] = urls
                acc["caption"] = cap
                acc["duration"] = int((obj.get("duration") or 0) / 1000) or 0
                return True
        for v in obj.values():
            if isinstance(v, (dict, list)):
                if _ks_find_photo(v, target_id, acc, caption_hint):
                    return True
    elif isinstance(obj, list):
        for v in obj:
            if _ks_find_photo(v, target_id, acc, caption_hint):
                return True
    return False


def _fetch_kuaishou(url):
    """快手：headless 会被判定为旧浏览器，必须 headful（窗口已移出屏幕）。

    视频直链可能来自两个地方：
      - 详情接口响应（含 photoUrls）
      - 页面上 <video> 的 src
    标题从 document.title 取（形如 "标题-快手"）。
    """
    out = {"ok": False, "url": "", "title": "", "error": "", "exact": False}
    target_id = ""
    m = KUAISHOU_RE.search(url)
    if m:
        target_id = m.group(1)
    sess = CDPSession(headless=False, persist_dir=str(Path(__file__).resolve().parent / "data" / "cdp-kuaishou"))
    try:
        sess.start()
        tid, ws = sess.new_page("about:blank")
        sess.connect(ws)
        sess.send("Network.enable")
        sess.send("Page.enable")
        sess.send("Page.navigate", {"url": url})

        # 快手是 SPA，详情数据在若干次 graphql 里陆续到达。
        # 早退会抓到空壳，所以固定等到足够多的响应出现。
        deadline = time.time() + 30
        seen = 0
        idle = 0
        while time.time() < deadline:
            evs = sess.events(seen)
            seen += len(evs)
            new = 0
            for ev in evs:
                if ev.get("method") != "Network.responseReceived":
                    continue
                p = ev.get("params") or {}
                u = ((p.get("response") or {}).get("url") or "")
                if ("graphql" in u) or ("/rest/v/" in u and "log" not in u
                                        and "comment" not in u):
                    new += 1
            if new:
                idle = 0
            else:
                idle += 1
            if seen > 0 and idle >= 12:      # 连续 12 轮（约 4s）没有新响应 → 稳定了
                break
            time.sleep(0.35)
        time.sleep(1.2)

        # ⓪ 最可靠的一手：页面上 <video> 的 src。详情页渲染出的播放器就是当前这条，
        #    URL 里的 clientCacheKey=<目标id>_b.mp4 等于给了一个硬指纹，
        #    推荐流顶替不了它。顺带把 video.duration 当二次校验。
        dom_urls = []
        dom_dur = 0.0
        try:
            r = sess.send("Runtime.evaluate", {
                "expression": "JSON.stringify([...document.querySelectorAll('video')]"
                              ".map(v=>({s:v.src||v.currentSrc||'',d:v.duration||0})))",
                "returnByValue": True,
            })
            raw = ((r.get("result") or {}).get("result") or {}).get("value") or "[]"
            for item in json.loads(raw):
                su = item.get("s") or ""
                if not su.startswith("http"):
                    continue
                if target_id and (target_id in su):
                    dom_urls.append(su)
                    dom_dur = item.get("d") or 0
        except Exception:
            pass

        best = ""
        wanted = []
        for ev in sess.events(0):
            if ev.get("method") != "Network.responseReceived":
                continue
            p = ev.get("params") or {}
            u = ((p.get("response") or {}).get("url") or "")
            if ("graphql" in u) or ("/rest/v/" in u and "log" not in u
                                    and "comment" not in u):
                wanted.append(p.get("requestId"))

        # 先把所有响应体取回来
        bodies = []
        for rid in wanted:
            try:
                r = sess.send("Network.getResponseBody", {"requestId": rid})
                bodies.append(((r.get("result") or {}).get("body") or ""))
            except Exception:
                continue

        # ① 精确对齐：必须找到目标作品，否则宁可报错也不下错片
        cands = list(dom_urls)
        if cands:
            out["exact"] = True
            if dom_dur:
                out["duration"] = round(float(dom_dur), 1)
        page_caption = (out.get("title") or "").strip()
        if page_caption in ("短视频", "快手"):
            page_caption = ""
        if not cands and (target_id or page_caption):
            for body in bodies:
                if target_id and target_id not in body and (
                        not page_caption or page_caption[:14] not in body):
                    continue
                try:
                    data = json.loads(body)
                except Exception:
                    continue
                acc = {}
                if _ks_find_photo(data, target_id, acc, page_caption) and acc.get("urls"):
                    cands = acc["urls"]
                    out["title"] = acc.get("caption") or out.get("title") or ""
                    out["duration"] = acc.get("duration") or 0
                    out["exact"] = True
                    break

        # ② 拿不到精确匹配时只认大响应里的 photoUrl，并标记 exact=False
        if not cands:
            for body in bodies:
                if ".mp4" in body and len(body) > len(best):
                    best = body
        if not cands and best:
            out["exact"] = False
            cands = re.findall(r'"photoUrl"\s*:\s*"(https?:[^"\\]+)"', best)
            if not cands:
                cands = re.findall(r'"(?:srcNoMark|playUrl)"\s*:\s*"(https?:[^"\\]+)"', best)
            if not cands:
                cands = [u.replace("\\/", "/") for u in
                         re.findall(r'https?:\\/\\/[^"\\\s]+?\.mp4[^"\\\s]*', best)]

        # 标题
        try:
            rt = sess.send("Runtime.evaluate", {
                "expression": "document.title", "returnByValue": True})
            title = (((rt.get("result") or {}).get("result") or {}).get("value") or "")
            out["title"] = re.sub(r"[-_]快手\s*$", "", title).strip()
        except Exception:
            pass

        # DOM 退路
        if not cands:
            r = sess.send("Runtime.evaluate", {
                "expression": "JSON.stringify([...document.querySelectorAll('video')]"
                              ".map(v=>v.src||v.currentSrc).filter(Boolean))",
                "returnByValue": True,
            })
            raw = ((r.get("result") or {}).get("result") or {}).get("value") or "[]"
            try:
                cands = json.loads(raw)
            except Exception:
                cands = []

        cands = [u for u in dict.fromkeys(cands) if u.startswith("http")]
        if cands:
            # 优先不带 watermark 标记的，其次第一个
            cands.sort(key=lambda u: (1 if "watermark" in u else 0, len(u)))
            out.update({"ok": True, "url": cands[0]})
            return out
        out["error"] = "没抓到视频地址（可能需要登录，或该作品已删除/仅粉丝可见）"
        return out
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out
    finally:
        sess.close()


# ---------------------------------------------------------------- 对外

def fetch_video_info(url):
    """识别平台并抓取视频直链。返回 {ok, platform, id, url, title, error}"""
    url = (url or "").strip()
    res = {"ok": False, "platform": "", "id": "", "url": "", "title": "", "error": ""}

    m = DOUYIN_RE.search(url)
    if m:
        res["platform"] = "douyin"
        res["id"] = m.group(1)
        r = _fetch_douyin("https://www.douyin.com/video/" + m.group(1))
        res.update(r)
        return res

    m = KUAISHOU_RE.search(url)
    if m:
        res["platform"] = "kuaishou"
        res["id"] = m.group(1)
        r = _fetch_kuaishou(url)
        res.update(r)
        return res

    res["error"] = "不是抖音或快手链接"
    return res


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "https://www.douyin.com/video/6961737553342991651"
    print(json.dumps(fetch_video_info(target), ensure_ascii=False, indent=2))