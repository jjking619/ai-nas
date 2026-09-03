#!/usr/bin/env python3
import argparse
import gc
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from local_voice_chat import (
    asr_transcribe,
    build_asr_recognizer,
    build_tts,
    check_cmd_exists,
    collect_keyword_bins,
    detect_wakeup,
    detect_wakeup_any,
    ensure_sensevoice_model,
    record_audio_auto_backend,
    record_speech_until_silence,
    tts_speak,
    wakeword_hint_from_bin,
    wav_level_dbfs,
)


def load_runtime_env(env_file: str | Path | None = None) -> dict[str, str]:
    """Load repo-level .env values without overriding already-exported process env.

    This keeps the project portable across shell, systemd, and tests while still
    treating explicit process environment variables as authoritative.
    """
    candidates = []
    if env_file is not None:
        candidates.append(Path(env_file))
    repo_root = Path(__file__).resolve().parent.parent
    candidates.extend([
        repo_root / ".env",
        Path.cwd() / ".env",
        Path.home() / ".env",
    ])

    path = next((p for p in candidates if p and p.exists()), None)
    if path is None:
        return {}

    values: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key:
                continue
            final_value = os.environ.get(key, value)
            values[key] = final_value
            os.environ[key] = final_value
    except OSError:
        return {}
    return values


load_runtime_env()


def _resolve_sdk_root(folder_name: str) -> Path:
    env_base = os.getenv("VOICE_SDK_BASE")
    candidates = []
    if env_base:
        candidates.append(Path(env_base) / folder_name)

    candidates.append(Path(__file__).resolve().parent.parent / folder_name)
    candidates.append(Path("/home/pi/voice") / folder_name)

    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


def parse_args():
    kws_root = _resolve_sdk_root("kws1.0.0.1_SDK_16k_10ms_enwatermark_8h")
    asr_root = _resolve_sdk_root("asr_cpu_1.19")
    tts_root = _resolve_sdk_root("tts_cpu_2.1")

    parser = argparse.ArgumentParser(
        description="Voice bridge: KWS -> ASR -> OpenClaw -> TTS"
    )
    parser.add_argument("--kws-root", default=str(kws_root))
    parser.add_argument("--asr-root", default=str(asr_root))
    parser.add_argument("--tts-root", default=str(tts_root))

    # Default wake word: 小远同学
    parser.add_argument(
        "--wake-keyword-bin",
        default=str(kws_root / "res_shuffnet_v2" / "keyword_xiaoyuantongxue.bin"),
    )
    parser.add_argument(
        "--wake-any-keyword",
        action="store_true",
        help="Try all built-in keyword_*.bin and wake if any one matches",
    )
    parser.add_argument(
        "--wake-filler",
        default=str(
            kws_root
            / "res_shuffnet_v2"
            / "Filler"
            / "state_filler_3000s_kladi_1179.txt"
        ),
    )
    parser.add_argument("--wake-mlp", default=str(kws_root / "res_shuffnet_v2" / "mlp.bin"))

    parser.add_argument("--wake-duration", type=float, default=4.0)
    parser.add_argument(
        "--wake-low-level-dbfs",
        type=float,
        default=-43.0,
        help="Wake录音低于该电平且首次未命中时，触发一次增益重试",
    )
    parser.add_argument(
        "--wake-boost-db",
        type=float,
        default=6.0,
        help="低电平重试时的增益(dB)，设为0可关闭",
    )
    parser.add_argument("--record-backend", choices=["auto", "pulse", "alsa"], default="alsa")
    parser.add_argument("--mic-input", default="plughw:0,0")

    parser.add_argument("--speech-duration", type=float, default=15.0)
    parser.add_argument("--speech-min-duration", type=float, default=1.5)
    parser.add_argument("--speech-tail-window", type=float, default=0.8)
    parser.add_argument("--speech-silence-threshold-dbfs", type=float, default=-45.0)

    parser.add_argument("--asr-language", default="zh")
    parser.add_argument("--asr-model", default="")
    parser.add_argument("--no-auto-download-asr", action="store_true")
    parser.add_argument(
        "--asr-engine",
        choices=["sensevoice", "conformer"],
        default="conformer",
        help="sensevoice=不支持热词；conformer=离线大模型+热词",
    )
    parser.add_argument("--hotwords-file", default="", help="热词文件路径，默认自动生成 hotwords.txt")
    parser.add_argument("--hotwords-score", type=float, default=2.5, help="热词增益分数")

    parser.add_argument("--session-idle-rounds", type=int, default=3)

    parser.add_argument("--openclaw-container", default="openclaw")
    parser.add_argument("--openclaw-session-key", default="agent:main:voice-bridge-v2")
    parser.add_argument("--openclaw-timeout", type=int, default=300)
    parser.add_argument("--openclaw-dry-run", action="store_true")

    parser.add_argument("--jellyfin-url", default="http://127.0.0.1:8096",
                        help="Jellyfin 服务地址")
    parser.add_argument("--jellyfin-api-key", default="",
                        help="Jellyfin API Key（管理后台→控制台→API 密钥→新增密钥）")

    parser.add_argument(
        "--tts-max-chars",
        type=int,
        default=80,
        help="TTS常规最大播报字数",
    )
    parser.add_argument(
        "--tts-brief-max-chars",
        type=int,
        default=36,
        help="短指令场景下的TTS压缩播报上限",
    )
    parser.add_argument(
        "--tts-brief-user-len",
        type=int,
        default=12,
        help="用户输入长度小于等于该值时，启用短回复优先",
    )

    parser.add_argument("--no-play", action="store_true")
    parser.add_argument("--work-dir", default="/tmp/voice_bridge")
    parser.add_argument(
        "--http-mode",
        action="store_true",
        help="Run lightweight HTTP trigger server instead of always-on wake loop",
    )
    parser.add_argument(
        "--wake-mode",
        action="store_true",
        help="Force the legacy always-on wake loop even in non-interactive sessions",
    )
    parser.add_argument(
        "--http-trigger-host",
        default="0.0.0.0",
        help="Bind host for HTTP trigger server",
    )
    parser.add_argument(
        "--http-trigger-port",
        type=int,
        default=28082,
        help="Bind port for HTTP trigger server",
    )
    parser.add_argument(
        "--http-trigger-token",
        default="",
        help="Optional token for /trigger requests",
    )
    parser.add_argument(
        "--http-keep-models",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep ASR/TTS models in memory in HTTP mode for lower latency",
    )
    parser.add_argument(
        "--http-wakeword",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable background wake-word loop in HTTP mode",
    )
    parser.add_argument(
        "--http-wakeword-prompt",
        default="我在，请说。",
        help="TTS prompt after wake-word in HTTP mode",
    )
    parser.add_argument(
        "--log-file",
        default="/home/pi/NAS-Demo/logs/voice_bridge.log",
        help="Local log file path",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="打印高频调试日志（唤醒循环音量等），默认关闭以压缩日志",
    )

    return parser.parse_args()


_HTTP_MODEL_CACHE = {
    "tts": None,
    "recognizers": {},
}

_HTTP_DIALOG_STATE = {
    "pending_image_filter": None,
    "pending_danger_confirm": None,  # {"original_text": str}
    "pending_question": 0,  # 助手追问澄清后，唤醒通道需免唤醒继续收听答案的次数
}

_VOICE_TURNS: list = []
_VOICE_TURNS_LOCK = threading.Lock()
_TURNS_FILE: Path | None = None
_TURNS_MAX = 100

_BRIDGE_STATE: dict = {"state": "idle", "ts": 0.0, "last_text": ""}
_BRIDGE_STATE_LOCK = threading.Lock()


def _set_bridge_state(state: str, last_text: str = "") -> None:
    with _BRIDGE_STATE_LOCK:
        _BRIDGE_STATE["state"] = state
        _BRIDGE_STATE["ts"] = time.time()
        if last_text:
            _BRIDGE_STATE["last_text"] = last_text


