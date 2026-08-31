#!/usr/bin/env python3
"""精灵带 v5：三态分镜稿（Codex 生成的网格 sheet）→ 生产精灵带 + 托盘帧。

上游素材：logo/sprites_v5/sheet_{working,busy,idle}.png（品红底网格分镜）
产物：web/assets/{bot,super,sleep}_sprite.png（128px 帧高横向精灵带）
      win/tray_frames/fNN.ico（working 帧托盘动画）
      logo/sprites_v5/preview_{state}.gif（目检用）

管线要点（v5 抠底/归一经验沿用 gen_logo_v4）：
- 抠底＝按格边框采样底色（品红会漂移）+ 色距阈值 + alpha 去噪 + 1px 收边去镶边
- 归一＝按「机身宽度」缩放（不能用全内容宽：火焰/zZ 会让体型伪脉动），
  机身＝橙色像素桶 bbox；再做全局防溢出钳制（各帧同乘一个系数，体型恒定）
- 锚定＝内容底边贴 y=BASE（悬浮环/水圈落地，机身浮沉相位保留）
"""
from PIL import Image, ImageFilter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'logo' / 'sprites_v5'
OUT_WEB = ROOT / 'web' / 'assets'
OUT_TRAY = ROOT / 'win' / 'tray_frames'

FRAME = 128            # 生产帧尺寸
BASE = 126             # 内容底边锚定线
MARGIN = 4             # 防溢出边距

STATES = {
    # state: (sheet, cols, rows, 产物名, 机身目标宽 px, 出托盘帧, 去上邻渗入)
    # decontam=True：删除完整位于机身顶之上的连通块（上行悬浮环弧渗入实案；
    # 天线与机身同连通块不受影响）。idle 的 zZ 在头顶属本帧内容，禁开
    'working': ('sheet_working.png', 4, 3, 'bot_sprite.png', 74, True, True),
    'busy':    ('sheet_busy.png',    5, 2, 'super_sprite.png', 64, True, True),
    'idle':    ('sheet_idle.png',    4, 2, 'sleep_sprite.png', 96, True, False),
}

# 托盘特写变体（v2 教训：多元素 logo 托盘尺寸必须做减法，不能一稿通吃）：
# 裁掉悬浮环/浮沉余量，机身占满画布；busy 留火焰所以机身目标更小
# state: (输出目录, 帧前缀, 机身目标宽@32px 画布)
TRAY = {
    'working': ('tray_frames', 'f', 28),
    'busy':    ('tray_super',  's', 20),
    'idle':    ('tray_sleep',  's', 28),
}


