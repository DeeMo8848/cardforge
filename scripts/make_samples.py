"""生成 DIY 素材示例：背景/卡面/边框/特效（程序化生成，可按喜好替换）。"""
from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
A = ROOT / "assets"
W, H = 900, 1200


def gradient(size, top, bottom, mode="RGB"):
    img = Image.new(mode, size)
    d = ImageDraw.Draw(img)
    w, h = size
    for y in range(h):
        t = y / (h - 1)
        color = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        d.line([(0, y), (w, y)], fill=color + (255,) if mode == "RGBA" else color)
    return img


def vignette(img, strength=0.45):
    """四周压暗，模拟景深氛围。"""
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    cx, cy, r = w / 2, h / 2, min(w, h) * 0.72
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(90))
    dark = Image.new("RGBA", (w, h), (0, 0, 0, int(255 * strength)))
    img = img.convert("RGBA")
    img = Image.composite(img, dark, mask)
    return img


def sample_background():
    """暖色羊皮纸渐变背景。"""
    bg = gradient((W, H), (54, 38, 24), (196, 152, 96))
    bg = vignette(bg, 0.30)
    return bg


def sample_face():
    """深蓝夜空卡面底图（带星点）。"""
    random.seed(7)
    face = gradient((W, H), (16, 22, 46), (38, 56, 96))
    d = ImageDraw.Draw(face, "RGBA")
    for _ in range(140):
        x, y = random.randint(0, W), random.randint(0, H)
        r = random.choice((1, 1, 2, 2, 3))
        a = random.randint(120, 230)
        d.ellipse((x - r, y - r, x + r, y + r), fill=(235, 240, 255, a))
    return vignette(face, 0.42)


def sample_frame():
    """金色圆角边框（透明背景）。"""
    frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(frame, "RGBA")
    m = 34  # 外边距
    # 外描边
    d.rounded_rectangle((m - 8, m - 8, W - m + 8, H - m + 8), radius=46,
                        outline=(60, 40, 16, 255), width=14)
    # 金色主框
    d.rounded_rectangle((m, m, W - m, H - m), radius=40,
                        outline=(235, 190, 96, 255), width=12)
    # 内侧细线
    d.rounded_rectangle((m + 16, m + 16, W - m - 16, H - m - 16), radius=30,
                        outline=(196, 148, 62, 180), width=3)
    # 四角菱形装饰
    for cx, cy in ((m + 14, m + 14), (W - m - 14, m + 14),
                   (m + 14, H - m - 14), (W - m - 14, H - m - 14)):
        r = 16
        d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)],
                  fill=(255, 214, 120, 255), outline=(90, 60, 20, 255))
    return frame


def sample_seal():
    """斜向光泽卡封（半透明）。"""
    fx = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(fx, "RGBA")
    band = Image.new("RGBA", (W, 320), (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    for i in range(320):
        t = abs(i - 160) / 160
        a = int(46 * (1 - t))
        bd.line([(0, i), (W, i)], fill=(255, 244, 214, a))
    band = band.rotate(-24, expand=True, resample=Image.BICUBIC)
    fx.alpha_composite(band, ((W - band.width) // 2, (H - band.height) // 2))
    return fx


def main():
    (A / "backgrounds").mkdir(parents=True, exist_ok=True)
    (A / "faces").mkdir(parents=True, exist_ok=True)
    (A / "frames").mkdir(parents=True, exist_ok=True)
    (A / "seals").mkdir(parents=True, exist_ok=True)

    sample_background().convert("RGB").save(A / "backgrounds" / "sample-1.png")
    sample_face().convert("RGB").save(A / "faces" / "sample-1.png")
    sample_frame().save(A / "frames" / "sample-1.png")
    sample_seal().save(A / "seals" / "sample-1.png")
    print("示例素材已生成：")


if __name__ == "__main__":
    main()