def _record_voice_turn(source: str, text: str, reply: str, cost_ms: int) -> None:
    turn = {
        "id": f"{int(time.time() * 1000):x}",
        "ts": time.time(),
        "source": source,
        "text": text,
        "reply": reply,
        "cost_ms": cost_ms,
    }
    with _VOICE_TURNS_LOCK:
        _VOICE_TURNS.append(turn)
        if len(_VOICE_TURNS) > _TURNS_MAX:
            del _VOICE_TURNS[:-_TURNS_MAX]
    if _TURNS_FILE is not None:
        try:
            with _TURNS_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(turn, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            print(f"[TURNS] write failed: {e}")


class _TeeStream:
    """按文件名每次写入时追加打开，日志轮转（rename）后自动写到新文件。"""

    def __init__(self, stream, log_path):
        self._stream = stream
        self._log_path = log_path
        self._write_count = 0

    def write(self, data):
        self._stream.write(data)
        try:
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(data)
        except Exception:
            pass
        self._write_count += 1
        if self._write_count % 200 == 0:
            _maybe_rotate_log(str(self._log_path))

    def flush(self):
        self._stream.flush()

    def isatty(self):
        return self._stream.isatty()


_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUPS = 3


def _maybe_rotate_log(log_file: str) -> None:
    """简单轮转：超过 5MB 时 .log -> .log.1（最多保留 3 份），无需外部 logrotate。"""
    try:
        p = Path(log_file)
        if not p.exists() or p.stat().st_size < _LOG_MAX_BYTES:
            return
        for i in range(_LOG_BACKUPS - 1, 0, -1):
            src = Path(f"{p}.{i}")
            if src.exists():
                src.replace(Path(f"{p}.{i + 1}"))
        p.replace(Path(f"{p}.1"))
    except Exception:
        pass


def _setup_file_logging(log_file: str) -> None:
    if not log_file:
        return
    try:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        _maybe_rotate_log(log_file)
        sys.stdout = _TeeStream(sys.stdout, path)
        sys.stderr = _TeeStream(sys.stderr, path)
        print(f"[LOG] file logging enabled: {path}")
    except Exception as e:  # noqa: BLE001
        print(f"[LOG] file logging setup failed: {e}")


def _ensure_audio_runtime_env() -> None:
    """补齐 systemd 场景常缺失的音频环境变量，避免 ffmpeg 只录到极短片段。"""
    if not os.getenv("XDG_RUNTIME_DIR"):
        runtime_dir = Path(f"/run/user/{os.getuid()}")
        if runtime_dir.exists():
            os.environ["XDG_RUNTIME_DIR"] = str(runtime_dir)
            print(f"[AUDIO] XDG_RUNTIME_DIR not set, using {runtime_dir}")

    if not os.getenv("PULSE_SERVER"):
        pulse_native = Path("/run/pulse/native")
        if pulse_native.exists():
            os.environ["PULSE_SERVER"] = f"unix:{pulse_native}"
            print(f"[AUDIO] PULSE_SERVER not set, using unix:{pulse_native}")


def _wav_meta_for_log(path: Path) -> str:
    """返回录音文件的关键元信息，便于排查录音异常。"""
    if not path.exists():
        return f"missing path={path}"

    size = path.stat().st_size
    try:
        import wave

        with wave.open(str(path), "rb") as wf:
            sr = wf.getframerate()
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            n = wf.getnframes()
        dur = (float(n) / float(sr)) if sr else 0.0
        return f"path={path} size={size}B dur={dur:.2f}s sr={sr} ch={ch} sw={sw}"
    except Exception as e:  # noqa: BLE001
        return f"path={path} size={size}B wave_err={e}"


def _json_response(handler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler, status: int, html: str) -> None:
    body = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _http_ui_html(trigger_port: int) -> str:
    return f"""<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>对话助手</title>
    <style>
        :root {{
            --bg-1: #08131c;
            --bg-2: #163247;
            --card: rgba(255,255,255,0.08);
            --line: rgba(255,255,255,0.16);
            --text: #f5f7fa;
            --muted: #a9bac7;
            --accent: #f59e0b;
            --accent-2: #ea580c;
            --ok: #22c55e;
            --warn: #fbbf24;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            background:
                radial-gradient(circle at top left, rgba(245,158,11,0.22), transparent 34%),
                radial-gradient(circle at bottom right, rgba(14,165,233,0.24), transparent 30%),
                linear-gradient(145deg, var(--bg-1), var(--bg-2));
            color: var(--text);
            font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Segoe UI", sans-serif;
            padding: 24px;
        }}
        .panel {{
            width: min(560px, 100%);
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 24px;
            padding: 28px;
            backdrop-filter: blur(14px);
            box-shadow: 0 18px 60px rgba(0,0,0,0.28);
        }}
        h1 {{ margin: 0 0 10px; font-size: 30px; }}
        p {{ margin: 0; color: var(--muted); line-height: 1.7; }}
        .hero {{ margin-bottom: 22px; }}
        .button {{
            margin-top: 22px;
            width: 100%;
            border: 0;
            border-radius: 18px;
            padding: 20px 18px;
            font-size: 22px;
            font-weight: 700;
            color: #fff;
            cursor: pointer;
            background: linear-gradient(135deg, var(--accent), var(--accent-2));
            box-shadow: 0 14px 36px rgba(234,88,12,0.35);
        }}
        .button[disabled] {{ cursor: not-allowed; opacity: 0.65; }}
        .status {{
            margin-top: 18px;
            min-height: 84px;
            border-radius: 16px;
            padding: 16px 18px;
            background: rgba(0,0,0,0.2);
            border: 1px solid rgba(255,255,255,0.08);
            white-space: pre-wrap;
            line-height: 1.7;
        }}
        .meta {{ margin-top: 16px; font-size: 13px; color: var(--muted); }}
        .ok {{ color: var(--ok); }}
        .warn {{ color: var(--warn); }}
    </style>
</head>
<body>
    <main class=\"panel\">
        <div class=\"hero\">
            <h1>点击后直接说话</h1>
            <p>按钮触发后会立即开始录音，不再常驻监听唤醒词。这样待机几乎不占 CPU，只在你点击时才加载识别与播报能力。</p>
        </div>

        <button id=\"triggerBtn\" class=\"button\">开始一次语音指令</button>
        <div id=\"status\" class=\"status\">待机中。点击按钮后，请立刻对麦克风说话。</div>
        <div class=\"meta\">接口地址：/trigger · 端口：{trigger_port}</div>
    </main>

    <script>
        const btn = document.getElementById('triggerBtn');
        const status = document.getElementById('status');

        async function triggerVoice() {{
            btn.disabled = true;
            status.textContent = '已触发，正在准备录音，请立刻说话...';
            try {{
                const resp = await fetch('/trigger', {{ cache: 'no-store' }});
                const data = await resp.json();
                if (!resp.ok) {{
                    status.textContent = '触发失败：' + (data.error || resp.statusText);
                    return;
                }}
                const lines = [
                    data.message || '处理完成',
                    data.text ? '识别内容：' + data.text : '',
                    data.reply ? '系统回复：' + data.reply : ''
                ].filter(Boolean);
                status.textContent = lines.join('\n');
            }} catch (err) {{
                status.textContent = '接口请求失败：' + err;
            }} finally {{
                btn.disabled = false;
            }}
        }}

        btn.addEventListener('click', triggerVoice);
    </script>
</body>
</html>
"""


def _run_agent_cmd(cmd, timeout_sec):
    # Try plain docker first. If permission denied, fallback to sudo -n docker.
    attempts = [cmd, ["sudo", "-n"] + cmd]
    last_err = None
    for c in attempts:
        try:
            p = subprocess.run(
                c,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_sec,
            )
            out = (p.stdout or "") + "\n" + (p.stderr or "")
            if p.returncode == 0:
                return True, out
            # Retry with next method only on common permission failures.
            low = out.lower()
            if "permission denied" in low or "docker daemon socket" in low:
                last_err = out.strip()
                continue
            return False, out.strip()
        except subprocess.TimeoutExpired:
            return False, f"OpenClaw agent timeout after {timeout_sec}s"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    return False, last_err or "OpenClaw agent failed"


def ensure_openclaw_exec_access(container_name: str) -> None:
    checks = [
        ["docker", "ps"],
        ["sudo", "-n", "docker", "ps"],
    ]

    ok = False
    detail = ""
    for c in checks:
        p = subprocess.run(c, text=True, capture_output=True, check=False)
        out = (p.stdout or "") + "\n" + (p.stderr or "")
        if p.returncode == 0:
            ok = True
            break
        detail = out.strip() or detail

    if not ok:
        raise RuntimeError(
            "No non-interactive Docker access for OpenClaw.\n"
            "Please run these commands once and restart shell/service:\n"
            "  sudo usermod -aG docker pi\n"
            "  newgrp docker\n"
            "Then verify with: docker ps\n"
            f"Current error: {detail}"
        )

    # Ensure target container exists/running before entering voice loop.
    p = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    names = set((p.stdout or "").split()) if p.returncode == 0 else set()
    if container_name not in names:
        p2 = subprocess.run(
            ["sudo", "-n", "docker", "ps", "--format", "{{.Names}}"],
            text=True,
            capture_output=True,
            check=False,
        )
        names = set((p2.stdout or "").split()) if p2.returncode == 0 else names

    if container_name not in names:
        raise RuntimeError(
            f"OpenClaw container '{container_name}' is not running. "
            "Please run: /home/pi/NAS-Demo/oc.sh status"
        )


def sync_runtime_scripts() -> None:
    """Single maintenance entry for runtime copies used by containers.

    Keep the logic here centralized so both the HTTP mode and the interactive mode
    follow the same sync behavior instead of each reproducing the same steps.
    """
    sync_nas_classify_script()
    sync_image_batch_script()


def sync_nas_classify_script() -> Path:
    """同步 NAS-Demo 下的唯一源文件到容器内运行位置。

    /home/pi/NAS-Demo/local_voice_chat/nas_classify.py 是唯一维护源；
    容器通过 /nas_share 挂载访问 /nas_share/tools/nas_classify.py（运行必需），
    每次启动时若内容有差异自动覆盖，避免两份漂移。
    """
    src = Path(__file__).resolve().parent / "nas_classify.py"
    dst = Path("/home/pi/nas_share/tools/nas_classify.py")

    if not src.exists():
        raise RuntimeError(f"nas_classify.py 源文件不存在: {src}")

    try:
        if not dst.exists() or src.read_bytes() != dst.read_bytes():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            print(f"[SYNC] nas_classify.py 已同步: {src} -> {dst}")
        return dst
    except OSError as e:
        print(f"[WARN] nas_classify.py 同步失败（不影响语音主流程）: {e}")
        return dst


def sync_image_batch_script() -> Path:
    """同步滤镜批处理脚本到 /nas_share/tools，便于容器挂载可见。"""
    src = Path(__file__).resolve().parent / "image_batch.py"
    dst = Path("/home/pi/nas_share/tools/image_batch.py")

    if not src.exists():
        raise RuntimeError(f"image_batch.py 源文件不存在: {src}")

    try:
        if not dst.exists() or src.read_bytes() != dst.read_bytes():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            print(f"[SYNC] image_batch.py 已同步: {src} -> {dst}")
        return dst
    except OSError as e:
        print(f"[WARN] image_batch.py 同步失败（不影响语音主流程）: {e}")
        return dst


def _json_from_mixed_output(raw):
    raw = raw.strip()
    if not raw:
        return None

    # Fast path: full JSON
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        pass

    # Find first '{' and last '}' as fallback
    i = raw.find("{")
    j = raw.rfind("}")
    if i >= 0 and j > i:
        candidate = raw[i : j + 1]
        try:
            return json.loads(candidate)
        except Exception:  # noqa: BLE001
            return None
    return None


def _extract_text_from_agent_json(obj):
    if not isinstance(obj, dict):
        return ""

    # Preferred OpenClaw shape
    payloads = obj.get("result", {}).get("payloads", [])
    if isinstance(payloads, list):
        texts = []
        for p in payloads:
            if isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str) and t.strip():
                    texts.append(t.strip())
        if texts:
            return "\n".join(texts)

    # Generic fallback
    for key in ("text", "message", "reply", "output"):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()

    return ""


def normalize_asr_text(text: str) -> str:
    """Fix common SenseVoice mis-recognitions of known NAS folder names.

    SenseVoice is a generic CTC model and cannot be biased with hotwords,
    so we normalize frequent errors at the application layer.
    """
    replacements = {
        "家庭册": "家庭相册",
        "家庭像册": "家庭相册",
        "家庭象册": "家庭相册",
        "家相册": "家庭相册",
        "家像册": "家庭相册",
        "家象册": "家庭相册",
        "手机册": "手机相册",
        "手机像册": "手机相册",
        "手机象册": "手机相册",
        "机相册": "手机相册",
        "机像册": "手机相册",
        "机象册": "手机相册",
        "照片夹": "相册",
        "像片夹": "相册",
        "像册": "相册",
        "特视频": "测试视频",
        "测视频": "测试视频",
        "下载特": "下载测试",
        "下载测": "下载测试",
        "下载韩纪录片": "下载海洋纪录片",
        # 知识库查询常见误识别（住房合同）
        "住房盒": "住房合同",
        "住房合盒": "住房合同",
    }
    for wrong, right in replacements.items():
        text = text.replace(wrong, right)

    # 上下文补全：仅当句子属于 NAS 操作语境时，才做"缺字补全"，降低误伤
    is_nas_cmd = any(k in text for k in ("分类", "照片", "相册", "图片", "移动", "整理", "备份"))
    if is_nas_cmd:
        for base, full in (("家庭", "家庭相册"), ("家", "家庭相册"), ("手机", "手机相册"), ("机", "手机相册")):
            if base in text and full not in text:
                for d in ("下面", "里面", "里的", "中的", "内", "下", "里"):
                    pat = f"{base}{d}"
                    if pat in text:
                        text = text.replace(pat, f"{full}{d}")
                        break
        text = text.replace("所有图", "所有图片")
        text = text.replace("全部图", "全部图片")

    # 下载/播放语境下的轻量纠偏（避免把"测试视频"识别成"特视频/测视频"）
    is_media_cmd = any(k in text for k in ("下载", "播放", "视频", "电影", "预告片", "纪录片"))
    if is_media_cmd:
        text = text.replace("特视频", "测试视频")
        text = text.replace("测视频", "测试视频")
        text = text.replace("测试试视频", "测试视频")
    return text


