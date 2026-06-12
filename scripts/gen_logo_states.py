#!/usr/bin/env python3
"""Agents Island — 睡觉 / 爆发 状态精灵图 + 托盘帧

母版：logo/island/sleep1-4.png（睡觉，趴卧闭眼）、super1-6.png（爆发，火焰环绕）
复用 gen_logo_v4 的棋盘格抠底（屏幕几何保脸）+ 屏幕宽度归一化。

产物：
  web/assets/sleep_sprite.png   4 帧横向精灵（岛内睡觉，steps(4) 慢循环）
  web/assets/super_sprite.png   6 帧横向精灵（岛内爆发，steps(6) 快循环）
  win/tray_sleep/s00-03.ico     托盘睡觉帧
  win/tray_super/s00-05.ico     托盘爆发帧
"""
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from gen_logo_v4 import knockout_checker   # 复用棋盘格抠底（屏幕几何保脸）

LOGO = Path(os.environ.get('ISLAND_STATE_LOGO_DIR', 'logo/island'))
CELL = 128


def screen_width(im: Image.Image) -> int:
    a = np.asarray(im)
    dark = (a[:, :, :3].astype(int).sum(axis=2) < 210) & (a[:, :, 3] > 128)
    col = dark.sum(axis=0)
    return int((col > col.max() * 0.5).sum()) or im.width


def normalize(frames):
    """按屏幕暗矩形宽度对齐尺度 + 底部居中锚定（与 v4 一致）。"""
    contents = [f.crop(f.getbbox()) for f in frames]
    sws = [screen_width(c) for c in contents]
    med = sorted(sws)[len(sws) // 2]
    norm = []
    for c, sw in zip(contents, sws):
        k = max(0.8, min(1.25, med / sw))
        norm.append(c.resize((round(c.width * k), round(c.height * k)), Image.LANCZOS))
    side = max(max(c.width for c in norm), max(c.height for c in norm)) + 8
    out = []
    for c in norm:
        cv = Image.new('RGBA', (side, side), (0, 0, 0, 0))
        cv.alpha_composite(c, ((side - c.width) // 2, side - c.height - 4))
        out.append(cv)
    return out


def build(names, sprite_path, tray_dir):
    frames = [knockout_checker(Image.open(LOGO / n).convert('RGB')) for n in names]
    frames = normalize(frames)
    n = len(frames)
    sheet = Image.new('RGBA', (CELL * n, CELL), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        sheet.paste(f.resize((CELL, CELL), Image.LANCZOS), (i * CELL, 0))
    sprite_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(sprite_path)
    tray_dir.mkdir(parents=True, exist_ok=True)
    for old in tray_dir.glob('*.ico'):
        old.unlink()
    for i, f in enumerate(frames):
        f.resize((32, 32), Image.LANCZOS).save(
            tray_dir / f's{i:02d}.ico', format='ICO', sizes=[(32, 32)])
    print(f'  {sprite_path.name}: {n} 帧 + 托盘 {tray_dir.name}/')


def main():
    build(['sleep1.png', 'sleep2.png', 'sleep3.png', 'sleep4.png'],
          ROOT / 'web' / 'assets' / 'sleep_sprite.png', ROOT / 'win' / 'tray_sleep')
    build(['super1.png', 'super2.png', 'supe3.png', 'super4.png', 'super5.png', 'super6.png'],
          ROOT / 'web' / 'assets' / 'super_sprite.png', ROOT / 'win' / 'tray_super')
    print('done: sleep(4) + super(6)')


if __name__ == '__main__':
    main()
