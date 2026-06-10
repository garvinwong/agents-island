#!/usr/bin/env python3
"""Agents Island logo 生成器 v2 — 「星云轨道小机器人 · 终端脸」

设计语言（三家融合）:
  · 主体 = 像素小机器人头（Claude Code 像素小兽血统，陶土橙 #D97757）
  · 脸   = 终端提示符 `>_`：`>` 为左眼、`_` 为右眼光标 —— 眨眼 = 光标闪烁
           （大字率构图，16px 仍可读；致敬 Codex 终端云）
  · 轨道 = DeepSeek 鲸蓝流线弧 + 卫星点（Codex 紫 / 鲸蓝），agent working 时公转
  · 变体 = app（带椭圆轨道，主图标）/ tray（机器人撑满 80%，单卫星贴边，托盘 16px 专用）

产物:
  win/island.ico          多尺寸主图标（app 变体）
  win/tray_frames/f00-11  托盘动画帧（32px tray 变体，公转 30°/帧，光标闪烁）
  docs/logo_preview.gif   app 变体动画预览
  docs/logo_256.png       静态大图
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
CLAY   = (217, 119, 87, 255)    # Claude 陶土橙
LILAC  = (167, 139, 250, 255)   # Codex 紫
WHALE  = (77, 107, 254, 255)    # DeepSeek 鲸蓝
WHITE  = (255, 255, 255, 255)
ORBIT  = (139, 124, 248, 150)   # 轨道：半透明紫蓝

SS = 4  # 超采样倍数


def _rounded(d, box, r, fill):
    d.rounded_rectangle(box, radius=r, fill=fill)


def _draw_bot(d, u, cx, cy, scale=1.0, cursor_on=True):
    """机器人头 + 终端脸 `>_`。scale 控制主体占比。"""
    k = u * scale
    hw, hh = 110 * k, 86 * k
    hx0, hy0, hx1, hy1 = cx - hw / 2, cy - hh / 2, cx + hw / 2, cy + hh / 2
    _rounded(d, [hx0, hy0, hx1, hy1], 16 * k, CLAY)
    # 侧臂
    aw, ah = 18 * k, 28 * k
    _rounded(d, [hx0 - aw + 4 * k, cy - ah / 2, hx0 + 6 * k, cy + ah / 2], 6 * k, CLAY)
    _rounded(d, [hx1 - 6 * k, cy - ah / 2, hx1 + aw - 4 * k, cy + ah / 2], 6 * k, CLAY)
    # 小短腿 ×2
    lw, lh = 17 * k, 16 * k
    for lx in (cx - 32 * k, cx + 15 * k):
        d.rectangle([lx, hy1 - 2 * k, lx + lw, hy1 + lh], fill=CLAY)
    # ── 终端脸（大字率）──────────────────────────────────────────
    sw = max(2, int(15 * k))             # 笔画粗
    # `>` 左眼
    gx, gy = cx - 34 * k, cy
    d.line([gx, gy - 17 * k, gx + 22 * k, gy], fill=WHITE, width=sw)
    d.line([gx + 22 * k, gy, gx, gy + 17 * k], fill=WHITE, width=sw)
    for px, py in [(gx, gy - 17 * k), (gx + 22 * k, gy), (gx, gy + 17 * k)]:
        r = sw / 2
        d.ellipse([px - r, py - r, px + r, py + r], fill=WHITE)
    # `_` 右眼 = 终端光标（闪烁帧隐去）
    if cursor_on:
        _rounded(d, [cx + 8 * k, cy + 8 * k, cx + 40 * k, cy + 20 * k], 5 * k, WHITE)


def draw_logo(size=256, orbit_t=210.0, cursor_on=True, variant='app'):
    S = size * SS
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = S / 256.0
    if variant == 'tray':
        cx, cy = 128 * u, 126 * u
        bot_scale, a, b, tilt_deg = 1.55, 122, 96, -32
        dots = ((0, WHALE, 17),)                       # 单卫星，贴边公转
        ow = 0                                          # 不画轨道线
    else:
        cx, cy = 128 * u, 132 * u
        bot_scale, a, b, tilt_deg = 1.18, 116, 44, -24
        dots = ((0, WHALE, 14), (180, LILAC, 11))
        ow = max(2.0, 3.4 * u)

    A, B = a * u, b * u
    tilt = math.radians(tilt_deg)
    ct, st = math.cos(tilt), math.sin(tilt)

    def orbit_pos(deg):
        t = math.radians(deg)
        x, y = A * math.cos(t), B * math.sin(t)
        return (cx + x * ct - y * st, cy + x * st + y * ct, math.sin(t))

    # 轨道背面段
    if ow:
        for deg in range(0, 360, 2):
            x, y, front = orbit_pos(deg)
            if front <= 0.05:
                d.ellipse([x - ow, y - ow, x + ow, y + ow], fill=ORBIT)
    # 背面卫星
    for phase, color, r in dots:
        x, y, front = orbit_pos(orbit_t + phase)
        if front <= 0:
            rr = r * u * 0.78
            d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=color)

    _draw_bot(d, u, cx, cy, scale=bot_scale, cursor_on=cursor_on)

    # 轨道前段 + 前面卫星
    if ow:
        for deg in range(0, 360, 2):
            x, y, front = orbit_pos(deg)
            if front > 0.05:
                d.ellipse([x - ow, y - ow, x + ow, y + ow], fill=ORBIT[:3] + (90,))
    for phase, color, r in dots:
        x, y, front = orbit_pos(orbit_t + phase)
        if front > 0:
            rr = r * u
            glow = color[:3] + (60,)
            d.ellipse([x - rr * 1.7, y - rr * 1.7, x + rr * 1.7, y + rr * 1.7], fill=glow)
            d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=color)

    return img.resize((size, size), Image.LANCZOS)


def main():
    win = ROOT / 'win'
    docs = ROOT / 'docs'
    docs.mkdir(exist_ok=True)
    frames_dir = win / 'tray_frames'
    frames_dir.mkdir(exist_ok=True)

    # 1) 主 .ico（app 变体；16/24 小尺寸用 tray 变体保可读）
    pack = [draw_logo(s, variant='tray' if s <= 24 else 'app') for s in (16, 24, 32, 48, 64, 256)]
    pack[-1].save(win / 'island.ico', format='ICO',
                  sizes=[(s, s) for s in (16, 24, 32, 48, 64, 256)], append_images=pack[:-1])

    # 2) 托盘动画帧（tray 变体 32px ×12；光标闪烁节奏：6 帧亮 / 3 帧灭 / 3 帧亮）
    for i in range(12):
        cursor_on = not (6 <= i <= 8)
        draw_logo(32, orbit_t=210 + i * 30, cursor_on=cursor_on, variant='tray') \
            .save(frames_dir / f'f{i:02d}.ico', format='ICO', sizes=[(32, 32)])

    # 3) 预览
    big = [draw_logo(128, orbit_t=210 + i * 30, cursor_on=not (6 <= i <= 8)) for i in range(12)]
    flat = []
    for f in big:
        bg = Image.new('RGBA', f.size, (32, 32, 36, 255))
        bg.alpha_composite(f)
        flat.append(bg.convert('P', palette=Image.ADAPTIVE))
    flat[0].save(docs / 'logo_preview.gif', save_all=True,
                 append_images=flat[1:], duration=120, loop=0)
    draw_logo(256).save(docs / 'logo_256.png')
    print('✅ island.ico / tray_frames×12 / docs/logo_preview.gif / docs/logo_256.png')


if __name__ == '__main__':
    main()
