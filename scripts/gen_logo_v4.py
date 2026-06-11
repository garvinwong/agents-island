#!/usr/bin/env python3
"""Agents Island logo v4 — Owner 9 帧动效稿 →「悬浮舞」帧动画

母版：logo/ChatGPT Image 2026年6月11日 14_18_54~57 (1)~(9).png
帧语义：机器人左右摇摆 + 光标闪烁 + 悬浮环强弱；#7 腾空跳跃（环最亮）。

母版是 RGB 假透明（棋盘格烤底），抠底要点：
  - 棋盘两色（白/浅灰）从角部采样；屏幕表情也是纯白 → 不能全局删色，
    用「边缘连通 + 双色混合封闭区」两道判定（表情区是单色白不含灰格）
  - 蓝环光晕与棋盘混色 → 移除区邻接的低饱和亮像素做 alpha 渐隐去边

产物：
  web/assets/bot_sprite.png     9 帧横向精灵图（岛内 steps(9) 动画）
  win/tray_frames/f00-08.ico    托盘动画帧（working 循环）
  win/tray_idle.ico             托盘空闲帧（#1 静帧）
  win/island.ico                主图标（#1 多尺寸）
  docs/logo_v4_preview.gif      预览
"""
import glob
import os
import re
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
LOGO_DIR = Path(os.environ.get('ISLAND_LOGO_DIR', 'logo'))
CELL = 128   # 精灵图单元尺寸


def load_frames():
    files = sorted(
        glob.glob(str(LOGO_DIR / 'ChatGPT Image 2026年6月11日 14_18_5*.png')),
        key=lambda f: int(re.search(r'\((\d+)\)', f).group(1)))
    assert len(files) == 9, f'期望 9 帧，实际 {len(files)}'
    return [Image.open(f).convert('RGB') for f in files]


def knockout_checker(img: Image.Image) -> Image.Image:
    """去棋盘格假透明底。返回 RGBA。"""
    rgb = np.asarray(img).astype(np.int16)
    h, w = rgb.shape[:2]

    # 棋盘两色采样：四角各取一块，聚类出两种主色
    corners = np.concatenate([
        rgb[2:34, 2:34].reshape(-1, 3), rgb[2:34, -34:-2].reshape(-1, 3),
        rgb[-34:-2, 2:34].reshape(-1, 3), rgb[-34:-2, -34:-2].reshape(-1, 3)])
    bright = corners[corners.sum(axis=1) >= np.median(corners.sum(axis=1))].mean(axis=0)
    dark   = corners[corners.sum(axis=1) <  np.median(corners.sum(axis=1))].mean(axis=0)

    d_bright = np.abs(rgb - bright).sum(axis=2)
    d_dark   = np.abs(rgb - dark).sum(axis=2)
    sat = rgb.max(axis=2) - rgb.min(axis=2)
    near = ((d_bright < 60) | (d_dark < 60)) & (sat < 30)

    # 连通域标记（4 邻接）
    labels = np.zeros((h, w), np.int32)
    cur = 0
    comp_info = {}
    for y in range(h):
        for x in range(w):
            if near[y, x] and labels[y, x] == 0:
                cur += 1
                qs = deque([(y, x)])
                labels[y, x] = cur
                px = []
                edge = False
                while qs:
                    cy, cx = qs.popleft()
                    px.append((cy, cx))
                    if cy in (0, h - 1) or cx in (0, w - 1):
                        edge = True
                    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < h and 0 <= nx < w and near[ny, nx] and labels[ny, nx] == 0:
                            labels[ny, nx] = cur
                            qs.append((ny, nx))
                comp_info[cur] = (px, edge)

    remove = np.zeros((h, w), bool)
    for cid, (px, edge) in comp_info.items():
        ys = np.array([p[0] for p in px]); xs = np.array([p[1] for p in px])
        vals = rgb[ys, xs]
        db = np.abs(vals - bright).sum(axis=1) < 60
        dd = np.abs(vals - dark).sum(axis=1) < 60
        is_checker = edge or (db.any() and dd.any() and len(px) > 400)
        if is_checker:
            remove[ys, xs] = True

    alpha = np.where(remove, 0, 255).astype(np.uint8)
    # 去边：移除区 2px 邻域内的低饱和亮像素 alpha 渐隐（光晕/抗锯齿混色带）
    return _finish(rgb, alpha, sat)


