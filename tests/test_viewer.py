"""查看窗（win/island_viewer.py）开窗参数。

模块是 Windows 专属（导入时调 ctypes.windll 设任务栏分组），WSL 下临时桩掉 windll 再导入，
导入后立即撤桩，免得别的用例把“有 windll”误当成 Windows。
"""
import ctypes
import sys
import types
from pathlib import Path

import pytest

WIN = Path(__file__).resolve().parent.parent / 'win'


@pytest.fixture(scope='module')
def viewer():
    stubbed = not hasattr(ctypes, 'windll')
    if stubbed:
        ctypes.windll = types.SimpleNamespace(shell32=types.SimpleNamespace(
            SetCurrentProcessExplicitAppUserModelID=lambda *_: 0))
    sys.path.insert(0, str(WIN))
    try:
        import island_viewer
    finally:
        sys.path.remove(str(WIN))
        if stubbed:
            del ctypes.windll
    return island_viewer


def test_md_reader_text_selectable(viewer):
    """pywebview 默认 text_select=False，会往页面注入 body{user-select:none}：
    md 阅读器整页选不中、复制不了（Owner 09-26 报）。"""
    assert viewer.window_opts('md', True)['text_select'] is True


def test_image_viewer_keeps_selection_off(viewer):
    """图片窗靠拖动平移，开了文字选择拖图会误选，保持关闭。"""
    assert viewer.window_opts('image', True)['text_select'] is False


def test_window_opts_keeps_existing_shape(viewer):
    """抽成函数不改原有开窗参数：frameless 跟随是否套壳，其余照旧。"""
    o = viewer.window_opts('md', True)
    assert (o['width'], o['height'], o['on_top'], o['easy_drag'], o['zoomable']) == (980, 720, False, False, True)
    assert o['frameless'] is True and viewer.window_opts('html', False)['frameless'] is False
