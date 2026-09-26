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

const MODES = ['sliver', 'compact', 'approval', 'expanded', 'menu'];
const AREA = { sliver: 1, compact: 2, menu: 3, approval: 3, expanded: 4 };  // 大小序，用于判断展开/收起方向
const AGENT_COLOR = {
  claude: '#D97757', codex: '#22C55E', agy: '#3B72D9', gemini: '#5B8DEF', kimi: '#7C5DC9',
  butler: '#E8A75C',   // 外部助手（butler）：琥珀
};
const AGENT_LABEL = { claude: 'Claude', codex: 'Codex', agy: 'AGY', gemini: 'Gemini', kimi: 'Kimi',
  butler: 'Butler' };
// 数据驱动：桥侧新增适配器时（state.sessions 多出未知键）自动渲染分组，无需改前端
const agentColor = a => AGENT_COLOR[a] || '#9AA3AE';
const agentLabel = a => AGENT_LABEL[a] || String(a || 'Agent').toUpperCase();
const agentKeys = () =>
  [...new Set([...Object.keys(AGENT_COLOR), ...Object.keys(S.sessions || {})])];

/* ── i18n：navigator.language 自动判定，桥设置 lang 可覆盖（zh/en） ── */
const I18N = {
  zh: {
    toolCall: '工具调用', doneRound: '完成一轮任务',
    needApprove: '终端在等你批准', needInput: '终端在等你输入',
    permTag: '终端也在等你',
    stNeed: '需要你', stActive: '进行中', stReply: '待回复', stIdle: '空闲',
    markRead: '点击标为已读',
    resetTip: (l, p, left, at) => `${l === '5h' ? '5 小时' : '7 天'}额度已用 ${p}%，${left} 后重置（${at}）`,
    jumpTab: t => `已切到终端，会话在「${t}」标签页`, jumpNone: '没找到这个会话的终端窗口',
    moreWorking: n => ` · 另 ${n} 个进行中`, yourTurn: '等你回复', replyCount: n => `${n} 待回复`,
    offline: 'bridge offline <span class="dim">重连中…</span>',
    noLive: '<span class="dim">无运行中实例</span>',
    emptyPanel: '当前没有运行中的 Agent 实例',
    bridgeDown: '● bridge 离线', muteTag: ' · 🔕勿扰',
    stats: (t, w, d) => `${t} live · ${w} working · 已审 ${d}`,
    autoAllowIn: n => `${n}s 后自动放行`,
    footHint: 'A 允许 · D 拒绝 · S 始终 · Esc 收起',
    jumpTitle: '双击跳转到该会话终端',
    yoloTitle: 'YOLO：本会话工具审批秒放行（提问/计划仍上岛）',
    askPlaceholder: '自定义回答…（Enter 发送）', askSend: '发送',
    askTerminal: '改在终端回答 →',
    planPlaceholder: '驳回理由 / 修改意见…（可留空）',
    planReject: '驳回重拟', planApprove: '批准执行',
    choose: l => `选择「${l}」`, customInput: v => `自定义输入：${v}`,
    askAnswerMsg: (q, t) => `[用户已在 Agents Island 面板作答] 问题：「${q}」 用户的回答：${t}。请按此回答继续，无需再次询问。`,
    planRejectMsg: fb => `[用户在 Agents Island 审阅了计划] 决定：驳回，请修改后重新提出。${fb ? '修改意见：' + fb : ''}`,
    menuExpand: '展开 / 收起面板', menuMute: '勿扰', menuAuto: '超时自动放行 25s',
    menuSkin: '底纹皮肤', skinNames: { N2: '焦散弧', N1: '窗影', N3: '纯净', N4: '稿纸格', N5: '流纹' },
    menuAlpha: '面板透明度',
    menuReload: '重载页面', menuQuit: '退出', menuAutostart: '开机自启',
    reloaded: '✓ 页面已重载',
  },
  en: {
    toolCall: 'Tool call', doneRound: 'finished a turn',
    needApprove: 'waiting for approval in terminal', needInput: 'waiting for input in terminal',
    permTag: 'terminal is waiting too',
    stNeed: 'Needs you', stActive: 'Working', stReply: 'Your turn', stIdle: 'Idle',
    markRead: 'Click to mark as read',
    resetTip: (l, p, left, at) => `${l} quota ${p}% used, resets in ${left} (${at})`,
    jumpTab: t => `Terminal focused — session is in tab "${t}"`, jumpNone: 'No terminal window found for this session',
    moreWorking: n => ` · +${n} working`, yourTurn: 'your turn', replyCount: n => `${n} to reply`,
    offline: 'bridge offline <span class="dim">reconnecting…</span>',
    noLive: '<span class="dim">no live sessions</span>',
    emptyPanel: 'No running agent sessions',
    bridgeDown: '● bridge offline', muteTag: ' · 🔕DND',
    stats: (t, w, d) => `${t} live · ${w} working · ${d} decided`,
    autoAllowIn: n => `auto-allow in ${n}s`,
    footHint: 'A allow · D deny · S always · Esc collapse',
    jumpTitle: 'Double-click to jump to this session',
    yoloTitle: 'YOLO: auto-allow tool approvals for this session (questions/plans still surface)',
    askPlaceholder: 'Custom answer… (Enter to send)', askSend: 'Send',
    askTerminal: 'Answer in terminal →',
    planPlaceholder: 'Rejection reason / feedback… (optional)',
    planReject: 'Reject', planApprove: 'Approve',
    choose: l => `Selected "${l}"`, customInput: v => `Custom input: ${v}`,
    askAnswerMsg: (q, t) => `[User answered on the Agents Island panel] Question: "${q}" Answer: ${t}. Continue with this answer; do not ask again.`,
    planRejectMsg: fb => `[User reviewed the plan on Agents Island] Decision: rejected, please revise and re-propose.${fb ? ' Feedback: ' + fb : ''}`,
    menuExpand: 'Toggle panel', menuMute: 'Do not disturb', menuAuto: 'Auto-allow 25s',
    menuSkin: 'Texture skin', skinNames: { N2: 'Caustic', N1: 'Window', N3: 'Pure', N4: 'Grid', N5: 'Flow' },
    menuAlpha: 'Panel opacity',
    menuReload: 'Reload page', menuQuit: 'Quit', menuAutostart: 'Launch at startup',
    reloaded: '✓ Page reloaded',
  },
};
let LANG = qs.get('lang')
  || ((navigator.language || 'zh').toLowerCase().startsWith('zh') ? 'zh' : 'en');
if (!I18N[LANG]) LANG = 'en';
const T = key => I18N[LANG][key];

