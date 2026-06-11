/* Agents Island — 状态机 / 轮询 / 渲染
 *
 * 运行环境双模：
 *   1. pywebview（生产）：window.pywebview.api 负责原生窗口随态缩放
 *   2. 浏览器（自测）：?bridge=http://127.0.0.1:5599 指定桥地址，窗口缩放为 no-op
 *
 * 四态：sliver ⇄ compact ⇄ expanded；审批到达任意态 → approval（expanded 内则内联）
 */
'use strict';

const qs = new URLSearchParams(location.search);
const BRIDGE = qs.get('bridge') || '';
const POLL_MS = parseInt(qs.get('poll') || '1000', 10);

const stage = document.getElementById('stage');
const island = document.getElementById('island');

const MODES = ['sliver', 'compact', 'approval', 'expanded'];
const AREA = { sliver: 1, compact: 2, approval: 3, expanded: 4 };  // 大小序，用于判断展开/收起方向
const AGENT_COLOR = {
  claude: '#D97757', gemini: '#3B72D9', kimi: '#7C5DC9', codex: '#22C55E',
};
const AGENT_LABEL = { claude: 'Claude', gemini: 'Gemini', kimi: 'Kimi', codex: 'Codex' };

const S = {
  mode: 'sliver',
  pending: [],          // 桥侧待审批（FIFO）
  sessions: {},
  online: false,
  failCount: 0,
  resolving: false,     // 审批卡滑出动画中
  shownNotify: new Set(),
  collapseTimer: null,
  decided: 0,
};

/* ── 黑匣子：关键事件回传桥侧落盘（跨系统诊断用） ─────────────────── */
function clog(msg) {
  try {
    fetch(`${BRIDGE}/api/client_log`, {
      method: 'POST', body: JSON.stringify({ msg }),
    }).catch(() => {});
  } catch (e) { /* ignore */ }
}
window.addEventListener('error', e => clog(`JSERR ${e.message} @${e.filename}:${e.lineno}`));
document.addEventListener('pointerdown', e => clog(`CLICKPROBE pointerdown @${Math.round(e.clientX)},${Math.round(e.clientY)} target=${e.target.id||e.target.className}`), true);
document.addEventListener('mousemove', e => { if (!window.__mvlog) { window.__mvlog = 1; clog('CLICKPROBE first-mousemove'); } }, true);
window.addEventListener('unhandledrejection', e => clog(`REJECT ${e.reason}`));

/* ── 原生窗口协调 ─────────────────────────────────────────────────── */
async function pyResize(mode, h) {
  try {
    if (window.pywebview?.api?.resize_for) {
      clog(`pyResize ${mode} ${h || 0} calling`);
      await window.pywebview.api.resize_for(mode, h || 0);
      clog(`pyResize ${mode} done`);
    } else {
      clog(`pyResize ${mode} skipped (no api: pywebview=${typeof window.pywebview})`);
    }
  } catch (e) { clog(`pyResize ${mode} ERR ${e}`); }
}

/* 展开高度按内容自适应：头部+审批卡+各分区+底栏，钳制 [300, 480] */
function expandedHeight() {
  let live = 0, secs = 0;
  for (const a of Object.keys(AGENT_COLOR)) {
    const n = liveSessions(a).length;
    if (n) { secs++; live += n; }
  }
  const h = 96 + S.pending.length * 54 + secs * 30 + live * 44 + (live ? 0 : 90);
  return Math.max(300, Math.min(480, h));
}

