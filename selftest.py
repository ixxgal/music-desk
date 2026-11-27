#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端到端自检：把每条下载路径都真跑一遍，再回头验证产物是不是真东西。

验的是三件事，缺一不算过：
  1. 任务状态 done，文件确实落在 downloads 里
  2. ffmpeg 能读出时长和编码（不是空壳）
  3. 音频/视频里真有数据：静音检测 mean_volume 不会是 -91 dB，视频有画面尺寸

用法：python selftest.py [port]      （默认 8899，也就是装好的那个）
"""
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
BASE = f"http://127.0.0.1:{PORT}"
ROOT = Path(__file__).resolve().parent

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = "ffmpeg"


# ------------------------------------------------------------------ HTTP
def api(path, data=None, method=None):
    url = BASE + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method or ("POST" if body else "GET"))
    if body:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def submit(source, **kw):
    payload = {"source": source}
    payload.update(kw)
    r = api("/api/jobs", {"items": [payload]})
    return r["jobs"][0]


def wait(jid, limit=240):
    t0 = time.time()
    last = ""
    while time.time() - t0 < limit:
        j = api("/api/jobs/" + jid)["job"]
        stage = f'{j["status"]}|{j["stage"]}'
        if stage != last:
            last = stage
        if j["status"] in ("done", "error"):
            return j
        time.sleep(3)
    return api("/api/jobs/" + jid)["job"]


# ------------------------------------------------------------- 产物体检
def probe(path: Path) -> dict:
    """ffmpeg 读元信息 + 音量/画面检测。返回 dict，ok=False 表示产物有问题。"""
    out = {"exists": path.exists(), "bytes": 0, "duration": 0, "vcodec": "", "acodec": "",
           "width": 0, "height": 0, "bitrate": 0, "mean_volume": None, "ok": False, "why": ""}
    if not path.exists():
        out["why"] = "文件不存在"
        return out
    out["bytes"] = path.stat().st_size
    if out["bytes"] < 40 * 1024:
        out["why"] = f'太小（{out["bytes"]} B），多半是错误页'
        return out

    p = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)],
                       capture_output=True, text=True, errors="ignore")
    info = p.stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", info)
    if m:
        out["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"Video:\s*(\w+)", info)
    if m:
        out["vcodec"] = m.group(1)
    m = re.search(r"Audio:\s*(\w+)", info)
    if m:
        out["acodec"] = m.group(1)
    m = re.search(r"(\d{2,5})x(\d{2,5})", info)
    if m:
        out["width"], out["height"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"bitrate:\s*(\d+)\s*kb/s", info)
    if m:
        out["bitrate"] = int(m.group(1))

    if not out["duration"]:
        out["why"] = "读不出时长（可能只有封面/空流）"
        return out
    if not out["acodec"]:
        out["why"] = "没有音频流"
        return out

    # 音量检测：静音会是 -91 dB 或 exactly 0 的 flat factor
    v = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path),
                        "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True, errors="ignore")
    m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", v.stderr)
    if m:
        out["mean_volume"] = float(m.group(1))
        if out["mean_volume"] <= -90:
            out["why"] = f'静音（mean_volume {out["mean_volume"]} dB）'
            return out

    if out["vcodec"] and not (out["width"] and out["height"]):
        out["why"] = "视频没画面尺寸"
        return out

    out["ok"] = True
    return out


def fmt_dur(s):
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"


# ------------------------------------------------------------------ 用例
def find_free_netease():
    """挑一首网易云免费曲（fee=0）当 netease 直连样本，避免总走换源。"""
    try:
        r = api("/api/search?src=netease&q=" + urllib.parse.quote("纯音乐 轻音乐") + "&limit=12")
        for it in r.get("items", []):
            if it.get("downloadable") and not it.get("vip"):
                return it
    except Exception as e:
        print("  (网易云样本查找失败:", e, ")")
    return None


def main():
    print(f"=== 音乐工具台 端到端自检  {BASE} ===")
    try:
        ping = api("/api/ping")
        print(f'服务在线  ffmpeg={"有" if ping.get("ffmpeg") else "无"}  version={ping.get("version")}')
    except Exception as e:
        print(f"连不上服务：{e}")
        return 1

    dl = ROOT / "downloads"
    cases = []

    # 1) B站（视频转音频）
    cases.append(("B站", dict(source="https://www.bilibili.com/video/av114912554719208")))

    # 2) 网易云（免费曲直链；找不到就用一首已知免费的）
    free = find_free_netease()
    if free:
        cases.append(("网易云", dict(source=free["url"], platform="netease",
                                     platform_id=str(free.get("id") or ""),
                                     title=free.get("title"), artist=free.get("artist"))))
        print(f'  网易云样本：{free.get("title")} - {free.get("artist")}')
    else:
        cases.append(("网易云", dict(source="https://music.163.com/song?id=1473782328", platform="netease",
                                     platform_id="1473782328", title="小尖尖", artist="薛之谦/韩红")))

    # 3) 喜马拉雅（原音频直取）
    cases.append(("喜马拉雅", dict(source="https://www.ximalaya.com/sound/712817729")))

    # 4) QQ音乐元数据 → 自动换源
    try:
        q = api("/api/search?src=qq&q=" + urllib.parse.quote("晴天 周杰伦") + "&limit=3")
        hit = q["items"][0]
        cases.append(("QQ音乐→换源", dict(source=hit["url"], platform="qq", platform_id=str(hit.get("id") or ""),
                                       title=hit.get("title"), artist=hit.get("artist"),
                                       duration=hit.get("duration"))))
    except Exception as e:
        print("  QQ音乐样本跳过：", e)

    # 5) 酷狗元数据 → 自动换源
    try:
        q = api("/api/search?src=kugou&q=" + urllib.parse.quote("晴天 周杰伦") + "&limit=3")
        hit = q["items"][0]
        cases.append(("酷狗→换源", dict(source=hit["url"], platform="kugou", platform_id=str(hit.get("id") or ""),
                                     title=hit.get("title"), artist=hit.get("artist"),
                                     duration=hit.get("duration"))))
    except Exception as e:
        print("  酷狗样本跳过：", e)

    # 6) 抖音（浏览器解析）
    cases.append(("抖音", dict(source="https://www.douyin.com/video/6961737553342991651")))

    # 7) 快手（浏览器解析，可能有风控）
    cases.append(("快手", dict(source="https://www.kuaishou.com/short-video/3xbfsf6nje489hy")))

    # 8) 直链（用喜马拉雅那条音频的 CDN 地址当样本）
    try:
        import platforms as PLT
        tr = PLT.ximalaya_track("712817729")
        if tr.get("url"):
            cases.append(("音频直链", dict(source=tr["url"], title="直链测试")))
    except Exception as e:
        print("  直链样本跳过：", e)

    results = []
    for i, (name, payload) in enumerate(cases, 1):
        print(f"\n[{i}/{len(cases)}] {name} …")
        try:
            job = submit(**payload)
        except Exception as e:
            results.append((name, "提交失败", str(e), None))
            print("   提交失败:", e)
            continue
        print(f'   任务 {job["id"]}  platform={job.get("platform") or "?"}  engine={job.get("engine")}')
        j = wait(job["id"])
        if j["status"] != "done":
            results.append((name, "任务失败", (j.get("error") or "")[:110], None))
            print("   预期外失败:", (j.get("error") or "")[:160])
            continue
        fname = j.get("filename") or ""
        path = dl / fname
        # 安装版下载目录可能在 %LOCALAPPDATA%
        if not path.exists():
            alt = Path.home() / "AppData" / "Local" / "MusicDesk" / "downloads" / fname
            if alt.exists():
                path = alt
        info = probe(path)
        verdict = "通过" if info["ok"] else "产物异常"
        detail = ""
        if info["ok"]:
            bits = [fmt_dur(info["duration"]), f'{info["bytes"] / 1048576:.1f}MB']
            if info["acodec"]:
                bits.append(info["acodec"] + (f' {info["bitrate"]}kbps' if info["bitrate"] else ""))
            if info["width"]:
                bits.append(f'{info["width"]}x{info["height"]}')
            if info["mean_volume"] is not None:
                bits.append(f'音量{info["mean_volume"]:.1f}dB')
            detail = " · ".join(bits)
        else:
            detail = info["why"]
        results.append((name, verdict, detail, path.name))
        print(f'   {verdict}: {fname}  {detail}')

    print("\n" + "=" * 78)
    print(f'{"路径":<14}{"结果":<10}详情')
    print("-" * 78)
    for name, verdict, detail, _ in results:
        print(f"{name:<14}{verdict:<10}{detail}")
    print("=" * 78)

    ok = sum(1 for r in results if r[1] == "通过")
    print(f"\n通过 {ok} / {len(results)}")
    return 0 if ok == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())