const S = {
  mode: 'sliver',
  pending: [],          // 桥侧待审批（FIFO）
  sessions: {},
  online: false,
  failCount: 0,
  resolving: false,     // 审批卡滑出动画中
  shownNotify: new Set(),
  snoozed: new Set(),   // Esc 搁置的审批 id（新 pending 到达即自然再弹）
  collapseTimer: null,
  decided: 0,
  termWait: {},         // session_id → 终端弹出批准/输入框的时刻（桥时钟），见 sessionState
  seen: loadSeen(),     // session_id → 标为已读的时刻（待回复 → 空闲）
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

/* 展开高度初值估算（渲染前用，防布局塌缩）；真实高度由 measureExpandedHeight 实测 */
function expandedHeight() {
  let live = 0, secs = 0;
  for (const a of agentKeys()) {
    const n = liveSessions(a).length;
    if (n) { secs++; live += n; }
  }
  const h = 96 + S.pending.length * 54 + secs * 30 + live * 44 + (live ? 0 : 90);
  return Math.max(300, Math.min(480, h));
}

/* 展开面板按内容量定高（2026-09-26 Owner 定）：渲染后实测各块，替代原"每行 44px"
   估算——带副标题的行实为 ~49px，5 个会话差 26px 出滚动条。上限 480 不变（Owner：
   不希望岛太长），下限 200 防空面板过扁。会话区不能取 scrollHeight（窗口比内容高时
   它等于窗口高，缩不回来），改为累加子元素；FLIP 退场中的绝对定位行不计 */
function blockContentHeight(el) {
  const c = getComputedStyle(el);
  let t = parseFloat(c.paddingTop) + parseFloat(c.paddingBottom);
  for (const ch of el.children) {
    const cc = getComputedStyle(ch);
    if (cc.position === 'absolute' || cc.display === 'none') continue;
    t += ch.offsetHeight + parseFloat(cc.marginTop) + parseFloat(cc.marginBottom);
  }
  return t;
}
function measureExpandedHeight() {
  const face = document.querySelector('.face-expanded');
  if (!face) return 0;
  // 真机窗口=岛体：展开前窗口还是细条宽（黑匣子实录 vp=220x36），此时组头与底栏
  // 折行，量出 369 而非 324 → 先大后缩两次 resize。临时按展开宽度排版再量，
  // 同步完成、不经绘制，不会闪
  const keep = face.style.cssText;
  const tw = parseFloat(getComputedStyle(stage).getPropertyValue('--w-expanded')) || 478;
  face.style.right = 'auto';
  face.style.width = `${tw}px`;
  const cs = getComputedStyle(face);
  let h = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
  for (const el of face.children) {
    const c = getComputedStyle(el);
    if (c.display === 'none') continue;
    h += (el.id === 'ex-body' ? blockContentHeight(el) : el.offsetHeight)
       + parseFloat(c.marginTop) + parseFloat(c.marginBottom);
  }
  face.style.cssText = keep;
  return Math.max(200, Math.min(480, Math.ceil(h)));
}
let lastExpandedH = 0;
function applyExpandedHeight() {
  if (S.mode !== 'expanded') return;
  const h = measureExpandedHeight();
  if (!h || Math.abs(h - lastExpandedH) < 6) return;   // 抖动阈值，免无谓 resize
  lastExpandedH = h;
  stage.style.setProperty('--h-expanded', `${h}px`);
  pyResize('expanded', h);
}

/* ── 入场编舞：setMode 瞬间给 #island 挂 600ms .entering 窗口
   （CSS 三层错峰只在窗口内播，轮询重渲染不重播）；--enter-scale 按前后态
   真实面积比取入场起点（grow=√(小/大) 钳 .90~.96，shrink=1.03 轻微前凸）── */
let enterTimer = 0;
function pxArea(mode) {
  const cs = getComputedStyle(stage);
  const w = parseFloat(cs.getPropertyValue(`--w-${mode}`)) || 300;
  const h = parseFloat(cs.getPropertyValue(`--h-${mode}`)) || 100;
  return w * h;
}
function markEntering(from, to, growing) {
  let s = 1.03;
  if (growing) s = Math.max(.90, Math.min(.96, Math.sqrt(pxArea(from) / pxArea(to))));
  island.style.setProperty('--enter-scale', s.toFixed(3));
  island.classList.remove('entering');
  void island.offsetWidth;                 // 重置编舞动画相位（同名类需重触发）
  island.classList.add('entering');
  clearTimeout(enterTimer);
  enterTimer = setTimeout(() => island.classList.remove('entering'), 600);
}

/* ── 共享元素连续性（梯队一#1）：compact↔expanded 的机器人 logo 不淡出
   重现，而是从旧位置滑行缩放到新位置（FLIP：窗口几何落定后测量播放）。
   approval/menu/sliver 无 logo 时自动 no-op。 ── */
function captureSharedRect() {
  const el = document.querySelector(`.face-${S.mode} .claude-mark`);
  return el ? el.getBoundingClientRect() : null;
}
function playShared(from) {
  if (!from || (typeof REDUCED !== 'undefined' && REDUCED)) return;
  const el = document.querySelector(`.face-${S.mode} .claude-mark`);
  if (!el) return;
  const to = el.getBoundingClientRect();
  if (!to.width) return;
  const dx = from.left - to.left, dy = from.top - to.top;
  const sc = from.width / to.width;
  if (Math.abs(dx) < 1 && Math.abs(dy) < 1 && Math.abs(sc - 1) < .02) return;
  el.animate([
    { transform: `translate(${dx}px, ${dy}px) scale(${sc})`, transformOrigin: 'top left' },
    { transform: 'none' },
  ], { duration: 340, easing: 'cubic-bezier(.3,.9,.2,1)', delay: 40, fill: 'backwards' });
}

/* 展开：窗口先扩，再做 CSS 形变；收起：CSS 先缩（无回弹曲线），窗口后缩 */
let modeSeq = 0;
async function setMode(target) {
  if (!MODES.includes(target) || target === S.mode) return;
  clog(`setMode ${S.mode} -> ${target} vp=${innerWidth}x${innerHeight} dpr=${devicePixelRatio} isl=${island.offsetWidth}x${island.offsetHeight} native=${document.body.classList.contains('native')}`);
  const seq = ++modeSeq;
  const growing = AREA[target] > AREA[S.mode];
  const sharedFrom = captureSharedRect();     // 共享元素起点（换脸前测量）
  let exH = 0;
  if (target === 'expanded') {
    exH = expandedHeight();
    stage.style.setProperty('--h-expanded', `${exH}px`);
  } else if (target === 'approval') {
    exH = approvalHeight();                 // 初值；render 后 applyApprovalHeight 实测覆盖
    stage.style.setProperty('--h-approval', `${exH}px`);
    lastApprovalH = 0;                      // 强制本次进入必重测
  } else if (target === 'menu') {
    renderMenu();                            // 先用上次 autostart 态即时渲染，不阻塞
    exH = menuHeight();
    stage.style.setProperty('--h-menu', `${exH}px`);
    // autostart 态异步回填，回来若仍是本次菜单则刷新开关亮点（不阻塞菜单打开，避免 await 竞态卡死）
    (async () => {
      try {
        const a = await window.pywebview?.api?.is_autostart?.();
        if (seq === modeSeq && S.mode === 'menu') { S.autostart = a; renderMenu(); }
      } catch (e) { /* 浏览器 */ }
    })();
  }
  // seq 守卫：本次调用在任何 await 前已被后续 setMode 抢占，则放弃，避免污染 S.mode
  // （曾因 menu 的 await is_autostart 被 sliver 抢占后仍写回 S.mode='menu'，窗口却是 sliver，
  //   右键 toggle 永久算成 sliver 致菜单再不弹——systematic debug 实锤）
  if (seq !== modeSeq) return;
  const interactive = (target === 'approval' || target === 'expanded' || target === 'menu');
  // 进入交互态必须取消历史收起定时器——此前审批结束排下的 2.5s 收起会在
  // 用户已手动展开面板后照常触发（"面板自己收起"潜伏 bug，批1 时序变化下
  // 由 toast 用例偶发暴露）
  if (interactive) cancelCollapse();
  try { window.pywebview?.api?.set_interactive?.(interactive); } catch (e) { /* 浏览器 */ }
  const from = S.mode;
  S.mode = target;
  if (growing) {
    island.classList.remove('shrinking');  // 打断收起编舞时防新 face 被隐藏规则压住
    stage.dataset.mode = target;        // 内容先渲染，窗口生长=揭幕（消黑板闪现）
    markEntering(from, target, true);
    render();
    if (target === 'expanded') {           // 内容已渲染：按实测高度一步到位
      exH = measureExpandedHeight() || exH;
      lastExpandedH = exH;
      stage.style.setProperty('--h-expanded', `${exH}px`);
    }
    // approval 的窗口高由 render()→applyApprovalHeight 实测后 resize，
    // 此处不再重复 pyResize（否则与实测值并发冲突、抖动）
    if (target !== 'approval') await pyResize(target, exH);
    if (seq !== modeSeq) return;
    requestAnimationFrame(() => playShared(sharedFrom));
  } else {
    // 收起编舞倒放（真机反馈#2）：dataset 暂不换——旧 face 先液态收拢
    // （CSS #island.shrinking .face），240ms 后换脸+窗口一步收。
    // 旧实现 t=0 即换脸致"瞬间收完"，窗口跳变前还挂着 320ms 黑板
    island.classList.add('shrinking');
    setTimeout(async () => {
      if (seq !== modeSeq) { island.classList.remove('shrinking'); return; }
      stage.dataset.mode = target;
      markEntering(from, target, false);
      render();
      island.classList.remove('shrinking');
      await pyResize(target);
      if (seq === modeSeq) requestAnimationFrame(() => playShared(sharedFrom));
    }, 480);
  }
  if (target === 'approval') {
    beep('alert');
    try { window.pywebview?.api?.surface_alert?.(); } catch (e) { /* 浏览器 */ }  // 审批/提问弹出：抬到置顶最前+闪烁兜底
  }
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
let pollInFlight = false;
async function poll() {
  if (pollInFlight) return;    // 桥悬挂时防轮询叠加（Kimi 抓）
  pollInFlight = true;
  try {
    const r = await fetch(`${BRIDGE}/api/state${S.rev ? `?since=${S.rev}` : ''}`,
      { cache: 'no-store', signal: AbortSignal.timeout(3000) });
    S.failCount = 0;
    S.online = true;
    if (r.status === 204) {
      // 内容未变（批D 增量协商）：跳过 ingest/render，只补时间敏感渲染
      if (S.mode === 'approval' && !S.resolving) renderApproval();
      return;
    }
    const data = await r.json();
    S.rev = data.rev || '';
    S.pending = data.pending || [];
    S.sessions = data.sessions || {};
    S.usage = data.usage || {};
    S.muted = !!data.muted;
    if (data.panel_alpha != null) S.panelAlpha = data.panel_alpha;
    // 透明度恢复（Owner：重载后回 100%）：旧实现只在值变化那一拍调 win API，
    // 若彼时 pywebview 未就绪调用静默丢失且永不重试。改'应用回执'制：
    // 每轮 poll 核对 appliedAlpha，未生效就重试直到 API 就绪
    if (S.panelAlpha != null && appliedAlpha !== S.panelAlpha
        && window.pywebview?.api?.set_panel_alpha) {
      try {
        window.pywebview.api.set_panel_alpha(S.panelAlpha);
        appliedAlpha = S.panelAlpha;
      } catch (e2) { /* 下轮重试 */ }
    }
    if (data.tex_skin && data.tex_skin !== S.texSkin) {
      S.texSkin = data.tex_skin;
      applyTexSkin();
    }
    if (S.night !== !!data.night) {
      S.night = !!data.night;
      document.body.classList.toggle('night', S.night);   // 夜息：岛也在休息
    }
    S.autoAllow = data.auto_allow_timeout || 0;
    S.yolo = new Set(data.yolo_sessions || []);
    if (data.lang && I18N[data.lang] && data.lang !== LANG) {
      LANG = data.lang;
      document.getElementById('foot-hint').textContent = T('footHint');
    }
    S.bridgeTs = data.ts || 0;
    for (const n of data.notify || []) {        // 勿扰也要记：这是状态不是提醒
      if (n.session_id && (n.notification_type === 'permission_prompt'
                           || n.notification_type === 'elicitation_dialog')) {
        S.termWait[n.session_id] = Math.max(S.termWait[n.session_id] || 0, n._arrived || S.bridgeTs || 0);
      }
    }
    (data.notify || []).forEach(showToast);
    handleUi(data.ui);
    handleShow(data.show);
  } catch (e) {
    if (++S.failCount >= 3) { S.online = false; }
  } finally {
    pollInFlight = false;
  }
  if (Date.now() < renderHold) return;   // 热键回执编舞期：本轮不渲染（编舞尾自会补）
  applyState();
  render();
}
let renderHold = 0;   // 热键回执渲染压制截止时刻
let appliedAlpha = null;   // 面板透明度已应用值（win 侧回执，未生效每轮重试）

/* 展示请求（scripts/show.sh → 桥 /api/show）：转发岛壳弹独立查看窗。
   seq 纪律与 handleUi 同源——首拉只对齐不回放，seq 回退=桥重启须向下重对齐 */
let lastShowSeq = null;
function handleShow(items) {
  if (!items) return;
  const maxSeq = Math.max(0, ...items.map(e => e.seq));
  if (lastShowSeq === null || maxSeq < lastShowSeq) {
    if (lastShowSeq !== null) clog(`show_seq realign ${lastShowSeq} -> ${maxSeq}`);
    lastShowSeq = maxSeq;
    return;
  }
  for (const ev of items) {
    if (ev.seq <= lastShowSeq) continue;
    lastShowSeq = ev.seq;
    window.pywebview.api.show_content(ev.kind, ev.win_path, ev.name || '', !!ev.raw);
  }
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
  // 桥重启 seq 归零而页面记着旧大值 → 后续事件全被吞（「右键弹不出」偶发根因，
  // 2026-08-15 实锤）。检测到 seq 回退即向下重对齐；本轮 deque 里的残留事件
  // 不回放（防幽灵动作重放，v3.2 已有前科），代价=重启后第一次点击可能丢、
  // 第二次必通，胜过永久失聪
  const maxSeq = Math.max(0, ...ui.events.map(e => e.seq));
  if (maxSeq < lastUiSeq) {
    clog(`ui_seq realign ${lastUiSeq} -> ${maxSeq} (bridge restarted)`);
    lastUiSeq = maxSeq;
    return;
  }
  for (const ev of ui.events) {
    if (ev.seq <= lastUiSeq) continue;
    lastUiSeq = ev.seq;
    if (ev.action === 'toggle') {
      setMode(S.mode === 'expanded' ? 'sliver' : 'expanded');
    } else if (ev.action === 'menu') {
      setMode('menu');   // 右键总是打开菜单（非 toggle，避免异步 S.mode 竞态卡死）；消散交给失焦/点项/点外
    } else if (ev.action === 'menu_dismiss') {
      if (S.mode === 'menu') setMode('sliver');   // 失焦：点击别处/桌面即收
    } else if (ev.action && ev.action.startsWith('hotkey:')) {
      // 全局热键的视觉回执（桥回推）。桥已直接决策，本轮 poll 的 pending
      // 已不含该条——若立即重渲染回执必被杀：压住渲染，按"回执 240ms →
      // 滑出 260ms"编舞播完再放行（与键盘直审同拍）
      flashKey(ev.action.slice(7));
      flashFx(ev.action.slice(7));
      if (S.mode === 'approval' && !S.resolving) {
        S.resolving = true;
        const face = document.querySelector('.face-approval');
        const hasNext = S.pending.length > 0;   // 桥已扣减，本地 poll 已同步
        if (hasNext) {
          renderHold = Date.now() + 240;        // 只压回执期
          setTimeout(() => {
            ghostExit(face);
            riseFromPeek();
            promoteNext = true;
            S.resolving = false;
            applyState(); render();
          }, 240);
        } else {
          renderHold = Date.now() + 520;
          setTimeout(() => face.classList.add('resolve'), 240);
          setTimeout(() => {
            face.classList.remove('resolve');
            S.resolving = false;
            applyState(); render();
          }, 520);
        }
      }
    }
  }
}

/* 底纹皮肤应用（N2 焦散弧=默认无属性 / N1 窗影 / N3 纯净） */
function applyTexSkin() {
  if (!S.texSkin || S.texSkin === 'N2') delete document.documentElement.dataset.tex;
  else document.documentElement.dataset.tex = S.texSkin;
}

/* ── 细条会话刻度（2026-09-26，Owner 选 A）：每个在线会话一格，颜色=状态
   （同状态胶囊：进行中=CLI 身份色 / 待回复=亮白 / 需要你=琥珀 / 空闲=暗）。
   顺序按首次出现固定，防格子随活跃度跳动；有待批卡却没有对应在线会话的
   （如外部助手的决策卡）补一格"需要你"。静态渲染，只有"需要你"按节拍闪两下（性能定律）── */
const tickOrder = new Map();   // 格子 key → 首次出现序号
let tickSeq = 0;
function sliverTickModel(sessions = S.sessions, ctx = {}) {
  const pending = ctx.pending ?? S.pending;
  if (tickOrder.size > 500) tickOrder.clear();
  const out = [];
  for (const a of Object.keys(sessions || {})) {
    for (const s of (sessions[a] || []).filter(x => x.is_live)) {
      const sid = String(s.session_id || `${a}:${s.title || s.cwd || ''}`);
      if (!tickOrder.has(sid)) tickOrder.set(sid, ++tickSeq);
      out.push({ sid, st: sessionState(s, ctx), c: agentColor(a), o: tickOrder.get(sid) });
    }
  }
  out.sort((x, y) => x.o - y.o);
  const live = new Set(out.map(t => t.sid));
  if (pending.some(p => !p.session_id || !live.has(String(p.session_id)))) {
    out.push({ sid: '_pending', st: 'need', c: 'var(--amber)', o: Infinity });
  }
  return out;
}
/* 刻度宽度随任务数变化（Owner 09-26 多次微调；平时最多并行 5~6 个）：
   1 个 80px、2 个 44px；3~8 个按刻度总长占细条宽度定档——3 个 50%、4 个 60%、5 个 70%、
   6 个 80%、7 个 85%、8 个 90%（几乎占满）；9 个起总长保持 90% 以内、每格变短，最短 5px。
   每格：3 个 35、4 个 31、5 个 28、6 个 27、7 个 24、8 个 22、10 个 17、20 个 7 */
const SLIVER_W = 220;          // 细条宽度，与 island.css --w-sliver 一致
const TICK_GAP = 3;            // 与 .sliver-ticks gap 一致
const TICK_SPAN = { 3: .50, 4: .60, 5: .70, 6: .80, 7: .85, 8: .90 };
function sliverTickWidth(n) {
  if (n <= 1) return 80;
  if (n === 2) return 44;
  const gaps = TICK_GAP * (n - 1);
  if (n <= 8) return Math.max(5, Math.round((SLIVER_W * TICK_SPAN[n] - gaps) / n));
  return Math.max(5, Math.floor((SLIVER_W * .90 - gaps) / n));   // 向下取整：总长不超 90%
}
function renderSliverTicks() {
  const face = document.querySelector('.face-sliver');
  if (!face) return;
  let box = document.getElementById('sliver-ticks');
  if (!box) {
    box = document.createElement('div');
    box.id = 'sliver-ticks';
    box.className = 'sliver-ticks';
    face.appendChild(box);
  }
  const ticks = sliverTickModel();
  const n = ticks.length;
  const tw = sliverTickWidth(n);
  const html = ticks.map((t, i) =>
    `<span class="tick ${t.st}" style="--c:${t.c};--i:${i}"></span>`).join('');
  if (html + tw === rendered.ticks) return;
  rendered.ticks = html + tw;
  box.style.setProperty('--tw', `${tw}px`);
  box.innerHTML = html;
  stage.classList.toggle('has-ticks', n > 0);
}

/* sliver 分段色条（梯队二#4）：按"工作中"agent 的会话数比例分段上色
   （2026-09-26 起只在没有在线会话时显示，平时由会话刻度接替） */
function updateSliverSegments() {
  const bar = document.querySelector('.sliver-bar');
  if (!bar) return;
  const seg = [];
  for (const a of agentKeys()) {
    const n = liveSessions(a).filter(x => statusKind(x.status) === 'active').length;
    if (n) seg.push([agentColor(a), n]);
  }
  let css = '';
  if (seg.length > 1) {
    const total = seg.reduce((t, [, n]) => t + n, 0);
    let acc = 0;
    const stops = seg.map(([c, n]) => {
      const from = (acc / total * 100).toFixed(0); acc += n;
      return `${c}66 ${from}% ${(acc / total * 100).toFixed(0)}%`;
    });
    css = `linear-gradient(90deg, ${stops.join(', ')})`;
  } else if (seg.length === 1) {
    css = `linear-gradient(180deg, ${seg[0][0]}29, ${seg[0][0]}4D)`;
  }
  if (css !== rendered.sliver) {
    rendered.sliver = css;
    if (css) bar.style.setProperty('--sliver-grad', css);
    else bar.style.removeProperty('--sliver-grad');
  }
}

let lastWorkingPush = -1;
function applyState() {
  stage.classList.toggle('offline', !S.online);
  stage.classList.toggle('has-pending', S.pending.length > 0);
  const working = countWorking();
  // 机器人三态：0=睡觉 / 1-2=悬浮舞 / >2=爆发（并发会话超 2 个）
  stage.classList.toggle('sleeping', working === 0);
  stage.classList.toggle('working', working > 0);
  stage.classList.toggle('super', working > 2);
  if (working !== lastWorkingPush) {
    lastWorkingPush = working;
    try { window.pywebview?.api?.set_working?.(working); } catch (e) { /* 浏览器模式 */ }
  }

  updateSliverSegments();
  renderSliverTicks();
  // snooze 剪枝 + 自动弹窗只看未搁置条目
  if (S.snoozed.size) {
    const alive = new Set(S.pending.map(p => p.id));
    for (const id of S.snoozed) if (!alive.has(id)) S.snoozed.delete(id);
  }
  const unsnoozed = S.pending.some(p => !S.snoozed.has(p.id));
  if (unsnoozed && !S.resolving) {
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
  const hasNext = S.mode === 'approval' && S.pending.length > 1;
  if (S.mode === 'approval' && !hasNext) face.classList.add('resolve');
  if (hasNext) { ghostExit(face); riseFromPeek(); }   // 幽灵退场+升板同屏
  try {
    await fetch(`${BRIDGE}/api/decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, decision, reason: reason || '' }),
    });
    S.decided++;
    beep(entry.kind === 'ask' ? 'ok'
      : decision === 'deny' ? 'deny' : decision === 'always' ? 'always' : 'ok');
    if (entry.kind !== 'ask') flashFx(decision);   // ask 作答走 deny 通道但语义中性
  } catch (e) { /* 桥不可达：条目留在队列，hook 35s 兜底 */ }
  S.pending = S.pending.filter(p => p.id !== id);
  if (hasNext) {
    // 立即换卡：新卡升起与幽灵退场同屏（连贯的物理堆叠）
    promoteNext = true;
    render();
    setTimeout(() => { S.resolving = false; applyState(); }, 220);
  } else {
    setTimeout(() => {
      face.classList.remove('resolve');
      S.resolving = false;
      applyState();
      render();
    }, 280);
  }
}

/* ── 键盘审批视觉回执（Owner：快捷键按没按上要看得见）────────────────
   flashEl=让对应按钮播放"被按下"：果冻按压 + 220ms 提亮脉冲。
   覆盖三条路：页内 A/D/S（decideFirst）、ask 数字选项、
   全局热键 Ctrl+Alt+A/D/S（桥 /api/hotkey 回推 hotkey:<action>，
   经轮询中继到达，延迟≤1s——迟到的回执也比没有强）。 */
/* 受光随鼠标（梯队三#7）：交互态里顶弧光中心随光标水平微移 ±6%
   （rAF 节流，pointermove 驱动=静止零帧；玻璃"感知"到手） */
let rimRaf = 0, rimX = 0;
document.addEventListener('pointermove', e => {
  if (S.mode === 'sliver' || S.mode === 'compact') return;
  rimX = e.clientX;
  if (rimRaf) return;
  rimRaf = requestAnimationFrame(() => {
    rimRaf = 0;
    const x = 50 + ((rimX / innerWidth) - .5) * 12;
    island.style.setProperty('--rim-x', `${x.toFixed(1)}%`);
  });
}, { passive: true });

/* 数字滚动（梯队三#6）：直赋值路径的数值滚动 */
function setTextRoll(el, text) {
  if (!el || el.textContent === text) return;
  el.textContent = text;
  if (typeof REDUCED !== 'undefined' && REDUCED) return;
  el.animate([{ transform: 'translateY(55%)', opacity: 0 },
              { transform: 'none', opacity: 1 }],
             { duration: 280, easing: 'cubic-bezier(.3,.9,.2,1)' });
}

/* 光反馈语言（梯队二#5）：一次性光效回执 */
function flashFx(decision) {
  const cls = decision === 'deny' ? 'fx-deny' : 'fx-approve';
  island.classList.remove('fx-deny', 'fx-approve');
  void island.offsetWidth;
  island.classList.add(cls);
  setTimeout(() => island.classList.remove(cls), 760);
}

function flashEl(el) {
  if (!el) return;
  el.classList.add('kbd-hit');
  window.__jellyPunch?.(el);
  setTimeout(() => el.classList.remove('kbd-hit'), 220);
}
function flashKey(decision) {
  const cls = { allow: '.btn-allow', deny: '.btn-deny', always: '.btn-always' }[decision];
  if (!cls) return;
  let el = null;
  if (S.mode === 'approval') {
    // 类查询通吃普通卡(#btn-*)与 plan 卡(#plan-*，同类名)；跳过隐藏钮
    el = [...document.querySelectorAll(`.face-approval ${cls}`)]
      .find(b => b.offsetParent !== null);
  } else if (S.mode === 'expanded') {
    el = document.querySelector(`.ex-pending .pend-card ${cls}`)
      || document.querySelector('.ex-pending .pend-card');
  }
  flashEl(el);
}

/* 键盘直审：先回执后决策（Owner：动效没播完面板就收了）——
   按下动效播 240ms 再 decide（decide 才触发 .resolve 滑出），
   kbdDecideLock 防延迟窗口内连按重复决策 */
let kbdDecideLock = false;
function decideFirst(decision) {
  if (!S.pending.length || kbdDecideLock || S.resolving) return;
  const id = S.pending[0].id;
  flashKey(decision);
  kbdDecideLock = true;
  setTimeout(() => { kbdDecideLock = false; decide(id, decision); }, 240);
}
window.islandHotkey = decideFirst;  // Python 全局热键入口

/* 岛上作答：deny+reason 通道把选择/输入传回模型（allow=回落终端 TUI） */
function answerAsk(text) {
  const e = S.pending[0];
  if (!e || e.kind !== 'ask') return;
  const q = askPayload(e)?.question || '';
  decide(e.id, 'deny',
    I18N[LANG].askAnswerMsg(q.slice(0, 120), text));
}

document.querySelector('.face-approval').addEventListener('click', ev => {
  const opt = ev.target.closest('.ask-opt');
  if (opt) {
    const e = S.pending[0];
    const o = askPayload(e)?.options[Number(opt.dataset.i)];
    if (o) answerAsk(I18N[LANG].choose(o.label || o));
    return;
  }
  if (ev.target.id === 'ask-send') {
    const v = document.getElementById('ask-input')?.value.trim();
    if (v) answerAsk(I18N[LANG].customInput(v));
    return;
  }
  if (ev.target.id === 'plan-approve') {
    decideFirst('allow');                  // 批准：回终端走正常确认流
    return;
  }
  if (ev.target.id === 'plan-reject') {
    const fb = document.getElementById('plan-feedback')?.value.trim();
    const e = S.pending[0];
    if (e) decide(e.id, 'deny',
      I18N[LANG].planRejectMsg(fb));
    return;
  }
  if (ev.target.id === 'plan-feedback') {
    try { window.pywebview?.api?.focus_input?.(); } catch (e2) { /* 浏览器 */ }
    setTimeout(() => document.getElementById('plan-feedback')?.focus(), 120);
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
    if (v) answerAsk(I18N[LANG].customInput(v));
  }
});

/* ── 会话状态：需要你 / 进行中 / 待回复 / 空闲（2026-09-26，Owner 定：标在
   标题旁的小胶囊，不加分组标题、不增高度；行序不变，防行在鼠标下跳动） ── */
const REPLY_WINDOW = 7200;   // 答完 2 小时内算"待回复"，再久沉为"空闲"
const ST_LABEL = { need: 'stNeed', active: 'stActive', reply: 'stReply', idle: 'stIdle' };
function loadSeen() {
  try {
    const d = JSON.parse(localStorage.getItem('island_seen') || '{}');
    const cut = Date.now() / 1000 - 7 * 86400;      // 七天前的已读记录丢掉
    return Object.fromEntries(Object.entries(d).filter(([, t]) => t > cut));
  } catch (e) { return {}; }
}
function markSeen(sid) {
  if (!sid) return;
  S.seen[sid] = S.bridgeTs || Date.now() / 1000;
  try { localStorage.setItem('island_seen', JSON.stringify(S.seen)); } catch (e) { /* 不可用则只存内存 */ }
}
/* ctx 可注入（测试用）；缺省取当前状态 */
function sessionState(s, ctx = {}) {
  const now = ctx.now ?? (S.bridgeTs || Date.now() / 1000);
  const pending = ctx.pending ?? S.pending, termWait = ctx.termWait ?? S.termWait;
  const seen = ctx.seen ?? S.seen;
  const sid = s.session_id, kind = statusKind(s.status);
  const lastWrite = now - (s.age_seconds || 0);
  if (sid && pending.some(p => p.session_id === sid)) return 'need';     // 岛上有它的待批卡
  if (kind === 'waiting') return 'need';
  if (sid && termWait[sid] && lastWrite <= termWait[sid] + 2) return 'need';  // 终端弹框后还没动过
  if (kind === 'active') return 'active';
  if (s.subagent) return 'idle';                                        // 子代理不等人回复
  if ((s.age_seconds ?? Infinity) <= REPLY_WINDOW && !(sid && seen[sid] >= lastWrite - 1)) return 'reply';
  return 'idle';
}
/* 胶囊实时活动（第 8 条方案 A）：显示最近在动的会话，其余计数；没有在动的就显示
   最近一个待回复的；都没有返回 html=null 由调用方回落原文案 */
function compactActivity(sessions = S.sessions, ctx = {}) {
  const all = [];
  for (const a of Object.keys(sessions || {})) {
    for (const s of (sessions[a] || []).filter(x => x.is_live)) all.push({ s, st: sessionState(s, ctx) });
  }
  const byAge = (x, y) => (x.s.age_seconds || 0) - (y.s.age_seconds || 0);
  const working = all.filter(x => x.st === 'active').sort(byAge);
  const replies = all.filter(x => x.st === 'reply').sort(byAge);
  const name = s => esc(s.title || s.slug || s.session_id);
  let html = null;
  if (working.length) {
    const s = working[0].s;
    const bits = [s.last_tool, fmtAge(s.age_seconds)].filter(Boolean).map(esc).join(' · ');
    html = `<span class="cap-title">${name(s)}</span><span class="dim">${bits ? ' · ' + bits : ''}`
      + `${working.length > 1 ? esc(I18N[LANG].moreWorking(working.length - 1)) : ''}</span>`;
  } else if (replies.length) {
    html = `<span class="cap-title">${name(replies[0].s)}</span><span class="dim"> · ${T('yourTurn')}</span>`;
  }
  return { html, replies: replies.length };
}

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
/* 列表副标题/通知的 Markdown 泄漏清洗（Kimi 视觉评审#2）：
   剥代码块/行内标记/链接语法，折叠空白 */
function stripMd(t) {
  return String(t || '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/[`*_#>~]/g, '')
    .replace(/\s+/g, ' ').trim();
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
    rendered.ctext = '';   // 破坏缓存：toast 过期后脏检查必重写（残留 bug，Kimi 抓）
    return;
  }
  let ctext;
  const act = S.online && totalLive ? compactActivity() : { html: null, replies: 0 };
  if (!S.online) {
    ctext = T('offline');
  } else if (totalLive === 0) {
    ctext = T('noLive');
  } else {
    ctext = act.html || `${totalLive} agents<span class="dim"> · ${working} working</span>`;
  }
  if (ctext !== rendered.ctext) {
    rendered.ctext = ctext;
    txt.innerHTML = ctext;
  }
  // 脏检查：每秒轮询重写 innerHTML 会重置 dot-breathe 动画相位（呼吸中断跳跃），
  // 内容没变就不动 DOM，动画相位才能连续
  const pendDot = S.pending.length
    ? `<span class="dot pend-dot" title="待审批 ${S.pending.length}"></span>` : '';
  // 显示活动时右侧只留待回复签（Owner 选定方案 A）；回落原文案时保留 agent 点
  const dotsHtml = act.html
    ? pendDot + (act.replies ? `<span class="cap-pill">${esc(I18N[LANG].replyCount(act.replies))}</span>` : '')
    : pendDot + agentKeys().map(a => {
    const live = liveSessions(a);
    if (!live.length) return '';
    const w = live.some(s => statusKind(s.status) === 'active');
    return `<span class="dot ${w ? 'working' : ''}" style="--c:${agentColor(a)}"></span>`;
  }).join('');
  if (dotsHtml !== rendered.dots) {
    rendered.dots = dotsHtml;
    document.getElementById('compact-dots').innerHTML = dotsHtml;
  }
}

/* AskUserQuestion 解析：单问题才走岛上作答（多问题罕见，回落普通审批） */
function askPayload(e) {
  if (e.kind !== 'ask') return null;
  const qs = e.tool_input?.questions;
  if (!Array.isArray(qs) || qs.length !== 1) return null;
  const q = qs[0];
  return { question: q.question || '', options: (q.options || []).slice(0, 6) };
}

function planPayload(e) {
  if (e.kind !== 'plan') return null;
  return { plan: String(e.tool_input?.plan || '').slice(0, 8000) };
}

/* 极简 Markdown 渲染（标题/粗体/行内码/代码块/列表/段落） */
function mdToHtml(md) {
  let h = esc(md);
  h = h.replace(/```([\s\S]*?)```/g, (m, c) => `<pre>${c}</pre>`);
  h = h.replace(/^### (.*)$/gm, '<h4>$1</h4>')
       .replace(/^## (.*)$/gm, '<h3>$1</h3>')
       .replace(/^# (.*)$/gm, '<h3>$1</h3>')
       .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
       .replace(/`([^`]+)`/g, '<code>$1</code>')
       .replace(/^[-*] (.*)$/gm, '<li>$1</li>')
       .replace(/^\d+\. (.*)$/gm, '<li>$1</li>');
  return h.split(/\n{2,}/).map(b =>
    /^<(h3|h4|li|pre)/.test(b.trim()) ? b.replace(/\n(?=<li)/g, '') : `<p>${b.replace(/\n/g, '<br>')}</p>`
  ).join('');
}

function approvalHeight() {
  // 进入 approval 前的初值估算（让 render 时 CSS 高合理，避免内部布局塌缩）；
  // 真实高度由 applyApprovalHeight() 在内容渲染后实测覆盖。
  const e = S.pending[0];
  if (!e) return 118;
  const ask = askPayload(e);
  if (ask) return Math.min(440, 150 + ask.options.length * 34);
  if (planPayload(e)) return 440;
  if (e.tool_name === 'Edit') return 200;
  return 134;                                // 普通审批：够 head+2行命令+按钮
}

/* 渲染后实测审批卡所需高度（offsetHeight 不受 face 入场 transform 影响，
   line-clamp/折行后的真实盒高都算得准）→ 窗口一次到位，根治多行命令被裁。*/
function measureApprovalHeight() {
  const face = document.querySelector('.face-approval');
  if (!face) return 134;
  const fcs = getComputedStyle(face);
  let h = parseFloat(fcs.paddingTop) + parseFloat(fcs.paddingBottom);
  for (const el of face.children) {
    if (el.hidden) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none') continue;
    // ap-detail 用 -webkit-box+line-clamp：offsetHeight 塌缩为 0、scrollHeight
    // 是未截断全文高 → 取大值后再按 line-clamp×行高 钳上限（=视觉截断后真实高）
    let bh = Math.max(el.offsetHeight, el.scrollHeight);
    const clamp = cs.getPropertyValue('-webkit-line-clamp');
    if (clamp && clamp !== 'none') {
      const lh = parseFloat(cs.lineHeight) || 18;
      bh = Math.min(bh, lh * (parseInt(clamp, 10) || 2) + 2);
    }
    // 滚动容器(ask-box 有 max-height):超长选项列表按 max-height 收口(盒内自滚)，
    // 不把窗口撑到全量未滚内容高。max(offset,scroll) 已保证窗口过大/过小时都取到内容真高。
    const maxH = parseFloat(cs.maxHeight);
    if (!isNaN(maxH) && maxH > 0) bh = Math.min(bh, maxH);
    h += bh + parseFloat(cs.marginTop) + parseFloat(cs.marginBottom);
  }
  return Math.max(96, Math.min(460, Math.ceil(h) + 2));
}

let lastApprovalH = 0;
function applyApprovalHeight() {
  if (S.mode !== 'approval') return;
  const h = measureApprovalHeight();
  if (Math.abs(h - lastApprovalH) < 6) return;   // 抖动阈值，避免无谓 resize
  lastApprovalH = h;
  stage.style.setProperty('--h-approval', `${h}px`);
  pyResize('approval', h);
}

let askRendered = '';
function renderApproval() {
  const e = S.pending[0];
  if (!e) return;
  const ask = askPayload(e);
  const plan = planPayload(e);
  const box = document.getElementById('ask-box');
  const actions = document.getElementById('ap-actions');
  const detail = document.getElementById('ap-detail');
  if (plan) {
    detail.style.display = 'none';
    actions.style.display = 'none';
    box.hidden = false;
    if (askRendered !== e.id) {
      askRendered = e.id;
      box.innerHTML = `<div class="plan-md">${mdToHtml(plan.plan)}</div>
        <div class="ask-input-row">
          <input class="ask-input" id="plan-feedback" placeholder="${T('planPlaceholder')}">
        </div>
        <div class="ap-actions plan-actions">
          <button class="btn btn-deny" id="plan-reject">${T('planReject')}<kbd>D</kbd></button>
          <button class="btn btn-allow" id="plan-approve">${T('planApprove')}<kbd>A</kbd></button>
        </div>`;
    }
    return;
  }
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
           <input class="ask-input" id="ask-input" placeholder="${T('askPlaceholder')}">
           <button class="ask-send" id="ask-send">${T('askSend')}</button>
         </div>
         <div class="ask-foot"><button class="ask-terminal" id="ask-terminal">${T('askTerminal')}</button></div>`;
    }
  } else {
    askRendered = '';
    box.hidden = true;
    detail.style.display = '';
    actions.style.display = '';
  }
  const agent = e.agent_source || 'claude';
  document.getElementById('ap-agent-dot').style.setProperty('--c', agentColor(agent));
  // 按来源 CLI 给 Allow 染身份色（CSS 从 --ap-c 派生全部色调）；
  // data-ap-agent 供 Always 环分流（Claude 橙 / 其余中性灰，Owner 裁决）
  const apFace = document.querySelector('.face-approval');
  apFace.style.setProperty('--ap-c', agentColor(agent));
  apFace.dataset.apAgent = agent;
  document.getElementById('ap-tool').textContent = e.tool_name || T('toolCall');
  document.getElementById('ap-proj').textContent =
    [e._remote ? `☁${e._remote}` : '', agentLabel(agent),
     e.title || e.project || e.session_slug].filter(Boolean).join(' · ');
  setTextRoll(document.getElementById('ap-queue'), S.pending.length > 1 ? `1 / ${S.pending.length}` : '');
  // perm 卡：终端框同步上岛，只给允许/拒绝，不自动放行
  const perm = e.kind === 'perm';
  document.getElementById('btn-always').style.display = perm ? 'none' : '';
  // 超时自动放行倒计时（仅普通工具审批；ask/plan/perm 永不自动批）
  const timer = document.getElementById('ap-timer');
  if (perm) {
    timer.textContent = T('permTag');
  } else if (S.autoAllow > 0 && !ask && !plan && e._arrived) {
    const left = Math.ceil(S.autoAllow - (S.bridgeTs - e._arrived));
    timer.textContent = left > 0 ? I18N[LANG].autoAllowIn(left) : '';
  } else {
    timer.textContent = '';
  }
  if (e.tool_name === 'Edit' && e.tool_input?.old_string !== undefined) {
    const cut = (t) => esc(String(t)).split('\n').slice(0, 5);
    detail.innerHTML =
      `<div class="diff-file">${esc(e.tool_input.file_path || '')}</div>` +
      cut(e.tool_input.old_string).map(l => `<div class="dl-del">- ${l}</div>`).join('') +
      cut(e.tool_input.new_string).map(l => `<div class="dl-add">+ ${l}</div>`).join('');
  } else {
    detail.textContent = entryDetail(e);
  }
  // 队列堆叠 peek（梯队二#3）
  const peek = document.getElementById('queue-peek');
  if (peek) {
    peek.hidden = S.pending.length <= 1;
    if (!peek.hidden) {
      peek.dataset.depth = String(Math.min(S.pending.length - 1, 2));
      // 内容残影用下一张卡的 agent 色（peek 是"真的下一张"，不是装饰条）
      peek.style.setProperty('--c', agentColor(S.pending[1]?.agent_source || 'claude'));
    }
  }
  // 换卡升起：仅在"处理完上一张、队列还有下一张"时播（promoteNext 由决策路径点火）
  if (promoteNext && S.mode === 'approval') {
    promoteNext = false;
    const face = document.querySelector('.face-approval');
    face.classList.remove('promote');
    void face.offsetWidth;
    face.classList.add('promote');
    setTimeout(() => face.classList.remove('promote'), 420);
  }
  applyApprovalHeight();   // 内容就位后实测高度，排队换条时也重测
}

let promoteNext = false;   // 队列换卡升起动画点火标志

/* 队列换卡 v4：升板——与 peek 同材质的玻璃板从 peek 原位向上生长
   （底边锚定顶边上行），旧卡幽灵同时上滑，内容延后淡入。
   升起的主体是"露出的那块玻璃"本身（Owner 四轮反馈定案叙事） */
function riseFromPeek() {
  if (typeof REDUCED !== 'undefined' && REDUCED) return;
  const peek = document.getElementById('queue-peek');
  const isl = island.getBoundingClientRect();
  let sT, sH = 10, sW = isl.width * .94, sL = isl.width * .03;
  if (peek && !peek.hidden) {
    const r = peek.getBoundingClientRect();
    sT = r.top - isl.top; sH = r.height; sW = r.width; sL = r.left - isl.left;
  } else {
    sT = isl.height - 22;
  }
  const riser = document.createElement('div');
  riser.className = 'ap-riser';
  island.insertBefore(riser, island.querySelector('.face'));   // 压在 faces 之下
  const eT = 5, eL = 4, eW = isl.width - 8, eH = isl.height - 11;
  riser.animate([
    { top: `${sT}px`, left: `${sL}px`, width: `${sW}px`, height: `${sH}px`, opacity: 1 },
    { top: `${eT}px`, left: `${eL}px`, width: `${eW}px`, height: `${eH}px`,
      opacity: 1, offset: .72 },
    { top: `${eT}px`, left: `${eL}px`, width: `${eW}px`, height: `${eH}px`, opacity: 0 },
  ], { duration: 540, easing: 'cubic-bezier(.3,.9,.2,1)' }).finished
    .then(() => riser.remove()).catch(() => riser.remove());
}

/* 队列换卡幽灵层：旧卡克隆上滑退场（不带 peek——栈不离场），
   与升板同屏重叠。300ms 一次性，自清理 */
function ghostExit(face) {
  if (typeof REDUCED !== 'undefined' && REDUCED) return;
  const ghost = face.cloneNode(true);
  ghost.classList.remove('face', 'face-approval', 'promote');
  ghost.classList.add('ap-ghost');
  ghost.querySelector('.queue-peek')?.remove();
  ghost.querySelectorAll('[id]').forEach(el => el.removeAttribute('id'));
  face.parentElement.appendChild(ghost);
  ghost.animate([
    { opacity: 1, transform: 'none' },
    { opacity: 0, transform: 'translateY(-18px) scale(.97)', filter: 'blur(3px)' },
  ], { duration: 300, easing: 'ease-in' }).finished
    .then(() => ghost.remove()).catch(() => ghost.remove());
}

/* 脏检查缓存：内容不变不触碰 DOM（防止轮询重渲染打断点击/hover） */
const rendered = { pend: '', body: '', ctext: '', dots: '', sliver: '', ticks: '' };

/* ═══ FLIP 列表基建（梯队一#2）═══════════════════════════════════════
   keyed 调和：节点按 data-k 复用——内容变化就地 morph（只更文本/属性，
   不重建节点=不重置动画相位，根治"每秒重建"老病根）；增删/换位用 WAAPI
   一次性动画（enter 升入 / exit 原位淡出 / 存活者 FLIP 平滑滑动），
   静止零常驻帧（性能定律）。 */
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;

function morphNode(oldEl, newEl) {
  if (oldEl.tagName !== newEl.tagName) { oldEl.replaceWith(newEl); return; }
  // 同步属性（class/style/title/data-*）
  for (const { name } of [...newEl.attributes]) {
    const v = newEl.getAttribute(name);
    if (oldEl.getAttribute(name) !== v) oldEl.setAttribute(name, v);
  }
  for (const { name } of [...oldEl.attributes]) {
    if (name !== 'data-k' && !newEl.hasAttribute(name)) oldEl.removeAttribute(name);
  }
  if (oldEl.hasAttribute('data-hold')) return;   // 子树由独立 reconcile 管理
  // 子节点按位对齐（模板结构固定）：文本只改 nodeValue，元素递归
  const oc = [...oldEl.childNodes], nc = [...newEl.childNodes];
  if (oc.length !== nc.length) { oldEl.innerHTML = newEl.innerHTML; return; }
  for (let i = 0; i < nc.length; i++) {
    const o = oc[i], n = nc[i];
    if (o.nodeType !== n.nodeType) { o.replaceWith(n.cloneNode(true)); continue; }
    if (o.nodeType === Node.TEXT_NODE) {
      if (o.nodeValue !== n.nodeValue) {
        o.nodeValue = n.nodeValue;
        const pe = o.parentElement;      // 数字滚动（梯队三#6）
        if (pe && pe.hasAttribute('data-roll') && !REDUCED) {
          pe.animate([{ transform: 'translateY(55%)', opacity: 0 },
                      { transform: 'none', opacity: 1 }],
                     { duration: 280, easing: 'cubic-bezier(.3,.9,.2,1)' });
        }
      }
    } else if (o.nodeType === Node.ELEMENT_NODE) {
      morphNode(o, n);
    }
  }
}

function reconcileFLIP(container, items) {
  const old = new Map([...container.children].map(el => [el.dataset.k, el]));
  const first = new Map();
  if (!REDUCED) for (const [k, el] of old) first.set(k, el.getBoundingClientRect());
  const keep = new Set(items.map(it => it.key));
  // 退场：原位绝对定位淡出（存活者由 FLIP 平滑补位）
  for (const [k, el] of old) {
    if (keep.has(k)) continue;
    old.delete(k);
    if (REDUCED) { el.remove(); continue; }
    const r = first.get(k), cr = container.getBoundingClientRect();
    Object.assign(el.style, { position: 'absolute', left: `${r.left - cr.left}px`,
      top: `${r.top - cr.top}px`, width: `${r.width}px`, margin: '0',
      pointerEvents: 'none' });
    el.animate([{ opacity: 1, transform: 'scale(1)' },
                { opacity: 0, transform: 'scale(.94)' }],
               { duration: 170, easing: 'ease-in' })
      .finished.then(() => el.remove()).catch(() => el.remove());
  }
  // 调和顺序 + 内容
  const frag = [];
  const tpl = document.createElement('div');
  for (const it of items) {
    let el = old.get(it.key);
    tpl.innerHTML = it.html;
    const fresh = tpl.firstElementChild;
    fresh.dataset.k = it.key;
    if (el) { morphNode(el, fresh); } else { el = fresh; el.dataset.entering = '1'; }
    frag.push(el);
  }
  // 按目标顺序就位（appendChild 移动既有节点不重建）
  for (const el of frag) container.appendChild(el);
  if (REDUCED) return;
  // Last-Invert-Play：存活者滑动 / 新入者升入
  for (const el of frag) {
    const k = el.dataset.k;
    if (el.dataset.entering) {
      delete el.dataset.entering;
      el.animate([{ opacity: 0, transform: 'translateY(-7px) scale(.96)' },
                  { opacity: 1, transform: 'none' }],
                 { duration: 260, easing: 'cubic-bezier(.3,.9,.2,1)' });
    } else if (first.has(k)) {
      const a = first.get(k), b = el.getBoundingClientRect();
      const dx = a.left - b.left, dy = a.top - b.top;
      if (Math.abs(dx) > .5 || Math.abs(dy) > .5) {
        el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'none' }],
                   { duration: 300, easing: 'cubic-bezier(.3,.9,.2,1)' });
      }
    }
  }
}

/* 距重置的短时长：10m / 2h14m / 2.6d */
function fmtLeft(sec) {
  if (sec < 3600) return `${Math.max(1, Math.ceil(sec / 60))}m`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h${String(Math.floor(sec % 3600 / 60)).padStart(2, '0')}m`;
  return `${(sec / 86400).toFixed(1)}d`;
}
/* resets_at：Claude 给秒级时间戳，Kimi 给 ISO 字符串；解析不出返回 0 */
function resetEpoch(v) {
  if (typeof v === 'number') return v > 1e12 ? v / 1000 : v;
  const t = Date.parse(String(v || ''));
  return Number.isFinite(t) ? t / 1000 : 0;
}

/* 按 agent 生成 5h/7d 用量条（数据源：官方 rate_limits）；有重置时刻的
   在百分比后跟短倒计时（↻2h14m），悬停看完整说明——不占额外高度 */
function usageBars(agent) {
  const u = S.usage?.[agent] ?? (agent === 'claude' ? S.usage : {});
  const now = S.bridgeTs || Date.now() / 1000;
  const bar = (label, w) => {
    if (!w || w.used_percentage == null) return '';
    const pct = Math.round(w.used_percentage);
    const warn = pct >= 80 ? ' warn' : '';
    const at = resetEpoch(w.resets_at), left = at - now;   // 已过期（缓存陈旧）不显示
    const reset = left > 0 ? `<span class="u-reset">↻${fmtLeft(left)}</span>` : '';
    const tip = left > 0 ? ` title="${esc(I18N[LANG].resetTip(label, pct, fmtLeft(left),
      new Date(at * 1000).toLocaleString(LANG === 'zh' ? 'zh-CN' : 'en-US',
        { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })))}"` : '';
    return `<span class="u-item${warn}"${tip}>${label}<span class="u-track"><span class="u-fill" style="width:${Math.min(100, pct)}%;--uc:${agentColor(agent)}"></span></span><span class="u-num" data-roll>${pct}%</span>${reset}</span>`;
  };
  return bar('5h', u?.five_hour) + bar('7d', u?.seven_day);
}

