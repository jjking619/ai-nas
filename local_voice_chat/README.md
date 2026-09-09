# Local Voice Chat (open-source ASR wake -> ASR -> TTS)

This directory adds a minimal local voice chain on Linux arm64 without any proprietary KWS SDK.

## What it does

1. Record a short wakeup clip from microphone
2. Run open-source ASR wake-word matching to detect configured wake words
3. If wakeup is detected, record user speech clip
4. Run ASR (SenseVoice via `sherpa-onnx`)
5. Synthesize reply with MatchaTTS (official model auto-download or local `tts` layout)
6. Play reply audio

By default, you only need to wake once. Then continuous dialog is active.

Speech capture is now dynamic:
- It listens for at least a minimum duration
- It keeps extending while you are still speaking
- It ends turn when trailing audio is silent, or when max duration is reached

## Files

- `local_voice_chat.py`: main script
- `run_local_voice_chat.sh`: launcher (auto-installs Python deps)
- `voice_bridge.py`: bridge to OpenClaw (`docker exec openclaw ... agent --json`)
- `voice-bridge.service`: systemd unit template
- `install_voice_bridge_service.sh`: helper to install/enable service

## Requirements

- Linux arm64 (Debian is OK)
- `ffmpeg`, `ffplay`, `curl`
- Microphone input available via PulseAudio or ALSA

## Quick start

```bash
cd ~/NAS-Demo/local_voice_chat
chmod +x run_local_voice_chat.sh
./run_local_voice_chat.sh
```

## OpenClaw bridge mode

```bash
cd ~/NAS-Demo/local_voice_chat
python3 voice_bridge.py
```

Default wake words in bridge mode are 小远同学 and xiaoyuan.

Install as systemd service:

```bash
chmod +x install_voice_bridge_service.sh
./install_voice_bridge_service.sh
```

If OpenClaw call fails with docker permission/sudo password error:

```bash
sudo usermod -aG docker "$(id -un)"
# newgrp may fail with "setgid failed" in current session; use passwordless sudoers instead (works immediately):
USER_NAME="$(id -un)"
echo "${USER_NAME} ALL=(ALL) NOPASSWD: /usr/bin/docker" | sudo tee "/etc/sudoers.d/${USER_NAME}-docker"
sudo chmod 440 "/etc/sudoers.d/${USER_NAME}-docker"
sudo -n docker ps

sudo systemctl restart voice-bridge
```

`voice_bridge.py` tries `docker exec` first, then falls back to `sudo -n docker exec`.
The sudoers rule above makes that fallback work without re-login.

Recommended for first real-mic run:

```bash
./run_local_voice_chat.sh --wake-words "小远同学,xiaoyuan" --wake-duration 4
```

This will try the configured wake words and print mic level each turn.
After one successful wakeup, later turns do not need wake words.

## Offline file test (no microphone)

Use existing sample files to verify the full chain once:

```bash
./run_local_voice_chat.sh \
	--once \
	--no-play \
	--wake-audio-file /path/to/wake.wav \
	--speech-audio-file /path/to/speech.wav
```

## First run note

If `${HOME}/voice/asr/model/model.int8.onnx` (or `model.onnx`) is missing,
this script will auto-download SenseVoice int8 model from sherpa-onnx release.

If you want to disable auto-download:

```bash
./run_local_voice_chat.sh --no-auto-download-asr
```

## Useful options

```bash
# One round only
./run_local_voice_chat.sh --once

# One round only, file-mode (no microphone)
./run_local_voice_chat.sh --once --wake-audio-file /path/to/wake.wav --speech-audio-file /path/to/speech.wav

# Configure open-source wake words (bridge mode)
python3 voice_bridge.py --wake-words "小远同学,xiaoyuan"

# Use a custom wake-word list
./run_local_voice_chat.sh --wake-words "小远同学,xiaoyuan"

# Legacy behavior: require wake word before every turn
./run_local_voice_chat.sh --wake-any-keyword --require-wake-each-turn

# Adjust recording durations
./run_local_voice_chat.sh --wake-duration 2.5 --speech-duration 7

# If pulse is unstable, force ALSA backend
./run_local_voice_chat.sh --record-backend alsa

# Run without speaker playback
./run_local_voice_chat.sh --no-play

# Session sleeps after 5 consecutive empty ASR rounds
./run_local_voice_chat.sh --session-idle-rounds 5

# Tune dynamic speech end
./run_local_voice_chat.sh --speech-min-duration 3 --speech-duration 18 --speech-silence-threshold-dbfs -45
```

## If wakeup keeps failing

- Watch this line after each recording: `[MIC] wake clip level: -xx.x dBFS`
- If below `-45 dBFS`, mic volume is too low; move closer or raise input gain
- Try `--wake-any-keyword` first, then say one of:
	- 小创小创
	- 小燕小燕
	- 云铃云铃
	- 小远同学
- Increase wake recording window:

```bash
./run_local_voice_chat.sh --wake-any-keyword --wake-duration 5
```

- If PulseAudio source is wrong, switch backend:

```bash
./run_local_voice_chat.sh --wake-any-keyword --record-backend alsa
```

## 服务模式（HTTP，生产使用）

以 systemd 服务运行（`voice-bridge.service`），默认 HTTP 模式监听 `:28082`：

- 页面 `http://<NAS-IP>:28082/`：点击按钮 → 宿主麦克风录音 → 处理 → 播报
- 接口 `POST /trigger`：`{"text":"..."}` 文本指令 / 空 body 触发一次语音
- 页面底部「对话历史」：所有轮次（唤醒/按钮/文本）实时同步显示

```
对话历史同步原理：每轮结束写一条 JSONL → voice_turns.jsonl（日志目录）
→ voice_remote 容器（:28083）读取同一文件 → /api/turns → 页面每 1.5s 增量轮询
```

## 安全防护

- **危险指令二次确认**：含「删除/清空/移除/格式化」等词的指令先拦截，必须再说「确认」才执行，其余话术一律取消
- **每轮独立会话**：OpenClaw 使用 `voice-turn:<时间戳>` session-key，无跨轮上下文，杜绝「上一轮待确认被下一轮无关语音误触发」
- **滤镜权限诊断**：批处理遇 `Permission denied` 时明确提示修复目录归属

## 已知问题 / 注意

- **目录归属错乱**（已修复过）：容器进程曾以 `pulse` 用户创建 NAS 目录，导致当前登录用户无法写入（表现为滤镜「失败 N 张」）。若复现：
	`sudo find "${HOME}/nas_share" -user pulse -exec chown "$(id -un)":"$(id -gn)" {} +`
- **唤醒词识别依赖麦克风电平**：建议保持在 -35dBFS 以上（调整 `pactl set-source-volume`），唤醒循环已移除 +6dB 后处理重试