/* 展开：窗口先扩，再做 CSS 形变；收起：CSS 先缩（无回弹曲线），窗口后缩 */
let modeSeq = 0;
async function setMode(target) {
  if (!MODES.includes(target) || target === S.mode) return;
  clog(`setMode ${S.mode} -> ${target} vp=${innerWidth}x${innerHeight} dpr=${devicePixelRatio} isl=${island.offsetWidth}x${island.offsetHeight} native=${document.body.classList.contains('native')}`);
  const seq = ++modeSeq;
  const growing = AREA[target] > AREA[S.mode];
  let exH = 0;
  if (target === 'expanded') {
    exH = expandedHeight();
    stage.style.setProperty('--h-expanded', `${exH}px`);
  } else if (target === 'approval') {
    exH = approvalHeight() || 118;
    stage.style.setProperty('--h-approval', `${exH}px`);
  }
  const interactive = (target === 'approval' || target === 'expanded');
  try { window.pywebview?.api?.set_interactive?.(interactive); } catch (e) { /* 浏览器 */ }
  S.mode = target;
  if (growing) {
    stage.dataset.mode = target;        // 内容先渲染，窗口生长=揭幕（消黑板闪现）
    render();
    await pyResize(target, exH);
    if (seq !== modeSeq) return;
  } else {
    island.classList.add('shrinking');
    stage.dataset.mode = target;
    setTimeout(async () => {
      island.classList.remove('shrinking');
      if (seq === modeSeq) await pyResize(target);
    }, 320);
  }
  if (target === 'approval') beep('alert');
  render();
}

function scheduleCollapse(delay) {
  cancelCollapse();
  S.collapseTimer = setTimeout(() => {
    if (S.pending.length && S.mode !== 'expanded') return;  // 审批在场不收
    setMode('sliver');
  }, delay);
}
function cancelCollapse() {
  if (S.collapseTimer) { clearTimeout(S.collapseTimer); S.collapseTimer = null; }
}

/* ── 桥轮询 ───────────────────────────────────────────────────────── */
async function poll() {
  try {
    const r = await fetch(`${BRIDGE}/api/state`, { cache: 'no-store' });
    const data = await r.json();
    S.failCount = 0;
    S.online = true;
    S.pending = data.pending || [];
    S.sessions = data.sessions || {};
    (data.notify || []).forEach(showToast);
    handleUi(data.ui);
  } catch (e) {
    if (++S.failCount >= 3) { S.online = false; }
  }
  applyState();
  render();
}

/* Python 侧事件经桥中转（光标进出窗口 / 热键 / 托盘动作） */
let lastCursorInside = null;
let lastUiSeq = null;
function handleUi(ui) {
  if (!ui) return;
  if (lastUiSeq === null) {            // 首拉只对齐序号，不回放历史动作
    lastUiSeq = Math.max(0, ...ui.events.map(e => e.seq));
    lastCursorInside = ui.cursor_inside;
    return;
  }
  if (ui.cursor_inside !== lastCursorInside) {
    lastCursorInside = ui.cursor_inside;
    window.islandCursor(ui.cursor_inside);
  }
  for (const ev of ui.events) {
    if (ev.seq <= lastUiSeq) continue;
    lastUiSeq = ev.seq;
    if (ev.action === 'toggle') {
      setMode(S.mode === 'expanded' ? 'sliver' : 'expanded');
    }
  }
}

let lastWorkingPush = -1;
function applyState() {
  stage.classList.toggle('offline', !S.online);
  stage.classList.toggle('has-pending', S.pending.length > 0);
  const working = countWorking();
  stage.classList.toggle('working', working > 0);
  if (working !== lastWorkingPush) {
    lastWorkingPush = working;
    try { window.pywebview?.api?.set_working?.(working); } catch (e) { /* 浏览器模式 */ }
  }

  if (S.pending.length > 0 && !S.resolving) {
    if (S.mode === 'sliver' || S.mode === 'compact') setMode('approval');
  } else if (S.mode === 'approval' && S.pending.length === 0 && !S.resolving) {
    setMode('compact');
    scheduleCollapse(2500);
  }
}