function renderExpanded() {
  const exPending = document.getElementById('ex-pending');
  const pendItems = S.pending.map(e => {
    const agent = e.agent_source || 'claude';
    return { key: String(e.id), html: `<div class="pend-card" data-id="${esc(e.id)}"
      style="--ap-c:${agentColor(agent)}">
      <span class="agent-dot" style="--c:${agentColor(agent)}"></span>
      <div class="pend-info">
        <div class="pend-tool">${esc(e.tool_name || T('toolCall'))}</div>
        <div class="pend-sub">${esc(entryDetail(e)).slice(0, 80)}</div>
      </div>
      <button class="btn btn-deny mini"  data-act="deny">Deny</button>
      <button class="btn btn-allow mini" data-act="allow">Allow</button>
    </div>` };
  });
  const pendSig = pendItems.map(i => i.html).join('');
  if (pendSig !== rendered.pend) {
    rendered.pend = pendSig;
    reconcileFLIP(exPending, pendItems);
  }

  let total = 0;
  const secItems = [];
  const rowsBySec = new Map();
  agentKeys().forEach(agent => {
    const live = liveSessions(agent);
    if (!live.length) return;
    total += live.length;
    const sorted = [...live].sort((a, b) => (a.subagent ? 1 : 0) - (b.subagent ? 1 : 0));
    const rows = sorted.map(s => {
      const kind = statusKind(s.status);
      const sub = s.subagent ? ' row-sub-agent' : '';
      const subBadge = s.subagent ? '<span class="sub-badge">↳ subagent</span>' : '';
      const st = sessionState(s);
      const pill = `<span class="st-pill ${st}"${st === 'reply'
        ? ` data-seen="${esc(s.session_id || '')}" title="${T('markRead')}"` : ''}>${T(ST_LABEL[st])}</span>`;
      // 副标题：完成→结果首句；工作中→最后一句指令（同目录多会话时项目·分支
      // 全是同一串，区分不了谁在干什么）；都没有才回落项目·分支
      const subText = (kind === 'idle' && s.recap)
        ? `✓ ${esc(stripMd(firstSentence(s.recap)))}`   // 只取首句：摘要常带表格/列表
        : (kind !== 'idle' && s.last_prompt)
          ? `› ${esc(s.last_prompt)}`
          : esc([s.project, s.git_branch].filter(Boolean).join(' · '));
      return `<div class="row${sub}" style="--c:${agentColor(agent)}" title="${T('jumpTitle')}"
        data-sid="${esc(s.session_id || '')}" data-agent="${agent}"
        data-title="${esc(s.title || '')}" data-cwd="${esc(s.cwd || '')}"
        data-remote="${esc(s.remote || '')}" data-remote-ssh="${esc(s.remote_ssh || '')}">
        <span class="st ${kind}"></span>
        <div class="row-main">
          <div class="row-title"><span class="rt-text">${esc(s.title || s.slug || s.session_id)}</span>${pill}${subBadge}${
            s.remote ? `<span class="remote-badge" title="SSH remote">☁ ${esc(s.remote)}</span>` : ''}</div>
          <div class="row-sub">${subText}</div>
        </div>
        <span class="row-meta">${s.last_tool ? esc(s.last_tool) + ' · ' : ''}${(typeof s.context_pct === 'number')
          ? ` <span class="ctx-pct${s.context_pct >= 85 ? ' warn' : ''}">ctx ${s.context_pct}%</span>` : ''} ${fmtAge(s.age_seconds)}</span>
        <button class="yolo-btn${S.yolo?.has(s.session_id) ? ' on' : ''}" data-yolo="${esc(s.session_id || '')}"
          title="${T('yoloTitle')}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M13 2 3 14h7l-1 8 11-12h-7l1-8z"/></svg></button>
      </div>`;
    });
    rowsBySec.set(agent, sorted.map((s, i) => ({
      key: String(s.session_id || `${agent}_${s.title || ''}_${s.cwd || ''}`),
      html: rows[i] })));
    secItems.push({ key: agent, html: `<div class="sec">
      <div class="sec-head" style="--c:${agentColor(agent)}">${agentLabel(agent)}
        <span class="cnt">${live.length}</span>
        <span class="sec-usage">${usageBars(agent)}</span></div>
      <div class="sec-rows" data-hold></div></div>` });
  });

  const exBody = document.getElementById('ex-body');
  const bodySig = total
    ? secItems.map(i => i.html).join('') + [...rowsBySec.values()].flat().map(i => i.html).join('')
    : `<div class="ex-empty">${T('emptyPanel')}</div>`;
  if (bodySig !== rendered.body) {
    rendered.body = bodySig;
    if (!total) {
      exBody.innerHTML = bodySig;
    } else {
      if (exBody.querySelector('.ex-empty')) exBody.innerHTML = '';
      reconcileFLIP(exBody, secItems);
      for (const [agent, rowItems] of rowsBySec) {
        const holder = exBody.querySelector(`.sec[data-k="${CSS.escape(agent)}"] .sec-rows`);
        if (holder) reconcileFLIP(holder, rowItems);
      }
    }
  }
  setTextRoll(document.getElementById('ex-stats'),
    I18N[LANG].stats(total, countWorking(), S.decided));
  const bs = document.getElementById('bridge-status');
  bs.textContent = (S.online ? '● bridge' : T('bridgeDown')) + (S.muted ? T('muteTag') : '');
  bs.className = `foot-link ${S.online ? 'ok' : 'down'}`;
  // 空面板兜底：没有任何运行实例时，顶部显示用量摘要（额度与实例存续无关，
  // 关完会话后"还剩多少额度"仍是开新会话的决策输入）
  const us = document.getElementById('usage-strip');
  // 数据驱动：任何 agent 只要桥给了 usage.<agent> 就自动出条，不写死名单
  // （原先写死 claude+codex，Kimi 接了官方额度端点后兜底摘要却不显示）
  const html = total === 0 ? agentKeys().map(usageBars).join('') : '';
  if (us.innerHTML !== html) us.innerHTML = html;
  applyExpandedHeight();   // 会话增减/待批卡进出后按内容量重设高度
}

