"""
音乐下载台 · 桌面版

双击运行 = 内置 HTTP 服务 + 原生窗口（WebView2 内核），
不依赖浏览器，也不需要手动起服务。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).resolve().parent
else:
    BASE = Path(__file__).resolve().parent
os.chdir(BASE)
sys.path.insert(0, str(BASE))

if sys.stdout is None or sys.stderr is None:  # --windowed 打包时无控制台
    class _Null:
        def write(self, *a): pass
        def flush(self): pass
        encoding = "utf-8"
    sys.stdout = sys.stdout or _Null()
    sys.stderr = sys.stderr or _Null()

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass


def suppress_console_windows() -> None:
    """--windowed 打包后没有控制台，任何子进程（ffmpeg / powershell / edge）都会
    各弹一个黑色 cmd 窗口。这里统一给 subprocess.Popen 补上 CREATE_NO_WINDOW，
    连 yt-dlp 内部调用 ffmpeg 也一并盖住。"""
    if os.name != "nt":
        return
    import subprocess as _sp

    flag = 0x08000000  # CREATE_NO_WINDOW
    if getattr(_sp.Popen.__init__, "_md_patched", False):
        return
    _orig = _sp.Popen.__init__

    def _patched(self, *args, **kwargs):
        kwargs["creationflags"] = int(kwargs.get("creationflags") or 0) | flag
        return _orig(self, *args, **kwargs)

    _patched._md_patched = True
    _sp.Popen.__init__ = _patched


suppress_console_windows()

import server as S  # noqa: E402

try:                                    # PyInstaller 启动闪屏
    import pyi_splash  # type: ignore
except Exception:
    pyi_splash = None


def close_splash():
    if pyi_splash:
        try:
            pyi_splash.close()
        except Exception:
            pass

APP_NAME = "音乐下载台"
SINGLE_INSTANCE_MUTEX = "MusicDesk_SingleInstance_v1"
_mutex_handle = None


def acquire_single_instance() -> bool:
    """命名互斥量判重。True = 本次是第一个实例（拿不到锁说明已有窗口在跑）。"""
    global _mutex_handle
    if os.name != "nt":
        return True
    import ctypes
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, True, SINGLE_INSTANCE_MUTEX)
    return ctypes.windll.kernel32.GetLastError() != 183      # ERROR_ALREADY_EXISTS


def focus_existing_window() -> bool:
    """把已经在跑的那个窗口顶到前面。"""
    if os.name != "nt":
        return False
    import ctypes
    for _ in range(8):
        hwnd = ctypes.windll.user32.FindWindowW(None, APP_NAME)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)        # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            return True
        time.sleep(0.25)
    return False


def start_server(port: int):
    S.load_jobs()
    host = "0.0.0.0" if S.settings.get("allow_lan") else "127.0.0.1"
    srv = S.ThreadingHTTPServer((host, port), S.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def splashed_screen(win):
    """窗口显示后的一点入场效果。"""
    try:
        win.show()
    except Exception:
        pass


def dark_titlebar() -> None:
    """把系统标题栏改成深色，并染成和界面一致的墨蓝。"""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    hwnd = ctypes.windll.user32.FindWindowW(None, APP_NAME)
    if not hwnd:
        return
    try:
        dwm = ctypes.windll.dwmapi
        one = ctypes.c_int(1)
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE（旧系统是 19）
        for attr in (20, 19):
            if dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), attr,
                                         ctypes.byref(one), ctypes.sizeof(one)) == 0:
                break
        # 35 = CAPTION_COLOR, 36 = TEXT_COLOR, 34 = BORDER_COLOR（COLORREF: 0x00BBGGRR）
        caption = ctypes.c_int(0x00160F0B)   # #0b0f16
        text = ctypes.c_int(0x00E9EEF8)      # #f8eee9
        border = ctypes.c_int(0x00160F0B)
        dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 35, ctypes.byref(caption), 4)
        dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 36, ctypes.byref(text), 4)
        dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 34, ctypes.byref(border), 4)
    except Exception:
        pass


def enable_native_menus():
    """pywebview 只在 debug 模式打开 WebView2 的默认右键菜单
    （edgechromium.py 里 `settings.AreDefaultContextMenusEnabled = _state['debug']`），
    所以默认状态下右键点不出菜单、也就没法粘贴。

    这里单独把右键菜单和浏览器快捷键打开，不整个切到 debug 模式。
    """
    try:
        from webview.platforms import edgechromium as _ec
    except Exception:
        return
    orig = getattr(_ec.EdgeChrome, "on_webview_ready", None)
    if orig is None or getattr(orig, "_music_desk_patched", False):
        return

    def patched(self, sender, args):
        orig(self, sender, args)
        try:
            st = sender.CoreWebView2.Settings
            st.AreDefaultContextMenusEnabled = True    # 右键 → 复制 / 粘贴 / 全选
            st.AreBrowserAcceleratorKeysEnabled = True  # Ctrl+A/C/V、F5
        except Exception:
            pass

    patched._music_desk_patched = True
    _ec.EdgeChrome.on_webview_ready = patched


def main():
    # 已经有一个窗口在跑 → 把它顶到前面就退出（绝不再甩一个浏览器出来）
    if not acquire_single_instance():
        if focus_existing_window():
            close_splash()
            return
        # 有僵死实例但找不到窗口：本次照常启动，端口会自动避让

    port = S.pick_port(8899, "0.0.0.0" if S.settings.get("allow_lan") else "127.0.0.1")
    srv = start_server(port)
    url = f"http://127.0.0.1:{port}/"

    import webview

    enable_native_menus()

    window = webview.create_window(
        APP_NAME,
        url,
        width=1300,
        height=880,
        min_size=(900, 620),
        background_color="#070a10",
        text_select=True,
        confirm_close=False,
    )

    def on_closing():
        try:
            from concurrent.futures import ThreadPoolExecutor
            S._pool.shutdown(wait=False)
        except Exception:
            pass
        try:
            srv.shutdown()
        except Exception:
            pass

    window.events.closed += on_closing

    def on_shown():
        close_splash()
        time.sleep(0.35)
        dark_titlebar()

    try:
        webview.start(on_shown, gui="edgechromium",
                      debug=bool(os.environ.get("MUSIC_DESK_DEBUG")),
                      private_mode=False, storage_path=str(BASE / "data" / "webview"))
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()