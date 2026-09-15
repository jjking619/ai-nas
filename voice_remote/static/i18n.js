(function () {
  const I18N = {
    zh: {
      appTitle: '语音助手',
      leadText: '点击即可语音，或输入文本直接执行。两种模式都会触发 TTS 语音播报。',
      voiceMode: '语音模式',
      voiceHint: '1. 点击开始  2. 立刻对麦克风说话  3. 等待助手播报',
      voiceBtnStart: '开始语音指令',
      voiceBtnBusy: '🎙 正在录音...',
      textMode: '文本模式',
      textHint: '输入示例：播放测试视频 / 住房合同在哪',
      textInputPlaceholder: '输入文本指令',
      textBtn: '发送文本',
      statusIdle: '待机中。请选择语音或文本模式。',
      turnsTitle: '对话历史',
      clearTurns: '清空',
      realTimeStatus: '实时状态',
      sourceWake: '🎤 唤醒',
      sourceButton: '🖱 按钮',
      sourceText: '⌨ 文本',
      stateAwake: '🎙 已唤醒，请说话...',
      stateListening: '🎙 正在聆听...',
      stateAsr: '🔍 正在识别...',
      stateProcessing: '⚙️ 正在处理...',
      stateSpeaking: '🔊 正在播报...',
      taskQueued: '请求已提交，正在排队...',
      taskCreated: '任务已创建：',
      taskStart: '开始执行...',
      taskQueryFailed: '任务查询失败：',
      triggerFailed: '触发失败：',
      requestFailed: '请求失败：',
      processingFailed: '处理失败：',
      pleaseInputText: '请输入文本指令。',
      turnCost: '耗时',
      unknownError: '未知错误',
    },
    en: {
      appTitle: 'Voice Assistant',
      leadText: 'Click to speak, or type a command directly. Both modes trigger TTS playback.',
      voiceMode: 'Voice mode',
      voiceHint: '1. Click begin  2. Speak into the mic immediately  3. Wait for the assistant response',
      voiceBtnStart: 'Start voice command',
      voiceBtnBusy: '🎙 Recording...',
      textMode: 'Text mode',
      textHint: 'Example: Play the sample video / Where is the housing contract?',
      textInputPlaceholder: 'Type a command',
      textBtn: 'Send text',
      statusIdle: 'Idle. Choose voice or text mode.',
      turnsTitle: 'Conversation history',
      clearTurns: 'Clear',
      realTimeStatus: 'Live status',
      sourceWake: '🎤 Wake',
      sourceButton: '🖱 Button',
      sourceText: '⌨ Text',
      stateAwake: '🎙 Awake, please speak...',
      stateListening: '🎙 Listening...',
      stateAsr: '🔍 Recognizing...',
      stateProcessing: '⚙️ Processing...',
      stateSpeaking: '🔊 Speaking...',
      taskQueued: 'request submitted and waiting in queue...',
      taskCreated: 'Task created: ',
      taskStart: 'starting execution...',
      taskQueryFailed: 'Task query failed: ',
      triggerFailed: 'Trigger failed: ',
      requestFailed: 'Request failed: ',
      processingFailed: 'Processing failed: ',
      pleaseInputText: 'Please enter a text command.',
      turnCost: 'Cost',
      unknownError: 'unknown error',
    }
  };

  function getPreferredLang() {
    try {
      const stored = window.localStorage.getItem('voice_ui_lang');
      if (stored === 'zh' || stored === 'en') return stored;
    } catch (_) {}

    const params = new URLSearchParams(window.location.search || '');
    const queryLang = params.get('lang');
    if (queryLang === 'zh' || queryLang === 'en') return queryLang;

    return 'zh';
  }

  function setStoredLang(lang) {
    try {
      window.localStorage.setItem('voice_ui_lang', lang);
    } catch (_) {}
  }

  function applyLang(lang) {
    const next = (lang === 'en') ? 'en' : 'zh';
    setStoredLang(next);
    document.documentElement.lang = next === 'en' ? 'en-US' : 'zh-CN';

    document.querySelectorAll('[data-i18n]').forEach((el) => {
      const key = el.dataset.i18n;
      if (!key) return;
      const val = I18N[next][key];
      if (val !== undefined) el.textContent = val;
    });

    document.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
      const key = el.dataset.i18nPlaceholder;
      const val = I18N[next][key];
      if (val !== undefined) el.setAttribute('placeholder', val);
    });

    const toggle = document.getElementById('langToggle');
    if (toggle) {
      toggle.textContent = next === 'en' ? '中文' : 'English';
      toggle.setAttribute('aria-label', next === 'en' ? 'Switch to Chinese' : 'Switch to English');
    }

    document.dispatchEvent(new CustomEvent('voice:lang-change', { detail: { lang: next } }));
  }

  window.voiceI18n = {
    getLang() {
      return getPreferredLang();
    },
    setLang(lang) {
      applyLang(lang);
    },
    applyLang,
    t(key, fallback = '') {
      const lang = getPreferredLang();
      const dict = I18N[lang] || I18N.zh;
      return dict[key] !== undefined ? dict[key] : (fallback || key);
    }
  };

  document.addEventListener('DOMContentLoaded', function () {
    applyLang(getPreferredLang());
    const toggle = document.getElementById('langToggle');
    if (toggle) {
      toggle.addEventListener('click', function () {
        const next = window.voiceI18n.getLang() === 'en' ? 'zh' : 'en';
        window.voiceI18n.setLang(next);
      });
    }
  });
})();
