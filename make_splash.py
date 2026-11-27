"""生成启动闪屏 splash.png。"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent
W, H = 520, 300
SS = 3
w, h = W * SS, H * SS

img = Image.new("RGB", (w, h), (7, 10, 16))
d = ImageDraw.Draw(img)

# 顶部光晕
glow = Image.new("RGB", (w, h), (7, 10, 16))
gd = ImageDraw.Draw(glow)
gd.ellipse([-w * 0.25, -h * 0.9, w * 0.75, h * 0.45], fill=(20, 18, 58))
gd.ellipse([w * 0.45, -h * 0.7, w * 1.3, h * 0.35], fill=(8, 40, 52))
img = Image.blend(img, glow, 0.75)
d = ImageDraw.Draw(img)

# 图标
icon = Image.open(BASE / "icon.png").convert("RGBA").resize((112 * SS, 112 * SS), Image.LANCZOS)
ix, iy = (w - icon.width) // 2, int(h * 0.18)
img.paste(icon, (ix, iy), icon)

# 圆角遮挡（把四角染回背景色，模拟圆角窗口）
mask = Image.new("L", (w, h), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=18 * SS, fill=255)
flat = Image.new("RGB", (w, h), (7, 10, 16))
img = Image.composite(img, flat, mask)
d = ImageDraw.Draw(img)

def font(size, bold=False):
    names = ["msyhbd.ttc", "msyh.ttc"] if bold else ["msyh.ttc", "segoeui.ttf"]
    for n in names:
        p = Path("C:/Windows/Fonts") / n
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                continue
    return ImageFont.load_default()

f1 = font(int(30 * SS), bold=True)
f2 = font(int(13 * SS))
title = "音乐工具台"
bbox = d.textbbox((0, 0), title, font=f1)
d.text(((w - (bbox[2] - bbox[0])) / 2, int(h * 0.60)), title, font=f1, fill=(233, 238, 248))

sub = "链接 · 描述 · 文件  →  MP3"
bbox2 = d.textbbox((0, 0), sub, font=f2)
d.text(((w - (bbox2[2] - bbox2[0])) / 2, int(h * 0.75)), sub, font=f2, fill=(110, 122, 145))

# 底部加载条
bar_w = int(w * 0.28)
bx = (w - bar_w) // 2
by = int(h * 0.87)
d.rounded_rectangle([bx, by, bx + bar_w, by + 4 * SS], radius=2 * SS, fill=(30, 36, 52))
d.rounded_rectangle([bx, by, bx + int(bar_w * 0.62), by + 4 * SS], radius=2 * SS, fill=(124, 92, 255))

img.resize((W, H), Image.LANCZOS).save(BASE / "splash.png")
print("splash.png ok", (BASE / "splash.png").stat().st_size)