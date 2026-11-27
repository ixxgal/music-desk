# -*- coding: utf-8 -*-
"""把产品名「音乐工具台」统一改成「音乐下载台」。"""
import py_compile
from pathlib import Path

NEW = "音乐下载台"
OLD = "音乐工具台"

# ---- desktop.py：窗口标题 + docstring
p = Path("desktop.py")
t = p.read_text(encoding="utf-8")
t = t.replace(OLD + " · 桌面版", NEW + " · 桌面版")
t = t.replace('APP_NAME = "%s"' % OLD, 'APP_NAME = "%s"' % NEW)
p.write_text(t, encoding="utf-8")

# ---- server.py：docstring / argparse / 启动横幅
p = Path("server.py")
t = p.read_text(encoding="utf-8")
t = t.replace(OLD + " · 后端", NEW + " · 后端")
t = t.replace('description="%s"' % OLD, 'description="%s"' % NEW)
t = t.replace('print("  %s · Music Desk")' % OLD, 'print("  %s · Music Desk")' % NEW)
p.write_text(t, encoding="utf-8")

# ---- install.ps1：快捷方式名 / 描述 / 提示 + 清旧快捷方式
p = Path("install.ps1")
t = p.read_text(encoding="utf-8-sig")
t = t.replace(OLD + ".lnk", NEW + ".lnk")
t = t.replace("'%s — 链接 / 描述 / 文件 转 MP3'" % OLD, "'%s — 贴链接 / 说歌名 自动抓 MP3'" % NEW)
t = t.replace("桌面双击「%s」即可启动" % OLD, "桌面双击「%s」即可启动" % NEW)
if "旧名字" not in t:
    t = t.replace(
        "    $path = Join-Path $t.Dir $t.Name\n",
        "    # 先清掉旧名字留下的快捷方式，免得桌面出现两个图标\n"
        "    $oldPath = Join-Path $t.Dir '" + OLD + ".lnk'\n"
        "    if (Test-Path $oldPath) { Remove-Item $oldPath -Force -ErrorAction SilentlyContinue }\n"
        "    $path = Join-Path $t.Dir $t.Name\n",
    )
p.write_text(t, encoding="utf-8-sig")

for f in ("desktop.py", "server.py"):
    py_compile.compile(f, doraise=True)
print("改名完成 →", NEW, "｜compile OK")

for f in ("desktop.py", "server.py", "install.ps1"):
    txt = Path(f).read_text(encoding="utf-8-sig")
    left = txt.count(OLD)
    print("  %-14s 残留旧名 %d 处，含新名 %d 处" % (f, left, txt.count(NEW)))