# Agents Island 🏝️

iOS 灵动岛风格的 Coding Agent 监控台：停靠 Windows 屏幕顶部居中，监控 WSL 内
Claude Code / Codex / Gemini CLI / Kimi CLI 实例，审批事件自动弹出，
hover 唤出、点击展开全量实例面板。

```
┌────────────────────────── Windows 本机 ──────────────────────────┐
│  win/island.py（pywebview 6.1 + WebView2，无边框/透明/置顶）       │
│  全局热键 RegisterHotKey：Ctrl+Alt+A / D / S / Q                  │
└──────────────┬───────────────────────────────────────────────────┘
               │ http://127.0.0.1:5599（WSL2 localhost 转发）
┌──────────────▼────────────── WSL Ubuntu-OMD ─────────────────────┐
│  bridge/island_bridge.py（stdlib HTTP，零第三方依赖）              │
│   ├─ 只读 import AgentMonitor/{claude,codex,gemini,kimi}_monitor  │
│   ├─ 尾随 /tmp/claude_perm_queue.jsonl（inode+offset，防截断）     │
│   └─ 写 /tmp/claude_perm_responses/<id>.json + always 标志        │
└───────────────────────────────────────────────────────────────────┘
```

## 启动

| 方式 | 命令 |
|------|------|
| 一键（推荐） | 双击 `launch/AgentsIsland.vbs`（静默拉桥+岛） |
| 开机自启 | 把 `AgentsIsland.vbs` 快捷方式放入 `shell:startup` |
| 手动分步 | WSL: `bash launch/start_bridge.sh` → Win: `pythonw win\island.py` |

## 四态交互

| 状态 | 触发 | 说明 |
|------|------|------|
| sliver | 默认/Esc/自动缩回 | 缩入顶边，仅露 5px 呼吸条（橙=正常，红闪=有待审批） |
| compact | 鼠标移到顶缘中央触发条 | 胶囊：`N agents · M working` + 各 agent 色点 |
| approval | 审批事件到达（自动弹出） | 工具名+命令摘要+Deny/Allow/Always；多条排队显示 `1 / N` |
| expanded | 点击 compact 胶囊 | 全量运行实例（四色分组、状态呼吸点、分支/工时）+ 内联审批 |

## 快捷键

| 键 | 作用 | 生效范围 |
|----|------|----------|
| `A` / `D` / `S` | Allow / Deny / Always Allow | 岛聚焦时 |
| `Esc` | 收回 sliver | 岛聚焦时 |
| `Ctrl+Alt+A` / `D` / `S` | 审批最早一条待审 | 全局（岛未聚焦也可） |
| `Ctrl+Alt+E` | 开/关全量实例面板 | 全局 |
| `Ctrl+Alt+Q` | 退出岛 | 全局 |

## 测试

```bash
cd /mnt/d/OMD-Workspace/apps/agents-island
python3 -m pytest tests/test_bridge.py -v   # 桥协议 10 例
python3 tests/ui_test.py                    # Playwright UI 23 例
# 端到端伪审批（需桥以 --debug 启动）
curl -s -X POST localhost:5599/api/test/enqueue -d '{"tool_name":"Bash","tool_input":{"command":"echo test"}}'
```

## 已知限制与约定

- 与旧 AgentMonitor popup **并行共存、先应者赢**（D-117）；满意后再决定停旧链路。
- hook 35s 超时默认 allow 是既有行为，岛崩溃不会卡死 Claude（也意味着漏审会放行）。
- Always 标志在 agent 完成一轮（Stop hook）后自动清除，与旧行为一致。
- 全局热键被其他软件占用时静默降级（岛内按键不受影响），可在 `win/island_config.json` 关闭。
- 配置：`win/island_config.json`（桥端口/轮询间隔/热键开关/顶部边距）。

## 实现要点（踩坑速查，详见 memory/project_agents_island.md）

- 异形透明窗 = pywebview `transparent=True`（透 WebView2 表面）+ Form `TransparencyKey=#010101`（抠掉顶层 Form 底色，附带键色区点击穿透）。岛体纯黑 #000000 不受键色影响。
- 窗口几何走 Win32 `SetWindowPos`（物理像素，`GetDpiForWindow` 实测倍率）。pywebview 的 resize 不乘 DPI、move 乘 DPI，200% 屏上不可用；改 Z 序标志会被拒（窗口已 TOPMOST），必须 `SWP_NOZORDER`。
- js_api 对象严禁挂 pywebview Window 公开属性（桥递归序列化 native Form 会爆栈，整窗挂死「未响应」）——用 `_window` 下划线私有。
- hover 权威信号来自 Python 全局光标轮询（窗口原生移动时 Chrome 边界事件失灵）。
- 页面自愈：桥死 → WebView2 停在错误页 → page_watchdog 检测 JS 死亡，桥复活后自动重载。
- 诊断三板斧：`/tmp/island_client.log`（页面黑匣子）、`win/island_win.log`（壳日志）、`/tmp/island_bridge.log`（桥日志）；`tests/probe_*.py` 为参数二分定位脚本。

## 设计依据

视觉/动画规格（spring 曲线、squircle、blur 交叉过渡、错峰编排）与架构调研见
`reports/proposals/2026-06-10_agents-island/`，决策 D-116 ~ D-118。
