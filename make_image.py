#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Instagram 投稿画像（1080x1350）を作る。
既存の @wardrobe__lab の投稿に合わせて「写真＋黒帯のタイトル＋WARDROBE LAB ロゴ」。

  上:   アイキャッチ（16:9 をそのまま全幅）
  中:   黒帯に白文字のタイトル
  下:   アイキャッチを拡大して切り出した写真＋白抜きロゴ
"""

import io
import re

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1350
TOP_H = 608            # 1080 x 16:9
BAND_H = 250
FONT_PATHS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
]


def _font(size):
    for p in FONT_PATHS:
        try:
            return ImageFont.truetype(p, size, index=0)  # index 0 = JP
        except OSError:
            continue
    raise RuntimeError("日本語フォントが見つからない（fonts-noto-cjk を入れる）")


def _cover(img, w, h, focus_y=0.5, focus_x=0.5, zoom=1.0):
    """img を w x h に隙間なく収まるよう拡大して切り出す"""
    s = max(w / img.width, h / img.height) * zoom
    nw, nh = int(img.width * s + 0.5), int(img.height * s + 0.5)
    img = img.resize((nw, nh), Image.LANCZOS)
    x = int((nw - w) * focus_x)
    y = int((nh - h) * focus_y)
    return img.crop((x, y, x + w, y + h))


def _split_title(title):
    """「｜」「。」などの区切りで2行に分ける。区切りがなければ幅で折り返す"""
    t = re.sub(r"\s+", " ", title).strip()
    for sep in ["｜", "|", "。", "！", "？", "　", "、"]:
        i = t.find(sep)
        if 0 < i < len(t) - 1:
            if sep in "｜|":
                a = t[:i].strip() + "｜"
            elif sep in "。！？、":
                a = t[:i + 1]
            else:
                a = t[:i]
            return [a, t[i + 1:].strip()]
    return [t]


def _wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) <= max_w:
            cur += ch
        else:
            # 行頭に来てはいけない文字は前の行に残す
            if ch in "、。，．）」』】！？ー～…":
                cur += ch
                lines.append(cur)
                cur = ""
            else:
                lines.append(cur)
                cur = ch
    if cur:
        lines.append(cur)
    return lines


def _layout_title(draw, title, max_w, max_h):
    parts = _split_title(title)
    for size in range(56, 27, -2):
        f = _font(size)
        lines = []
        for p in parts:
            lines += _wrap(draw, p, f, max_w)
        lh = int(size * 1.45)
        if len(lines) <= 3 and lh * len(lines) <= max_h:
            return f, lines, lh
    f = _font(28)
    lines = []
    for p in parts:
        lines += _wrap(draw, p, f, max_w)
    lines = lines[:3]
    lines[-1] = lines[-1][:-1] + "…"
    return f, lines, int(28 * 1.45)


def _white_logo(logo):
    """黒ロゴを白抜きにする（透過PNG想定）"""
    logo = logo.convert("RGBA")
    alpha = logo.getchannel("A")
    white = Image.new("RGBA", logo.size, (255, 255, 255, 255))
    white.putalpha(alpha)
    return white


def _text_logo(draw, cx, y):
    f1 = _font(46)
    f2 = _font(18)
    t1 = "W A R D R O B E"
    w1 = draw.textlength(t1, font=f1)
    draw.text((cx - w1 / 2, y), t1, font=f1, fill="white")
    t2 = "L  A  B"
    w2 = draw.textlength(t2, font=f2)
    yy = y + 62
    draw.text((cx - w2 / 2, yy), t2, font=f2, fill="white")
    draw.line((cx - w1 / 2, yy + 11, cx - w2 / 2 - 14, yy + 11), fill="white", width=2)
    draw.line((cx + w2 / 2 + 14, yy + 11, cx + w1 / 2, yy + 11), fill="white", width=2)


def make_image(eyecatch_bytes, title, logo_bytes=None):
    src = Image.open(io.BytesIO(eyecatch_bytes)).convert("RGB")
    canvas = Image.new("RGB", (W, H), "black")

    # 上：アイキャッチ全体
    canvas.paste(_cover(src, W, TOP_H), (0, 0))

    # 下：同じ写真の一部を拡大して切り出し（上と同じ絵の繰り返しにならないよう寄る）、
    #     少し暗くしてロゴを載せる
    bottom_h = H - TOP_H - BAND_H
    bottom = _cover(src, W, bottom_h, focus_y=0.55, focus_x=0.75, zoom=1.6)
    shade = Image.new("RGB", bottom.size, "black")
    grad = Image.linear_gradient("L").resize(bottom.size).point(lambda v: int(v * 0.55))
    bottom = Image.composite(shade, bottom, grad)
    canvas.paste(bottom, (0, TOP_H + BAND_H))

    draw = ImageDraw.Draw(canvas)

    # 中：黒帯＋タイトル
    y0 = TOP_H
    draw.rectangle((0, y0, W, y0 + BAND_H), fill=(0, 0, 0))
    font, lines, lh = _layout_title(draw, title, W - 140, BAND_H - 50)
    total = lh * len(lines)
    y = y0 + (BAND_H - total) // 2 - int(lh * 0.12)
    for ln in lines:
        w = draw.textlength(ln, font=font)
        draw.text(((W - w) / 2, y), ln, font=font, fill="white")
        y += lh

    # ロゴ
    cx = W // 2
    if logo_bytes:
        try:
            logo = _white_logo(Image.open(io.BytesIO(logo_bytes)))
            lw = 300
            logo = logo.resize((lw, int(logo.height * lw / logo.width)), Image.LANCZOS)
            canvas.paste(logo, (cx - lw // 2, H - logo.height - 60), logo)
        except Exception:  # noqa: BLE001
            _text_logo(draw, cx, H - 140)
    else:
        _text_logo(draw, cx, H - 140)

    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=90, optimize=True, progressive=False)
    return out.getvalue()


if __name__ == "__main__":
    import sys
    data = open(sys.argv[1], "rb").read()
    logo = open(sys.argv[3], "rb").read() if len(sys.argv) > 3 else None
    open(sys.argv[2], "wb").write(make_image(data, sys.argv[4] if len(sys.argv) > 4 else "ハイブランドの傘おすすめ｜大人メンズに似合う高級傘の選び方と魅力", logo))
