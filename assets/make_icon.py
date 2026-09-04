# -*- coding: utf-8 -*-
"""앱 아이콘(icon.ico) 생성 — 실행하면 같은 폴더에 icon.ico 를 만든다.

도안: 어두운 라운드 사각 바탕 위에 3D 큐브(와이어) + 사진 카드.
UI 와 같은 색을 쓴다(배경 #0e1319 · 액센트 #12b5cc).
"""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
BG, ACCENT, ACCENT_2, PAPER = (14, 19, 25), (18, 181, 204), (10, 90, 104), (231, 237, 243)
SIZES = [16, 24, 32, 48, 64, 128, 256]
S = 512                                   # 원본을 크게 그린 뒤 축소해 계단을 줄인다


def rounded(size, radius, fill):
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle([0, 0, size - 1, size - 1], radius, fill=fill)
    return im


def build():
    img = rounded(S, int(S * 0.22), BG + (255,))
    d = ImageDraw.Draw(img)

    # --- 3D 큐브(아이소메트릭 와이어) ---
    cx, cy, r = S * 0.42, S * 0.44, S * 0.23
    top = (cx, cy - r)
    right = (cx + r * 0.87, cy - r * 0.5)
    br = (cx + r * 0.87, cy + r * 0.5)
    bottom = (cx, cy + r)
    bl = (cx - r * 0.87, cy + r * 0.5)
    left = (cx - r * 0.87, cy - r * 0.5)
    w = int(S * 0.030)

    d.polygon([top, right, (cx, cy), left], fill=ACCENT_2 + (255,))      # 윗면만 채워 입체감
    for a, b in [(top, right), (right, br), (br, bottom), (bottom, bl),
                 (bl, left), (left, top), (top, (cx, cy)),
                 ((cx, cy), right), ((cx, cy), left), ((cx, cy), bottom)]:
        d.line([a, b], fill=ACCENT + (255,), width=w, joint="curve")

    # --- 사진 카드(우하단에 겹침) ---
    px, py, pw, ph = S * 0.50, S * 0.54, S * 0.34, S * 0.26
    d.rounded_rectangle([px, py, px + pw, py + ph], int(S * 0.035),
                        fill=PAPER + (255,), outline=BG + (255,), width=int(S * 0.022))
    # 사진 속 풍경(산 + 해)
    d.ellipse([px + pw * 0.68, py + ph * 0.16, px + pw * 0.86, py + ph * 0.42],
              fill=ACCENT + (255,))
    d.polygon([(px + pw * 0.10, py + ph * 0.86), (px + pw * 0.40, py + ph * 0.36),
               (px + pw * 0.70, py + ph * 0.86)], fill=ACCENT_2 + (255,))

    out = os.path.join(HERE, "icon.ico")
    img.save(out, format="ICO", sizes=[(s, s) for s in SIZES])
    img.resize((256, 256), Image.LANCZOS).save(os.path.join(HERE, "icon.png"))
    print("생성:", out)


if __name__ == "__main__":
    build()
