#!/usr/bin/env python3
"""install_hooks.py 的 PermissionRequest 单项增删（沙箱 settings，不碰真实 ~/.claude）。

要求：只增删这一项钩子、带时间戳备份、幂等；不动已有的其他钩子与设置。
（旧 --uninstall 是整份备份覆盖回去，会冲掉备份之后别处对设置的改动，故不复用。）

运行：cd apps/agents-island && python3 -m pytest tests/test_install_hooks.py -v
"""
import json
import subprocess
import sys
from pathlib import Path

INSTALL = Path(__file__).resolve().parent.parent / 'scripts' / 'install_hooks.py'


def run(cfg, *args):
    return subprocess.run([sys.executable, str(INSTALL), '--config', str(cfg), *args],
                          capture_output=True, text=True, timeout=30)


def base_settings(cfg):
    cfg.write_text(json.dumps({
        'model': 'opus',
        'hooks': {'PreToolUse': [{'matcher': '', 'hooks': [{'type': 'command', 'command': 'bash /x/pre_tool_use.sh'}]}],
                  'Stop': [{'matcher': '', 'hooks': [{'type': 'command', 'command': 'bash /x/notify_hook.sh'}]}]},
    }, indent=2))


def pr_entries(cfg):
    return [h for e in json.loads(cfg.read_text()).get('hooks', {}).get('PermissionRequest', [])
            for h in e.get('hooks', [])]


def test_add_permission_request_hook(tmp_path):
    cfg = tmp_path / 'settings.json'
    base_settings(cfg)
    p = run(cfg, '--permission-request')
    assert p.returncode == 0, p.stderr
    hs = pr_entries(cfg)
    assert len(hs) == 1 and hs[0]['command'].endswith('hooks/permission_request.sh')
    assert hs[0]['command'].startswith('bash '), '以 bash 调用，不依赖执行位'
    assert hs[0]['timeout'] == 120
    d = json.loads(cfg.read_text())
    assert d['model'] == 'opus' and len(d['hooks']['PreToolUse']) == 1 and len(d['hooks']['Stop']) == 1
    assert list(tmp_path.glob('settings.json.bak-pr-*')), '须留时间戳备份'


def test_add_is_idempotent(tmp_path):
    cfg = tmp_path / 'settings.json'
    base_settings(cfg)
    run(cfg, '--permission-request')
    run(cfg, '--permission-request')
    assert len(pr_entries(cfg)) == 1


def test_remove_only_touches_permission_request(tmp_path):
    cfg = tmp_path / 'settings.json'
    base_settings(cfg)
    run(cfg, '--permission-request')
    d = json.loads(cfg.read_text())
    d['theme'] = 'dark'                       # 登记之后别处又改了设置
    cfg.write_text(json.dumps(d))
    p = run(cfg, '--remove-permission-request')
    assert p.returncode == 0, p.stderr
    d = json.loads(cfg.read_text())
    assert 'PermissionRequest' not in d['hooks']
    assert d['theme'] == 'dark', '删除时不得冲掉登记之后的其他改动'
    assert len(d['hooks']['PreToolUse']) == 1


def test_dry_run_writes_nothing(tmp_path):
    cfg = tmp_path / 'settings.json'
    base_settings(cfg)
    before = cfg.read_text()
    run(cfg, '--permission-request', '--dry')
    assert cfg.read_text() == before