def _make_tray(state, frames):
    subdir, prefix, tb = TRAY[state]
    out = ROOT / 'win' / subdir
    out.mkdir(exist_ok=True)
    for old in out.glob(f'{prefix}*.ico'):
        old.unlink()
    icons = []
    for i, fr in enumerate(frames):
        bb = body_bbox(fr)
        cb = fr.getbbox() or bb
        # busy 连火焰一起裁（内容 bbox），working/idle 裁机身特写
        box = cb if state == 'busy' else bb
        pad = 2
        crop = fr.crop((max(box[0] - pad, 0), max(box[1] - pad, 0),
                        min(box[2] + pad, FRAME), min(box[3] + pad, FRAME)))
        s = tb / max(bb[2] - bb[0], 1)                 # 按机身宽归一防帧间抖动
        s = min(s, 31 / max(crop.width, crop.height))  # 防溢出
        img = crop.resize((max(1, round(crop.width * s)),
                           max(1, round(crop.height * s))), Image.LANCZOS)
        canvas = Image.new('RGBA', (32, 32), (0, 0, 0, 0))
        canvas.paste(img, ((32 - img.width) // 2, (32 - img.height) // 2), img)
        canvas.save(out / f'{prefix}{i:02d}.ico',
                    sizes=[(16, 16), (24, 24), (32, 32)])
        icons.append(canvas)
    if state == 'working':                             # idle 静态托盘图标=特写静帧
        icons[0].save(ROOT / 'win' / 'tray_idle.ico',
                      sizes=[(16, 16), (24, 24), (32, 32)])
    strip = Image.new('RGBA', (34 * len(icons), 32), (240, 240, 240, 255))
    for i, ic in enumerate(icons):
        strip.paste(ic, (i * 34, 0), ic)
    strip.save(SRC / f'tray_preview_{state}.png')
    print(f'tray[{state}]: {len(icons)} 帧 -> win/{subdir}/{prefix}NN.ico (机身宽 {tb}px)')


def key_out_bg(cell):
    """品红抠底：格子四边采样底色中位数，色距阈值键控 + 去噪 + 收边。"""
    px = cell.convert('RGB').load()
    w, h = cell.size
    border = []
    for x in range(0, w, 3):
        border += [px[x, 0], px[x, h - 1]]
    for y in range(0, h, 3):
        border += [px[0, y], px[w - 1, y]]
    border.sort(key=lambda c: c[0] * 3 + c[2] - c[1])
    bg = border[len(border) // 2]
    rgba = cell.convert('RGBA')
    dat = rgba.load()
    TOL2 = 88 ** 2
    for y in range(h):
        for x in range(w):
            r, g, b, a = dat[x, y]
            d2 = (r - bg[0]) ** 2 + (g - bg[1]) ** 2 + (b - bg[2]) ** 2
            # 品红族兜底：高R高B低G 一律视为底（渐变噪声防漏）
            if d2 < TOL2 or (r > 190 and b > 190 and g < 130):
                dat[x, y] = (0, 0, 0, 0)
    # alpha 去噪（中值滤波）+ 1px 收边去品红镶边
    a = rgba.split()[3].filter(ImageFilter.MedianFilter(3))
    a = a.point(lambda v: 255 if v > 128 else 0)
    a = a.filter(ImageFilter.MinFilter(3))
    rgba.putalpha(a)
    _drop_top_touching(rgba)
    return rgba


def _drop_top_touching(rgba):
    """删除触碰格子顶边的连通块——网格分镜行距不足时，上邻格的悬浮环底弧/
    zZ 会渗入本格顶部（working 帧 9-12 青弧实案）。角色本体不贴顶边，安全。"""
    dat = rgba.load()
    w, h = rgba.size
    seen = set()
    stack = [(x, y) for y in (0, 1, 2) for x in range(w) if dat[x, y][3] > 0]
    while stack:
        x, y = stack.pop()
        if (x, y) in seen or not (0 <= x < w and 0 <= y < h) or dat[x, y][3] == 0:
            continue
        seen.add((x, y))
        dat[x, y] = (0, 0, 0, 0)
        stack += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]


def body_bbox(rgba):
    """机身 bbox＝橙色（clay）像素范围；抗火焰/zZ/环干扰。"""
    dat = rgba.load()
    w, h = rgba.size
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            r, g, b, a = dat[x, y]
            if a > 0 and r > 140 and 40 < g < 150 and b < 110 and r - b > 60:
                xs.append(x); ys.append(y)
    if not xs:
        return rgba.getbbox()
    return (min(xs), min(ys), max(xs) + 1, max(ys) + 1)


def _drop_above_body(rgba, body_top):
    """删除完整位于机身顶之上的连通块（上邻格渗入物）。"""
    dat = rgba.load()
    w, h = rgba.size
    visited = [[False] * w for _ in range(h)]
    for y0 in range(min(body_top, h)):
        for x0 in range(w):
            if visited[y0][x0] or dat[x0, y0][3] == 0:
                continue
            comp, maxy, stack = [], 0, [(x0, y0)]
            while stack:
                x, y = stack.pop()
                if not (0 <= x < w and 0 <= y < h) or visited[y][x] or dat[x, y][3] == 0:
                    continue
                visited[y][x] = True
                comp.append((x, y)); maxy = max(maxy, y)
                stack += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
            if maxy < body_top - 2:                    # 整块悬在机身之上=渗入物
                for x, y in comp:
                    dat[x, y] = (0, 0, 0, 0)


def process(state):
    sheet_name, cols, rows, out_name, body_target, tray, decontam = STATES[state]
    sheet = Image.open(SRC / sheet_name)
    cw, ch = sheet.width // cols, sheet.height // rows
    cells = []
    for r in range(rows):
        for c in range(cols):
            cell = sheet.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch))
            cell = key_out_bg(cell)
            if decontam:
                bb = body_bbox(cell)
                if bb:
                    _drop_above_body(cell, bb[1])
            cells.append(cell)

    # 每帧机身宽 → 归一系数；全局防溢出钳制（各帧同乘，体型不脉动）
    scales = []
    for cell in cells:
        bb = body_bbox(cell)
        bw = max(bb[2] - bb[0], 1)
        scales.append(body_target / bw)
    clamp = 1.0
    for cell, s in zip(cells, scales):
        cb = cell.getbbox()
        if not cb:
            continue
        cwid, chei = (cb[2] - cb[0]) * s, (cb[3] - cb[1]) * s
        lim = (FRAME - MARGIN) / max(cwid, chei)
        clamp = min(clamp, lim if lim < 1 else 1.0)

    frames = []
    for cell, s in zip(cells, scales):
        s *= clamp
        cb = cell.getbbox()
        if not cb:
            frames.append(Image.new('RGBA', (FRAME, FRAME), (0, 0, 0, 0)))
            continue
        content = cell.crop(cb)
        nw, nh = max(1, round(content.width * s)), max(1, round(content.height * s))
        content = content.resize((nw, nh), Image.LANCZOS)
        bb = body_bbox(cell)
        body_cx = ((bb[0] + bb[2]) / 2 - cb[0]) * s     # 机身中心相对内容
        frame = Image.new('RGBA', (FRAME, FRAME), (0, 0, 0, 0))
        ox = round(FRAME / 2 - body_cx)
        oy = BASE - nh
        frame.paste(content, (ox, oy), content)
        frames.append(frame)

    strip = Image.new('RGBA', (FRAME * len(frames), FRAME), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        strip.paste(f, (i * FRAME, 0))
    strip.save(OUT_WEB / out_name)
    print(f'{state}: {len(frames)} 帧 -> {out_name} {strip.size}, clamp={clamp:.3f}')

    if tray:
        _make_tray(state, frames)

    # 目检 GIF（放大 2x 便于看）
    gif = [f.resize((256, 256), Image.NEAREST).convert('P', palette=Image.ADAPTIVE)
           for f in frames]
    dur = {'working': 140, 'busy': 90, 'idle': 350}[state]
    gif[0].save(SRC / f'preview_{state}.gif', save_all=True, append_images=gif[1:],
                duration=dur, loop=0, disposal=2)


if __name__ == '__main__':
    for st in (sys.argv[1:] or STATES):
        process(st)