/* ── 通知 toast ───────────────────────────────────────────────────── */
/* 回复首句：按中文句末标点/换行/英文句点+空格截断（保留句末标点） */
function firstSentence(t) {
  const s = String(t || '').trim();
  return (s.split(/(?<=[。！？!?])|\.\s|\n/)[0] || s).trim();
}

/* 通知分类（2026-09-26，9 天 504 条通知实测）：
   · idle_prompt = 答完 60 秒没人理，Claude Code 再提醒一次——与 Stop 是同一件事，不再响；
   · permission_prompt / elicitation_dialog = 终端在等你（岛没接住），醒目且不能打 ✓
     （⚠ 后加 U+FE0E 强制文本字形，防 Windows 回退成彩色 emoji）；
   · 其余按"完成"：会话名 · 回复首句（旧版只显示 CLI 名 + 固定文案，看不出是哪个会话）。
   返回 null = 不提示 */
function toastOf(n) {
  const nt = n.notification_type || '';
  if (nt === 'idle_prompt') return null;
  const agent = n.agent_source || 'claude';
  const who = String(n.title || '').slice(0, 16) || agentLabel(agent);
  if (nt === 'permission_prompt') {
    // 同会话的 perm 卡已上岛：终端框与岛卡是同一件事，不再重复提醒
    if (n.session_id && S.pending.some(p => p.kind === 'perm' && p.session_id === n.session_id)) return null;
    return { kind: 'alert', text: `⚠\uFE0E ${who} · ${T('needApprove')}` };
  }
  if (nt === 'elicitation_dialog') return { kind: 'alert', text: `⚠\uFE0E ${who} · ${T('needInput')}` };
  if (!n.hook_event_name && !n.agent_source) {       // 岛自己的提示（如重载确认）
    return { kind: 'done', text: stripMd(n.message || '') };
  }
  const last = n.last_assistant_message || n.message;
  return { kind: 'done', text: `✓ ${who} · ${last ? stripMd(firstSentence(last)) : T('doneRound')}` };
}

