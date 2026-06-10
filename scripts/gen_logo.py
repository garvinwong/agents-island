#!/usr/bin/env python3
"""Agents Island logo 生成器 — 「星云轨道小机器人」

设计语言（三家融合）:
  · 核心 = 像素小机器人头（Claude Code 像素小兽血统，陶土橙 #D97757）
  · 轨道 = 环绕的椭圆星云轨道（DeepSeek 鲸鱼的流线弧 + Codex 云的柔和感）
  · 卫星 = 轨道上两颗 Agent 点（Codex 紫 #A78BFA / DeepSeek 鲸蓝 #4D6BFE）
  · 动态 = 卫星公转（agent working 时托盘/岛上同步转动）

产物:
  win/island.ico            多尺寸主图标（程序/窗口/快捷方式）
  win/tray_frames/f00..11   托盘动画帧（32px，公转 30°/帧）
  docs 预览                  logo_preview.gif / logo_256.png
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


def draw_logo(size=256, orbit_t=210.0, blink=False):
    """orbit_t: 主卫星相位角(度)；blink: 眨眼帧"""
    S = size * SS
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = S / 256.0          # 设计坐标(256) → 画布
    cx, cy = 128 * u, 134 * u

    # ── 轨道参数（椭圆，逆时针倾斜）─────────────────────────────
    a, b = 112 * u, 42 * u
    tilt = math.radians(-24)
    ct, st = math.cos(tilt), math.sin(tilt)

    def orbit_pos(deg):
        t = math.radians(deg)
        x, y = a * math.cos(t), b * math.sin(t)
        return (cx + x * ct - y * st, cy + x * st + y * ct, math.sin(t))

    # 轨道线：用密集小圆点近似带透明度的椭圆环（仅背面一段实线感更轻盈）
    ow = max(2.0, 3.2 * u)
    for deg in range(0, 360, 2):
        x, y, front = orbit_pos(deg)
        if front <= 0.05:                       # 背面 + 侧缘
            d.ellipse([x - ow, y - ow, x + ow, y + ow], fill=ORBIT)

    # ── 机器人头（像素小兽风）────────────────────────────────────
    hw, hh = 104 * u, 78 * u
    hx0, hy0 = cx - hw / 2, cy - hh / 2
    hx1, hy1 = cx + hw / 2, cy + hh / 2
    _rounded(d, [hx0, hy0, hx1, hy1], 14 * u, CLAY)
    # 侧臂（参考 Claude 像素兽的左右伸出块）
    aw, ah = 18 * u, 26 * u
    _rounded(d, [hx0 - aw + 4 * u, cy - ah / 2, hx0 + 6 * u, cy + ah / 2], 6 * u, CLAY)
    _rounded(d, [hx1 - 6 * u, cy - ah / 2, hx1 + aw - 4 * u, cy + ah / 2], 6 * u, CLAY)
    # 小短腿 ×2
    lw, lh = 16 * u, 16 * u
    for lx in (cx - 30 * u, cx + 14 * u):
        d.rectangle([lx, hy1 - 2 * u, lx + lw, hy1 + lh], fill=CLAY)
    # 眼睛：白色竖槽（眨眼帧压扁）
    ew, eh = 13 * u, (5 if blink else 26) * u
    ey = cy - 8 * u - eh / 2
    for ex in (cx - 26 * u, cx + 13 * u):
        _rounded(d, [ex, ey, ex + ew, ey + eh], min(ew, eh) / 2, WHITE)
    # 嘴：终端提示符 ›（Codex 的 >_ 致意，极简一笔）
    mw = 3.5 * u
    mx, my = cx - 7 * u, cy + 18 * u
    d.line([mx, my, mx + 8 * u, my + 6 * u], fill=WHITE, width=int(mw))
    d.line([mx + 8 * u, my + 6 * u, mx, my + 12 * u], fill=WHITE, width=int(mw))
    d.line([mx + 16 * u, my + 12 * u, mx + 26 * u, my + 12 * u], fill=WHITE, width=int(mw))

    # ── 轨道前段 + 卫星（绘于头之上）────────────────────────────
    for deg in range(0, 360, 2):
        x, y, front = orbit_pos(deg)
        if front > 0.05:
            f = ORBIT[:3] + (90,)
            d.ellipse([x - ow, y - ow, x + ow, y + ow], fill=f)

    for phase, color, r in ((0, WHALE, 13 * u), (180, LILAC, 10 * u)):
        x, y, front = orbit_pos(orbit_t + phase)
        rr = r * (1.0 if front > 0 else 0.78)   # 背面略小 → 纵深感
        glow = color[:3] + (60,)
        d.ellipse([x - rr * 1.8, y - rr * 1.8, x + rr * 1.8, y + rr * 1.8], fill=glow)
        d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=color)

    return img.resize((size, size), Image.LANCZOS)


def main():
    win = ROOT / 'win'
    frames_dir = win / 'tray_frames'
    frames_dir.mkdir(exist_ok=True)

    # 1) 主 .ico（静态、卫星定格在最佳构图位）
    sizes = (16, 24, 32, 48, 64, 256)
    imgs = [draw_logo(s) for s in sizes]
    imgs[-1].save(win / 'island.ico', format='ICO',
                  sizes=[(s, s) for s in sizes], append_images=imgs[:-1])
    imgs[-1].save(ROOT / 'docs_logo_256.png') if False else None

    # 2) 托盘动画帧（32px × 12，公转一周；第 6 帧带眨眼）
    for i in range(12):
        frame = draw_logo(32, orbit_t=210 + i * 30, blink=(i == 6))
        frame.save(frames_dir / f'f{i:02d}.ico', format='ICO', sizes=[(32, 32)])

    # 3) 预览（GIF 128px + PNG 256px）
    big = [draw_logo(128, orbit_t=210 + i * 30, blink=(i == 6)) for i in range(12)]
    flat = []
    for f in big:                      # GIF 不支持 alpha：垫深灰底
        bg = Image.new('RGBA', f.size, (32, 32, 36, 255))
        bg.alpha_composite(f)
        flat.append(bg.convert('P', palette=Image.ADAPTIVE))
    flat[0].save(ROOT / 'logo_preview.gif', save_all=True,
                 append_images=flat[1:], duration=120, loop=0)
    draw_logo(256).save(ROOT / 'logo_256.png')
    print(f'✅ island.ico / tray_frames×12 / logo_preview.gif / logo_256.png')


if __name__ == '__main__':
    main()
