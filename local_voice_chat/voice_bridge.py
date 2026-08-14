#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
from pathlib import Path

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
    parser.add_argument("--record-backend", choices=["auto", "pulse", "alsa"], default="auto")
    parser.add_argument("--mic-input", default="default")

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
    parser.add_argument("--openclaw-session-key", default="agent:main:voice-bridge")
    parser.add_argument("--openclaw-timeout", type=int, default=300)
    parser.add_argument("--openclaw-dry-run", action="store_true")

    parser.add_argument("--no-play", action="store_true")
    parser.add_argument("--work-dir", default="/tmp/voice_bridge")

    return parser.parse_args()


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
            "Please run: /home/pi/openclaw-casaos/oc.sh status"
        )


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
        "手机册": "手机相册",
        "手机像册": "手机相册",
        "手机象册": "手机相册",
    }
    for wrong, right in replacements.items():
        text = text.replace(wrong, right)

    # 上下文补全：仅当句子属于 NAS 操作语境时，才做"缺字补全"，降低误伤
    is_nas_cmd = any(k in text for k in ("分类", "照片", "相册", "图片", "移动", "整理", "备份"))
    if is_nas_cmd:
        for base, full in (("家庭", "家庭相册"), ("手机", "手机相册")):
            if base in text and full not in text:
                for d in ("下面", "里面", "里的", "中的", "内", "下", "里"):
                    pat = f"{base}{d}"
                    if pat in text:
                        text = text.replace(pat, f"{full}{d}")
                        break
        text = text.replace("所有图", "所有图片")
        text = text.replace("全部图", "全部图片")
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


def ask_openclaw(args, user_text):
    if args.openclaw_dry_run:
        return f"[dry-run] 你说的是：{user_text}"

    fast_reply = _fast_local_classify_reply(args, user_text)
    if fast_reply is not None:
        return fast_reply

    bridge_prompt = (
        "你是quectel pi上的语音助手，负责执行用户口头指令。"
        "可调用你已有工具（如文件/NAS/相册等）来完成任务。"
        "请直接执行并给结果，回复用简短中文，不要自我介绍，不超过50字。"
        "严禁使用markdown格式（如**加粗**、-列表、#标题），只输出纯文字。\n"
        "操作NAS文件时，必须通过 nas_files 工具（如 list_directory/move_file/create_directory）执行，禁止猜测或编造路径。\n"
        "NAS根目录(/nas_share)下的可用目录名（语音识别可能有误，请按此白名单对齐）：\n"
        "  备份、家庭相册、工作文档、手机相册、旅行\n"
        "照片分类归档规则：当用户要求按内容分类/归档/整理时，禁止仅凭文件名猜测。"
        "优先用 exec 工具一次性执行脚本自动归档（识别+重命名+移动）：\n"
        "  timeout 120 python3 /nas_share/tools/nas_classify.py --dir <目录> --recursive --archive\n"
        "默认直接执行归档；只有用户明确说“预览/先别动”才允许 dry-run。\n"
        "发现重复前缀文件名时，直接自动清理并重命名，不要询问用户确认。\n"
        "脚本归档规则：按内容分类到类别目录，并重命名为 YYYYMMDD_HHMMSS_类别_原文件名.ext；"
        "低置信度会归入“待确认”。只有脚本模式失败时，才回退 nas_files 手动逐个移动。\n"
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
        args.openclaw_session_key,
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


def main():
    args = parse_args()

    check_cmd_exists("ffmpeg")
    check_cmd_exists("ffplay")
    check_cmd_exists("curl")
    check_cmd_exists("docker")

    ensure_openclaw_exec_access(args.openclaw_container)
    sync_nas_classify_script()

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

        record_speech_until_silence(
            user_wav,
            mic_input=args.mic_input,
            backend=args.record_backend,
            min_duration=args.speech_min_duration,
            max_duration=args.speech_duration,
            tail_window_sec=args.speech_tail_window,
            silence_threshold_dbfs=args.speech_silence_threshold_dbfs,
        )

        level = wav_level_dbfs(user_wav)
        print(f"[MIC] speech clip level: {level:.1f} dBFS")

        text = asr_transcribe(recognizer, user_wav, engine=args.asr_engine)
        normalized = normalize_asr_text(text)
        if normalized != text:
            print(f"[ASR] normalized: {normalized}")
            text = normalized
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

        # Local control keywords for bridge process
        if any(k in text for k in ("休眠", "停止监听", "待机")):
            session_awake = False
            reply = "好的，我先待机。需要时再叫我。"
            print(f"[TTS] reply: {reply}")
            tts_speak(tts, reply, reply_wav, play=not args.no_play)
            continue

        if any(k in text for k in ("退出程序", "关闭语音助手")):
            reply = "好的，我现在退出。"
            print(f"[TTS] reply: {reply}")
            tts_speak(tts, reply, reply_wav, play=not args.no_play)
            print("[EXIT] done")
            break

        reply = ask_openclaw(args, text)
        # Strip markdown symbols that TTS cannot pronounce
        import re
        reply = re.sub(r"\*+", "", reply)
        reply = re.sub(r"^\s*[-#]+\s*", "", reply, flags=re.MULTILINE)
        reply = re.sub(r"[_`]", "", reply)
        reply = re.sub(r"[^\u0000-\u007F\u4e00-\u9fff\u3000-\u303f\uff00-\uffef，。！？、：；""''（）…—\s]", "", reply)
        reply = re.sub(r"\s+", " ", reply).strip()
        # Guard TTS from very long outputs
        if len(reply) > 80:
            reply = reply[:80] + "。"

        print(f"[OpenClaw] reply: {reply}")
        tts_speak(tts, reply, reply_wav, play=not args.no_play)


if __name__ == "__main__":
    main()