function showToast(n) {
  if (S.shownNotify.has(n.id)) return;
  S.shownNotify.add(n.id);
  if (S.shownNotify.size > 200) S.shownNotify.clear();
  if (S.muted) return;                       // 勿扰：通知不弹岛（审批不受影响）
  const t = toastOf(n);
  if (!t) return;
  beep(t.kind === 'alert' ? 'alert' : 'done');   // 去重在函数首行，不会重复响
  try { window.pywebview?.api?.surface_alert?.(); } catch (e) { /* 浏览器 */ }  // 抬到置顶最前+任务栏闪烁，防被其他窗口盖住
  const hold = t.kind === 'alert' ? 10000 : 6000;  // 等你处理的多停一会
  if (document.body.classList.contains('native')) {
    // Region 窗口无岛外空间：通知改为 compact 胶囊内联闪示
    S.toastMsg = { text: t.text.slice(0, 48), until: Date.now() + hold };
    if (S.mode === 'sliver') {
      setMode('compact');
      scheduleCollapse(hold + 500);
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
  el.innerHTML = `<span class="agent-dot" style="--c:${agentColor(agent)}"></span>
    <span class="t-msg">${esc(t.text)}</span>`;
  box.appendChild(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 320); }, 9000);
}

/* ── 声效（WebAudio 轻提示音） ────────────────────────────────────── */
let audioCtx = null;
function beep(kind) {
  if (S.muted) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') audioCtx.resume();
    // 语义化音色（梯队三#8）：[freq, at, dur, gain, 波形]
    // approve=清脆软tick / always=上行确认 / deny=低沉thud（三角波更钝）
    const seq = {
      alert:  [[740, 0, .22, .06, 'sine'], [988, .09, .22, .06, 'sine']],
      ok:     [[1320, 0, .07, .05, 'sine']],
      always: [[880, 0, .09, .05, 'sine'], [1175, .08, .13, .05, 'sine']],
      deny:   [[220, 0, .16, .07, 'triangle'], [165, .05, .2, .06, 'triangle']],
      done:   [[660, 0, .22, .06, 'sine'], [880, .08, .22, .06, 'sine']],
    }[kind] || [];
    seq.forEach(([freq, at, dur = .22, vol = .06, wave = 'sine']) => {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.type = wave; o.frequency.value = freq;
      g.gain.setValueAtTime(0.0001, audioCtx.currentTime + at);
      g.gain.exponentialRampToValueAtTime(vol, audioCtx.currentTime + at + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + at + dur);
      o.connect(g).connect(audioCtx.destination);
      o.start(audioCtx.currentTime + at); o.stop(audioCtx.currentTime + at + dur + .03);
    });
  } catch (e) { /* 无声环境忽略 */ }
}
// WebView2/Chromium autoplay 策略：AudioContext 须经用户手势才能出声。通知音是
// 轮询触发（无手势），若 audioCtx 从未被手势解锁，resume() 也无效→全程哑。
// 故首次任意手势时创建并 resume 解锁，之后 poll 驱动的 beep（含完成通知音）才有声。
function unlockAudio() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') audioCtx.resume();
  } catch (e) { /* 无声环境忽略 */ }
}
['pointerdown', 'keydown', 'mouseenter'].forEach(ev =>
  window.addEventListener(ev, unlockAudio, { passive: true }));

