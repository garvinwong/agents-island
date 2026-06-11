#!/usr/bin/env python3
"""Agents Island logo v3 — CRT 像素机器人 ×「蒸汽环」工作态

母版：Owner 提供的两张参考图（logo/ChatGPT Image 2026年6月11日 09_15_24/09_20_57）
  · idle    = 机器人本体（无环）
  · working = 底部蓝色蒸汽光环

处理流程：
  1. ring 差分层 = working - idle（光环独立成层）
  2. 动画帧 = idle + ring × 脉冲(α/缩放相位)，12 帧
  3. 托盘 / 主 ico / 岛内 PNG（idle 本体 + ring 层各一张，岛内 CSS 做动画）

产物：
  win/island.ico               主图标（idle 静态，多尺寸）
  win/tray_frames/f00-11.ico   托盘动画帧（working 蒸汽环脉冲）
  win/tray_idle.ico            托盘空闲帧（无环）
  web/assets/bot.png           岛内机器人本体（透明）
  web/assets/ring.png          岛内蒸汽环层（透明，CSS 脉冲）
  docs/logo_preview.gif        动画预览
"""
import math
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance

ROOT = Path(__file__).resolve().parent.parent
LOGO_DIR = Path(os.environ.get('ISLAND_LOGO_DIR', 'logo'))
IDLE_SRC = LOGO_DIR / 'ChatGPT Image 2026年6月11日 09_15_24.png'
WORK_SRC = LOGO_DIR / 'ChatGPT Image 2026年6月11日 09_20_57.png'


def load_layers():
    idle = Image.open(IDLE_SRC).convert('RGBA')
    work = Image.open(WORK_SRC).convert('RGBA')
    if work.size != idle.size:
        work = work.resize(idle.size, Image.LANCZOS)
    a = np.asarray(idle).astype(np.int16)
    b = np.asarray(work).astype(np.int16)
    diff = np.abs(b[:, :, :3] - a[:, :, :3]).sum(axis=2)
    # 蒸汽环 = 差异显著的像素（取 working 图的颜色，α 随差异强度）；
    # 两张参考图非逐像素一致，差分会带机器人残影 —— 环只可能在底部，掩掉上 2/3
    alpha = np.clip((diff - 18) * 6, 0, 255).astype(np.uint8)
    alpha[: int(a.shape[0] * 0.68), :] = 0
    ring = b.astype(np.uint8).copy()
    ring[:, :, 3] = alpha
    return idle, Image.fromarray(ring, 'RGBA')


def knockout_plaque(img: Image.Image) -> Image.Image:
    """去掉深色圆角底板：从四角洪泛吸附近似底色（机器人屏幕为内部区域不受影响）。"""
    arr = np.asarray(img).astype(np.int16)
    h, w = arr.shape[:2]
    base = arr[4, 4, :3]
    # 阈值放宽 + 限定中性暗色（橙色机器人/白字 sum-diff 远超阈值不受影响）
    diff = np.abs(arr[:, :, :3] - base).sum(axis=2)
    rgb = arr[:, :, :3]
    sat = rgb.max(axis=2) - rgb.min(axis=2)        # 低饱和=中性灰底板
    near = (diff < 130) & (sat < 45)
    # 洪泛：仅与边界连通的 near 区域才剔除
    from collections import deque
    seen = np.zeros((h, w), bool)
    dq = deque()
    for x in range(w):
        for y in (0, h - 1):
            if near[y, x] and not seen[y, x]:
                seen[y, x] = True
                dq.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if near[y, x] and not seen[y, x]:
                seen[y, x] = True
                dq.append((y, x))
    while dq:
        y, x = dq.popleft()
        for dy, dx in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and near[ny, nx] and not seen[ny, nx]:
                seen[ny, nx] = True
                dq.append((ny, nx))
    out = np.asarray(img).copy()
    out[:, :, 3] = np.where(seen, 0, out[:, :, 3])
    return Image.fromarray(out, 'RGBA')


