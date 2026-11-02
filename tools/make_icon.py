"""生成「组件版数字人 + 弹幕朗读」一键启动器的桌面图标。

设计: 深紫→洋红渐变圆角方块 + 中央麦克风(彩色 emoji, 回退到手绘麦克风) + 弹幕虚线点缀。
输出: assets/icon-component.ico (16/32/48/64/128/256 多尺寸) 与同名 .png 预览。

跑法: C:\\Users\\<你>\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tools\\make_icon.py
"""
import math
import pathlib

from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_ICO = ROOT / "assets" / "icon-component.ico"
OUT_PNG = ROOT / "assets" / "icon-component.png"
SIZE = 512


def gradient(size: int, top=(38, 24, 74), bottom=(198, 46, 140)) -> Image.Image:
    """竖直渐变背景(深紫 → 洋红)。"""
    img = Image.new("RGB", (1, size))
    px = img.load()
    for y in range(size):
        t = y / max(1, size - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return img.resize((size, size), Image.BILINEAR)


def rounded_mask(size: int, radius_ratio: float = 0.22) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * radius_ratio), fill=255)
    return m


def draw_emoji(img: Image.Image, glyph: str, ratio: float) -> bool:
    """用 Windows 彩色 emoji 字体渲染(anchor='mm' 精确居中); 失败返回 False。
    注意: seguiemj 里 🎙️(U+1F399) 只有灰阶字形, 🎤(U+1F3A4) 才是彩色 → 用后者。"""
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/seguiemj.ttf", int(SIZE * ratio))
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.text((SIZE / 2, SIZE / 2 + SIZE * 0.01), glyph, font=font,
               anchor="mm", embedded_color=True)
        if layer.getbbox() is None:
            return False
        img.alpha_composite(layer)
        return True
    except Exception:
        return False


def draw_glow(img: Image.Image) -> None:
    """麦克风后面的柔光, 让主体从渐变底上"浮"起来。"""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    r = SIZE * 0.34
    for i in range(18):
        rr = r * (1 - i / 22)
        a = int(16 + i * 2.2)
        d.ellipse([SIZE / 2 - rr, SIZE / 2 - rr, SIZE / 2 + rr, SIZE / 2 + rr],
                  fill=(255, 235, 250, min(a, 70)))
    img.alpha_composite(layer)


def draw_mic(img: Image.Image) -> None:
    """手绘麦克风(emoji 不可用时的回退)。"""
    d = ImageDraw.Draw(img)
    cx, cy = SIZE / 2, SIZE / 2 - SIZE * 0.03
    w, h = SIZE * 0.20, SIZE * 0.40
    d.rounded_rectangle([cx - w / 2, cy - h / 2 - SIZE * 0.06, cx + w / 2, cy + h / 2 - SIZE * 0.06],
                        radius=w / 2, fill=(255, 255, 255, 245))
    d.arc([cx - SIZE * 0.22, cy - SIZE * 0.20, cx + SIZE * 0.22, cy + SIZE * 0.26],
          start=20, end=160, fill=(255, 255, 255, 245), width=int(SIZE * 0.05))
    d.rounded_rectangle([cx - SIZE * 0.022, cy + SIZE * 0.24, cx + SIZE * 0.022, cy + SIZE * 0.38],
                        radius=SIZE * 0.022, fill=(255, 255, 255, 245))
    d.rounded_rectangle([cx - SIZE * 0.10, cy + SIZE * 0.36, cx + SIZE * 0.10, cy + SIZE * 0.41],
                        radius=SIZE * 0.025, fill=(255, 255, 255, 245))


def draw_danmaku(img: Image.Image) -> None:
    """点缀: 四角若干条半透明"弹幕"短条(暗示弹幕流, 避开中央主体)。"""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    bars = [(0.05, 0.13, 0.22, 0.042), (0.72, 0.16, 0.19, 0.042),
            (0.06, 0.79, 0.19, 0.042), (0.76, 0.83, 0.18, 0.042)]
    for x, y, w, h in bars:
        d.rounded_rectangle([SIZE * x, SIZE * y, SIZE * (x + w), SIZE * (y + h)],
                            radius=SIZE * h / 2, fill=(255, 255, 255, 105))
    img.alpha_composite(layer)


def draw_badge(img: Image.Image, glyph: str) -> None:
    """右下角白色圆底小徽章(🎤 = 能唱能说), 让图标同时表达"数字人 + 朗读"。"""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy, r = SIZE * 0.755, SIZE * 0.765, SIZE * 0.175
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 240))
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/seguiemj.ttf", int(r * 1.55))
        d.text((cx, cy + r * 0.04), glyph, font=font, anchor="mm", embedded_color=True)
    except Exception:
        pass
    img.alpha_composite(layer)


def build() -> Image.Image:
    base = gradient(SIZE, top=(20, 16, 48), bottom=(126, 30, 152)).convert("RGBA")
    draw_danmaku(base)
    draw_glow(base)
    if not draw_emoji(base, "\U0001F916", 0.50):        # 🤖 数字人(彩色字形最抢眼)
        draw_mic(base)
    draw_badge(base, "\U0001F3A4")                      # 🎤 徽章 = 朗读/唱歌
    mask = rounded_mask(SIZE)
    out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    out.paste(base, (0, 0), mask)
    return out


def main() -> None:
    img = build()
    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT_PNG)
    img.save(OUT_ICO, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"已生成:\n  {OUT_ICO}\n  {OUT_PNG}")


if __name__ == "__main__":
    main()