/* ── 交互 ─────────────────────────────────────────────────────────── */
island.addEventListener('mouseenter', () => {
  cancelCollapse();
  if (S.mode === 'sliver') setMode(S.pending.length ? 'approval' : 'compact');
});
function onLeave() {
  if (S.mode === 'compact') scheduleCollapse(1200);
  else if (S.mode === 'expanded') scheduleCollapse(2500);
  else if (S.mode === 'menu') scheduleCollapse(3500);   // 菜单：离开 3.5s 自动收
}
/* 生产环境窗口≈岛体，body/island 等价；浏览器自测视口更大，两者都挂 */
document.body.addEventListener('mouseleave', onLeave);
island.addEventListener('mouseleave', onLeave);
document.body.addEventListener('mouseenter', cancelCollapse);

/* 权威 hover 信号（Python 全局光标轮询推送）：原生窗口移动/缩放会让
   浏览器边界事件失灵，此通道兜底纠偏。浏览器自测模式无此调用。 */
/* 跳转结果提示（底栏临时换字 4s）：标题没匹配上时岛壳只聚焦了终端窗口
   （不再新开 claude --resume，防同一会话两个进程并发写），告诉用户去哪个标签页 */
let jumpHintTimer = 0;
function jumpHint(result, title) {
  const msg = result === 'terminal' ? I18N[LANG].jumpTab(String(title || '').slice(0, 16))
    : result === 'notfound' ? T('jumpNone') : '';
  if (!msg) return;
  const el = document.getElementById('foot-hint');
  el.textContent = msg;
  clearTimeout(jumpHintTimer);
  jumpHintTimer = setTimeout(() => { el.textContent = T('footHint'); }, 4000);
}

