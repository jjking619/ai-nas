const appRoot = document.querySelector('.app');
const appVersion = (appRoot && appRoot.dataset.uiVersion) || '';
const voiceBtn = document.getElementById('voiceBtn');
const textBtn = document.getElementById('textBtn');
const textInput = document.getElementById('textInput');
const statusEl = document.getElementById('status');
const turnsEl = document.getElementById('turns');
const clearTurnsBtn = document.getElementById('clearTurns');
let _taskBusy = false;
let _liveTurnEl = null;

if (statusEl) {
  statusEl.style.display = 'none';
}

function _ensureLiveTurn() {
  if (!turnsEl) return null;
  if (_liveTurnEl && _liveTurnEl.isConnected) return _liveTurnEl;
  const div = document.createElement('div');
  div.className = 'turn turn-live';
  div.innerHTML = '<div class="turn-src">🧾 实时状态</div><div class="turn-txt"></div>';
  turnsEl.prepend(div);
  _liveTurnEl = div;
  return div;
}

function setStatusText(text) {
  const live = _ensureLiveTurn();
  if (!live) return;
  const textEl = live.querySelector('.turn-txt');
  if (textEl) textEl.textContent = text;
}

window.__voiceSetStatus = setStatusText;

function apiUrl(path) {
  const sep = path.includes('?') ? '&' : '?';
  return path + sep + 'v=' + encodeURIComponent(appVersion);
}

function setBusy(busy) {
  _taskBusy = busy;
  voiceBtn.disabled = busy;
  voiceBtn.textContent = busy ? '🎙 正在录音...' : '开始语音指令';
  textBtn.disabled = busy;
  textInput.disabled = busy;
}

async function pollTask(taskId) {
  for (;;) {
    const resp = await fetch(apiUrl('/api/task/' + encodeURIComponent(taskId)), { cache: 'no-store' });
    const data = await resp.json();
    if (!resp.ok) {
      setStatusText('任务查询失败：' + (data.error || resp.statusText));
      setBusy(false);
      return;
    }
    const task = data.task;
    if (task.status === 'error') {
      setStatusText('处理失败：' + (task.error || '未知错误'));
      setBusy(false);
      return;
    }
    if (task.status === 'done') {
      setBusy(false);
      return;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 1200));
  }
}

async function submitTask(payload, modeHint) {
  setBusy(true);
  setStatusText(modeHint + ' 请求已提交，正在排队...');
  try {
    const resp = await fetch(apiUrl('/api/trigger'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      cache: 'no-store'
    });
    const data = await resp.json();
    if (!(resp.status === 202 && data.task_id)) {
      setStatusText('触发失败：' + (data.error || resp.statusText));
      setBusy(false);
      return;
    }
    setStatusText('任务已创建：' + data.task_id + '\n开始执行...');
    await pollTask(data.task_id);
  } catch (err) {
    setStatusText('请求失败：' + err);
    setBusy(false);
  }
}

async function triggerVoice() {
  await submitTask({}, '语音模式');
}

async function triggerText() {
  const text = textInput.value.trim();
  if (!text) {
    setStatusText('请输入文本指令。');
    return;
  }
  await submitTask({ text }, '文本模式');
}

voiceBtn.addEventListener('click', triggerVoice);
textBtn.addEventListener('click', triggerText);
textInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') triggerText();
});

// ── 对话历史时间线 ──────────────────────────────────────────────
let lastTurnTs = Date.now() / 1000 - 3600; // 初始加载最近1小时的记录

const srcLabels = { wake: '🎤 唤醒', button: '🖱 按钮', text: '⌨ 文本' };

function renderNewTurns(turns) {
  if (!turns || !turns.length) return;
  turns.forEach(t => {
    const div = document.createElement('div');
    div.className = 'turn';
    const src = srcLabels[t.source] || t.source;
    const hhmm = new Date(t.ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    div.innerHTML =
      `<div class="turn-src">${src} · ${hhmm}</div>` +
      (t.text ? `<div class="turn-txt">🗣 ${t.text}</div>` : '') +
      (t.reply ? `<div class="turn-rep">💬 ${t.reply}</div>` : '') +
      `<div class="turn-meta">耗时 ${t.cost_ms} ms</div>`;
    turnsEl.appendChild(div);
    if (t.ts > lastTurnTs) lastTurnTs = t.ts;
  });
  turnsEl.scrollTop = turnsEl.scrollHeight;
}

async function fetchTurns() {
  if (document.hidden) return;
  try {
    const resp = await fetch(apiUrl('/api/turns?since=' + lastTurnTs), { cache: 'no-store' });
    if (!resp.ok) return;
    const data = await resp.json();
    renderNewTurns(data.turns);
  } catch (_) {}
}

if (clearTurnsBtn) {
  clearTurnsBtn.addEventListener('click', () => {
    turnsEl.innerHTML = '';
    _liveTurnEl = null;
    lastTurnTs = Date.now() / 1000;
    setStatusText('已清空历史，待机中。');
  });
}

setInterval(fetchTurns, 1500);
fetchTurns();
setStatusText('待机中。请选择语音或文本模式。');

// ── 语音桥状态轮询 ──────────────────────────────────────────────
const STATE_LABELS = {
  awake: '🎙 已唤醒，请说话...',
  listening: '🎙 正在聆听...',
  asr: '🔍 正在识别...',
  processing: '⚙️ 正在处理...',
  speaking: '🔊 正在播报...',
};

let _lastBridgeState = 'idle';

function _applyBridgeState(state, busy) {
  // 按钮/文本框是否可用统一由桥状态决定；_taskBusy 仅覆盖提交后的短暂空档
  const active = _taskBusy || busy || (state !== 'idle');
  voiceBtn.disabled = active;
  voiceBtn.textContent = STATE_LABELS[state] || (active ? '正在录音...' : '开始语音指令');
  textBtn.disabled = active;
  textInput.disabled = active;
  if (state !== 'idle') {
    setStatusText(STATE_LABELS[state] || ('对话助手状态：' + state));
  } else if (_lastBridgeState !== 'idle') {
    setStatusText('待机中。请选择语音或文本模式。');
  }
  _lastBridgeState = state;
}

async function pollBridgeStatus() {
  if (document.hidden) return;
  try {
    const resp = await fetch(apiUrl('/api/status'), { cache: 'no-store' });
    if (!resp.ok) return;
    const data = await resp.json();
    _applyBridgeState(data.state || 'idle', !!data.busy);
  } catch (_) {}
}

setInterval(pollBridgeStatus, 1500);
pollBridgeStatus();

document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  fetchTurns();
  pollBridgeStatus();
});

window.__voiceUiLoaded = true;