/* ── 审批决策 ─────────────────────────────────────────────────────── */
async function decide(id, decision, reason) {
  const entry = S.pending.find(p => p.id === id);
  if (!entry || S.resolving) return;
  S.resolving = true;
  try { window.pywebview?.api?.unfocus_input?.(); } catch (e) { /* 浏览器模式 */ }
  const face = document.querySelector('.face-approval');
  if (S.mode === 'approval') face.classList.add('resolve');
  try {
    await fetch(`${BRIDGE}/api/decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, decision, reason: reason || '' }),
    });
    S.decided++;
    beep(decision === 'deny' ? 'deny' : 'ok');
  } catch (e) { /* 桥不可达：条目留在队列，hook 35s 兜底 */ }
  S.pending = S.pending.filter(p => p.id !== id);
  setTimeout(() => {
    face.classList.remove('resolve');
    S.resolving = false;
    applyState();
    render();
  }, 280);
}

function decideFirst(decision) {
  if (S.pending.length) decide(S.pending[0].id, decision);
}
window.islandHotkey = decideFirst;  // Python 全局热键入口

/* 岛上作答：deny+reason 通道把选择/输入传回模型（allow=回落终端 TUI） */
function answerAsk(text) {
  const e = S.pending[0];
  if (!e || e.kind !== 'ask') return;
  const q = askPayload(e)?.question || '';
  decide(e.id, 'deny',
    `[用户已在 Agents Island 面板作答] 问题：「${q.slice(0, 120)}」 用户的回答：${text}。请按此回答继续，无需再次询问。`);
}

document.querySelector('.face-approval').addEventListener('click', ev => {
  const opt = ev.target.closest('.ask-opt');
  if (opt) {
    const e = S.pending[0];
    const o = askPayload(e)?.options[Number(opt.dataset.i)];
    if (o) answerAsk(`选择「${o.label || o}」`);
    return;
  }
  if (ev.target.id === 'ask-send') {
    const v = document.getElementById('ask-input')?.value.trim();
    if (v) answerAsk(`自定义输入：${v}`);
    return;
  }
  if (ev.target.id === 'ask-terminal') {
    decideFirst('allow');                  // allow → 问题回落终端正常作答
    return;
  }
  if (ev.target.id === 'ask-input') {
    try { window.pywebview?.api?.focus_input?.(); } catch (e2) { /* 浏览器 */ }
    setTimeout(() => document.getElementById('ask-input')?.focus(), 120);
  }
});
document.querySelector('.face-approval').addEventListener('keydown', ev => {
  if (ev.target.id === 'ask-input' && ev.key === 'Enter') {
    const v = ev.target.value.trim();
    if (v) answerAsk(`自定义输入：${v}`);
  }
});

/* ── 渲染 ─────────────────────────────────────────────────────────── */
function liveSessions(agent) {
  return (S.sessions[agent] || []).filter(s => s.is_live);
}
function countWorking() {
  let n = 0;
  for (const a of Object.keys(AGENT_COLOR)) {
    n += liveSessions(a).filter(s => statusKind(s.status) === 'active').length;
  }
  return n;
}
function statusKind(st) {
  st = String(st || '').toLowerCase();
  if (/exec|work|run|active|tool/.test(st)) return 'active';
  if (/wait|input|perm|ask|attention/.test(st)) return 'waiting';
  return 'idle';
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function fmtAge(sec) {
  if (sec == null) return '';
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  return `${Math.floor(sec / 3600)}h${Math.floor((sec % 3600) / 60)}m`;
}
function entryDetail(e) {
  const ti = e.tool_input || {};
  return ti.command || ti.file_path || ti.description || ti.url ||
         (Object.keys(ti).length ? JSON.stringify(ti) : '');
}

function render() {
  renderCompact();
  if (S.mode === 'approval') renderApproval();
  if (S.mode === 'expanded') renderExpanded();
}

function renderCompact() {
  const totalLive = Object.keys(AGENT_COLOR).reduce((n, a) => n + liveSessions(a).length, 0);
  const working = countWorking();
  const txt = document.getElementById('compact-text');
  if (S.toastMsg && Date.now() < S.toastMsg.until) {
    txt.textContent = S.toastMsg.text;
    return;
  }
  if (!S.online) {
    txt.innerHTML = 'bridge offline <span class="dim">重连中…</span>';
  } else if (totalLive === 0) {
    txt.innerHTML = '<span class="dim">无运行中实例</span>';
  } else {
    txt.innerHTML = `${totalLive} agents<span class="dim"> · ${working} working</span>`;
  }
  const dots = document.getElementById('compact-dots');
  dots.innerHTML = Object.keys(AGENT_COLOR).map(a => {
    const live = liveSessions(a);
    if (!live.length) return '';
    const w = live.some(s => statusKind(s.status) === 'active');
    return `<span class="dot ${w ? 'working' : ''}" style="--c:${AGENT_COLOR[a]}"></span>`;
  }).join('');
}

/* AskUserQuestion 解析：单问题才走岛上作答（多问题罕见，回落普通审批） */
function askPayload(e) {
  if (e.kind !== 'ask') return null;
  const qs = e.tool_input?.questions;
  if (!Array.isArray(qs) || qs.length !== 1) return null;
  const q = qs[0];
  return { question: q.question || '', options: (q.options || []).slice(0, 6) };
}

function approvalHeight() {
  const e = S.pending[0];
  const ask = e && askPayload(e);
  if (!ask) return 0;                       // 普通审批用 CSS 默认高
  return Math.min(420, 118 + 24 + ask.options.length * 34 + 44);
}

let askRendered = '';
function renderApproval() {
  const e = S.pending[0];
  if (!e) return;
  const ask = askPayload(e);
  const box = document.getElementById('ask-box');
  const actions = document.getElementById('ap-actions');
  const detail = document.getElementById('ap-detail');
  if (ask) {
    detail.style.display = 'none';
    actions.style.display = 'none';
    box.hidden = false;
    const key = e.id;
    if (askRendered !== key) {
      askRendered = key;
      box.innerHTML = `<div class="ask-q">${esc(ask.question)}</div>` +
        ask.options.map((o, i) =>
          `<button class="ask-opt" data-i="${i}"><kbd>${i + 1}</kbd>
             <span>${esc(o.label || o)}</span>
             ${o.description ? `<span class="opt-desc">${esc(o.description).slice(0, 60)}</span>` : ''}
           </button>`).join('') +
        `<div class="ask-input-row">
           <input class="ask-input" id="ask-input" placeholder="自定义回答…（Enter 发送）">
           <button class="ask-send" id="ask-send">发送</button>
         </div>
         <div class="ask-foot"><button class="ask-terminal" id="ask-terminal">改在终端回答 →</button></div>`;
    }
  } else {
    askRendered = '';
    box.hidden = true;
    detail.style.display = '';
    actions.style.display = '';
  }
  const agent = e.agent_source || 'claude';
  document.getElementById('ap-agent-dot').style.setProperty('--c', AGENT_COLOR[agent] || AGENT_COLOR.claude);
  document.getElementById('ap-tool').textContent = e.tool_name || '工具调用';
  document.getElementById('ap-proj').textContent =
    [AGENT_LABEL[agent], e.title || e.project || e.session_slug].filter(Boolean).join(' · ');
  document.getElementById('ap-queue').textContent = S.pending.length > 1 ? `1 / ${S.pending.length}` : '';
  document.getElementById('ap-detail').textContent = entryDetail(e);
}

/* 脏检查缓存：内容不变不触碰 DOM（防止轮询重渲染打断点击/hover） */
const rendered = { pend: '', body: '' };

function renderExpanded() {
  const exPending = document.getElementById('ex-pending');
  const pendHtml = S.pending.map(e => {
    const agent = e.agent_source || 'claude';
    return `<div class="pend-card" data-id="${esc(e.id)}">
      <span class="agent-dot" style="--c:${AGENT_COLOR[agent]}"></span>
      <div class="pend-info">
        <div class="pend-tool">${esc(e.tool_name || '工具调用')}</div>
        <div class="pend-sub">${esc(entryDetail(e)).slice(0, 80)}</div>
      </div>
      <button class="btn btn-deny mini"  data-act="deny">Deny</button>
      <button class="btn btn-allow mini" data-act="allow">Allow</button>
    </div>`;
  }).join('');
  if (pendHtml !== rendered.pend) {
    rendered.pend = pendHtml;
    exPending.innerHTML = pendHtml;
  }

  let total = 0;
  const secs = Object.keys(AGENT_COLOR).map(agent => {
    const live = liveSessions(agent);
    if (!live.length) return '';
    total += live.length;
    const rows = live.map(s => {
      const kind = statusKind(s.status);
      return `<div class="row" style="--c:${AGENT_COLOR[agent]}">
        <span class="st ${kind}"></span>
        <div class="row-main">
          <div class="row-title">${esc(s.title || s.slug || s.session_id)}</div>
          <div class="row-sub">${esc([s.project, s.git_branch].filter(Boolean).join(' · '))}</div>
        </div>
        <span class="row-meta">${esc(s.last_tool || '')} ${fmtAge(s.age_seconds)}</span>
      </div>`;
    }).join('');
    return `<div class="sec">
      <div class="sec-head" style="--c:${AGENT_COLOR[agent]}">${AGENT_LABEL[agent]}
        <span class="cnt">${live.length}</span></div>
      ${rows}</div>`;
  }).join('');

  const bodyHtml = total ? secs : '<div class="ex-empty">当前没有运行中的 Agent 实例</div>';
  if (bodyHtml !== rendered.body) {
    rendered.body = bodyHtml;
    document.getElementById('ex-body').innerHTML = bodyHtml;
  }
  document.getElementById('ex-stats').textContent =
    `${total} live · ${countWorking()} working · 已审 ${S.decided}`;
  const bs = document.getElementById('bridge-status');
  bs.textContent = S.online ? '● bridge' : '● bridge 离线';
  bs.className = `foot-link ${S.online ? 'ok' : 'down'}`;
}

/* ── 通知 toast ───────────────────────────────────────────────────── */
function showToast(n) {
  if (S.shownNotify.has(n.id)) return;
  S.shownNotify.add(n.id);
  if (S.shownNotify.size > 200) S.shownNotify.clear();
  if (document.body.classList.contains('native')) {
    // Region 窗口无岛外空间：通知改为 compact 胶囊内联闪示 6s
    const agent = n.agent_source || 'claude';
    S.toastMsg = {
      text: `✓ ${AGENT_LABEL[agent] || 'Agent'} · ${(n.message || n.title || '完成一轮任务')}`.slice(0, 48),
      until: Date.now() + 6000,
    };
    if (S.mode === 'sliver') {
      setMode('compact');
      scheduleCollapse(6500);
    }
    render();
    return;
  }
  if (S.mode === 'sliver') return;          // 收起时不打扰（红条已示意）
  const box = document.getElementById('toast');
  if (box.children.length >= 3) box.firstChild?.remove();
  const agent = n.agent_source || 'claude';
  const el = document.createElement('div');
  el.className = 'toast-item';
  el.innerHTML = `<span class="agent-dot" style="--c:${AGENT_COLOR[agent] || '#888'}"></span>
    <span class="t-msg">${esc(n.message || n.title || `${AGENT_LABEL[agent] || 'Agent'} 完成一轮任务`)}</span>`;
  box.appendChild(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 320); }, 6000);
}

/* ── 声效（WebAudio 轻提示音） ────────────────────────────────────── */
let audioCtx = null;
function beep(kind) {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') audioCtx.resume();
    const seq = { alert: [[740, 0], [988, .09]], ok: [[880, 0]], deny: [[330, 0]] }[kind] || [];
    seq.forEach(([freq, at]) => {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.type = 'sine'; o.frequency.value = freq;
      g.gain.setValueAtTime(0.0001, audioCtx.currentTime + at);
      g.gain.exponentialRampToValueAtTime(0.06, audioCtx.currentTime + at + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + at + 0.22);
      o.connect(g).connect(audioCtx.destination);
      o.start(audioCtx.currentTime + at); o.stop(audioCtx.currentTime + at + 0.25);
    });
  } catch (e) { /* 无声环境忽略 */ }
}

/* ── 交互 ─────────────────────────────────────────────────────────── */
island.addEventListener('mouseenter', () => {
  cancelCollapse();
  if (S.mode === 'sliver') setMode(S.pending.length ? 'approval' : 'compact');
});
function onLeave() {
  if (S.mode === 'compact') scheduleCollapse(1200);
  else if (S.mode === 'expanded') scheduleCollapse(2500);
}
/* 生产环境窗口≈岛体，body/island 等价；浏览器自测视口更大，两者都挂 */
document.body.addEventListener('mouseleave', onLeave);
island.addEventListener('mouseleave', onLeave);
document.body.addEventListener('mouseenter', cancelCollapse);

/* 权威 hover 信号（Python 全局光标轮询推送）：原生窗口移动/缩放会让
   浏览器边界事件失灵，此通道兜底纠偏。浏览器自测模式无此调用。 */
window.islandCursor = inside => {
  if (inside) {
    cancelCollapse();
    if (S.mode === 'sliver') setMode(S.pending.length ? 'approval' : 'compact');
  } else {
    onLeave();
  }
};

island.addEventListener('click', e => {
  if (e.target.closest('.btn')) return;
  if (S.mode === 'compact') setMode('expanded');
});

document.getElementById('btn-allow').addEventListener('click', () => decideFirst('allow'));
document.getElementById('btn-deny').addEventListener('click', () => decideFirst('deny'));
document.getElementById('btn-always').addEventListener('click', () => decideFirst('always'));
document.getElementById('ex-pending').addEventListener('click', e => {
  const btn = e.target.closest('.mini');
  if (!btn) return;
  decide(btn.closest('.pend-card').dataset.id, btn.dataset.act);
});

window.addEventListener('keydown', e => {
  const k = e.key.toLowerCase();
  if (k === 'escape') { setMode('sliver'); return; }
  if (S.pending.length && (S.mode === 'approval' || S.mode === 'expanded')) {
    const ask = askPayload(S.pending[0]);
    if (ask && document.activeElement?.id !== 'ask-input') {
      const n = parseInt(e.key, 10);
      if (n >= 1 && n <= ask.options.length) {
        const o = ask.options[n - 1];
        answerAsk(`选择「${o.label || o}」`);
        return;
      }
    }
    if (document.activeElement?.id === 'ask-input') return;  // 输入框内不抢键
    if (k === 'a') decideFirst('allow');
    else if (k === 'd') decideFirst('deny');
    else if (k === 's') decideFirst('always');
  }
});

/* Playwright / 调试探针 */
window.__island = {
  get mode() { return S.mode; },
  get state() { return S; },
  setMode,
};

/* ── 启动 ─────────────────────────────────────────────────────────── */
if (window.pywebview) document.body.classList.add('native');
window.addEventListener('pywebviewready', () => document.body.classList.add('native'));
stage.dataset.mode = S.mode;
clog(`boot ua=${navigator.userAgent.slice(-40)} pywebview=${typeof window.pywebview}`);
document.addEventListener('visibilitychange',
  () => clog(`visibility=${document.visibilityState}`));
window.addEventListener('pywebviewready', () => clog('pywebviewready'));
poll();
setInterval(poll, POLL_MS);
