# -*- coding: utf-8 -*-
"""从项目 icon.png 生成 Android 各尺寸 launcher 图标。"""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "icon.png"
RES = ROOT / "android" / "app" / "src" / "main" / "res"

SIZES = {
    "mipmap-mdpi": 48,
    "mipmap-hdpi": 72,
    "mipmap-xhdpi": 96,
    "mipmap-xxhdpi": 144,
    "mipmap-xxxhdpi": 192,
}

src = Image.open(SRC).convert("RGBA")
print("源图:", src.size)

for folder, size in SIZES.items():
    d = RES / folder
    d.mkdir(parents=True, exist_ok=True)
    img = src.resize((size, size), Image.LANCZOS)
    out = d / "ic_launcher.png"
    img.save(out, "PNG", optimize=True)
    print("  {}  {}x{}  {} bytes".format(folder, size, size, out.stat().st_size))

print("完成")