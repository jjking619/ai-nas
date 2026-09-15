const appRoot = document.querySelector('.app');
const appVersion = (appRoot && appRoot.dataset.uiVersion) || '';
const voiceBtn = document.getElementById('voiceBtn');
const textBtn = document.getElementById('textBtn');
const textInput = document.getElementById('textInput');
const statusEl = document.getElementById('status');
const turnsEl = document.getElementById('turns');
const clearTurnsBtn = document.getElementById('clearTurns');
const langToggle = document.getElementById('langToggle');
let _taskBusy = false;
let _liveTurnEl = null;

const tr = (key, fallback = '') => {
  if (window.voiceI18n && typeof window.voiceI18n.t === 'function') {
    return window.voiceI18n.t(key, fallback);
  }
  return fallback || key;
};

if (statusEl) {
  statusEl.style.display = 'none';
}

function _ensureLiveTurn() {
  if (!turnsEl) return null;
  if (_liveTurnEl && _liveTurnEl.isConnected) return _liveTurnEl;
  const div = document.createElement('div');
  div.className = 'turn turn-live';
  div.innerHTML = `<div class="turn-src">🧾 ${tr('realTimeStatus')}</div><div class="turn-txt"></div>`;
  turnsEl.prepend(div);
  _liveTurnEl = div;
  return div;
}

function setStatusText(text) {
  const live = _ensureLiveTurn();
  if (!live) return;
  const textEl = live.querySelector('.turn-txt');
  if (textEl) {
    const display = localizeDisplayText(text || '');
    textEl.textContent = display;
    textEl.dataset.rawText = text || '';
  }
  if (statusEl) {
    statusEl.textContent = localizeDisplayText(text || '');
  }
}

window.__voiceSetStatus = setStatusText;

let _statusRenderer = null;

function setStatus(build) {
  _statusRenderer = build;
  setStatusText(build());
}

function apiUrl(path) {
  const sep = path.includes('?') ? '&' : '?';
  return path + sep + 'v=' + encodeURIComponent(appVersion);
}

function setBusy(busy) {
  _taskBusy = busy;
  if (voiceBtn) {
    voiceBtn.disabled = busy;
    voiceBtn.textContent = busy ? tr('voiceBtnBusy') : tr('voiceBtnStart');
  }
  if (textBtn) {
    textBtn.disabled = busy;
  }
  if (textInput) {
    textInput.disabled = busy;
  }
}