def _finish(rgb, alpha, sat):
    h, w = alpha.shape
    removed = alpha == 0
    near1 = np.zeros_like(removed)
    near1[1:, :] |= removed[:-1, :]; near1[:-1, :] |= removed[1:, :]
    near1[:, 1:] |= removed[:, :-1]; near1[:, :-1] |= removed[:, 1:]
    near2 = np.zeros_like(near1)
    near2[1:, :] |= near1[:-1, :]; near2[:-1, :] |= near1[1:, :]
    near2[:, 1:] |= near1[:, :-1]; near2[:, :-1] |= near1[:, 1:]
    fringe = (~removed) & near2 & (sat < 40) & (rgb.sum(axis=2) > 480)
    alpha = alpha.copy()
    alpha[fringe & near1] = 70
    alpha[fringe & ~near1] = 150
    out = np.dstack([rgb.astype(np.uint8), alpha])
    return Image.fromarray(out, 'RGBA')


def main():
    assets = ROOT / 'web' / 'assets'
    win = ROOT / 'win'
    docs = ROOT / 'docs'
    frames_dir = win / 'tray_frames'
    for d in (assets, docs, frames_dir):
        d.mkdir(exist_ok=True)
    for old in frames_dir.glob('f*.ico'):
        old.unlink()

    frames = [knockout_checker(im) for im in load_frames()]

    # 帧间归一化：AI 稿各帧尺度有抖动，整体宽度受腿部姿态干扰（叉腿帧会被误缩）
    # → 以「屏幕暗色矩形宽度」为基准（机器人最稳定特征），底部居中锚定
    # （悬浮环贴地；跳跃帧 #7 机器人在格内自然升起）
    def screen_width(im: Image.Image) -> int:
        a = np.asarray(im)
        dark = (a[:, :, :3].astype(int).sum(axis=2) < 210) & (a[:, :, 3] > 128)
        col = dark.sum(axis=0)
        good = col > col.max() * 0.5      # 屏幕列：暗像素连续高密度；轮廓线列稀疏
        return int(good.sum()) or im.width

    contents = [f.crop(f.getbbox()) for f in frames]
    sws = [screen_width(c) for c in contents]
    med_sw = sorted(sws)[len(sws) // 2]
    norm = []
    for c, sw in zip(contents, sws):
        k = max(0.8, min(1.25, med_sw / sw))
        norm.append(c.resize((round(c.width * k), round(c.height * k)), Image.LANCZOS))
    side = max(max(c.width for c in norm), max(c.height for c in norm)) + 8
    frames = []
    for c in norm:
        canvas = Image.new('RGBA', (side, side), (0, 0, 0, 0))
        canvas.alpha_composite(c, ((side - c.width) // 2, side - c.height - 4))
        frames.append(canvas)

    # ── 岛内精灵图（横向 9 格） ─────────────────────────────────────
    sheet = Image.new('RGBA', (CELL * 9, CELL), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        sheet.paste(f.resize((CELL, CELL), Image.LANCZOS), (i * CELL, 0))
    sheet.save(assets / 'bot_sprite.png')

    # ── 托盘动画帧 + 空闲帧 ─────────────────────────────────────────
    for i, f in enumerate(frames):
        f.resize((32, 32), Image.LANCZOS).save(
            frames_dir / f'f{i:02d}.ico', format='ICO', sizes=[(32, 32)])
    frames[0].resize((32, 32), Image.LANCZOS).save(
        win / 'tray_idle.ico', format='ICO', sizes=[(32, 32)])

    # ── 主 .ico（#1 多尺寸） ────────────────────────────────────────
    sizes = (16, 24, 32, 48, 64, 256)
    pack = [frames[0].resize((s, s), Image.LANCZOS) for s in sizes]
    pack[-1].save(win / 'island.ico', format='ICO',
                  sizes=[(s, s) for s in sizes], append_images=pack[:-1])

    # ── 预览 GIF ────────────────────────────────────────────────────
    flat = []
    for f in frames:
        bg = Image.new('RGBA', (CELL, CELL), (28, 28, 32, 255))
        bg.alpha_composite(f.resize((CELL, CELL), Image.LANCZOS))
        flat.append(bg.convert('P', palette=Image.ADAPTIVE))
    flat[0].save(docs / 'logo_v4_preview.gif', save_all=True,
                 append_images=flat[1:], duration=140, loop=0)
    print('v4: bot_sprite.png(9帧) / tray f00-08 / island.ico / preview gif')


if __name__ == '__main__':
    main()