/* ── 托盘 HTML 玻璃菜单 ───────────────────────────────────────────── */
// 图标用全字体通用细线几何符号（避免 emoji 字体缺失显空框），与岛克制风统一
const MENU_ITEMS = [
  { key: 'toggle',    ico: '\u2630', t: 'menuExpand' },
  { key: 'mute',      ico: '\u2298', t: 'menuMute',  toggle: () => S.muted },
  { key: 'autoallow', ico: '\u25F7', t: 'menuAuto',  toggle: () => S.autoAllow > 0 },
  { key: 'autostart', ico: '\u2299', t: 'menuAutostart', toggle: () => !!S.autostart },
  { key: 'skin',      ico: '\u25D1', t: 'menuSkin' },   // 点击循环 焦散弧→窗影→纯净
  { key: 'alpha',     ico: '\u25A3', t: 'menuAlpha' },  // 点击循环 100→94→88%（透出桌面）
  { sep: true },
  { key: 'reload',    ico: '\u21BB', t: 'menuReload' },
  { key: 'quit',      ico: '\u2715', t: 'menuQuit', danger: true },
];
function menuHeight() {
  const items = MENU_ITEMS.filter(i => !i.sep).length;
  const seps = MENU_ITEMS.filter(i => i.sep).length;
  return 16 + items * 44 + seps * 11;       // padding 8*2 + 项高 + 分隔
}
function renderMenu() {
  document.getElementById('menu-list').innerHTML = MENU_ITEMS.map(it => {
    if (it.sep) return '<div class="menu-sep"></div>';
    const on = it.toggle && it.toggle();
    if (it.key === 'alpha') {
      const pct = Math.round((S.panelAlpha || 1) * 100);
      return `<div class="menu-item mi-slider" data-key="alpha">
        <span class="mi-ico">${it.ico}</span>
        <span class="mi-label">${T(it.t)}</span>
        <input type="range" id="alpha-slider" min="90" max="100" step="1" value="${pct}">
        <span class="mi-val" id="alpha-val">${pct}%</span>
      </div>`;
    }
    const label = it.key === 'skin'
      ? `${T(it.t)} · ${(T('skinNames') || {})[S.texSkin || 'N2'] || 'N2'}`
      : T(it.t);
    return `<div class="menu-item${on ? ' on' : ''}${it.danger ? ' danger' : ''}" data-key="${it.key}">
      <span class="mi-ico">${it.ico}</span>
      <span class="mi-label">${label}</span>
      ${it.toggle ? '<span class="mi-state"></span>' : ''}
    </div>`;
  }).join('');
  const sl = document.getElementById('alpha-slider');
  if (sl) {
    sl.addEventListener('input', () => {
      const v = parseInt(sl.value, 10) / 100;
      S.panelAlpha = v;
      document.getElementById('alpha-val').textContent = `${sl.value}%`;
      try { window.pywebview?.api?.set_panel_alpha?.(v); appliedAlpha = v; } catch (e2) { /* 浏览器 */ }
    });
    sl.addEventListener('change', () => {
      try {
        fetch(`${BRIDGE}/api/settings`, { method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ panel_alpha: S.panelAlpha }) });
      } catch (e2) { /* 下轮 poll 拉回 */ }
    });
  }
}
document.getElementById('menu-list').addEventListener('click', async e => {
  const it = e.target.closest('.menu-item');
  if (!it) return;
  const key = it.dataset.key;
  if (key === 'toggle') { setMode('expanded'); return; }
  if (key === 'reload') {
    try { localStorage.setItem('island_reloaded', '1'); } catch (e2) {}   // 重载后启动时冒确认 toast
    try { window.pywebview?.api?.tray_action?.('reload'); } catch (e2) {}
    return;
  }
  if (key === 'quit')   { try { window.pywebview?.api?.tray_action?.('quit'); } catch (e2) {} return; }
  if (key === 'alpha') return;   // 拉杆自理，行点击不响应
  if (key === 'skin') {
    // 循环切换皮肤并持久化；菜单保持打开便于连点对比
    const order = ['N2', 'N1', 'N3', 'N4', 'N5'];
    const next = order[(order.indexOf(S.texSkin || 'N2') + 1) % order.length];
    S.texSkin = next;
    applyTexSkin();
    renderMenu();
    try {
      fetch(`${BRIDGE}/api/settings`, { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tex_skin: next }) });
    } catch (e2) { /* 桥暂不可达：下轮 poll 会拉回旧值 */ }
    return;
  }
  if (key === 'autostart') {
    try { S.autostart = await window.pywebview?.api?.set_autostart?.(!S.autostart); } catch (e2) {}
    renderMenu();                            // 刷新开关态，不收起（即时看到亮点）
    return;
  }
  if (key === 'mute') {
    fetch(`${BRIDGE}/api/mute`, { method: 'POST', body: '{}' }).catch(() => {});
  } else if (key === 'autoallow') {
    const next = S.autoAllow > 0 ? 0 : 25;
    fetch(`${BRIDGE}/api/settings`, { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ auto_allow_timeout: next }) }).catch(() => {});
  }
  setMode('sliver');                        // 开关类点完即收
});

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
document.getElementById('ex-body').addEventListener('dblclick', e => {
  const row = e.target.closest('.row');
  if (!row) return;
  markSeen(row.dataset.sid);                 // 跳过去看了 = 已读
  try {
    Promise.resolve(window.pywebview?.api?.jump_to?.({
      session_id: row.dataset.sid, agent: row.dataset.agent,
      title: row.dataset.title, cwd: row.dataset.cwd,
      remote: row.dataset.remote, remote_ssh: row.dataset.remoteSsh,
    })).then(r => jumpHint(r, row.dataset.title)).catch(() => {});
    clog(`jump_to ${row.dataset.agent}:${row.dataset.title?.slice(0, 16)}`);
  } catch (e2) { /* 浏览器模式 */ }
});
document.getElementById('ex-body').addEventListener('click', e => {
  const seen = e.target.closest('.st-pill[data-seen]');
  if (seen) { e.stopPropagation(); markSeen(seen.dataset.seen); render(); return; }
  const btn = e.target.closest('.yolo-btn');
  if (!btn) return;
  e.stopPropagation();
  const sid = btn.dataset.yolo;
  if (!sid) return;
  const on = !btn.classList.contains('on');
  btn.classList.toggle('on', on);            // 乐观更新，下轮 poll 校准
  fetch(`${BRIDGE}/api/session_yolo`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sid, on }),
  }).catch(() => {});
  clog(`yolo ${on ? 'on' : 'off'} ${sid.slice(0, 12)}`);
});
document.getElementById('ex-pending').addEventListener('click', e => {
  const btn = e.target.closest('.mini');
  if (!btn) return;
  decide(btn.closest('.pend-card').dataset.id, btn.dataset.act);
});

