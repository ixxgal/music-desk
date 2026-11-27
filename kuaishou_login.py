#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给快手扫码登录一次，把登录态存进固定的浏览器 profile。

为什么要这么做：快手的详情页对「没登录的新设备」几乎不给作品数据
（服务端直接返回 "photo": null），只能拿到推荐流里别的视频。
登录一次之后 profile 里有了 cookie，详情接口就会正常返回目标作品。

跑完之后 browser_cdp 里 _fetch_kuaishou 用的就是这个 profile，不改代码。

用法：python kuaishou_login.py [等待秒数]    默认等 300 秒
"""
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / "data" / "cdp-kuaishou"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser():
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def login_cookies():
    """数一下 profile 里跟快手登录有关的 cookie。浏览器开着时库是锁的，复制出来再读。"""
    ck = PROFILE / "Default" / "Network" / "Cookies"
    if not ck.exists():
        return 0
    tmpdir = Path(tempfile.mkdtemp())
    try:
        dst = tmpdir / "c.sqlite"
        shutil.copy2(ck, dst)
        con = sqlite3.connect(str(dst))
        rows = con.execute(
            "select name from cookies where host_key like '%kuaishou%'"
        ).fetchall()
        con.close()
        names = {r[0] for r in rows}
        # kuaishou 登录后会出现 userId / kuaishou.server.web_st 这类
        hit = [n for n in names if "userId" in n or "passToken" in n or "web_st" in n]
        return len(hit)
    except Exception:
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    wait_secs = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    exe = find_browser()
    if not exe:
        print("没找到 Edge / Chrome")
        return 1

    PROFILE.mkdir(parents=True, exist_ok=True)
    print("=" * 60)
    print("快手登录（只需做一次）")
    print("=" * 60)
    print(f"浏览器：{exe}")
    print(f"登录态目录：{PROFILE}")
    print()
    print("接下来会弹出一个浏览器窗口：")
    print("  1. 点右上角「登录」，用快手 App 扫码")
    print("  2. 看到自己的头像就算成功")
    print("  3. 不用管它，脚本检测到登录 cookie 会自动关掉窗口")
    print(f"  （最多等 {wait_secs} 秒）")
    print()

    before = login_cookies()
    print(f"登录前 cookie 数：{before}")

    proc = subprocess.Popen([
        exe,
        f"--user-data-dir={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1180,900",
        "https://www.kuaishou.com/",
    ])

    t0 = time.time()
    seen = 0
    while time.time() - t0 < wait_secs:
        time.sleep(3)
        n = login_cookies()
        if n > seen:
            seen = n
            print(f"  [{int(time.time() - t0)}s] 检测到登录 cookie × {n}")
        if n >= 1 and n == before and before > 0:
            break
        if n >= 1 and before == 0 and time.time() - t0 > 12:
            break

    print("正在关闭浏览器（给 cookie 落盘留几秒）…")
    time.sleep(2)
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    time.sleep(2)

    final = login_cookies()
    print()
    if final:
        print(f"✓ 登录态已保存（{final} 个登录 cookie）。之后下载快手视频会复用这个 profile。")
        return 0
    print("× 没检测到登录 cookie。可能没扫完码，或页面没登录成功。重跑一次即可。")
    return 2


if __name__ == "__main__":
    sys.exit(main())