"""生成应用图标 icon.ico：紫色渐变圆角方块 + 白色音符。"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent
S = 1024
SS = 4  # 超采样
W = S * SS

# 渐变：左上 #7c5cff → 右下 #22d3ee（对角）
c1 = (124, 92, 255)
c2 = (34, 211, 238)
grad = Image.new("RGB", (W, W))
px = grad.load()
for y in range(W):
    for x in range(0, W, 2):
        t = (x / W * 0.65 + y / W * 0.35)
        r = int(c1[0] + (c2[0] - c1[0]) * t)
        g = int(c1[1] + (c2[1] - c1[1]) * t)
        b = int(c1[2] + (c2[2] - c1[2]) * t)
        px[x, y] = (r, g, b)
        if x + 1 < W:
            px[x + 1, y] = (r, g, b)

# 圆角遮罩
mask = Image.new("L", (W, W), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, W - 1], radius=int(W * 0.225), fill=255)

icon = Image.new("RGBA", (W, W), (0, 0, 0, 0))
icon.paste(grad, (0, 0), mask)

# 音符（优先用系统符号字体）
note = "\u266b"
font = None
for cand in ("seguisym.ttf", "SegoeUISymbol.ttf", "arial.ttf"):
    p = Path("C:/Windows/Fonts") / cand
    if p.exists():
        try:
            font = ImageFont.truetype(str(p), int(W * 0.62))
            break
        except Exception:
            continue

d = ImageDraw.Draw(icon)
if font:
    bbox = d.textbbox((0, 0), note, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((W - tw) / 2 - bbox[0], (W - th) / 2 - bbox[1] - int(W * 0.02)),
           note, font=font, fill=(255, 255, 255, 245))
else:
    # 兜底：几何音符
    cx, cy, r = W * 0.40, W * 0.72, W * 0.11
    d.ellipse([cx - r, cy - r * 0.78, cx + r, cy + r * 0.78], fill=(255, 255, 255, 245))
    d.rectangle([cx + r * 0.72, W * 0.24, cx + r * 1.02, cy], fill=(255, 255, 255, 245))
    d.polygon([(cx + r * 1.02, W * 0.24), (cx + r * 2.2, W * 0.33),
               (cx + r * 2.2, W * 0.42), (cx + r * 1.02, W * 0.36)],
              fill=(255, 255, 255, 245))

icon = icon.resize((S, S), Image.LANCZOS)
sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
out = BASE / "icon.ico"
icon.save(out, format="ICO", sizes=sizes)
icon.resize((256, 256), Image.LANCZOS).save(BASE / "icon.png")
print("icon.ico", out.stat().st_size, "bytes")