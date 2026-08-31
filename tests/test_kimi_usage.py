#!/usr/bin/env python3
"""Kimi Code 官方额度接入测试（沙箱，不触碰真实凭证与生产缓存）。

运行：cd apps/agents-island && python3 -m pytest tests/test_kimi_usage.py -v

背景：早期结论是「Kimi 无本地额度落盘、不接假数据」，故岛上只有 Claude/Codex 有
5h/7d 双条。Kimi Code 0.27 起有了官方端点 GET /coding/v1/usages（2026-07-30 实测
HTTP 200 拿到真实额度），本组测试锁定解析与安全边界。

🔴 被测的安全红线：绝不刷新 token。Kimi 的 refresh 会轮换 refresh_token，
岛若自己刷新会让 CLI 手里那份失效 → Owner 被登出。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'bridge'))


@pytest.fixture()
def ib(tmp_path, monkeypatch):
    """每例一个干净的状态目录，import 前先把 STATE_DIR 指到沙箱。"""
    monkeypatch.setenv('ISLAND_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('ISLAND_KIMI_USAGE_CACHE', str(tmp_path / 'kimi_usage.json'))
    monkeypatch.setenv('ISLAND_KIMI_CRED', str(tmp_path / 'cred.json'))
    for m in [k for k in list(sys.modules) if k == 'island_bridge']:
        del sys.modules[m]
    import island_bridge
    # 模块级常量在 import 时已绑定，显式覆盖到沙箱
    island_bridge.KIMI_USAGE_CACHE = tmp_path / 'kimi_usage.json'
    island_bridge.KIMI_CRED_FILE = tmp_path / 'cred.json'
    return island_bridge


# 2026-07-30 真机实测的响应（脱敏：去掉 user/authentication 段）
REAL_PAYLOAD = {
    'usage': {'limit': '100', 'used': '49', 'remaining': '51',
              'resetTime': '2026-07-31T08:14:49.000583Z'},
    'limits': [{'window': {'duration': 300, 'timeUnit': 'TIME_UNIT_MINUTE'},
                'detail': {'limit': '100', 'used': '1', 'remaining': '99',
                           'resetTime': '2026-07-30T08:14:49.000583Z'}}],
    'parallel': {'limit': '20'},
}


def test_parse_real_payload(ib):
    u = ib._parse_kimi_usages(REAL_PAYLOAD)
    assert u['five_hour']['used_percentage'] == 1.0      # 1/100
    assert u['seven_day']['used_percentage'] == 49.0     # 49/100
    assert u['five_hour']['resets_at'].startswith('2026-07-30')
    assert u['seven_day']['resets_at'].startswith('2026-07-31')


def test_parse_claims_5h_window_by_duration_not_index(ib):
    """档位按窗口时长认领，不写死下标——官方加档位时不能错位。"""
    payload = {'limits': [
        {'window': {'duration': 1, 'timeUnit': 'TIME_UNIT_MINUTE'},
         'detail': {'limit': '10', 'used': '9'}},          # 干扰档，应被忽略
        {'window': {'duration': 5, 'timeUnit': 'TIME_UNIT_HOUR'},
         'detail': {'limit': '200', 'used': '50'}},        # 真 5h 档
    ]}
    u = ib._parse_kimi_usages(payload)
    assert u['five_hour']['used_percentage'] == 25.0
    assert 'seven_day' not in u                            # 无 usage 段


@pytest.mark.parametrize('payload', [
    {}, {'usage': {}}, {'usage': {'limit': '0', 'used': '5'}},
    {'usage': {'limit': 'abc', 'used': 'x'}}, {'limits': 'notalist'},
    {'limits': [None, 'x']},
])
def test_parse_bad_payload_is_empty_not_crash(ib, payload):
    """脏响应一律降级成空，绝不抛异常（会打断 /api/state）也绝不造假数据。"""
    assert ib._parse_kimi_usages(payload) == {}


def test_fetch_skips_when_token_expired(ib, monkeypatch):
    """🔴 红线：token 过期时必须直接放弃，绝不尝试刷新。"""
    ib.KIMI_CRED_FILE.write_text(json.dumps({
        'access_token': 'tok', 'refresh_token': 'rt',
        'expires_at': int(time.time()) - 10, 'token_type': 'Bearer',
    }), encoding='utf-8')
    called = []
    monkeypatch.setattr('urllib.request.urlopen',
                        lambda *a, **k: called.append(1))
    assert ib._fetch_kimi_usage() == {}
    assert not called, '过期 token 不得发任何请求（更不得走 refresh）'


def test_fetch_skips_when_cred_missing(ib):
    assert not ib.KIMI_CRED_FILE.exists()
    assert ib._fetch_kimi_usage() == {}


def test_fetch_uses_bearer_and_parses(ib, monkeypatch):
    ib.KIMI_CRED_FILE.write_text(json.dumps({
        'access_token': 'tok123', 'refresh_token': 'rt',
        'expires_at': int(time.time()) + 600, 'token_type': 'Bearer',
    }), encoding='utf-8')
    seen = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(REAL_PAYLOAD).encode()

    def fake_urlopen(req, timeout=None):
        seen['auth'] = req.get_header('Authorization')
        seen['url'] = req.full_url
        seen['timeout'] = timeout
        return FakeResp()

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
    u = ib._fetch_kimi_usage()
    assert seen['auth'] == 'Bearer tok123'
    assert seen['url'].endswith('/usages')
    assert seen['timeout'] and seen['timeout'] <= 10      # 必须有超时，别拖死 poller
    assert u['five_hour']['used_percentage'] == 1.0


def test_fetch_network_error_is_silent(ib, monkeypatch):
    ib.KIMI_CRED_FILE.write_text(json.dumps({
        'access_token': 'tok', 'expires_at': int(time.time()) + 600,
    }), encoding='utf-8')

    def boom(*a, **k):
        raise OSError('network down')

    monkeypatch.setattr('urllib.request.urlopen', boom)
    assert ib._fetch_kimi_usage() == {}      # 静默降级，不抛


def test_cached_reader_never_does_network(ib, monkeypatch):
    """usage() 走在 /api/state 同步路径上——读缓存这一步绝不能发网络请求。"""
    ib.KIMI_USAGE_CACHE.write_text(json.dumps({
        'five_hour': {'used_percentage': 12.5},
        'seven_day': {'used_percentage': 60.0},
        'fetched_at': 1785390000,
    }), encoding='utf-8')

    def boom(*a, **k):
        raise AssertionError('读缓存不得发网络请求')

    monkeypatch.setattr('urllib.request.urlopen', boom)
    u = ib._kimi_usage_cached()
    assert u['five_hour']['used_percentage'] == 12.5
    assert u['seven_day']['used_percentage'] == 60.0
    assert u['fetched_at'] == 1785390000


def test_cached_reader_tolerates_garbage(ib):
    ib.KIMI_USAGE_CACHE.write_text('not json{', encoding='utf-8')
    assert ib._kimi_usage_cached() == {}
    ib.KIMI_USAGE_CACHE.write_text(json.dumps({'five_hour': 'wrongtype'}),
                                   encoding='utf-8')
    assert ib._kimi_usage_cached() == {}


def test_usage_surfaces_kimi_key(ib):
    """端到端：缓存在位 → STATE.usage() 里出现 usage.kimi（UI 据此自动渲染双条）。"""
    ib.KIMI_USAGE_CACHE.write_text(json.dumps({
        'five_hour': {'used_percentage': 7.0},
        'seven_day': {'used_percentage': 49.0},
    }), encoding='utf-8')
    st = ib.STATE
    st._usage_ts = 0.0                       # 强制过期，触发重算
    u = st.usage()
    assert 'kimi' in u, u
    assert u['kimi']['five_hour']['used_percentage'] == 7.0
    assert u['kimi']['seven_day']['used_percentage'] == 49.0