async function pollTask(taskId) {
  for (;;) {
    const resp = await fetch(apiUrl('/api/task/' + encodeURIComponent(taskId)), { cache: 'no-store' });
    const data = await resp.json();
    if (!resp.ok) {
      setStatus(() => `${tr('taskQueryFailed')}${data.error || resp.statusText}`);
      setBusy(false);
      return;
    }
    const task = data.task;
    if (task.status === 'error') {
      setStatus(() => `${tr('processingFailed')}${task.error || tr('unknownError')}`);
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

async function submitTask(payload, modeKey) {
  setBusy(true);
  setStatus(() => `${tr(modeKey)} ${tr('taskQueued')}`);
  try {
    const resp = await fetch(apiUrl('/api/trigger'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      cache: 'no-store'
    });
    const data = await resp.json();
    if (!(resp.status === 202 && data.task_id)) {
      setStatus(() => `${tr('triggerFailed')}${data.error || resp.statusText}`);
      setBusy(false);
      return;
    }
    setStatus(() => `${tr('taskCreated')}${data.task_id}\n${tr('taskStart')}`);
    await pollTask(data.task_id);
  } catch (err) {
    setStatus(() => `${tr('requestFailed')}${err}`);
    setBusy(false);
  }
}

async function triggerVoice() {
  await submitTask({}, 'voiceMode');
}

async function triggerText() {
  const text = textInput ? textInput.value.trim() : '';
  if (!text) {
    setStatus(() => tr('pleaseInputText'));
    return;
  }
  await submitTask({ text }, 'textMode');
}

if (voiceBtn) {
  voiceBtn.addEventListener('click', triggerVoice);
}
if (textBtn) {
  textBtn.addEventListener('click', triggerText);
}
if (textInput) {
  textInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') triggerText();
  });
}

let lastTurnTs = Date.now() / 1000 - 3600;
const srcLabels = () => ({
  wake: tr('sourceWake'),
  button: tr('sourceButton'),
  text: tr('sourceText'),
});

function localizeDisplayText(text) {
  if (typeof text !== 'string' || !text.trim()) return text;
  if (!window.voiceI18n || typeof window.voiceI18n.getLang !== 'function') return text;
  if (window.voiceI18n.getLang() !== 'en') return text;

  let value = text;
  const categoryMap = {
    风景: 'scenery',
    美食: 'food',
    人物: 'people',
    动物: 'animals',
    建筑: 'architecture',
    交通工具: 'transportation',
    日常用品: 'daily items',
    植物: 'plants',
    家庭相册: 'Family album',
    手机相册: 'Phone album',
    旅行: 'Trip',
    备份: 'Backup',
  };

  const naturalSummaryPattern = /^(.*?)(?:\s*[:：]\s*)?((?:[^,]+,\s*)+[^,]+)$/;

  const replacements = [
    ['正在播放：', 'Playing: '],
    ['下载已完成，已保存到', 'Download complete. Saved to '],
    ['，文件名', ', file name '],
    ['文件名', 'file name '],
    ['家庭相册里有这些文件：', 'Family album contains these files: '],
    ['手机相册里有这些文件：', 'Phone album contains these files: '],
    ['旅行里有这些文件：', 'Trip folder contains these files: '],
    ['备份里有这些文件：', 'Backup folder contains these files: '],
    ['预览结果：', 'Preview result: '],
    ['已是最新，跳过', ' is already up to date; skipped '],
    ['无需重复处理。', ' no duplicate processing is required.'],
    ['知识库中未找到与"', 'No relevant result found for "'],
    ['"相关的内容', '".'],
    ['找到了，', 'Found: '],
    ['在文档文件夹里。', ' in the documents folder.'],
    ['在文档/文件夹里。', ' in the documents folder.'],
    ['第一个是', 'First is '],
    ['，在', ', in '],
    ['未做任何改动。', 'No changes were made.'],
    ['未做任何改动', 'No changes were made'],
    ['。', '.'],
    ['，', ', '],
  ];

  Object.entries(categoryMap).forEach(([source, target]) => {
    value = value.split(source).join(target);
  });

  replacements.forEach(([source, target]) => {
    value = value.split(source).join(target);
  });

  value = value.replace(/(\d+)张/g, '$1 images');
  value = value.replace(/\b(scenery|food|people|animals|architecture|transportation|daily items|plants)\s+(\d+)\s+images/gi, '$1: $2');
  value = value.replace(/\b(Family album|Phone album|Trip|Backup)\s+([A-Za-z ]+)/g, '$1: $2');

  if (naturalSummaryPattern.test(value)) {
    const m = value.match(naturalSummaryPattern);
    const prefix = (m && m[1] && !m[1].includes('Preview result')) ? m[1] : 'Preview result:';
    const summary = (m && m[2]) ? m[2] : value;
    const normalizedSummary = summary
      .replace(/\s*:\s*/g, ': ')
      .replace(/\s*,\s*/g, ', ')
      .replace(/\s+([a-zA-Z]+)/g, ' $1');
    if (normalizedSummary.includes(':')) {
      value = `${prefix} ${normalizedSummary}.`;
    }
  }

  value = value.replace(/\s+,\s+/g, ', ');
  value = value.replace(/\s+\./g, '.');

  return value;
}

function renderNewTurns(turns) {
  if (!turns || !turns.length || !turnsEl) return;
  const live = (_liveTurnEl && _liveTurnEl.isConnected) ? _liveTurnEl : null;
  const labels = srcLabels();
  turns.forEach((turn) => {
    const div = document.createElement('div');
    div.className = 'turn';
    const src = labels[turn.source] || turn.source;
    const locale = (window.voiceI18n && window.voiceI18n.getLang && window.voiceI18n.getLang() === 'en') ? 'en-US' : 'zh-CN';
    const hhmm = new Date(turn.ts * 1000).toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const costText = `${tr('turnCost')} ${turn.cost_ms} ms`;
    const textContent = turn.text ? localizeDisplayText(turn.text) : '';
    const replyContent = turn.reply ? localizeDisplayText(turn.reply) : '';
    div.innerHTML =
      `<div class="turn-src"><span class="turn-src-label" data-src-key="${turn.source}">${src}</span> · ${hhmm}</div>` +
      (turn.text ? `<div class="turn-txt" data-raw-text="${String(turn.text).replace(/"/g, '&quot;')}">🗣 ${textContent}</div>` : '') +
      (turn.reply ? `<div class="turn-rep" data-raw-text="${String(turn.reply).replace(/"/g, '&quot;')}">💬 ${replyContent}</div>` : '') +
      `<div class="turn-meta" data-cost-ms="${turn.cost_ms}">${costText}</div>`;
    if (live) {
      turnsEl.insertBefore(div, live.nextSibling);
    } else {
      turnsEl.prepend(div);
    }
    if (turn.ts > lastTurnTs) lastTurnTs = turn.ts;
  });
  turnsEl.scrollTop = 0;
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

function refreshDynamicI18n() {
  const labels = srcLabels();
  document.querySelectorAll('.turn-src-label[data-src-key]').forEach((el) => {
    const val = labels[el.dataset.srcKey];
    if (val !== undefined) el.textContent = val;
  });
  document.querySelectorAll('.turn-meta[data-cost-ms]').forEach((el) => {
    el.textContent = `${tr('turnCost')} ${el.dataset.costMs} ms`;
  });
  document.querySelectorAll('.turn-live .turn-src').forEach((el) => {
    el.textContent = `🧾 ${tr('realTimeStatus')}`;
  });
  document.querySelectorAll('.turn-txt, .turn-rep').forEach((el) => {
    const raw = el.dataset.rawText || '';
    if (!raw) return;
    const prefix = el.classList.contains('turn-txt') ? '🗣 ' : '💬 ';
    el.textContent = prefix + localizeDisplayText(raw);
  });
  if (_statusRenderer) setStatusText(_statusRenderer());
}

if (clearTurnsBtn) {
  clearTurnsBtn.addEventListener('click', () => {
    if (turnsEl) {
      turnsEl.innerHTML = '';
    }
    _liveTurnEl = null;
    lastTurnTs = Date.now() / 1000;
    setStatus(() => tr('statusIdle'));
  });
}

setInterval(fetchTurns, 1500);
fetchTurns();
setStatus(() => tr('statusIdle'));

const STATE_LABELS = {
  awake: 'stateAwake',
  listening: 'stateListening',
  asr: 'stateAsr',
  processing: 'stateProcessing',
  speaking: 'stateSpeaking',
};

let _lastBridgeState = 'idle';

function _applyBridgeState(state, busy) {
  const active = _taskBusy || busy || (state !== 'idle');
  if (voiceBtn) {
    voiceBtn.disabled = active;
    voiceBtn.textContent = STATE_LABELS[state] ? tr(STATE_LABELS[state]) : (active ? tr('voiceBtnBusy') : tr('voiceBtnStart'));
  }
  if (textBtn) {
    textBtn.disabled = active;
  }
  if (textInput) {
    textInput.disabled = active;
  }
  if (state !== 'idle') {
    setStatus(() => STATE_LABELS[state] ? tr(STATE_LABELS[state]) : `Voice Assistant status: ${state}`);
  } else if (_lastBridgeState !== 'idle') {
    setStatus(() => tr('statusIdle'));
  }
  _lastBridgeState = state;
}

async function pollBridgeStatus() {
  try {
    const resp = await fetch(apiUrl('/api/status'), { cache: 'no-store' });
    if (!resp.ok) return;
    const data = await resp.json();
    _applyBridgeState(data.state || 'idle', !!data.busy);
  } catch (_) {}
}

setInterval(pollBridgeStatus, 800);
pollBridgeStatus();

document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  fetchTurns();
  pollBridgeStatus();
});

document.addEventListener('voice:lang-change', () => {
  if (langToggle && window.voiceI18n && typeof window.voiceI18n.getLang === 'function') {
    const nextLang = window.voiceI18n.getLang();
    langToggle.textContent = nextLang === 'en' ? '中文' : 'English';
  }
  refreshDynamicI18n();
});

window.__voiceUiLoaded = true;