def _fast_local_classify_reply(args, user_text):
    """命中明确分类指令时，直跑脚本，避免 agent 多轮推理超时。

    返回 None 表示不命中，交给 agent 正常处理。
    """
    want = any(k in user_text for k in ("分类", "归档", "整理", "重命名", "清理"))
    is_photo = any(k in user_text for k in ("照片", "图片", "相册"))
    if not (want and is_photo):
        return None

    roots = ("手机相册", "家庭相册", "旅行", "备份")
    target = next((r for r in roots if r in user_text), None)
    if target is None:
        return None

    dry = any(k in user_text for k in ("预览", "看看", "先别动", "计划"))
    cmd = [
        "docker",
        "exec",
        args.openclaw_container,
        "python3",
        "/nas_share/tools/nas_classify.py",
        "--dir",
        f"/nas_share/{target}",
        "--recursive",
        "--dry-run" if dry else "--archive",
    ]
    ok, output = _run_agent_cmd(cmd, timeout_sec=120)
    if not ok:
        return f"分类脚本执行失败：{output[:120]}"

    counts = {}
    for line in output.splitlines():
        if "\t建议:" not in line:
            continue
        cat = line.split("\t建议:", 1)[1].split("(", 1)[0].strip()
        counts[cat] = counts.get(cat, 0) + 1
    if not counts:
        return f"{target}分类脚本已执行。"

    summary = "，".join(f"{k}{v}张" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
    if dry:
        return f"预览结果：{target} {summary}，未做任何改动。"
    return f"{target}分类完成：{summary}。"


_IMAGE_STYLE_ALIASES = {
    "复古风格": "vintage",
    "复古风": "vintage",
    "复古": "vintage",
    "vintage": "vintage",
    "日系风格": "japanese",
    "日系风": "japanese",
    "日系": "japanese",
    "japanese": "japanese",
    "胶片风格": "film",
    "胶片风": "film",
    "胶片": "film",
    "film": "film",
}

_IMAGE_STYLE_DIRS = {
    "vintage": "复古风格",
    "japanese": "日系风格",
    "film": "胶片风格",
}

_IMAGE_FILTER_STYLE_PROMPT = "要哪种风格：复古、日系还是胶片？"
_IMAGE_FILTER_TARGET_PROMPT = "请说要处理哪个目录，例如旅行或家庭相册。"
_IMAGE_FILTER_PENDING_MAX_ATTEMPTS = 4


def _looks_like_image_filter_request(user_text: str) -> bool:
    has_filter_intent = any(
        k in user_text
        for k in ("滤镜", "风格", "调色", "处理成", "处理", "改成", "变成", "弄成", "加滤镜")
    )
    has_photo_object = any(k in user_text for k in ("照片", "图片", "相册", "图像"))
    return has_filter_intent and has_photo_object


def _detect_image_filter_target(user_text: str):
    roots = ("手机相册", "家庭相册", "旅行", "备份")
    return next((r for r in roots if r in user_text), None)


def _extract_image_filter_request(user_text: str):
    if not _looks_like_image_filter_request(user_text):
        return None

    target = _detect_image_filter_target(user_text)
    style = _detect_image_style(user_text)
    dry = any(k in user_text for k in ("预览", "先看看", "先别动", "计划", "试运行", "dry-run"))
    return {
        "target": target,
        "style": style,
        "dry": dry,
    }


def _run_image_batch_reply(target: str, style: str, dry: bool):
    script = Path(__file__).resolve().parent / "image_batch.py"
    if not script.exists():
        return "滤镜脚本不存在，请先同步 image_batch.py。"

    cmd = [
        sys.executable,
        str(script),
        "--dir",
        str(Path("/home/pi/nas_share") / target),
        "--style",
        style,
        "--recursive",
    ]
    if dry:
        cmd.append("--dry-run")

    try:
        p = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            check=False,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return "滤镜处理超时，请缩小目录范围后重试。"
    except Exception as e:  # noqa: BLE001
        return f"滤镜处理失败：{e}"

    output = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
    summary = _parse_image_batch_summary(output)
    style_dir = summary.get("style") or _IMAGE_STYLE_DIRS.get(style, "风格")

    def _to_int(v, default=0):
        try:
            return int(str(v))
        except Exception:  # noqa: BLE001
            return default

    total = _to_int(summary.get("total"))
    planned = _to_int(summary.get("planned"))
    processed = _to_int(summary.get("processed"))
    failed = _to_int(summary.get("failed"))
    skipped = _to_int(summary.get("skipped"))

    if p.returncode != 0 and processed <= 0:
        tail = "；".join([x.strip() for x in output.splitlines()[-3:] if x.strip()])
        if "Permission denied" in output or "PermissionError" in output:
            return "滤镜处理失败：目录无写入权限，请先修复 NAS 目录归属后重试。"
        return f"滤镜处理失败：{(tail or '未知错误')[:120]}"

    if dry:
        return (
            f"预览完成：{target}共{total}张，计划处理{planned}张，"
            f"输出到各原目录/{style_dir}/，未写入文件。"
        )

    if total == 0:
        return f"{target}目录下未发现可处理图片。"
    if processed == 0 and skipped > 0:
        return f"{target}{style_dir}已是最新，跳过{skipped}张，无需重复处理。"
    reply = f"{target}{style_dir}处理完成：成功{processed}张"
    if skipped > 0:
        reply += f"，跳过{skipped}张"
    if failed > 0:
        reply += f"，失败{failed}张"
    return reply + f"，输出到各原目录/{style_dir}/。"


def _consume_pending_image_filter(text: str, pending: dict | None):
    if not pending:
        return None, pending

    target = pending.get("target")
    style = pending.get("style")
    if target is None:
        target = _detect_image_filter_target(text)
    if style is None:
        style = _detect_image_style(text)

    if target is not None and style is not None:
        return _run_image_batch_reply(target, style, bool(pending.get("dry"))), None

    attempts = int(pending.get("attempts", 0)) + 1
    if attempts >= _IMAGE_FILTER_PENDING_MAX_ATTEMPTS:
        return "我还是没听清，请重新说完整指令。", None

    pending["target"] = target
    pending["style"] = style
    pending["attempts"] = attempts
    if target is None:
        return _IMAGE_FILTER_TARGET_PROMPT, pending
    return "我没听清风格，请说复古、日系或胶片。", pending


def _detect_image_style(user_text: str):
    for k in sorted(_IMAGE_STYLE_ALIASES.keys(), key=len, reverse=True):
        if k in user_text:
            return _IMAGE_STYLE_ALIASES[k]
    return None


def _parse_image_batch_summary(output: str):
    m = re.search(r"\[SUMMARY\]\s+(.*)", output)
    if not m:
        return {}
    summary = {}
    for token in m.group(1).split():
        if "=" not in token:
            continue
        k, v = token.split("=", 1)
        summary[k.strip()] = v.strip()
    return summary


def _fast_local_image_filter_reply(_args, user_text: str):
    """命中图片风格化指令时，直跑本地脚本，避免走 agent 长链路。"""
    req = _extract_image_filter_request(user_text)
    if req is None:
        return None

    if req["target"] is None:
        return _IMAGE_FILTER_TARGET_PROMPT

    if req["style"] is None:
        return _IMAGE_FILTER_STYLE_PROMPT
    return _run_image_batch_reply(req["target"], req["style"], bool(req["dry"]))


# 口语/别名 → 库中媒体名（ASR 常把英文媒体名识别成中文口语）
_PLAY_ALIASES = {
    "兔子": "Big_Buck_Bunny",
    "bunny": "Big_Buck_Bunny",
    "大兔": "Big_Buck_Bunny",
    "大兔子": "Big_Buck_Bunny",
    "bbb": "Big_Buck_Bunny",
    "bb": "Big_Buck_Bunny",
    "预告片": "Sintel",
    "sintel": "Sintel",
}

_DOWNLOAD_MEDIA_LIBRARY = {
    "海洋": {
        "url": "https://vjs.zencdn.net/v/oceans.mp4",
        "default_folder": "视频",
    },
    "大海": {
        "url": "https://vjs.zencdn.net/v/oceans.mp4",
        "default_folder": "视频",
    },
    "预告片": {
        "url": "https://media.w3.org/2010/05/sintel/trailer.mp4",
        "default_folder": "家庭影院/电影",
    },
    "sintel": {
        "url": "https://media.w3.org/2010/05/sintel/trailer.mp4",
        "default_folder": "家庭影院/电影",
    },
    "兔子": {
        "url": "https://www.w3schools.com/html/mov_bbb.mp4",
        "default_folder": "家庭影院/电影",
    },
    "bunny": {
        "url": "https://www.w3schools.com/html/mov_bbb.mp4",
        "default_folder": "家庭影院/电影",
    },
    "样本": {
        "url": "https://www.w3schools.com/html/mov_bbb.mp4",
        "default_folder": "视频",
    },
    "测试": {
        "url": "https://vjs.zencdn.net/v/oceans.mp4",
        "default_folder": "视频",
    },
    "测试视频": {
        "url": "https://vjs.zencdn.net/v/oceans.mp4",
        "default_folder": "视频",
    },
}


def _fast_local_download_reply(args, user_text: str):
    """命中下载指令时，直连 media_downloader API，避免走 agent 长链路。"""
    short_dl = bool(re.search(r"(^|帮我|给我|请)下(测试视频|测试|样本|海洋|大海|预告片|兔子|sintel|bunny)", user_text))
    if ("下载" not in user_text) and (not short_dl):
        return None
    if "下载的" in user_text and not user_text.strip().startswith("下载") and "帮我下载" not in user_text:
        return None

    matched = None
    matched_key = ""
    for key in sorted(_DOWNLOAD_MEDIA_LIBRARY.keys(), key=len, reverse=True):
        if key in user_text:
            matched = _DOWNLOAD_MEDIA_LIBRARY[key]
            matched_key = key
            break

    term = user_text
    for w in (
        "帮我", "请帮", "请", "帮", "给我",
        "下载", "搜索", "查找", "找",
        "播放", "放一下", "放出来", "看一下", "看看", "并播放", "并且播放",
        "视频", "电影", "影片", "纪录片", "一下", "一部", "一个",
    ):
        term = term.replace(w, "")
    term = term.replace("并且", "").replace("并", "")
    term = term.strip()

    if not matched and not term:
        return None

    target_subdir = "视频"
    if matched:
        target_subdir = matched.get("default_folder", "视频")
    elif any(k in user_text for k in ("电影", "预告片", "剧集", "电视剧")):
        target_subdir = "家庭影院/电影"

    import json as _json
    import urllib.request as _ur

    api_url = (os.getenv("DOWNLOAD_API_URL") or "http://127.0.0.1:28081/download").strip()
    payload = {
        "url": matched.get("url", "") if matched else "",
        "query": "" if matched else term,
        "target_subdir": target_subdir,
        "notify_tts": False,
        "tts_message": "下载已完成",
    }

    try:
        req = _ur.Request(
            api_url,
            data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with _ur.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            data = _json.loads(raw) if raw else {}
            if not (200 <= resp.status < 300) or not data.get("ok"):
                print(f"[DL] local download not ok status={resp.status} body={raw[:240]}")
                return _download_failed_play_test_video(args)

        files = data.get("files") if isinstance(data.get("files"), list) else []
        safe_subdir = str(data.get("safe_subdir") or target_subdir)
        if files:
            first_name = Path(files[0]).name
            print(f"[DL] local download success key={matched_key or '(query)'} file={first_name}")
            return f"下载已完成，文件名{first_name}，已保存到{safe_subdir}。"
        print(f"[DL] local download success key={matched_key or '(query)'}")
        return f"下载已完成，已保存到{safe_subdir}。"
    except Exception as e:  # noqa: BLE001
        print(f"[DL] local download failed, default to test video: {e}")
        return _download_failed_play_test_video(args)


def _open_jellyfin_in_firefox(jellyfin_url: str, item_id: str | None = None) -> bool:
    """在 Firefox 中打开（并前置）Jellyfin 条目页，用于建立可控播放会话。"""
    target_url = _jellyfin_target_url(jellyfin_url, item_id=item_id)
    env = _build_firefox_desktop_env()

    try:
        subprocess.Popen(
            ["firefox", "--new-tab", target_url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        print(f"[Jellyfin] firefox opened: {target_url}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[Jellyfin] firefox open failed: {e}")
        return False


def _jellyfin_target_url(jellyfin_url: str, item_id: str | None = None) -> str:
    import urllib.parse as _up

    base = jellyfin_url.rstrip("/")
    if item_id:
        return f"{base}/web/index.html#!/details?id={_up.quote(item_id)}"
    return f"{base}/web/index.html"


def _build_firefox_desktop_env() -> dict[str, str]:
    import glob

    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    runtime_dir = env.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        candidate = f"/run/user/{os.getuid()}"
        if os.path.isdir(candidate):
            env["XDG_RUNTIME_DIR"] = candidate
            runtime_dir = candidate

    # systemd 服务进程通常没有 XAUTHORITY；不补齐会出现
    # "cannot open display: :0"，导致日志显示已打开但实际未打开。
    if not env.get("XAUTHORITY") and runtime_dir:
        xauth_candidates = sorted(glob.glob(os.path.join(runtime_dir, ".mutter-Xwaylandauth.*")))
        xauth_candidates.append(os.path.join(os.path.expanduser("~"), ".Xauthority"))
        for xauth_path in xauth_candidates:
            if os.path.isfile(xauth_path):
                env["XAUTHORITY"] = xauth_path
                break

    # Wayland 会话下 Firefox 以原生 Wayland 窗口运行，xdotool（仅 X11）看不到它。
    # 补齐后，`firefox --new-tab` 才能通过 D-Bus 复用已运行实例并前置窗口。
    if runtime_dir:
        env.setdefault("WAYLAND_DISPLAY", "wayland-0")
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")

    return env


def _focus_firefox_for_jellyfin(
    jellyfin_url: str,
    item_id: str | None = None,
    *,
    open_if_missing: bool = True,
) -> bool:
    env = _build_firefox_desktop_env()

    try:
        check_cmd_exists("xdotool")
    except Exception:
        if open_if_missing:
            print("[Jellyfin] xdotool not found, fallback opening tab")
            return _open_jellyfin_in_firefox(jellyfin_url, item_id=item_id)
        print("[Jellyfin] xdotool not found, skip opening new tab")
        return False

    try:
        res = subprocess.run(
            ["xdotool", "search", "--class", "firefox"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        windows = [w.strip() for w in res.stdout.splitlines() if w.strip()]
        if not windows:
            # 原生 Wayland 的 Firefox 没有 X11 窗口，xdotool search 找不到；
            # 仅在允许打开时才复用/开启 Jellyfin，避免重复开标签页。
            if open_if_missing:
                print("[Jellyfin] firefox window not found (xdotool), opening via firefox")
                return _open_jellyfin_in_firefox(jellyfin_url, item_id=item_id)
            print("[Jellyfin] firefox window not found (xdotool), skip opening new tab")
            return False

        subprocess.run(
            ["xdotool", "windowactivate", "--sync", windows[-1]],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            check=False,
        )

        active_title = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        ).stdout.strip()

        if "Jellyfin" in active_title:
            print(f"[Jellyfin] firefox focused: {active_title}")
            return True

        if open_if_missing:
            print("[Jellyfin] active window is not Jellyfin, ensuring tab")
            return _open_jellyfin_in_firefox(jellyfin_url, item_id=item_id)
        print("[Jellyfin] active window is not Jellyfin, skip opening new tab")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[Jellyfin] firefox focus failed: {e}")
        if open_if_missing:
            return _open_jellyfin_in_firefox(jellyfin_url, item_id=item_id)
        return False


def _fast_local_play_reply(args, user_text: str):
    """命中明确播放指令时，直接调 Jellyfin API，返回回复文本。

    返回 None 表示不命中，交给 agent 正常处理。
    """
    short_dl = bool(re.search(r"(^|帮我|给我|请)下(测试视频|测试|样本|海洋|大海|预告片|兔子|sintel|bunny)", user_text))
    if ("下载" in user_text and "下载的" not in user_text) or short_dl:
        return None

    if not args.jellyfin_api_key:
        return None

    # 1. 检测播放意图
    #    显式播放词：含下列任意一个即命中
    explicit_play = ("播放", "放一下", "放出来", "放视频", "放电影", "放个", "放部",
                     "看一下", "看看", "看视频", "看电影", "看个", "看部", "看",
                     "一下", "一部", "一个", "给我放",
                     "视频", "电影", "影片", "片子")
    #    口语 "放" 需搭配媒体词才算播放意图
    media_words = ("视频", "电影", "影片", "片子", "片", "纪录片", "预告片", "剧集", "短片", "动画")

    has_play = any(k in user_text for k in explicit_play)
    if not has_play:
        # 口语"放 + 内容名"（排除 放弃/放大/放小/放下/放手/放心/放松/放开 等非播放义）
        import re as _re
        if _re.search(r'放(?!弃|大|小|下|手|心|松|开)', user_text):
            has_play = True
    if not has_play:
        return None

    # 2. 剥离命令词，提取内容关键词
    term = user_text
    for w in ("帮我", "请帮", "请", "帮", "给我",
              "下载", "搜索", "查找", "找",
              "播放", "放一下", "放出来", "放视频", "放电影", "放个", "放部", "放",
              "看一下", "看看", "看视频", "看电影", "看个", "看部", "看",
              "一下", "一部", "一个", "给我放",
              "视频", "电影", "影片", "片子"):
        term = term.replace(w, "")
    term = re.sub(r"(并且播放|并播放|然后播放)$", "", term)
    term = term.replace("并且", "").replace("并", "")
    term = term.strip()
    is_generic = not term   # 泛称"放视频"，未指定具体内容
    if is_generic:
        term = "测试视频"
        is_generic = False
        print("[Jellyfin] generic play request, fallback term='测试视频'")

    import json as _json
    import urllib.parse as _up
    import urllib.request as _ur

    base = args.jellyfin_url.rstrip("/")
    hdrs_json = {"X-MediaBrowser-Token": args.jellyfin_api_key, "Content-Type": "application/json"}
    hdrs_get  = {"X-MediaBrowser-Token": args.jellyfin_api_key}

    def _req(method, path, body=None, timeout=8):
        url  = base + path
        data = _json.dumps(body).encode() if body is not None else None
        req  = _ur.Request(url, data=data, method=method,
                           headers=hdrs_json if data else hdrs_get)
        with _ur.urlopen(req, timeout=timeout) as r:
            content = r.read()
            return _json.loads(content) if content.strip() else {}

    def _fetch_library(limit=50):
        """拉取库中全部可播视频，供泛称/模糊匹配使用。"""
        try:
            result = _req(
                "GET",
                f"/Items?IncludeItemTypes=Movie,Video&Recursive=true&Limit={limit}",
            )
            return result.get("Items", [])
        except Exception as e:
            print(f"[Jellyfin] library fetch failed: {e}")
            return []

    # 3. 确定播放目标
    item_id = item_name = None

    if is_generic:
        # 泛称"放视频"：列出库中视频让用户选择
        lib_items = _fetch_library()
        if not lib_items:
            return "Jellyfin 库中暂无视频，可先下载内容。"
        if len(lib_items) > 1:
            names = "、".join(i["Name"] for i in lib_items[:4])
            return f"库中有：{names}，请说具体片名。"
        item_id, item_name = lib_items[0]["Id"], lib_items[0]["Name"]
    else:
        # 指定片名：先精确搜索
        try:
            result = _req(
                "GET",
                f"/Items?IncludeItemTypes=Movie,Video&Recursive=true&Limit=5"
                f"&searchTerm={_up.quote(term)}",
            )
            items = result.get("Items", [])
        except Exception as e:
            print(f"[Jellyfin] search failed: {e}")
            return None  # 网络/认证异常才降级 agent

        if not items and term in _PLAY_ALIASES:
            # 口语别名兜底：如"兔子"→ Big_Buck_Bunny
            alias = _PLAY_ALIASES[term]
            print(f"[Jellyfin] alias: {term!r} -> {alias!r}")
            try:
                result = _req(
                    "GET",
                    f"/Items?IncludeItemTypes=Movie,Video&Recursive=true&Limit=5"
                    f"&searchTerm={_up.quote(alias)}",
                )
                items = result.get("Items", [])
            except Exception as e:
                print(f"[Jellyfin] alias search failed: {e}")

        if items:
            item_id, item_name = items[0]["Id"], items[0]["Name"]
        else:
            # 模糊匹配兜底（ASR 轻微误识别）
            lib_items = _fetch_library()
            if lib_items:
                import difflib
                best = max(
                    lib_items,
                    key=lambda i: difflib.SequenceMatcher(None, term, i["Name"]).ratio(),
                )
                ratio = difflib.SequenceMatcher(None, term, best["Name"]).ratio()
                if ratio >= 0.45:
                    item_id, item_name = best["Id"], best["Name"]
                    print(f"[Jellyfin] fuzzy: {term!r} -> {item_name} (ratio={ratio:.2f})")
                else:
                    names = "、".join(i["Name"] for i in lib_items[:4])
                    return f"没找到{term}，库中有：{names}，请说具体片名。"
            else:
                return f"没找到{term}，Jellyfin 库中暂无视频。"

    # 4. 发送播放指令
    print(f"[Jellyfin] play target: {item_name} (id={item_id})")
    try:
        sessions = _req("GET", "/Sessions")
        candidates = [
            s for s in sessions
            if "Video" in s.get("Capabilities", {}).get("PlayableMediaTypes", [])
        ]
    except Exception as e:
        print(f"[Jellyfin] sessions failed: {e}")
        return f"请在 Jellyfin 手动播放：{item_name}"

    if not candidates:
        print("[Jellyfin] no controllable session found")
        opened = _focus_firefox_for_jellyfin(args.jellyfin_url, item_id=item_id)
        if opened:
            import time as _time

            for attempt in range(5):
                _time.sleep(2)
                try:
                    sessions = _req("GET", "/Sessions")
                    candidates = [
                        s for s in sessions
                        if "Video" in s.get("Capabilities", {}).get("PlayableMediaTypes", [])
                    ]
                    if candidates:
                        print(f"[Jellyfin] session detected after firefox open: attempt={attempt + 1}")
                        break
                except Exception as e:
                    print(f"[Jellyfin] sessions retry failed: {e}")
                    break

        if not candidates:
            if opened:
                return f"已打开 Jellyfin，正在进入：{item_name}"
            return f"请先打开 Jellyfin 网页，再说播放。"

    # 已有会话时也要主动前置窗口，避免后台播放看不到界面
    _focus_firefox_for_jellyfin(
        args.jellyfin_url,
        item_id=item_id,
        open_if_missing=False,
    )

    sid = candidates[0]["Id"]
    try:
        _req("POST",
             f"/Sessions/{sid}/Playing"
             f"?ItemIds={_up.quote(item_id)}&PlayCommand=PlayNow")
        print(f"[Jellyfin] play sent: {item_name} -> session {sid}")
        return f"正在播放：{item_name}"
    except Exception as e:
        print(f"[Jellyfin] play failed: {e}")
        return f"播放失败，请在 Jellyfin 手动播放：{item_name}"


def _download_failed_play_test_video(args):
    """下载失败时的默认兜底：直接播放 Jellyfin 库中已有的测试视频。

    返回兜底回复文本；优先走播放通道，播放不可用时回退为文字提示。
    """
    if getattr(args, "jellyfin_api_key", None):
        try:
            play_reply = _fast_local_play_reply(args, "播放测试视频")
            if play_reply:
                print(f"[DL] download failed, default to playing test video: {play_reply}")
                return play_reply
        except Exception as e:  # noqa: BLE001
            print(f"[DL] play test video on download fail error: {e}")
    return "下载暂时不可用，测试视频已存在，可在 Jellyfin 打开观看。"


def _fast_local_download_reply_with_args(args, user_text: str):
    return _fast_local_download_reply(args, user_text)


# KB 快速通道关键词：必须包含"查询意图词"之一
_KB_INTENT_WORDS = re.compile(
    r"在哪|哪里|哪个|找|查找|搜索|搜|是什么|有没有|有哪些|多少|内容|写着|写了"
)
# 同时包含"对象词"之一才命中
_KB_OBJECT_WORDS = re.compile(
    r"文件|文档|合同|报告|表格|表|记录|照片|图片|相册|视频|电影|音乐|资料|方案|说明|计划|协议"
)
_KB_API_URL = os.getenv("KB_API_URL") or "http://127.0.0.1:28084"

_KB_EXT_SPOKEN_MAP = {
    ".pdf": "PDF文件",
    ".doc": "Word文档",
    ".docx": "Word文档",
    ".xls": "Excel表格",
    ".xlsx": "Excel表格",
    ".txt": "文本文件",
    ".md": "文档",
    ".csv": "表格文件",
    ".jpg": "图片",
    ".jpeg": "图片",
    ".png": "图片",
    ".webp": "图片",
    ".gif": "图片",
    ".mp4": "视频",
    ".mkv": "视频",
    ".avi": "视频",
    ".mov": "视频",
    ".mp3": "音频",
    ".wav": "音频",
}


def _kb_path_to_spoken(path: str):
    """把路径转成便于 TTS 播报的口语描述。"""
    raw = (path or "").strip()
    if not raw:
        return "该文件", ""

    parts = [p for p in raw.replace("\\", "/").split("/") if p]
    if parts and parts[0].lower().replace("-", "_") in {"nas_share", "nasshare", "nas", "share"}:
        parts = parts[1:]
    if not parts:
        return "该文件", ""

    filename = parts[-1].replace("_", "")
    folders = [p.replace("_", "") for p in parts[:-1]]
    stem, ext = os.path.splitext(filename)
    ext_spoken = _KB_EXT_SPOKEN_MAP.get(ext.lower())
    file_spoken = (f"{stem}{ext_spoken}" if ext_spoken and stem else (ext_spoken or stem or filename)).strip()

    if not folders:
        return file_spoken or "该文件", ""
    if len(folders) == 1:
        return file_spoken or "该文件", f"在{folders[0]}文件夹里"
    return file_spoken or "该文件", f"在{folders[-2]}的{folders[-1]}文件夹里"


def _kb_snippet_to_spoken(snippet: str, max_chars: int = 60) -> str:
    """把 KB snippet 清理成适合 TTS 的口语片段。"""
    s = (snippet or "").replace("→", "").replace("←", "").replace("...", "，")
    s = re.sub(r"\s+", " ", s).strip(" ，。；、")
    if not s:
        return ""
    if len(s) > max_chars:
        s = s[:max_chars]
    s = re.sub(r"[(（][^()（）]*$", "", s)  # 去掉截断后不完整的括号
    return s.strip(" ，。；、")


def _format_kb_spoken_reply(results):
    total = len(results)
    first = results[0] if results else {}
    first_path = first.get("path", "")
    file_spoken, loc_spoken = _kb_path_to_spoken(first_path)
    content_spoken = _kb_snippet_to_spoken(first.get("snippet", ""))

    if content_spoken:
        # 命中内容时优先回答内容，再补位置（loc_spoken 形如"在xx文件夹里"）
        head = f"文档里写着：{content_spoken}。"
        if loc_spoken:
            head += f"它{loc_spoken}。"
        return head

    if total <= 1:
        if loc_spoken:
            return f"找到了，{file_spoken}，{loc_spoken}。"
        return f"找到了，{file_spoken}。"

    if loc_spoken:
        return f"找到{total}条，第一个是{file_spoken}，{loc_spoken}。"
    return f"找到{total}条，第一个是{file_spoken}。"


def _fast_local_kb_reply(_args, user_text: str):
    """命中知识库查询时，直连 KB API 搜索，避免走 agent 长链路。"""
    if not (_KB_INTENT_WORDS.search(user_text) and _KB_OBJECT_WORDS.search(user_text)):
        return None
    # 下载/播放意图优先，本通道不抢
    if "下载" in user_text or "播放" in user_text or "放一下" in user_text:
        return None

    import json as _json
    import urllib.request as _ur

    try:
        req = _ur.Request(
            f"{_KB_API_URL}/search",
            data=_json.dumps({"query": user_text}, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with _ur.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read())
    except Exception as e:
        print(f"[KB] api error: {e}")
        return None

    results = data.get("results", [])
    if not results:
        return f'知识库中未找到与"{user_text}"相关的内容'

    return _format_kb_spoken_reply(results)


# ── Immich 语义相册搜索快通道 ─────────────────────────────────────────────────
_IMMICH_API_URL = os.getenv("IMMICH_API_URL", "http://127.0.0.1:2283").rstrip("/")
_IMMICH_API_KEY = os.getenv("IMMICH_API_KEY") or ""

_IMMICH_INTENT_RE = re.compile(r"找|搜|查找|搜索|有哪些")
_IMMICH_OBJECT_RE = re.compile(r"照片|图片|相册")
# 位置/管理类意图不走语义搜索
_IMMICH_EXCLUDE_RE = re.compile(
    r"在哪|哪里|哪个|位置|文件夹|分类|归档|整理|下载|播放|删除|移动|滤镜|处理|备份"
)
_IMMICH_STRIP_RE = re.compile(
    r"帮我|请|找|查找|搜索|搜|所有|全部|有的|有|照片|图片|相册|包含|带|的|里|中"
)


def _immich_item_to_spoken(item: dict, idx: int) -> str:
    """把 Immich 结果项转成口语描述：中文文件名读名字，否则读所在分类目录。"""
    stem = os.path.splitext(item.get("originalFileName", "") or "")[0]
    stem = re.sub(r"\[[^\]]*\]", "", stem)  # 去掉 [id]
    stem = re.sub(r"^\d{8}_\d{6}_", "", stem)  # 去掉归档时间戳前缀
    stem = re.sub(r"^\S+?_(?=[一-鿿])", "", stem)  # 去掉归档类别前缀（如 风景_）
    stem = re.sub(r"[_\-. ]+", "", stem).strip()
    if len(re.findall(r"[一-鿿]", stem)) >= 2:
        return stem[:10]
    path = item.get("originalPath", "") or ""
    for p in reversed([x for x in path.split("/") if x][:-1]):
        if re.search(r"[一-鿿]", p):
            return f"{p}里的第{idx}张"
    return f"第{idx}张"


def _fast_local_immich_reply(_args, user_text: str):
    """语义找照片：直连 Immich smart search，秒回，不经 agent 长链路。"""
    if not (_IMMICH_INTENT_RE.search(user_text) and _IMMICH_OBJECT_RE.search(user_text)):
        return None
    if _IMMICH_EXCLUDE_RE.search(user_text):
        return None
    semantic = _IMMICH_STRIP_RE.sub("", user_text)
    semantic = semantic.strip("，。！？,.!?；;：: ")
    if len(semantic) < 2:
        return None

    import json as _json
    import urllib.request as _ur

    try:
        req = _ur.Request(
            f"{_IMMICH_API_URL}/api/search/smart",
            data=_json.dumps({"query": semantic, "size": 5}, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "x-api-key": _IMMICH_API_KEY,
            },
        )
        with _ur.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read())
    except Exception as e:
        print(f"[Immich] api error: {e}")
        return None

    items = (data.get("assets") or {}).get("items") or []
    items = [it for it in items if (it.get("type") or "").upper() == "IMAGE"]
    if not items:
        return f"相册里没有找到和{semantic}相关的照片。"
    names = [_immich_item_to_spoken(it, i + 1) for i, it in enumerate(items[:3])]
    return f"找到{len(items)}张和{semantic}相关的照片，比如：{'、'.join(names)}。"


# ── 危险指令拦截 ─────────────────────────────────────────────────────────────
_DANGER_WORDS_RE = re.compile(
    r'删除|清空|清除|移走|移除|抹去|格式化|覆盖|全部删|批量删|删光|删掉|删了|删除文件|删除照片'
)
_DANGER_SAFE_RE = re.compile(r'不要删|不能删|别删|防止删|禁止删')
_DANGER_CONFIRM_RE = re.compile(r'确认|执行|是的|对的|没错|好的')


def _is_dangerous_command(text: str) -> bool:
    """含破坏性词汇且无负向安全词时判定为危险指令。"""
    if _DANGER_SAFE_RE.search(text):
        return False
    return bool(_DANGER_WORDS_RE.search(text))


LOCAL_FAST_CHANNELS = (
    ("filter", _fast_local_image_filter_reply),
    ("classify", _fast_local_classify_reply),
    ("download", _fast_local_download_reply_with_args),
    ("play", _fast_local_play_reply),
    ("immich", _fast_local_immich_reply),
    ("kb", _fast_local_kb_reply),
)


def _run_local_fast_channels(args, user_text: str):
    for name, handler in LOCAL_FAST_CHANNELS:
        try:
            reply = handler(args, user_text)
        except Exception as e:  # noqa: BLE001
            print(f"[FAST] channel={name} error: {e}")
            continue
        if reply is not None:
            print(f"[FAST] hit channel={name}")
            return reply
    return None


def ask_openclaw(args, user_text):
    if args.openclaw_dry_run:
        return f"[dry-run] 你说的是：{user_text}"

    # ── 危险指令二次确认门 ──
    pending_danger = _HTTP_DIALOG_STATE.get("pending_danger_confirm")
    _danger_confirmed = False
    if pending_danger is not None:
        _HTTP_DIALOG_STATE["pending_danger_confirm"] = None
        if _DANGER_CONFIRM_RE.search(user_text):
            user_text = pending_danger["original_text"]
            _danger_confirmed = True  # 已确认，跳过二次拦截
            print(f"[DANGER] user confirmed, executing: {user_text!r}")
        else:
            print(f"[DANGER] user cancelled or unrelated: {user_text!r}")
            return "操作已取消。"

    fast_reply = _run_local_fast_channels(args, user_text)
    if fast_reply is not None:
        return fast_reply

    # 即将调用 agent：对危险指令加一道确认拦截（已确认的跳过）
    if not _danger_confirmed and _is_dangerous_command(user_text):
        _HTTP_DIALOG_STATE["pending_danger_confirm"] = {"original_text": user_text}
        print(f"[DANGER] intercepted for confirmation: {user_text!r}")
        return f"你说的是\"{ user_text[:24] }\"，这是危险操作，请再说\"确认\"来执行，或说\"取消\"放弃。"

    bridge_prompt = (
        "你是quectel pi上的对话助手，执行用户口头指令，可调用已有工具（文件/NAS/相册等）。"
        "直接执行给结果，回复简短中文，不超过20字，一句说完，纯文字，禁止markdown（**加粗**、-列表、#标题）。\n"
        "【执行规则】\n"
        "1.下载视频/音频：必须调用 download_media 工具，禁止编造结果。关键词（默认保存位置）："
        "海洋/大海→海洋纪录片(视频)、预告片/sintel→Sintel预告片(电影)、兔子/bunny→Big Buck Bunny(电影)、"
        "样本/测试→通用样本(视频)；用户给URL用url参数，说搜索影视名用query参数(yt-dlp)。\n"
        "2.播放库中视频：先尝试Jellyfin播放；无法播放则回复'请在Jellyfin打开'并列出可播放列表，禁止直接说无法播放。\n"
        "3.操作NAS文件：必须用 nas_files 工具(list_directory/move_file/create_directory)，禁止猜测或编造路径。"
        "根目录(/nas_share)可用目录：备份、家庭相册、工作文档、手机相册、旅行。\n"
        "4.照片按内容分类/归档/整理：禁止仅凭文件名猜测，优先 exec 脚本一次性归档："
        "timeout 120 python3 /nas_share/tools/nas_classify.py --dir <目录> --recursive --archive。"
        "默认直接执行，只有用户说'预览/先别动'才干跑；重复前缀文件名自动清理重命名无需询问；脚本失败才回退 nas_files。\n"
        "5.图片复古/日系/胶片滤镜：优先 exec 脚本批处理："
        "timeout 600 python3 /nas_share/tools/image_batch.py --dir <目录> --style <vintage|japanese|film> --recursive。"
        "用户说预览加 --dry-run；输出到<风格名>/子目录，禁止覆盖原图。\n"
        f"用户指令：{user_text}"
    )

    cmd = [
        "docker",
        "exec",
        args.openclaw_container,
        "node",
        "dist/index.js",
        "agent",
        "--session-key",
        f"voice-turn:{int(time.time() * 1000)}",  # 每轮独立会话，防止跨轮上下文误确认
        "--thinking", "minimal",  # 降低推理深度，减少首响应延迟
        "--message",
        bridge_prompt,
        "--json",
    ]

    ok, output = _run_agent_cmd(cmd, timeout_sec=args.openclaw_timeout)
    if not ok:
        low = output.lower()
        if "sudo: a password is required" in low:
            return (
                "OpenClaw调用失败：当前进程没有免密Docker权限。"
                "请先执行 sudo usermod -aG docker pi 并重新登录后重试。"
            )
        if "permission denied while trying to connect to the docker daemon socket" in low:
            return (
                "OpenClaw调用失败：当前用户无Docker权限。"
                "请先执行 sudo usermod -aG docker pi 并重新登录后重试。"
            )
        return f"OpenClaw调用失败：{output}"

    obj = _json_from_mixed_output(output)
    if obj is None:
        return output.strip()[:200] if output.strip() else "OpenClaw未返回可解析内容"

    text = _extract_text_from_agent_json(obj)
    if text:
        return text
    return "OpenClaw返回为空"


_IMPORTANT_REPLY_HINTS = (
    "失败",
    "错误",
    "警告",
    "风险",
    "确认",
    "不可",
    "无法",
    "删除",
    "覆盖",
    "文档里写着",  # KB 内容问答的答案不可被短指令压缩截断
)

_SHORT_CMD_HINTS = (
    "打开",
    "关闭",
    "开始",
    "停止",
    "退出",
    "查询",
    "看",
    "播放",
    "暂停",
    "分类",
    "整理",
    "移动",
    "重命名",
)

_SHORT_MEDIA_PHRASES = {
    "测": "播放测试视频",
    "试": "播放测试视频",
    "测试": "播放测试视频",
    "下测": "下载测试视频",
    "下试": "下载测试视频",
    "下测试": "下载测试视频",
    "帮我下测": "帮我下载测试视频",
    "帮我下试": "帮我下载测试视频",
    "帮我下测试": "帮我下载测试视频",
    "给我下测": "给我下载测试视频",
    "给我下试": "给我下载测试视频",
    "给我下测试": "给我下载测试视频",
}


def _expand_short_media_phrase(text: str) -> str:
    """将高频短口令补全为可执行指令，避免被短句过滤误跳过。"""
    compact = re.sub(r"\s+", "", (text or "").strip())
    compact = compact.strip("，。！？,.!?；;：:")
    if compact in _SHORT_MEDIA_PHRASES:
        return _SHORT_MEDIA_PHRASES[compact]
    return text


_HTTP_INCOMPLETE_PROMPT = "我这边听到你还没说完，请继续说。"
_HTTP_INCOMPLETE_MAX_FOLLOWUPS = 2
# 助手的澄清问句（如"想下载哪个视频？"）后，唤醒通道免唤醒继续收听的次数上限
_HTTP_QUESTION_MAX_FOLLOWUPS = 2
_INCOMPLETE_ENDINGS = (
    "的", "了", "下", "把", "给", "对", "并", "然后", "进行", "处理", "操作", "一下",
)
_INCOMPLETE_ACTION_WORDS = (
    "分类", "归档", "整理", "移动", "重命名", "删除", "复制", "备份",
    "下载", "上传", "创建", "新建", "同步",
    "播放", "放", "暂停", "查询", "查找", "搜索", "查", "找", "看看",
    "处理", "滤镜", "风格", "调色", "复古", "日系", "胶片",
    "显示", "列出", "打开", "关闭",
)
_INCOMPLETE_TARGET_WORDS = (
    # 目标对象
    "照片", "图片", "相册", "文件", "目录", "文件夹",
    "视频", "音乐", "电影", "预告片", "纪录片", "剧集", "电视剧",
    "文档", "报告",
    # 知识库相关对象词（知识库查询触发需要）
    "合同", "方案", "协议", "资料", "预算", "表格", "记录", "说明", "计划",
    # NAS 路径/业务词
    "家庭", "手机", "旅行", "家庭相册", "手机相册",
    "nas_share", "NAS",
)

# 疑问句本身即完整意图（如"我的住房合同在哪"、"有哪些文档"），
# 与 KB 快速通道的意图词保持一致，不应判为"没说完"而追问用户
_COMPLETE_QUESTION_RE = re.compile(
    r"在哪|哪里|哪儿|哪个|是谁|是什么|什么意思|有没有|有哪些|有多少|多少|怎么办|怎么样|(吗|呢|什么)$"
)


def _looks_like_incomplete_command(text: str) -> bool:
    s = re.sub(r"\s+", "", (text or "").strip())
    s = s.strip("，。！？,.!?；;：:")
    if not s:
        return False
    if len(s) <= 2:
        return True
    if _COMPLETE_QUESTION_RE.search(s):
        return False

    has_target = any(k in s for k in _INCOMPLETE_TARGET_WORDS)
    has_action = any(k in s for k in _INCOMPLETE_ACTION_WORDS)
    # 同时含目标词+动作词 = 完整指令，优先放行（如"帮我把家庭相册下的照片进行分类"），
    # 避免被下面的"把字句正则"或结尾词误判为没说完而追问用户。
    if has_target and has_action:
        return False
    if s.endswith(_INCOMPLETE_ENDINGS):
        return True
    if re.match(r"^(帮我|请|给我)?把.{0,12}$", s):
        return True
    if has_target and not has_action:
        return True
    return False


# 助手回复若是澄清问句（需要用户补充信息），唤醒通道应免唤醒继续收听答案。
# 限定：以疑问结尾 + 含疑问词，避免把"好的，正在处理"这类陈述误判为问句。
_QUESTION_ENDINGS = ("？", "?", "吗", "呢")
_QUESTION_WORDS = (
    "哪个", "什么", "怎么", "哪里", "哪儿", "哪些", "哪部", "哪种",
    "还是", "是否", "能不能", "要不要", "需要吗", "多少", "几位", "哪位",
    "需要", "帮你", "帮我",
)


def _reply_is_clarifying_question(reply: str) -> bool:
    s = (reply or "").strip()
    if not s or len(s) > 24:
        return False
    if not s.endswith(_QUESTION_ENDINGS):
        return False
    return any(w in s for w in _QUESTION_WORDS)


# ── 无关语音过滤 ─────────────────────────────────────────────────────────────
# Level 1: 纯语气词 / 噪音
_IRRELEVANT_EXACT = frozenset({
    "嗯", "啊", "哦", "呢", "哈", "呀", "唉", "哎", "噢", "哟", "喔", "呵", "嘿", "哼",
    "嗯嗯", "嗯哼", "哈哈", "啊啊", "哦哦", "呀呀",
})
_IRRELEVANT_RE = re.compile(r'^[嗯啊哦呢哈呀唉哎噢哟喔呵嘿哼]{1,5}$')

# Level 2: 社交/确认/客套用语 —— 无任务意图，直接跳过
_SOCIAL_IRRELEVANT = frozenset({
    "谢谢", "谢谢你", "谢谢了", "谢谢啊", "谢谢哈", "多谢", "感谢", "不用谢",
    "好的", "好吧", "好呢", "好嘞", "行", "行吧", "行的", "可以", "没问题",
    "知道了", "知道", "明白了", "明白", "懂了", "收到", "了解",
    "对", "对的", "对对", "是的", "是", "没错", "正确",
    "不用了", "算了", "不用",
    "再见", "拜拜",
    "没有", "没", "没事", "没关系",
    "等等", "等一下", "稍等",
    "哇", "哇哦", "厉害", "厉害了", "好厉害", "太棒了", "太好了", "真棒",
})

# Level 3: 任务关键词白名单 —— 含任意一个则视为有任务意图，不过滤
_TASK_KEYWORDS = frozenset({
    # 操作动词
    "分类", "归档", "整理", "移动", "重命名", "删除", "复制", "备份",
    "下载", "上传", "创建", "新建", "同步",
    "播放", "放", "暂停", "查询", "查找", "搜索", "查", "找", "看看",
    "处理", "滤镜", "风格", "调色", "复古", "日系", "胶片",
    "显示", "列出", "打开", "关闭",
    # 目标对象
    "照片", "图片", "相册", "文件", "目录", "文件夹",
    "视频", "音乐", "电影", "预告片", "纪录片", "剧集", "电视剧",
    "文档", "报告",
    # 知识库相关对象词（知识库查询触发需要）
    "合同", "方案", "协议", "资料", "预算", "表格", "记录", "说明", "计划",
    # NAS 路径/业务词
    "家庭", "手机", "旅行", "家庭相册", "手机相册",
    "nas_share", "NAS",
})


def _is_irrelevant_speech(text: str) -> bool:
    """判断 ASR 结果是否为无关输入，需静默跳过（不调用 TTS / OpenClaw）。

    三级过滤：
      L1 - 单字或纯语气音节（嗯/啊/哦…）
      L2 - 社交/确认用语（谢谢/好的/知道了…）
      L3 - 短文本（≤8字）且不含任何任务关键词
    """
    s = text.strip()
    # L1
    if len(s) <= 1:
        return True
    if s in _IRRELEVANT_EXACT:
        return True
    if _IRRELEVANT_RE.match(s):
        return True
    # L2
    if s in _SOCIAL_IRRELEVANT:
        return True
    # L3
    if len(s) <= 8 and not any(k in s for k in _TASK_KEYWORDS):
        return True
    return False


def _irrelevant_speech_reason(text: str) -> str | None:
    """返回被无关语音过滤命中的原因；未命中返回 None。"""
    s = text.strip()
    if len(s) <= 1:
        return "L1:len<=1"
    if s in _IRRELEVANT_EXACT:
        return "L1:exact"
    if _IRRELEVANT_RE.match(s):
        return "L1:regex"
    if s in _SOCIAL_IRRELEVANT:
        return "L2:social"
    if len(s) <= 8 and not any(k in s for k in _TASK_KEYWORDS):
        return "L3:short-no-task-keyword"
    return None


def _sanitize_reply_for_tts(reply: str) -> str:
    reply = re.sub(r"/nas_share/?", "", reply)  # 共享目录路径前缀不播报
    reply = re.sub(r"\*+", "", reply)
    reply = re.sub(r"^\s*[-#]+\s*", "", reply, flags=re.MULTILINE)
    reply = re.sub(r"[^\u0000-\u007F\u4e00-\u9fff\u3000-\u303f\uff00-\uffef，。！？、：；""''（）…—\s]", "", reply)
    reply = re.sub(r"\s+", " ", reply).strip()
    return reply


def _first_sentence(text: str) -> str:
    m = re.search(r"[。！？!?；;]", text)
    if not m:
        return text
    return text[: m.end()].strip()


def _adaptive_tts_reply(user_text: str, reply: str, max_chars: int, brief_max_chars: int, brief_user_len: int):
    """根据用户话轮长度动态压缩播报内容，并保留可恢复详情。"""
    if not reply:
        return "", ""

    max_chars = max(16, max_chars)
    brief_max_chars = max(12, brief_max_chars)
    brief_user_len = max(1, brief_user_len)

    important = any(k in reply for k in _IMPORTANT_REPLY_HINTS)
    short_cmd = (len(user_text.strip()) <= brief_user_len) or any(k in user_text for k in _SHORT_CMD_HINTS)

    if short_cmd and (not important):
        first = _first_sentence(reply)
        spoken = first if len(first) <= brief_max_chars else (first[:brief_max_chars].rstrip("，、；:： ") + "。")
        if spoken != reply:
            return spoken, reply
        return spoken, ""

    if len(reply) > max_chars:
        spoken = reply[:max_chars].rstrip("，、；:： ") + "。"
        return spoken, reply

    return reply, ""


def _jellyfin_play_after_download(user_text: str, raw_reply: str, jellyfin_url: str, api_key: str) -> str:
    """下载完成且含播放意图时，触发 Jellyfin 扫库并在活跃 session 播放。

    返回额外 TTS 播报文本；空字符串表示未触发或无需提示。
    需要先在 Jellyfin 管理后台 → 控制台 → API 密钥 创建密钥，
    并通过 --jellyfin-api-key <key> 传入。
    """
    has_play_intent = any(k in user_text for k in ("播放", "放一下", "放出来", "看一下", "看看"))
    has_dl_intent = any(k in user_text for k in ("下载", "找一下", "搜一个", "要看"))
    if not has_play_intent and not has_dl_intent:
        return ""
    # 触发条件：回复含下载完成类关键词，或用户指令本身含下载意图
    has_completion = any(k in raw_reply for k in (
        "下载完成", "已保存到", "下载已完成", "已下载",
        "已存", "存入", "保存到", "已保存",
    ))
    if not has_completion and not has_dl_intent:
        return ""

    import json as _json
    import re as _re
    import time as _time
    import urllib.parse as _up
    import urllib.request as _ur

    base = jellyfin_url.rstrip("/")
    hdrs_json = {"X-MediaBrowser-Token": api_key, "Content-Type": "application/json"}
    hdrs_get  = {"X-MediaBrowser-Token": api_key}

    def _req(method, path, body=None, timeout=8):
        url  = base + path
        data = _json.dumps(body).encode() if body is not None else None
        req  = _ur.Request(url, data=data, method=method,
                           headers=hdrs_json if data else hdrs_get)
        with _ur.urlopen(req, timeout=timeout) as r:
            content = r.read()
            return _json.loads(content) if content.strip() else {}

    # 1. 动态查找"扫描媒体库"任务并触发
    try:
        tasks = _req("GET", "/ScheduledTasks")
        scan_id = next(
            (t["Id"] for t in tasks
             if "扫描媒体库" in t.get("Name", "") or "Scan Media" in t.get("Name", "")),
            None,
        )
        if scan_id:
            _req("POST", f"/ScheduledTasks/Running/{scan_id}")
            print(f"[Jellyfin] library scan triggered (taskId={scan_id})")
        else:
            _req("POST", "/Library/Refresh")  # 旧版兜底
            print("[Jellyfin] library refresh triggered (fallback)")
    except Exception as e:
        print(f"[Jellyfin] scan failed: {e}")
        return ""

    # 2. 从回复提取文件名作为搜索词；提取失败时降级到 user_text 剥离命令词
    m = _re.search(r'文件名\s*[`「]([^`「」\n]+)[`」]', raw_reply)
    if not m:
        m = _re.search(r'文件名[:：]?\s*([^，。\n]+)', raw_reply)
    if not m:
        m = _re.search(r'([A-Za-z0-9_\-\[\] .]+\.(?:mp4|mkv|avi|mov|webm))', raw_reply, flags=_re.IGNORECASE)
    raw_name_stem = ""
    if m:
        raw_name = m.group(1)
        raw_name_stem = _re.sub(r'\.\w{2,5}$', '', raw_name).strip().lower()
        term = _re.sub(r'\s*[\[（【（][^\]）】]*[\]）】]', '', raw_name)
        term = _re.sub(r'\.\w{2,5}$', '', term).strip()
    else:
        # 降级：从用户原始指令中剥离命令词，提取核心内容词
        term = user_text
        for _w in ("帮我", "请帮", "请", "帮", "给我",
                   "下载", "搜索", "查找", "找",
                   "播放", "放一下", "放出来", "看一下", "看看",
                 "视频", "电影", "影片", "一下", "一部", "进行", "下"):
            term = term.replace(_w, "")
        term = re.sub(r"(并且播放|并播放|然后播放)$", "", term)
        term = term.replace("并且", "").replace("并", "")
        term = term.strip()
    if not term:
        return ""
    if term in _PLAY_ALIASES:
        term = _PLAY_ALIASES[term]
    print(f"[Jellyfin] search: {term!r} (from={'reply' if m else 'user_text'})")

    # 3. 等扫描写入后搜索（最多 4 次，每次间隔 3 秒）
    item_id, item_name = None, term
    for i in range(4):
        _time.sleep(3)
        try:
            result = _req(
                "GET",
                f"/Items?searchTerm={_up.quote(term)}"
                "&IncludeItemTypes=Movie,Video&Limit=5&Recursive=true",
            )
            items = result.get("Items", [])
            if items:
                item_id   = items[0]["Id"]
                item_name = items[0]["Name"]
                print(f"[Jellyfin] found: {item_name} (id={item_id}) attempt={i+1}")
                break
        except Exception as e:
            print(f"[Jellyfin] search [{i+1}]: {e}")

    # 回退：有些媒体库会把展示名改成中文，按英文文件名搜索不到。
    # 这时按下载文件名去匹配 Item.Path，更稳定。
    if (not item_id) and raw_name_stem:
        try:
            result = _req(
                "GET",
                "/Items?IncludeItemTypes=Movie,Video&Limit=200&Recursive=true&Fields=Path",
            )
            for it in result.get("Items", []):
                p = str(it.get("Path") or "").lower()
                if raw_name_stem in p:
                    item_id = it.get("Id")
                    item_name = it.get("Name") or term
                    print(f"[Jellyfin] path fallback hit: {item_name} (id={item_id})")
                    break
        except Exception as e:
            print(f"[Jellyfin] path fallback failed: {e}")

    if not item_id:
        return "Jellyfin 库已刷新，视频扫描中，稍后可在家庭影院查看。"

    # 4. 查找能播放 Video 的活跃 session，发送播放指令
    try:
        sessions = _req("GET", "/Sessions")
        # SupportsRemoteControl 在 Web 客户端通常为 null，改用 PlayableMediaTypes 过滤
        candidates = [
            s for s in sessions
            if "Video" in s.get("Capabilities", {}).get("PlayableMediaTypes", [])
        ]
        if candidates:
            sid = candidates[0]["Id"]
            # Jellyfin 播放接口用 query 参数，不是 body
            _req("POST",
                 f"/Sessions/{sid}/Playing"
                 f"?ItemIds={_up.quote(item_id)}&PlayCommand=PlayNow")
            print(f"[Jellyfin] play sent to session {sid}")
            return f"正在 Jellyfin 播放：{item_name}"
        print("[Jellyfin] no controllable session found")
        opened = _open_jellyfin_in_firefox(jellyfin_url, item_id=item_id)
        if opened:
            for attempt in range(5):
                _time.sleep(2)
                sessions = _req("GET", "/Sessions")
                candidates = [
                    s for s in sessions
                    if "Video" in s.get("Capabilities", {}).get("PlayableMediaTypes", [])
                ]
                if candidates:
                    sid = candidates[0]["Id"]
                    _req("POST",
                         f"/Sessions/{sid}/Playing"
                         f"?ItemIds={_up.quote(item_id)}&PlayCommand=PlayNow")
                    print(f"[Jellyfin] play sent after firefox open: session {sid}")
                    return f"正在 Jellyfin 播放：{item_name}"
        return f"已打开 Jellyfin，正在进入：{item_name}" if opened else f"请在 Jellyfin 播放：{item_name}"
    except Exception as e:
        print(f"[Jellyfin] play failed: {e}")
        return f"视频已就绪，请在 Jellyfin 播放：{item_name}"


def _resolve_asr_model_path(args, asr_root: Path):
    if args.asr_engine == "sensevoice":
        return Path(args.asr_model) if args.asr_model else ensure_sensevoice_model(
            asr_root / "model",
            force_download=not args.no_auto_download_asr,
        )
    return Path(args.asr_model) if args.asr_model else None


def _run_single_http_turn(
    args,
    work_dir: Path,
    asr_root: Path,
    tts_root: Path,
    forced_text: str | None = None,
    audio_lock=None,
    turn_source: str = "button",
) -> dict:
    _turn_start = time.monotonic()
    user_wav = work_dir / "user_http.wav"
    reply_wav = work_dir / "reply_http.wav"
    recognizer = None
    keep_models = bool(getattr(args, "http_keep_models", True))

    if not forced_text:
        if keep_models:
            cache_key = (
                args.asr_engine,
                args.asr_language,
                str(args.asr_model or ""),
                str(args.hotwords_file or ""),
                float(args.hotwords_score),
            )
            recognizer = _HTTP_MODEL_CACHE["recognizers"].get(cache_key)
            if recognizer is None:
                asr_model_path = _resolve_asr_model_path(args, asr_root)
                print(f"[HTTP] building ASR recognizer (engine={args.asr_engine})...")
                recognizer = build_asr_recognizer(
                    asr_root,
                    asr_model_path,
                    args.asr_language,
                    engine=args.asr_engine,
                    hotwords_file=args.hotwords_file,
                    hotwords_score=args.hotwords_score,
                )
                _HTTP_MODEL_CACHE["recognizers"][cache_key] = recognizer
            else:
                print(f"[HTTP] reusing ASR recognizer (engine={args.asr_engine})")
        else:
            asr_model_path = _resolve_asr_model_path(args, asr_root)
            print(f"[HTTP] building ASR recognizer (engine={args.asr_engine})...")
            recognizer = build_asr_recognizer(
                asr_root,
                asr_model_path,
                args.asr_language,
                engine=args.asr_engine,
                hotwords_file=args.hotwords_file,
                hotwords_score=args.hotwords_score,
            )

    if keep_models:
        tts = _HTTP_MODEL_CACHE.get("tts")
        if tts is None:
            print("[HTTP] building TTS engine...")
            tts = build_tts(tts_root)
            _HTTP_MODEL_CACHE["tts"] = tts
        else:
            print("[HTTP] reusing TTS engine")
    else:
        print("[HTTP] building TTS engine...")
        tts = build_tts(tts_root)

    try:
        listen_round = 0
        text = ""
        while True:
            if forced_text:
                text = forced_text.strip()
                print(f"[HTTP] text mode input: {text}")
            else:
                if listen_round == 0:
                    print("[HTTP] listening for one command...")
                else:
                    print(
                        f"[HTTP] listening for follow-up command... "
                        f"({listen_round}/{_HTTP_INCOMPLETE_MAX_FOLLOWUPS})"
                    )
                _set_bridge_state("listening")
                _t_mic = time.monotonic()
                if audio_lock is not None:
                    audio_lock.acquire()
                try:
                    record_speech_until_silence(
                        user_wav,
                        mic_input=args.mic_input,
                        backend=args.record_backend,
                        min_duration=args.speech_min_duration,
                        max_duration=args.speech_duration,
                        tail_window_sec=args.speech_tail_window,
                        silence_threshold_dbfs=args.speech_silence_threshold_dbfs,
                    )
                finally:
                    if audio_lock is not None:
                        audio_lock.release()
                level = wav_level_dbfs(user_wav)
                print(f"[HTTP] speech clip level: {level:.1f} dBFS")
                print(
                    f"[HTTP][AUDIO] mic={args.mic_input} backend={args.record_backend} "
                    f"{_wav_meta_for_log(user_wav)}"
                )
                print(f"[HTTP][PHASE] record cost={int((time.monotonic() - _t_mic) * 1000)}ms")

                _set_bridge_state("asr")
                _t_asr = time.monotonic()
                raw_text = asr_transcribe(recognizer, user_wav, engine=args.asr_engine)
                print(f"[HTTP][ASR] raw_text={raw_text!r} len={len(raw_text.strip())}")
                print(f"[HTTP][PHASE] asr cost={int((time.monotonic() - _t_asr) * 1000)}ms")
                normalized = normalize_asr_text(raw_text)
                if normalized != raw_text:
                    print(f"[HTTP] normalized: {normalized}")
                text = normalized

            expanded = _expand_short_media_phrase(text)
            if expanded != text:
                print(f"[HTTP][ASR] short phrase expanded: {text!r} -> {expanded!r}")
                text = expanded
            print(f"[HTTP] text: {text}")
            print(f"[HTTP][ASR] final_text={text!r} len={len(text.strip())}")

            if not text:
                return {
                    "ok": True,
                    "message": "未识别到有效语音，请重试。",
                    "text": "",
                    "reply": "",
                }

            pending_filter = _HTTP_DIALOG_STATE.get("pending_image_filter")
            if pending_filter is not None:
                pending_reply, new_pending = _consume_pending_image_filter(text, pending_filter)
                _HTTP_DIALOG_STATE["pending_image_filter"] = new_pending
                if pending_reply is not None:
                    print(f"[HTTP][FILTER] pending reply: {pending_reply}")
                    _set_bridge_state("speaking")
                    tts_speak(tts, pending_reply, reply_wav, play=not args.no_play)
                    _record_voice_turn(
                        source=turn_source,
                        text=text,
                        reply=pending_reply,
                        cost_ms=int((time.monotonic() - _turn_start) * 1000),
                    )
                    return {
                        "ok": True,
                        "message": "已承接上一轮滤镜参数补充。",
                        "text": text,
                        "reply": pending_reply,
                    }

            filter_req = _extract_image_filter_request(text)
            if filter_req is not None and (filter_req["target"] is None or filter_req["style"] is None):
                _HTTP_DIALOG_STATE["pending_image_filter"] = {
                    "target": filter_req["target"],
                    "style": filter_req["style"],
                    "dry": bool(filter_req["dry"]),
                    "attempts": 0,
                }
                missing = "target" if filter_req["target"] is None else "style"
                print(
                    f"[HTTP][FILTER] pending {missing} target={filter_req['target']} "
                    f"style={filter_req['style']} dry={1 if filter_req['dry'] else 0}"
                )
                prompt = _IMAGE_FILTER_TARGET_PROMPT if filter_req["target"] is None else _IMAGE_FILTER_STYLE_PROMPT
                _set_bridge_state("speaking")
                tts_speak(tts, prompt, reply_wav, play=not args.no_play)
                _record_voice_turn(
                    source=turn_source,
                    text=text,
                    reply=prompt,
                    cost_ms=int((time.monotonic() - _turn_start) * 1000),
                )
                return {
                    "ok": True,
                    "message": "等待补充滤镜参数。",
                    "text": text,
                    "reply": prompt,
                }

            if (not forced_text) and _looks_like_incomplete_command(text):
                if listen_round < _HTTP_INCOMPLETE_MAX_FOLLOWUPS:
                    listen_round += 1
                    print(
                        f"[HTTP][ASR] looks incomplete, continue listening: "
                        f"text={text!r} round={listen_round}/{_HTTP_INCOMPLETE_MAX_FOLLOWUPS}"
                    )
                    tts_speak(tts, _HTTP_INCOMPLETE_PROMPT, reply_wav, play=not args.no_play)
                    continue
                print(
                    f"[HTTP][ASR] looks incomplete but max follow-ups reached, proceed: "
                    f"text={text!r}"
                )

            break

        # 危险指令待确认时，"确认/取消"等短词豁免无关语音过滤
        _skip_irrelevant = _HTTP_DIALOG_STATE.get("pending_danger_confirm") is not None
        if not _skip_irrelevant and _is_irrelevant_speech(text):
            reason = _irrelevant_speech_reason(text) or "unknown"
            hit_keywords = [k for k in _TASK_KEYWORDS if k in text][:3]
            print(
                f"[HTTP][ASR] irrelevant skipped reason={reason} "
                f"text={text!r} hit_task_keywords={hit_keywords}"
            )
            return {
                "ok": True,
                "message": "识别到的是无任务语音，已跳过。",
                "text": text,
                "reply": "",
            }

        print("[HTTP] processing...")
        _set_bridge_state("processing", text)

        _t_agent = time.monotonic()
        raw_reply = ask_openclaw(args, text)
        print(f"[HTTP][PHASE] agent cost={int((time.monotonic() - _t_agent) * 1000)}ms")
        reply = _sanitize_reply_for_tts(raw_reply)
        spoken_reply, _tail = _adaptive_tts_reply(
            text,
            reply,
            max_chars=args.tts_max_chars,
            brief_max_chars=args.tts_brief_max_chars,
            brief_user_len=args.tts_brief_user_len,
        )
        print(f"[HTTP] reply: {spoken_reply}")
        _set_bridge_state("speaking")
        _t_tts = time.monotonic()
        tts_speak(tts, spoken_reply, reply_wav, play=not args.no_play)
        print(f"[HTTP][PHASE] tts cost={int((time.monotonic() - _t_tts) * 1000)}ms")

        if args.jellyfin_api_key:
            jf_hint = _jellyfin_play_after_download(
                text, raw_reply, args.jellyfin_url, args.jellyfin_api_key
            )
            if jf_hint:
                jf_hint = _sanitize_reply_for_tts(jf_hint)
                print(f"[HTTP][Jellyfin] {jf_hint}")
                tts_speak(tts, jf_hint, reply_wav, play=not args.no_play)

        _record_voice_turn(
            source="text" if forced_text else turn_source,
            text=text,
            reply=spoken_reply,
            cost_ms=int((time.monotonic() - _turn_start) * 1000),
        )
        # 唤醒通道：助手若反问了澄清（如"想下载哪个视频？"），
        # 记录待回答次数，供唤醒循环免唤醒继续收听用户答案。
        if turn_source == "wake":
            _HTTP_DIALOG_STATE["pending_question"] = (
                _HTTP_QUESTION_MAX_FOLLOWUPS if _reply_is_clarifying_question(spoken_reply) else 0
            )
        return {
            "ok": True,
            "message": "文本指令处理完成。" if forced_text else "语音指令处理完成。",
            "text": text,
            "reply": spoken_reply,
        }
    finally:
        if recognizer is not None and not keep_models:
            del recognizer
        if not keep_models:
            del tts
            gc.collect()
        _set_bridge_state("idle")


def _http_speak_wake_prompt(args, tts_root: Path, work_dir: Path) -> None:
    prompt = (args.http_wakeword_prompt or "").strip()
    if not prompt:
        return

    reply_wav = work_dir / "reply_http_wake.wav"
    keep_models = bool(getattr(args, "http_keep_models", True))
    if keep_models:
        tts = _HTTP_MODEL_CACHE.get("tts")
        if tts is None:
            print("[HTTP][WAKE] building TTS engine...")
            tts = build_tts(tts_root)
            _HTTP_MODEL_CACHE["tts"] = tts
        else:
            print("[HTTP][WAKE] reusing TTS engine")
    else:
        print("[HTTP][WAKE] building TTS engine...")
        tts = build_tts(tts_root)

    try:
        tts_speak(tts, prompt, reply_wav, play=not args.no_play)
    finally:
        if not keep_models:
            del tts
            gc.collect()


def _run_http_wakeword_loop(
    args,
    trigger_lock: threading.Lock,
    audio_lock: threading.Lock,
    work_dir: Path,
    asr_root: Path,
    tts_root: Path,
    kws_root: Path,
    wake_keyword_bin: Path,
    wake_filler: Path,
    wake_mlp: Path,
    wake_keyword_bins: list[Path],
) -> None:
    while True:
        if trigger_lock.locked():
            time.sleep(0.2)
            continue

        wake_wav = work_dir / "wake_http.wav"
        try:
            if not audio_lock.acquire(blocking=False):
                time.sleep(0.2)
                continue
            try:
                record_audio_auto_backend(
                    wake_wav,
                    duration=args.wake_duration,
                    mic_input=args.mic_input,
                    backend=args.record_backend,
                )
            finally:
                audio_lock.release()
            level = wav_level_dbfs(wake_wav)
            if args.verbose:
                print(f"[HTTP][WAKE] clip level: {level:.1f} dBFS")

            if args.wake_any_keyword:
                hit, hit_keyword, _raw, matched_bin = detect_wakeup_any(
                    kws_root,
                    wake_wav,
                    wake_keyword_bins,
                    wake_filler,
                    wake_mlp,
                )
            else:
                hit, hit_keyword, _raw = detect_wakeup(
                    kws_root,
                    wake_wav,
                    wake_keyword_bin,
                    wake_filler,
                    wake_mlp,
                )
                matched_bin = wake_keyword_bin

            if not hit:
                continue

            print(f"[HTTP][WAKE] detected: {hit_keyword or '(unknown)'} via {matched_bin.name}")
            _set_bridge_state("awake")

            if not trigger_lock.acquire(blocking=False):
                print("[HTTP][WAKE] ignored because another request is running")
                _set_bridge_state("idle")  # 未抢到锁时复位，避免按钮卡在“已唤醒”
                continue

            try:
                _http_speak_wake_prompt(args, tts_root, work_dir)
                time.sleep(0.8)  # 提示音结束后留出缓冲，避免开口音节被切
                result = _run_single_http_turn(
                    args,
                    work_dir,
                    asr_root,
                    tts_root,
                    forced_text=None,
                    audio_lock=audio_lock,
                    turn_source="wake",
                )
                print(f"[HTTP][WAKE] turn done: {result.get('message', '')}")
                # If a pending dialog state exists (e.g. waiting for filter style,
                # or the assistant just asked a clarifying question), keep listening
                # without requiring a new wake word.
                while (
                    _HTTP_DIALOG_STATE.get("pending_image_filter") is not None
                    or _HTTP_DIALOG_STATE.get("pending_question", 0) > 0
                ):
                    if _HTTP_DIALOG_STATE.get("pending_question", 0) > 0:
                        _HTTP_DIALOG_STATE["pending_question"] -= 1
                        print("[HTTP][WAKE] pending question, listening for answer without re-wake...")
                    else:
                        print("[HTTP][WAKE] pending filter, listening for follow-up without re-wake...")
                    result = _run_single_http_turn(
                        args,
                        work_dir,
                        asr_root,
                        tts_root,
                        forced_text=None,
                        audio_lock=audio_lock,
                        turn_source="wake",
                    )
                    print(f"[HTTP][WAKE] turn done: {result.get('message', '')}")
            except Exception as e:  # noqa: BLE001
                print(f"[HTTP][WAKE] turn failed: {e}")
                _HTTP_DIALOG_STATE.pop("pending_image_filter", None)
                _HTTP_DIALOG_STATE["pending_question"] = 0
                _set_bridge_state("idle")  # 异常时复位，避免按钮卡在“已唤醒”
            finally:
                trigger_lock.release()
        except Exception as e:  # noqa: BLE001
            print(f"[HTTP][WAKE] loop error: {e}")
            _set_bridge_state("idle")  # 兜底复位，覆盖 acquire/录检测阶段抛异常的路径
            time.sleep(0.5)


def _run_http_server(args) -> None:
    check_cmd_exists("ffmpeg")
    check_cmd_exists("ffplay")
    check_cmd_exists("curl")
    check_cmd_exists("docker")

    ensure_openclaw_exec_access(args.openclaw_container)
    sync_runtime_scripts()

    asr_root = Path(args.asr_root)
    tts_root = Path(args.tts_root)
    kws_root = Path(args.kws_root)
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    global _TURNS_FILE
    _TURNS_FILE = (
        Path(args.log_file).parent / "voice_turns.jsonl"
        if args.log_file
        else work_dir / "voice_turns.jsonl"
    )
    print(f"[TURNS] file: {_TURNS_FILE}")

    wake_keyword_bin = Path(args.wake_keyword_bin)
    wake_filler = Path(args.wake_filler)
    wake_mlp = Path(args.wake_mlp)
    wake_keyword_bins = collect_keyword_bins(kws_root)

    if args.wake_any_keyword and not wake_keyword_bins:
        raise RuntimeError("No keyword_*.bin found under res_shuffnet_v2")

    trigger_lock = threading.Lock()
    audio_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *values):
            pass  # print(f"[HTTP] {self.address_string()} - {fmt % values}")

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/":
                _html_response(self, HTTPStatus.OK, _http_ui_html(args.http_trigger_port))
                return
            if parsed.path == "/healthz":
                _json_response(self, HTTPStatus.OK, {
                    "ok": True,
                    "mode": "http-trigger",
                    "busy": trigger_lock.locked(),
                    "port": args.http_trigger_port,
                })
                return
            if parsed.path == "/api/turns":
                since = float(parse_qs(parsed.query).get("since", ["0"])[0] or "0")
                with _VOICE_TURNS_LOCK:
                    turns = [t for t in _VOICE_TURNS if t["ts"] > since]
                _json_response(self, HTTPStatus.OK, {"ok": True, "turns": turns})
                return
            if parsed.path == "/api/status":
                with _BRIDGE_STATE_LOCK:
                    state_copy = dict(_BRIDGE_STATE)
                state_copy["busy"] = trigger_lock.locked()
                _json_response(self, HTTPStatus.OK, {"ok": True, **state_copy})
                return
            if parsed.path == "/trigger":
                self._handle_trigger(parsed)
                return
            _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path == "/trigger":
                self._handle_trigger(parsed)
                return
            _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

        def _handle_trigger(self, parsed):
            req_start = time.monotonic()
            if args.http_trigger_token:
                token = parse_qs(parsed.query).get("token", [""])[0]
                if token != args.http_trigger_token:
                    print(f"[HTTP] trigger rejected: invalid token from={self.address_string()}")
                    _json_response(self, HTTPStatus.FORBIDDEN, {
                        "ok": False,
                        "error": "invalid token",
                    })
                    return

            text = parse_qs(parsed.query).get("text", [""])[0].strip()
            if not text and self.command == "POST":
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b""
                if raw:
                    ctype = (self.headers.get("Content-Type") or "").lower()
                    if "application/json" in ctype:
                        try:
                            obj = json.loads(raw.decode("utf-8", errors="ignore"))
                            if isinstance(obj, dict):
                                text = str(obj.get("text", "")).strip()
                        except Exception:  # noqa: BLE001
                            text = ""
                    elif "application/x-www-form-urlencoded" in ctype:
                        data = parse_qs(raw.decode("utf-8", errors="ignore"))
                        text = (data.get("text", [""]) or [""])[0].strip()

            mode = "text" if text else "voice"
            print(
                f"[HTTP] trigger start method={self.command} mode={mode} "
                f"text_len={len(text)} from={self.address_string()}"
            )

            if not trigger_lock.acquire(blocking=False):
                print(f"[HTTP] trigger busy mode={mode} from={self.address_string()}")
                _json_response(self, HTTPStatus.CONFLICT, {
                    "ok": False,
                    "error": "busy",
                    "message": "上一条语音仍在处理中，请稍后再试。",
                })
                return

            try:
                result = _run_single_http_turn(
                    args,
                    work_dir,
                    asr_root,
                    tts_root,
                    forced_text=text or None,
                    audio_lock=audio_lock,
                )
                cost_ms = int((time.monotonic() - req_start) * 1000)
                print(f"[HTTP] trigger done mode={mode} cost_ms={cost_ms}")
                _json_response(self, HTTPStatus.OK, result)
            except Exception as e:  # noqa: BLE001
                cost_ms = int((time.monotonic() - req_start) * 1000)
                print(f"[HTTP] trigger failed mode={mode} cost_ms={cost_ms}: {e}")
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {
                    "ok": False,
                    "error": str(e),
                })
            finally:
                trigger_lock.release()

    if args.http_wakeword:
        wake_thread = threading.Thread(
            target=_run_http_wakeword_loop,
            args=(
                args,
                trigger_lock,
                audio_lock,
                work_dir,
                asr_root,
                tts_root,
                kws_root,
                wake_keyword_bin,
                wake_filler,
                wake_mlp,
                wake_keyword_bins,
            ),
            daemon=True,
            name="http-wakeword-loop",
        )
        wake_thread.start()
        mode_hint = "any built-in keyword" if args.wake_any_keyword else wakeword_hint_from_bin(wake_keyword_bin)
        print(f"[HTTP][WAKE] enabled, mode={mode_hint}")
    else:
        print("[HTTP][WAKE] disabled")

    server = ThreadingHTTPServer((args.http_trigger_host, args.http_trigger_port), Handler)
    print(
        f"[HTTP] ready: http://{args.http_trigger_host}:{args.http_trigger_port} "
        "(click page at /, trigger API at /trigger)"
    )
    server.serve_forever()


def main():
    args = parse_args()
    _setup_file_logging(args.log_file)
    _ensure_audio_runtime_env()

    use_http_mode = args.http_mode or (not args.wake_mode and not os.isatty(0))
    if use_http_mode:
        _run_http_server(args)
        return

    check_cmd_exists("ffmpeg")
    check_cmd_exists("ffplay")
    check_cmd_exists("curl")
    check_cmd_exists("docker")

    ensure_openclaw_exec_access(args.openclaw_container)
    sync_runtime_scripts()

    kws_root = Path(args.kws_root)
    asr_root = Path(args.asr_root)
    tts_root = Path(args.tts_root)

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    wake_keyword_bin = Path(args.wake_keyword_bin)
    wake_filler = Path(args.wake_filler)
    wake_mlp = Path(args.wake_mlp)
    wake_keyword_bins = collect_keyword_bins(kws_root)

    if args.wake_any_keyword and not wake_keyword_bins:
        raise RuntimeError("No keyword_*.bin found under res_shuffnet_v2")

    if args.session_idle_rounds < 1:
        args.session_idle_rounds = 1

    if args.speech_min_duration < 0.5:
        args.speech_min_duration = 0.5
    if args.speech_duration < args.speech_min_duration:
        args.speech_duration = args.speech_min_duration

    if args.asr_engine == "sensevoice":
        asr_model_path = Path(args.asr_model) if args.asr_model else ensure_sensevoice_model(
            asr_root / "model",
            force_download=not args.no_auto_download_asr,
        )
    else:
        asr_model_path = Path(args.asr_model) if args.asr_model else None

    print(f"[INIT] building ASR recognizer (engine={args.asr_engine})...")
    recognizer = build_asr_recognizer(
        asr_root,
        asr_model_path,
        args.asr_language,
        engine=args.asr_engine,
        hotwords_file=args.hotwords_file,
        hotwords_score=args.hotwords_score,
    )
    print("[INIT] building TTS engine...")
    tts = build_tts(tts_root)

    if args.wake_any_keyword:
        print("[INIT] wake mode: any built-in keyword")
        print("[INIT] available wake words:")
        for kb in wake_keyword_bins:
            print(f"  - {wakeword_hint_from_bin(kb)} ({kb.name})")
    else:
        print(f"[INIT] wake mode: {wakeword_hint_from_bin(wake_keyword_bin)}")

    print(f"[INIT] OpenClaw target: container={args.openclaw_container}, session={args.openclaw_session_key}")
    print("[INIT] dialog mode: wake once, then continuous conversation")
    print("[INIT] ready.")

    session_awake = False
    idle_rounds = 0
    pending_reply_detail = ""
    pending_image_filter = None

    while True:
        wake_wav = work_dir / "wake.wav"
        user_wav = work_dir / "user.wav"
        reply_wav = work_dir / "reply.wav"

        if not session_awake:
            if args.wake_any_keyword:
                print("\n[WAIT] say wake word (any built-in wake word)...")
            else:
                print(f"\n[WAIT] say wake word: {wakeword_hint_from_bin(wake_keyword_bin)}")

            record_audio_auto_backend(
                wake_wav,
                duration=args.wake_duration,
                mic_input=args.mic_input,
                backend=args.record_backend,
            )
            level = wav_level_dbfs(wake_wav)
            if args.verbose:
                print(f"[MIC] wake clip level: {level:.1f} dBFS")
            if level < -45:
                print("[MIC] warning: volume is low, move closer or raise gain")

            if args.wake_any_keyword:
                hit, hit_keyword, _raw, matched_bin = detect_wakeup_any(
                    kws_root,
                    wake_wav,
                    wake_keyword_bins,
                    wake_filler,
                    wake_mlp,
                )
            else:
                hit, hit_keyword, _raw = detect_wakeup(
                    kws_root,
                    wake_wav,
                    wake_keyword_bin,
                    wake_filler,
                    wake_mlp,
                )
                matched_bin = wake_keyword_bin

            # 低音量下首次未命中：做一次增益重试，降低漏唤醒
            if (not hit) and (level < args.wake_low_level_dbfs) and (args.wake_boost_db > 0):
                boosted_wav = work_dir / "wake_boost.wav"
                if args.verbose:
                    print(
                        f"[KWS] low-level clip ({level:.1f} dBFS), retry with +{args.wake_boost_db:.1f} dB"
                    )
                p = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(wake_wav),
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-af",
                        f"volume={args.wake_boost_db}dB",
                        str(boosted_wav),
                        "-loglevel",
                        "error",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if p.returncode == 0 and boosted_wav.exists():
                    boosted_level = wav_level_dbfs(boosted_wav)
                    if args.verbose:
                        print(f"[MIC] boosted wake clip level: {boosted_level:.1f} dBFS")
                    if args.wake_any_keyword:
                        hit, hit_keyword, _raw, matched_bin = detect_wakeup_any(
                            kws_root,
                            boosted_wav,
                            wake_keyword_bins,
                            wake_filler,
                            wake_mlp,
                        )
                    else:
                        hit, hit_keyword, _raw = detect_wakeup(
                            kws_root,
                            boosted_wav,
                            wake_keyword_bin,
                            wake_filler,
                            wake_mlp,
                        )
                        matched_bin = wake_keyword_bin
                else:
                    err = (p.stderr or p.stdout or "ffmpeg boost failed").strip()
                    print(f"[KWS] boost retry skipped: {err}")

                boosted_wav.unlink(missing_ok=True)

            if not hit:
                print("[KWS] no wake word detected.")
                continue

            print(f"[KWS] wake word detected: {hit_keyword or '(unknown)'}")
            print(f"[KWS] matched model: {matched_bin.name}")
            print("[SESSION] wake accepted. Continuous dialog is active.")
            session_awake = True
            idle_rounds = 0

        print("[ASR] listening... (no wake word needed now)")
        print(
            f"[ASR] dynamic capture: min={args.speech_min_duration:.1f}s, "
            f"max={args.speech_duration:.1f}s, silence<{args.speech_silence_threshold_dbfs:.1f}dBFS"
        )

        try:
            record_speech_until_silence(
                user_wav,
                mic_input=args.mic_input,
                backend=args.record_backend,
                min_duration=args.speech_min_duration,
                max_duration=args.speech_duration,
                tail_window_sec=args.speech_tail_window,
                silence_threshold_dbfs=args.speech_silence_threshold_dbfs,
            )
        except Exception as e:
            print(f"[MIC] recording failed, re-listening: {e}")
            continue

        level = wav_level_dbfs(user_wav)
        print(f"[MIC] speech clip level: {level:.1f} dBFS")

        text = asr_transcribe(recognizer, user_wav, engine=args.asr_engine)
        normalized = normalize_asr_text(text)
        if normalized != text:
            print(f"[ASR] normalized: {normalized}")
            text = normalized
        expanded = _expand_short_media_phrase(text)
        if expanded != text:
            print(f"[ASR] short phrase expanded: {text!r} -> {expanded!r}")
            text = expanded
        print(f"[ASR] text: {text}")

        if not text:
            idle_rounds += 1
            print(f"[ASR] empty speech ({idle_rounds}/{args.session_idle_rounds})")
            if idle_rounds >= args.session_idle_rounds:
                session_awake = False
                idle_rounds = 0
                print("[SESSION] idle timeout. Wake word is required again.")
            continue

        idle_rounds = 0

        if pending_reply_detail and any(k in text for k in ("继续", "详细", "详情", "补充", "说完")):
            detail = pending_reply_detail
            pending_reply_detail = ""
            reply = f"补充说明：{detail}"
            reply = _sanitize_reply_for_tts(reply)
            spoken, tail = _adaptive_tts_reply(
                text,
                reply,
                max_chars=args.tts_max_chars,
                brief_max_chars=args.tts_brief_max_chars,
                brief_user_len=args.tts_brief_user_len,
            )
            if tail:
                pending_reply_detail = tail
            print(f"[OpenClaw] reply: {spoken}")
            tts_speak(tts, spoken, reply_wav, play=not args.no_play)
            continue

        # Local control keywords for bridge process
        if any(k in text for k in ("休眠", "停止监听", "待机")):
            session_awake = False
            reply = "好的，我先待机。需要时再叫我。"
            print(f"[TTS] reply: {reply}")
            tts_speak(tts, reply, reply_wav, play=not args.no_play)
            continue

        if any(k in text for k in ("退出程序", "关闭语音助手", "关闭对话助手")):
            reply = "好的，我现在退出。"
            print(f"[TTS] reply: {reply}")
            tts_speak(tts, reply, reply_wav, play=not args.no_play)
            print("[EXIT] done")
            break

        if pending_image_filter is not None:
            pending_reply, pending_image_filter = _consume_pending_image_filter(text, pending_image_filter)
            if pending_reply is not None:
                print(f"[FILTER] pending reply: {pending_reply}")
                tts_speak(tts, pending_reply, reply_wav, play=not args.no_play)
                continue

        filter_req = _extract_image_filter_request(text)
        if filter_req is not None and (filter_req["target"] is None or filter_req["style"] is None):
            pending_image_filter = {
                "target": filter_req["target"],
                "style": filter_req["style"],
                "dry": bool(filter_req["dry"]),
                "attempts": 0,
            }
            missing = "target" if filter_req["target"] is None else "style"
            print(
                f"[FILTER] pending {missing} target={filter_req['target']} "
                f"style={filter_req['style']} dry={1 if filter_req['dry'] else 0}"
            )
            prompt = _IMAGE_FILTER_TARGET_PROMPT if filter_req["target"] is None else _IMAGE_FILTER_STYLE_PROMPT
            tts_speak(tts, prompt, reply_wav, play=not args.no_play)
            continue

        if _is_irrelevant_speech(text):
            print(f"[ASR] irrelevant speech skipped: {text!r}")
            continue

        processing_hint = "正在处理，请稍等。"
        print(f"[OpenClaw] processing: {processing_hint}")
        tts_speak(tts, processing_hint, reply_wav, play=not args.no_play)

        reply = ask_openclaw(args, text)
        raw_reply = reply  # 保留原始回复供 Jellyfin 下载检测使用
        reply = _sanitize_reply_for_tts(reply)
        spoken_reply, tail = _adaptive_tts_reply(
            text,
            reply,
            max_chars=args.tts_max_chars,
            brief_max_chars=args.tts_brief_max_chars,
            brief_user_len=args.tts_brief_user_len,
        )
        pending_reply_detail = tail
        if tail:
            print("[TTS] long reply shortened; say '继续' to hear more")

        print(f"[OpenClaw] reply: {spoken_reply}")
        tts_speak(tts, spoken_reply, reply_wav, play=not args.no_play)

        # 下载完成+播放意图时，自动触发 Jellyfin 扫库并播放
        if args.jellyfin_api_key:
            jf_hint = _jellyfin_play_after_download(
                text, raw_reply, args.jellyfin_url, args.jellyfin_api_key
            )
            if jf_hint:
                jf_hint = _sanitize_reply_for_tts(jf_hint)
                print(f"[Jellyfin] {jf_hint}")
                tts_speak(tts, jf_hint, reply_wav, play=not args.no_play)


if __name__ == "__main__":
    main()