def ring_pulse(idle, ring, phase, plaque=True):
    """idle + ring×脉冲。phase∈[0,1)。"""
    k = 0.55 + 0.45 * math.sin(phase * 2 * math.pi)          # α 0.1~1.0
    r = ring.copy()
    alpha = r.getchannel('A').point(lambda v: int(v * (0.35 + 0.65 * k)))
    r.putalpha(alpha)
    r = ImageEnhance.Brightness(r).enhance(0.85 + 0.5 * k)
    out = idle.copy()
    out.alpha_composite(r)
    return out


def crop_content(img, pad_ratio=0.04):
    """裁掉透明边，留少量 pad。"""
    bbox = img.getbbox()
    if not bbox:
        return img
    pad = int(max(img.size) * pad_ratio)
    l, t, r, b = bbox
    l, t = max(0, l - pad), max(0, t - pad)
    r, b = min(img.width, r + pad), min(img.height, b + pad)
    out = img.crop((l, t, r, b))
    side = max(out.size)
    sq = Image.new('RGBA', (side, side), (0, 0, 0, 0))
    sq.alpha_composite(out, ((side - out.width) // 2, (side - out.height) // 2))
    return sq


def main():
    win = ROOT / 'win'
    docs = ROOT / 'docs'
    assets = ROOT / 'web' / 'assets'
    frames_dir = win / 'tray_frames'
    for d in (docs, assets, frames_dir):
        d.mkdir(exist_ok=True)

    idle_full, ring_full = load_layers()

    # 全局去黑底板：扣成纯机器人（+蒸汽环）透明前景，无任何背景色
    idle_t = knockout_plaque(idle_full)

    # ── 岛内 PNG：本体 + 蒸汽环（同坐标系裁紧）──────────────────────
    bbox = idle_t.getbbox()
    pad = int(idle_full.width * 0.03)
    l, t, r, b = (max(0, bbox[0] - pad), max(0, bbox[1] - pad),
                  min(idle_full.width, bbox[2] + pad),
                  min(idle_full.height, bbox[3] + pad * 4))
    bot_crop = idle_t.crop((l, t, r, b))
    ring_crop = ring_full.crop((l, t, r, b))
    bw = 192
    bot_crop.resize((bw, int(bw * bot_crop.height / bot_crop.width)), Image.LANCZOS).save(assets / 'bot.png')
    ring_crop.resize((bw, int(bw * ring_crop.height / ring_crop.width)), Image.LANCZOS).save(assets / 'ring.png')

    # ── 托盘空闲帧（无环，透明底，紧裁）─────────────────────────────
    crop_content(idle_t, 0.02).resize((32, 32), Image.LANCZOS).save(
        win / 'tray_idle.ico', format='ICO', sizes=[(32, 32)])

    # ── 托盘动画帧：透明底机器人 + 蒸汽环脉冲 ───────────────────────
    for i in range(12):
        frame = ring_pulse(idle_t, ring_full, i / 12)   # 用扣底后的 idle_t
        crop_content(frame, 0.02).resize((32, 32), Image.LANCZOS).save(
            frames_dir / f'f{i:02d}.ico', format='ICO', sizes=[(32, 32)])

    # ── 主 .ico：透明底多尺寸 ───────────────────────────────────────
    sizes = (16, 24, 32, 48, 64, 256)
    pack = [crop_content(idle_t, 0.02).resize((sz, sz), Image.LANCZOS) for sz in sizes]
    pack[-1].save(win / 'island.ico', format='ICO',
                  sizes=[(sz, sz) for sz in sizes], append_images=pack[:-1])

    # ── 预览 GIF（深灰底仅为 GIF 不支持 alpha，实际产物透明）─────────
    flat = []
    for i in range(12):
        f = crop_content(ring_pulse(idle_t, ring_full, i / 12), 0.02).resize((128, 128), Image.LANCZOS)
        bg = Image.new('RGBA', f.size, (32, 32, 36, 255))
        bg.alpha_composite(f)
        flat.append(bg.convert('P', palette=Image.ADAPTIVE))
    flat[0].save(docs / 'logo_preview.gif', save_all=True,
                 append_images=flat[1:], duration=110, loop=0)
    crop_content(idle_t, 0.02).resize((256, 256), Image.LANCZOS).save(docs / 'logo_256.png')
    print('v3.1 (transparent): island.ico / tray_idle+frames / bot+ring.png / preview')


if __name__ == '__main__':
    main()
