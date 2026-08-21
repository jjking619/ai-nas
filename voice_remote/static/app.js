const appRoot = document.querySelector('.app');
const appVersion = (appRoot && appRoot.dataset.uiVersion) || '';
const voiceBtn = document.getElementById('voiceBtn');
const textBtn = document.getElementById('textBtn');
const textInput = document.getElementById('textInput');
const statusEl = document.getElementById('status');

function apiUrl(path) {
  const sep = path.includes('?') ? '&' : '?';
  return path + sep + 'v=' + encodeURIComponent(appVersion);
}

function setBusy(busy) {
  voiceBtn.disabled = busy;
  textBtn.disabled = busy;
  textInput.disabled = busy;
}

function renderTask(task) {
  const statusMap = {
    queued: '已排队，等待执行...',
    running: '正在处理中，请稍候...',
    done: '处理完成。',
    error: '处理失败。'
  };
  const lines = [
    '任务ID：' + task.id,
    '模式：' + task.mode,
    '状态：' + (statusMap[task.status] || task.status)
  ];
  if (task.cost_ms != null) lines.push('耗时：' + task.cost_ms + ' ms');
  const result = task.result || {};
  if (result.message) lines.push('信息：' + result.message);
  if (result.text) lines.push('内容：' + result.text);
  if (result.reply) lines.push('回复：' + result.reply);
  if (task.error) lines.push('错误：' + task.error);
  statusEl.textContent = lines.join('\n');
}

async function pollTask(taskId) {
  for (;;) {
    const resp = await fetch(apiUrl('/api/task/' + encodeURIComponent(taskId)), { cache: 'no-store' });
    const data = await resp.json();
    if (!resp.ok) {
      statusEl.textContent = '任务查询失败：' + (data.error || resp.statusText);
      setBusy(false);
      return;
    }
    renderTask(data.task);
    if (data.task.status === 'done' || data.task.status === 'error') {
      setBusy(false);
      return;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 1500));
  }
}

async function submitTask(payload, modeHint) {
  setBusy(true);
  statusEl.textContent = modeHint + ' 请求已提交，正在排队...';
  try {
    const resp = await fetch(apiUrl('/api/trigger'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      cache: 'no-store'
    });
    const data = await resp.json();
    if (!(resp.status === 202 && data.task_id)) {
      statusEl.textContent = '触发失败：' + (data.error || resp.statusText);
      setBusy(false);
      return;
    }
    statusEl.textContent = '任务已创建：' + data.task_id + '\n开始执行...';
    await pollTask(data.task_id);
  } catch (err) {
    statusEl.textContent = '请求失败：' + err;
    setBusy(false);
  }
}

async function triggerVoice() {
  await submitTask({}, '语音模式');
}

async function triggerText() {
  const text = textInput.value.trim();
  if (!text) {
    statusEl.textContent = '请输入文本指令。';
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
const turnsEl = document.getElementById('turns');
const clearTurnsBtn = document.getElementById('clearTurns');
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
    lastTurnTs = Date.now() / 1000;
  });
}

setInterval(fetchTurns, 1500);
fetchTurns();

window.__voiceUiLoaded = true;

window.__voiceUiLoaded = true;