window.addEventListener('keydown', e => {
  const k = e.key.toLowerCase();
  if (k === 'escape') {
    // 稍后处理（Kimi 指出的交互死路）：Esc 搁置当前队列，不再被自动弹回；
    // 红条继续脉动提示，新审批到达自然再弹，展开面板随时可手动处理
    if (S.mode === 'approval') S.pending.forEach(p => S.snoozed.add(p.id));
    setMode('sliver');
    return;
  }
  if (S.pending.length && (S.mode === 'approval' || S.mode === 'expanded')) {
    const ask = askPayload(S.pending[0]);
    if (ask && document.activeElement?.id !== 'ask-input') {
      const n = parseInt(e.key, 10);
      if (n >= 1 && n <= ask.options.length) {
        if (kbdDecideLock) return;
        const o = ask.options[n - 1];
        flashEl(document.querySelectorAll('.ask-opt')[n - 1]);   // 数字键回执
        kbdDecideLock = true;                                    // 回执播完再作答
        setTimeout(() => { kbdDecideLock = false;
                           answerAsk(I18N[LANG].choose(o.label || o)); }, 240);
        return;
      }
    }
    if (['ask-input', 'plan-feedback'].includes(document.activeElement?.id)) return;  // 输入框内不抢键
    const plan = planPayload(S.pending[0]);
    if (plan && k === 'd') {
      if (kbdDecideLock) return;
      const e2 = S.pending[0];
      flashKey('deny');                                          // plan 驳回回执
      kbdDecideLock = true;
      setTimeout(() => { kbdDecideLock = false;
                         decide(e2.id, 'deny', I18N[LANG].planRejectMsg('')); }, 240);
      return;
    }
    if (k === 'a') decideFirst('allow');
    else if (k === 'd') decideFirst('deny');
    else if (k === 's' && S.pending[0]?.kind !== 'perm') decideFirst('always');   // perm 卡无 Always
  }
});

/* Playwright / 调试探针 */
window.__island = {
  get mode() { return S.mode; },
  get state() { return S; },
  get permTag() { return T('permTag'); },
  setMode,
  toastOf,
  sessionState,
  compactActivity,
  jumpHint,
  usageBars,
  fmtLeft,
  measureExpandedHeight,
  sliverTickModel,
  sliverTickWidth,
  sliverPulse,
};

/* ── 启动 ─────────────────────────────────────────────────────────── */
if (window.pywebview) document.body.classList.add('native');
window.addEventListener('pywebviewready', () => document.body.classList.add('native'));
stage.dataset.mode = S.mode;
document.getElementById('foot-hint').textContent = T('footHint');
clog(`boot ua=${navigator.userAgent.slice(-40)} pywebview=${typeof window.pywebview}`);
document.addEventListener('visibilitychange',
  () => clog(`visibility=${document.visibilityState}`));
window.addEventListener('pywebviewready', () => clog('pywebviewready'));
/* 重载反馈：菜单点「重载页面」前置 localStorage 标记，重载后启动时冒一条确认 toast */
try {
  if (localStorage.getItem('island_reloaded')) {
    localStorage.removeItem('island_reloaded');
    setTimeout(() => showToast({ id: 'reloaded_' + lastUiSeq, message: T('reloaded') }), 1000);
  }
} catch (e) { /* localStorage 不可用忽略 */ }
poll();
setInterval(poll, POLL_MS);

/* sliver 微光节拍器：每 9s 触发一次 1.2s 单次扫光（占空比降耗——
   无限 CSS 动画在置顶分层窗上恒吃 ~50% 核，动画只在播放窗口内存在；
   实测：无限循环 91%→占空比 6s 27%→9s ≈15% 单核） */
let sliverTick = 0;
setInterval(() => {
  if (S.mode !== 'sliver' || S.pending.length) return;
  if (S.night && (sliverTick++ % 2)) return;   // 夜息：掠光隔拍（18s 一次）
  const bar = stage.classList.contains('has-ticks')
    ? document.getElementById('sliver-ticks') : document.querySelector('.sliver-bar');
  if (!bar) return;
  bar.classList.remove('sweep');
  void bar.offsetWidth;          // 重排刷新，确保单次动画可重触发
  bar.classList.add('sweep');
  setTimeout(() => bar.classList.remove('sweep'), 2200);   // 刻度逐格错峰，留足时长
}, 9000);

/* 细条关注节拍（每 4s；占空比，不留常驻动画——性能定律）：
   "需要你"琥珀格闪两下；"待回复"白格呼吸一次（Owner 09-26：静态白格不够醒目；
   1.6s、12 帧步进，重绘次数远少于 60fps 动画） */
function sliverPulse() {
  if (S.mode !== 'sliver') return;
  const box = document.getElementById('sliver-ticks');
  if (!box) return;
  for (const [sel, cls, ms] of [['.tick.need', 'blink', 1300], ['.tick.reply', 'breathe', 1700]]) {
    if (!box.querySelector(sel)) continue;
    box.classList.remove(cls);
    void box.offsetWidth;
    box.classList.add(cls);
    setTimeout(() => box.classList.remove(cls), ms);
  }
}
setInterval(sliverPulse, 4000);

/* ── 真弹簧曲线注入：linear() 欠阻尼弹簧解析解采样。
   不可用（老 WebView2）时静默回落 CSS 里的 cubic-bezier 兜底（RISKS E1）。 */
(function initSprings() {
  try {
    if (!CSS.supports('animation-timing-function', 'linear(0, 1)')) return;
    const spring = (zeta, dur, steps = 24) => {
      const w0 = 2 * Math.PI / (dur / 1000) * 1.35;      // 经验频率：dur 内基本收敛
      const wd = w0 * Math.sqrt(1 - zeta * zeta);
      const pts = [];
      for (let i = 0; i <= steps; i++) {
        const t = (i / steps) * (dur / 1000);
        const x = 1 - Math.exp(-zeta * w0 * t) *
                  (Math.cos(wd * t) + (zeta * w0 / wd) * Math.sin(wd * t));
        pts.push(`${Math.round(x * 1000) / 1000} ${Math.round((i / steps) * 1000) / 10}%`);
      }
      return `linear(${pts.join(',')})`;
    };
    document.documentElement.style.setProperty('--spring-settle', spring(.85, 450));
  } catch (e) { /* 兜底曲线已在 CSS */ }
})();

/* ── 果冻交互：欠阻尼弹簧积分器（LG-Lite glass 手感：硬而收敛）。
   事件委托（按钮随 innerHTML 重建，绑实例必失联）；rAF 仅交互期存在，
   全部静止即停帧（性能定律：待机零常驻动画）。只写 inline transform，
   与渲染层脏检查无交集。系统减动效则整体不启用。 */
window.__jellyPunch = () => {};   // reduced-motion 兜底 no-op
(function jellyInit() {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const SEL = '.btn, .mini, .ask-opt, .ask-send';
  const st = new WeakMap();
  const live = new Set();
  let raf = 0, last = 0;
  const FREQ = 14, ZETA = 0.6, K = FREQ * FREQ, C = 2 * ZETA * FREQ;
  function tick(now) {
    const dt = Math.min((now - last) / 1000, 1 / 30); last = now;
    for (const el of live) {
      const s = st.get(el);
      if (!s || !el.isConnected) { live.delete(el); continue; }
      s.vx += (K * (s.tx - s.sx) - C * s.vx) * dt; s.sx += s.vx * dt;
      s.vy += (K * (s.ty - s.sy) - C * s.vy) * dt; s.sy += s.vy * dt;
      if (Math.abs(s.sx - s.tx) < .001 && Math.abs(s.vx) < .004 &&
          Math.abs(s.sy - s.ty) < .001 && Math.abs(s.vy) < .004) {
        s.sx = s.tx; s.sy = s.ty;
        el.style.transform = (s.tx === 1 && s.ty === 1) ? '' : `scale(${s.tx},${s.ty})`;
        if (s.tx === 1 && s.ty === 1) live.delete(el);
      } else {
        el.style.transform = `scale(${s.sx.toFixed(4)},${s.sy.toFixed(4)})`;
      }
    }
    raf = live.size ? requestAnimationFrame(tick) : 0;   // 静止零帧
  }
  function springTo(el, tx, ty) {
    let s = st.get(el);
    if (!s) { s = { sx: 1, sy: 1, vx: 0, vy: 0, tx: 1, ty: 1 }; st.set(el, s); }
    s.tx = tx; s.ty = ty; live.add(el);
    if (!raf) { last = performance.now(); raf = requestAnimationFrame(tick); }
  }
  const hit = e => e.target.closest?.(SEL);
  document.addEventListener('pointerdown', e => {
    const el = hit(e); if (el) springTo(el, 1.045, .94);
  }, true);
  document.addEventListener('pointerup', e => {
    const el = hit(e);
    if (el) { const hov = el.matches(':hover') ? 1.02 : 1; springTo(el, hov, hov); }
  }, true);
  document.addEventListener('pointerover', e => {
    const el = hit(e); if (el && !e.buttons) springTo(el, 1.02, 1.02);
  }, true);
  document.addEventListener('pointerout', e => {
    const el = hit(e); if (el) springTo(el, 1, 1);
  }, true);
  /* 键盘审批视觉回执用：程序化"按下-回弹"一次 */
  window.__jellyPunch = el => {
    springTo(el, 1.045, .94);
    setTimeout(() => springTo(el, 1, 1), 130);
  };
})();
