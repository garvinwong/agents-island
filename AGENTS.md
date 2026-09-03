# AGENTS.md

> Machine-facing guide for coding agents (Claude Code, Codex, Cursor, Kimi, …).
> Humans: read `README.md`. This file is the fast path to **install, run, verify, and extend**.

## What this is

**Agents Island** — an always-on-top "Dynamic Island"-style monitor for CLI coding
agents running inside WSL. A WSL **bridge** (Python stdlib HTTP, zero deps) aggregates
live session state and approval events; a Windows **pywebview shell** renders the island,
pops tool-approval requests (Deny/Allow/Always) and `AskUserQuestion` choice cards for
one-click, on-island answering.

## Environment — hard requirements

- **Windows 10/11 + WSL2.** The shell is Windows-side; the bridge is WSL-side. Both are required.
- WSL distro with **Python 3.10+** (bridge uses only the standard library).
- Windows **Python 3.10+** with `pywebview` + `pythonnet`, and the **WebView2 Runtime** (preinstalled on Win11).
- Not cross-platform: the shell uses Win32 + WinForms. The bridge alone runs anywhere but is useless without a shell.

## Layout — where code runs / where to edit

| Path | Runs on | Role |
|------|---------|------|
| `bridge/island_bridge.py` | WSL | HTTP server on `127.0.0.1:5599`; aggregates adapters, serves `/api/state`, records decisions |
| `bridge/vendor/<agent>_monitor.py` | WSL | per-CLI session scanner — **extend here** |
| `win/island.py` | Windows | pywebview + WebView2 + Win32 shell |
| `web/island.{html,css,js}` | (served by bridge) | the island UI; data-driven from `/api/state` |
| `hooks/*.sh` | WSL | PreToolUse / Stop / Notification hooks that feed the queue |
| `scripts/install_*.py` | WSL | idempotent hook/statusline installers (`--dry`, `--uninstall`) |
| `launch/start_bridge.sh`, `launch/AgentsIsland.vbs` | WSL / Win | launchers |
| `~/.agents-island/` (runtime, not in repo) | WSL | `queue.jsonl`, `responses/`, `settings.json`, `always_<agent>` |

## Install & run — in this order

```bash
# 1) WSL: start the bridge (idempotent; serves 127.0.0.1:5599)
bash launch/start_bridge.sh

# 2) WSL: install hooks for the CLIs you actually use (idempotent; --dry / --uninstall)
python3 scripts/install_hooks.py        # Claude Code  -> ~/.claude/settings.json
python3 scripts/install_kimi_hooks.py   # Kimi CLI     -> ~/.kimi/config.toml
python3 scripts/install_codex_hooks.py  # Codex        -> ~/.codex/hooks.json
```

```bat
:: 3) Windows: start the shell (or just double-click launch\AgentsIsland.vbs, which does steps 1+3)
pythonw win\island.py
```

## Verify

```bash
curl -s localhost:5599/api/health                       # -> {"ok":true,...}
curl -s localhost:5599/api/state | python3 -m json.tool # -> {sessions, pending, notify, ...}

# synthetic approval (bridge must be started with --debug) -> should pop on the island
curl -s -X POST localhost:5599/api/test/enqueue \
  -d '{"tool_name":"Bash","tool_input":{"command":"echo hello"}}'

python3 -m pytest tests/ -v        # bridge protocol + kimi hooks
python3 tests/ui_test.py           # Playwright UI checks
```

## Show files on the desktop (viewer windows)

Pop any local file into a standalone desktop viewer window (image / html / pdf / md), so a human can see your output without digging for file paths:

```bash
bash scripts/show.sh /path/to/screenshot.png        # kind auto-detected by extension
bash scripts/show.sh /path/to/demo.html             # html demo in a chrome-wrapped iframe
bash scripts/show.sh /path/to/report.md             # rendered dark-theme reader
bash scripts/show.sh /path/to/doc.pdf               # built-in Edge PDF reader
```

Flow: `show.sh` converts the path with `wslpath -w`, POSTs `/api/show` to the bridge; the island shell spawns `win/island_viewer.py` as a separate process per window (process isolation: a heavy page can only hang its own window, never the island). `--raw` opens an html file directly without the wrapper shell.

## Extend — add a new CLI (the one common task)

1. Create `bridge/vendor/<name>_monitor.py` exposing a function that returns a list of dicts with keys:
   `session_id, slug, project, cwd, status, last_tool, age_seconds, runtime, is_live, source`.
2. Register **one line** in `island_bridge.py` → `SESSION_ADAPTERS`: `'<name>': <name>_monitor.get_sessions`.
3. (optional) To get approvals on the island, have that CLI's PreToolUse hook append a queue entry (see protocol below).

The UI is fully data-driven from `state.sessions`; a new adapter auto-gets its own group
(fallback color + uppercase label). **No UI code is needed for monitoring.**

## Approval / question protocol (for manual hook wiring)

- **Approval**: append one JSON line to `~/.agents-island/queue.jsonl`:
  `{"id":"<unique>","tool_name":"Bash","tool_input":{"command":"…"},"session_id":"…","cwd":"…","agent_source":"<name>"}`
  Then poll `~/.agents-island/responses/<id>.json` for `{"decision":"allow"|"deny"}` and emit the hook's permission decision.
- **On-island answer**: set `tool_name:"AskUserQuestion"` with
  `tool_input.questions[0] = {question, options:[{label,description}]}` → renders as a choice card.
- Entries are de-duplicated by `id` — **`id` must be unique per event** (a constant id is silently dropped).

## Constraints — do not violate

- The bridge binds **`127.0.0.1` only**. Never expose port 5599 publicly.
- The shell's `js_api` object must **not** expose the pywebview `Window` as a public attribute
  (recursive serialization of the native Form crashes the window) — keep it as `_window`.
- Window geometry uses Win32 `SetWindowPos` with **`SWP_NOZORDER`** (the window is `TOPMOST`;
  changing Z-order flags is rejected). To raise it, re-assert `HWND_TOPMOST` with `SWP_NOACTIVATE`.
- Hooks **fail open** on timeout (≈35s → allow) so a crashed island never blocks the agent.

## Diagnostics

- Logs: `/tmp/island_client.log` (UI), `win/island_win.log` (shell), `/tmp/island_bridge.log` (bridge).
- `tests/probe_*.py` — bisection probes for window / DPI / transparency / sizing issues.

## License

MIT.
