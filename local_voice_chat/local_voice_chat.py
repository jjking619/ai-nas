#!/usr/bin/env python3
import argparse
import datetime as dt
import math
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import numpy as np
import sherpa_onnx

# ---- Hotword-capable transducer models (sherpa-onnx) ----
# SenseVoice / 离线Paraformer 不支持热词；transducer 系列（在线/离线）支持热词纠偏。
CONFORMER_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-conformer-zh-stateless2-2023-05-23.tar.bz2"
)

DEFAULT_HOTWORDS = [
    "家庭相册", "手机相册", "工作文档", "备份", "旅行",
    "图片", "照片", "文件夹", "文件", "视频",
    "分类", "移动到", "新建", "删除", "重命名",
    "打开", "复制", "所有", "全部", "播放",
	"下载", "测试", "海洋", "兔子", "预告片",
	"电影", "电视剧", "音乐", "歌曲", "剧集", "家庭影院", "Movies", "TV Shows", "movie", "series", "保存",
	"复古", "日系", "胶片", "滤镜", "风格",
    "合同", "住房合同", "预算", "方案", "协议", "表格", "报告", "记录",
    "租房", "房产",
]


def default_hotwords_path() -> Path:
    return Path(__file__).resolve().parent / "hotwords.txt"


def ensure_hotwords_file(path: Path = None) -> Path:
    """热词表不存在时自动创建（每行一个词，cjkchar 建模可直接写中文）。"""
    path = path or default_hotwords_path()
    if not path.exists():
        path.write_text("\n".join(DEFAULT_HOTWORDS) + "\n", encoding="utf-8")
        print(f"[ASR] hotwords file created: {path}")
    return path


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


def check_cmd_exists(name: str) -> None:
	if shutil.which(name) is None:
		raise RuntimeError(f"Missing required command: {name}")


def run_cmd(cmd, cwd=None, env=None, input_text=None):
	return subprocess.run(
		cmd,
		cwd=cwd,
		env=env,
		input=input_text,
		text=True,
		capture_output=True,
		check=False,
	)


def record_audio_with_ffmpeg(
	out_wav: Path, duration: float, mic_input: str, backend: str
) -> None:
	base = [
		"ffmpeg",
		"-y",
		"-f",
		backend,
		"-i",
		mic_input,
		"-ac",
		"1",
		"-ar",
		"16000",
		"-t",
		str(duration),
		str(out_wav),
		"-loglevel",
		"error",
	]
	p = run_cmd(base)
	if p.returncode != 0:
		raise RuntimeError(p.stderr.strip() or p.stdout.strip() or "ffmpeg record failed")


def record_audio_auto_backend(
	out_wav: Path, duration: float, mic_input: str, backend: str
) -> None:
	if backend in ("pulse", "alsa"):
		record_audio_with_ffmpeg(out_wav, duration, mic_input, backend)
		return

	# auto mode: try pulse first, then alsa
	last_err = None
	for b in ("pulse", "alsa"):
		try:
			record_audio_with_ffmpeg(out_wav, duration, mic_input, b)
			return
		except Exception as e:  # noqa: BLE001
			last_err = e
	raise RuntimeError(f"Unable to record audio with pulse/alsa: {last_err}")


def play_wav(wav_path: Path) -> None:
	p = run_cmd([
		"ffplay",
		"-nodisp",
		"-autoexit",
		"-loglevel",
		"error",
		str(wav_path),
	])
	if p.returncode != 0:
		raise RuntimeError(p.stderr.strip() or p.stdout.strip() or "ffplay failed")


def convert_to_wav_16k_mono(src: Path, dst: Path) -> None:
	p = run_cmd(
		[
			"ffmpeg",
			"-y",
			"-i",
			str(src),
			"-ac",
			"1",
			"-ar",
			"16000",
			str(dst),
			"-loglevel",
			"error",
		]
	)
	if p.returncode != 0:
		raise RuntimeError(p.stderr.strip() or p.stdout.strip() or "ffmpeg convert failed")


def load_wav_mono_16k_float(path: Path):
	import wave

	with wave.open(str(path), "rb") as wf:
		channels = wf.getnchannels()
		sample_width = wf.getsampwidth()
		sample_rate = wf.getframerate()
		frames = wf.readframes(wf.getnframes())

	if sample_width != 2:
		raise RuntimeError(f"Expected 16-bit PCM wav, got sample_width={sample_width}")

	data = np.frombuffer(frames, dtype=np.int16)
	if channels > 1:
		data = data[::channels]
	samples = data.astype(np.float32) / 32768.0
	return sample_rate, samples


def dbfs_from_samples(samples: np.ndarray) -> float:
	if samples.size == 0:
		return -99.0
	rms = float(np.sqrt(np.mean(np.square(samples))))
	if rms <= 1e-9:
		return -99.0
	return 20.0 * math.log10(rms)


def wav_level_dbfs(path: Path) -> float:
	_, samples = load_wav_mono_16k_float(path)
	return dbfs_from_samples(samples)


def write_wav_mono_16k(path: Path, samples: np.ndarray) -> None:
	import wave

	samples = np.clip(samples, -1.0, 1.0)
	pcm16 = (samples * 32767.0).astype(np.int16)
	with wave.open(str(path), "wb") as wf:
		wf.setnchannels(1)
		wf.setsampwidth(2)
		wf.setframerate(16000)
		wf.writeframes(pcm16.tobytes())


def record_speech_until_silence(
	out_wav: Path,
	mic_input: str,
	backend: str,
	min_duration: float,
	max_duration: float,
	tail_window_sec: float,
	silence_threshold_dbfs: float,
	chunk_duration: float = 1.2,
	consecutive_silence_chunks: int = 4,
) -> None:
	all_chunks = []
	total_sec = 0.0
	silence_count = 0
	tmp_files = []
	adaptive_base_threshold = float(silence_threshold_dbfs)
	dynamic_threshold_dbfs = adaptive_base_threshold
	speech_peak_dbfs = -99.0

	def _record_chunk(this_dur: float, label: str):
		nonlocal total_sec, speech_peak_dbfs, dynamic_threshold_dbfs
		chunk_wav = out_wav.parent / f"{out_wav.stem}.chunk.{len(tmp_files)}.wav"
		tmp_files.append(chunk_wav)

		# ffmpeg 偶发返回 0 但未落盘（PulseAudio 源不稳定时），重试最多 3 次
		recorded = False
		for attempt in range(3):
			record_audio_auto_backend(
				chunk_wav,
				duration=this_dur,
				mic_input=mic_input,
				backend=backend,
			)
			if chunk_wav.exists() and chunk_wav.stat().st_size > 44:  # WAV 头至少 44 字节
				recorded = True
				break
			print(f"[MIC] chunk recording produced no file, retry {attempt + 1}/3")
			chunk_wav.unlink(missing_ok=True)
		if not recorded:
			raise RuntimeError(
				f"Failed to record audio chunk after retries: {chunk_wav} "
				"(mic busy or PulseAudio unstable)"
			)

		try:
			sr, chunk_samples = load_wav_mono_16k_float(chunk_wav)
		except FileNotFoundError:
			raise RuntimeError(f"Recorded chunk vanished: {chunk_wav}")

		all_chunks.append(chunk_samples)
		total_sec += (len(chunk_samples) / float(sr))

		tail_n = max(1, int(sr * tail_window_sec))
		tail = chunk_samples[-tail_n:] if len(chunk_samples) >= tail_n else chunk_samples
		tail_db = dbfs_from_samples(tail)

		speech_peak_dbfs = max(speech_peak_dbfs, tail_db)
		if speech_peak_dbfs > -98.0:
			# 自适应阈值：用已观测到的语音峰值反推静音线。
			# 语音峰值越高，静音线可适当上抬，减少高噪环境下的“只到 max_duration 才停”。
			adaptive_from_peak = min(-34.0, speech_peak_dbfs - 10.0)
			dynamic_threshold_dbfs = max(adaptive_base_threshold, adaptive_from_peak)

		print(
			f"[MIC] {label} tail level: {tail_db:.1f} dBFS "
			f"(captured {total_sec:.1f}s, silence<{dynamic_threshold_dbfs:.1f}dBFS)"
		)
		return tail_db

	while total_sec < max_duration:
		this_dur = min(chunk_duration, max_duration - total_sec)
		tail_db = _record_chunk(this_dur, "speech")

		if total_sec >= min_duration:
			if tail_db < dynamic_threshold_dbfs:
				silence_count += 1
			else:
				silence_count = 0

			if silence_count >= consecutive_silence_chunks:
				remain = max_duration - total_sec
				if remain <= 0:
					break

				# 确认帧：避免句中长停顿被误判结束
				confirm_dur = min(chunk_duration, remain)
				confirm_tail_db = _record_chunk(confirm_dur, "confirm")
				if confirm_tail_db < dynamic_threshold_dbfs:
					print("[MIC] silence confirmed, stop capture.")
					break
				print("[MIC] confirm frame has speech, continue listening.")
				silence_count = 0

	if not all_chunks:
		write_wav_mono_16k(out_wav, np.zeros((0,), dtype=np.float32))
	else:
		merged = np.concatenate(all_chunks, axis=0)
		write_wav_mono_16k(out_wav, merged)

	for f in tmp_files:
		f.unlink(missing_ok=True)


def wakeword_hint_from_bin(keyword_bin: Path) -> str:
	name = keyword_bin.name
	mapping = {
		"keyword_xiaochuangxiaochuang.bin": "小创小创（可试：你好小创）",
		"keyword_xiaoyanxiaoyan.bin": "小燕小燕",
		"keyword_yunlingyunling.bin": "云铃云铃（可试：你好小云）",
		"keyword_xiaoyuantongxue.bin": "小远同学",
		"keyword_Amigo.bin": "Amigo",
		"keyword_kws_demo.bin": "KWS Demo 预置词",
	}
	return mapping.get(name, name)


def collect_keyword_bins(kws_root: Path):
	res_dir = kws_root / "res_shuffnet_v2"
	bins = sorted(res_dir.glob("keyword_*.bin"))
	return bins


def detect_wakeup_any(
	kws_root: Path,
	wav_path: Path,
	keyword_bins,
	filler_path: Path,
	mlp_path: Path,
):
	for kb in keyword_bins:
		hit, keyword, raw = detect_wakeup(kws_root, wav_path, kb, filler_path, mlp_path)
		if hit:
			return True, keyword, raw, kb
	return False, "", "", None


def detect_wakeup(
	kws_root: Path,
	wav_path: Path,
	keyword_bin: Path,
	filler_path: Path,
	mlp_path: Path,
):
	list_file = wav_path.with_suffix(".txt")
	list_file.write_text(str(wav_path) + "\n", encoding="utf-8")

	env = os.environ.copy()
	lib_dir = str(kws_root / "lib" / "linux_aarch64_v8")
	env["LD_LIBRARY_PATH"] = (
		lib_dir if not env.get("LD_LIBRARY_PATH") else lib_dir + ":" + env["LD_LIBRARY_PATH"]
	)

	cmd = [
		str(kws_root / "bin" / "linux_aarch64_v8" / "ivw_demo"),
		"-pcm",
		str(list_file),
		"-IVW_FILLER",
		str(filler_path),
		"-IVW_MLP",
		str(mlp_path),
		"-IVW_KEYWORD",
		str(keyword_bin),
	]

	p = run_cmd(cmd, cwd=str(kws_root), env=env, input_text="\n")
	output = (p.stdout or "") + "\n" + (p.stderr or "")

	hit = "ivw wakeup reslut string" in output
	keyword_match = re.search(r'"keyword":"([^"]+)"', output)
	keyword = keyword_match.group(1) if keyword_match else ""
	return hit, keyword, output


def _download_model_archive(url: str, dest_dir: Path, archive_name: str) -> Path:
	"""下载并解压 tar.bz2 模型包，返回解压目录。"""
	dest_dir.mkdir(parents=True, exist_ok=True)
	archive = dest_dir / archive_name
	print(f"[ASR] downloading {archive_name} ...")
	p = run_cmd(["curl", "-L", "-o", str(archive), url])
	if p.returncode != 0:
		raise RuntimeError(p.stderr.strip() or p.stdout.strip() or f"Failed to download {url}")

	extract_dir = dest_dir / "_extract"
	if extract_dir.exists():
		shutil.rmtree(extract_dir)
	extract_dir.mkdir(parents=True, exist_ok=True)
	with tarfile.open(archive, "r:bz2") as tf:
		tf.extractall(extract_dir)
	archive.unlink(missing_ok=True)
	return extract_dir


def _model_dir_with_files(eng_dir: Path):
	"""在引擎目录（或其直接子目录）中查找含完整模型文件的目录。

	兼容两种布局：
	  - 扁平：model_conformer/encoder-*.onnx ...
	  - 嵌套：model_conformer/sherpa-onnx-conformer-zh-.../encoder-*.onnx ...
	返回第一个匹配的目录；找不到返回 None。
	"""
	if not eng_dir.exists():
		return None
	candidates = [eng_dir]
	candidates += sorted(d for d in eng_dir.iterdir() if d.is_dir())
	for base in candidates:
		if (
			list(base.glob("*encoder*.onnx"))
			and list(base.glob("*decoder*.onnx"))
			and list(base.glob("*joiner*.onnx"))
			and (base / "tokens.txt").exists()
		):
			return base
	return None


def ensure_transducer_model(asr_root: Path, engine: str, force_download: bool = True) -> dict:
	"""获取支持热词的 transducer 模型，返回 {engine, encoder, decoder, joiner, tokens, root}。

	优先复用已存在的模型目录（SDK 自带或历史解压），仅在完全缺失时才下载。
	"""
	if engine == "conformer":
		url = CONFORMER_MODEL_URL
		rel_dir = "sherpa-onnx-conformer-zh-stateless2-2023-05-23"
		eng_dir = asr_root / "model_conformer"
	else:
		raise ValueError(f"unsupported transducer engine: {engine}")

	base = _model_dir_with_files(eng_dir)
	if base is None and force_download:
		# 仅当目录里确实没有完整模型时才下载解压
		extract_dir = _download_model_archive(url, asr_root, rel_dir + ".tar.bz2")
		src = _model_dir_with_files(extract_dir)
		if src is None:
			shutil.rmtree(extract_dir, ignore_errors=True)
			raise RuntimeError(f"模型包解压后未找到模型文件: {extract_dir}")
		eng_dir.mkdir(parents=True, exist_ok=True)
		# 把解压出的文件并入 eng_dir，绝不 rmtree 已有目录
		for item in src.iterdir():
			dst = eng_dir / item.name
			if dst.exists():
				if dst.is_dir():
					shutil.rmtree(dst)
				else:
					dst.unlink()
			shutil.move(str(item), str(dst))
		shutil.rmtree(extract_dir, ignore_errors=True)
		print(f"[ASR] {engine} model ready: {eng_dir}")
		base = eng_dir

	if base is None:
		raise RuntimeError(
			f"{engine} 模型未找到: {eng_dir}。"
			"请放入 encoder/decoder/joiner onnx 与 tokens.txt，或开启自动下载。"
		)

	def _pick(part: str) -> str:
		cands = sorted(base.glob(f"*{part}*.onnx"))
		if not cands:
			raise RuntimeError(f"missing {part} onnx under {base}")
		int8 = [c for c in cands if "int8" in c.name]
		return str((int8 or cands)[0])

	return {
		"engine": engine,
		"encoder": _pick("encoder"),
		"decoder": _pick("decoder"),
		"joiner": _pick("joiner"),
		"tokens": str(base / "tokens.txt"),
		"root": str(base),
	}


def ensure_sensevoice_model(asr_model_dir: Path, force_download: bool) -> Path:
	int8_model = asr_model_dir / "model.int8.onnx"
	fp_model = asr_model_dir / "model.onnx"

	# CPU 上优先 int8（4~5 倍速度，精度损失很小）；fp32 仅在无 int8 时使用
	if int8_model.exists():
		return int8_model
	if fp_model.exists():
		return fp_model


def build_asr_recognizer(
	asr_root: Path,
	model_path: Path,
	language: str,
	engine: str = "sensevoice",
	hotwords_file: str = "",
	hotwords_score: float = 2.5,
):
	threads = max(1, (os.cpu_count() or 2) // 2)

	# 热词路径：conformer（离线 transducer）支持热词
	if engine == "conformer":
		files = ensure_transducer_model(asr_root, engine)
		hw = ensure_hotwords_file(Path(hotwords_file) if hotwords_file else None)
		common = {
			"tokens": files["tokens"],
			"encoder": files["encoder"],
			"decoder": files["decoder"],
			"joiner": files["joiner"],
			"num_threads": threads,
			"decoding_method": "modified_beam_search",
			"hotwords_file": str(hw),
			"hotwords_score": hotwords_score,
			"modeling_unit": "cjkchar",
			"provider": "cpu",
		}
		return sherpa_onnx.OfflineRecognizer.from_transducer(**common)

	# 默认 SenseVoice（离线，不支持热词）
	tokens = asr_root / "model" / "tokens.txt"
	if not tokens.exists():
		raise RuntimeError(f"Missing ASR tokens file: {tokens}")

	hr_dict_dir = asr_root / "model" / "dict"
	hr_lexicon = asr_root / "model" / "lexicon.txt"
	hr_rule_fsts = asr_root / "model" / "replace.fst"
	use_hr = hr_dict_dir.exists() and hr_lexicon.exists() and hr_rule_fsts.exists()

	kwargs = {
		"model": str(model_path),
		"tokens": str(tokens),
		"use_itn": True,
		"language": language,
		"num_threads": threads,
		"provider": "cpu",
		"debug": False,
	}
	if use_hr:
		kwargs.update(
			{
				"hr_dict_dir": str(hr_dict_dir),
				"hr_lexicon": str(hr_lexicon),
				"hr_rule_fsts": str(hr_rule_fsts),
			}
		)

	return sherpa_onnx.OfflineRecognizer.from_sense_voice(**kwargs)


def asr_transcribe(recognizer, wav_path: Path, engine: str = "sensevoice") -> str:
	sample_rate, samples = load_wav_mono_16k_float(wav_path)

	stream = recognizer.create_stream()
	stream.accept_waveform(sample_rate, samples)
	recognizer.decode_stream(stream)

	result = stream.result
	text = getattr(result, "text", "")
	if not text:
		text = str(result)
	return text.strip()


def build_tts(tts_root: Path):
	model_dir = tts_root / "model"
	matcha = sherpa_onnx.OfflineTtsMatchaModelConfig(
		acoustic_model=str(model_dir / "model-steps-3.onnx"),
		vocoder=str(model_dir / "vocos-16khz-univ.onnx"),
		lexicon=str(model_dir / "lexicon.txt"),
		tokens=str(model_dir / "tokens.txt"),
		data_dir=str(model_dir / "espeak-ng-data"),
	)
	model_cfg = sherpa_onnx.OfflineTtsModelConfig(
		matcha=matcha,
		num_threads=4,  # 本机实测: 4线程455ms最优；8线程1282ms反而更慢(线程调度/缓存竞争)
		provider="cpu",
	)

	rule_fsts = ",".join(
		[
			str(model_dir / "phone-zh.fst"),
			str(model_dir / "date-zh.fst"),
			str(model_dir / "number-zh.fst"),
		]
	)
	cfg = sherpa_onnx.OfflineTtsConfig(
		model=model_cfg,
		rule_fsts=rule_fsts,
		max_num_sentences=1,
	)

	if not cfg.validate():
		print("[TTS] warning: config validation failed, continue anyway")

	return sherpa_onnx.OfflineTts(cfg)


def tts_speak(tts, text: str, out_wav: Path, play: bool) -> None:
	audio = tts.generate(text, sid=0, speed=1.0)
	sherpa_onnx.write_wave(str(out_wav), audio.samples, audio.sample_rate)
	if play:
		play_wav(out_wav)


def make_reply(text: str):
	text = text.strip()
	if not text:
		return "我没有听清，请再说一遍。", False

	if any(k in text for k in ("退出", "结束", "停止对话", "拜拜", "再见")):
		return "好的，我先退下了。", True

	if "几点" in text or "时间" in text:
		now = dt.datetime.now().strftime("%H点%M分")
		return f"现在是{now}。", False

	if "你好" in text:
		return "你好，我在。", False

	return f"我听到你说：{text}", False


def parse_args():
	kws_root = _resolve_sdk_root("kws1.0.0.1_SDK_16k_10ms_enwatermark_8h")
	asr_root = _resolve_sdk_root("asr_cpu_1.19")
	tts_root = _resolve_sdk_root("tts_cpu_2.1")

	parser = argparse.ArgumentParser(description="Local voice chain: KWS -> ASR -> TTS")
	parser.add_argument("--kws-root", default=str(kws_root))
	parser.add_argument("--asr-root", default=str(asr_root))
	parser.add_argument("--tts-root", default=str(tts_root))

	parser.add_argument(
		"--wake-keyword-bin",
		default=str(kws_root / "res_shuffnet_v2" / "keyword_xiaochuangxiaochuang.bin"),
	)
	parser.add_argument(
		"--wake-any-keyword",
		action="store_true",
		help="Try all built-in keyword_*.bin and wake if any one matches",
	)
	parser.add_argument(
		"--wake-filler",
		default=str(kws_root / "res_shuffnet_v2" / "Filler" / "state_filler_3000s_kladi_1179.txt"),
	)
	parser.add_argument("--wake-mlp", default=str(kws_root / "res_shuffnet_v2" / "mlp.bin"))

	parser.add_argument("--wake-duration", type=float, default=3.0)
	parser.add_argument("--speech-duration", type=float, default=12.0, help="Max speech capture duration in seconds")
	parser.add_argument("--speech-min-duration", type=float, default=1.5, help="Minimum speech capture duration before silence can end turn")
	parser.add_argument("--speech-tail-window", type=float, default=0.8, help="Tail window size in seconds for silence detection")
	parser.add_argument("--speech-silence-threshold-dbfs", type=float, default=-42.0, help="Tail dBFS below this is treated as silence")
	parser.add_argument("--record-backend", choices=["auto", "pulse", "alsa"], default="auto")
	parser.add_argument("--mic-input", default="default")
	parser.add_argument("--wake-audio-file", default="", help="Use existing audio file for wakeup detection")
	parser.add_argument("--speech-audio-file", default="", help="Use existing audio file for ASR input")
	parser.add_argument(
		"--require-wake-each-turn",
		action="store_true",
		help="Require wake word before every dialog turn (legacy behavior)",
	)
	parser.add_argument(
		"--session-idle-rounds",
		type=int,
		default=3,
		help="After N consecutive empty ASR rounds, session sleeps and needs wake word again",
	)

	parser.add_argument("--asr-language", default="zh")
	parser.add_argument("--asr-model", default="")
	parser.add_argument("--no-auto-download-asr", action="store_true")
	parser.add_argument(
		"--asr-engine",
		choices=["sensevoice", "conformer"],
		default="sensevoice",
		help="sensevoice=不支持热词(默认)；conformer=离线大模型+热词",
	)
	parser.add_argument("--hotwords-file", default="", help="热词文件路径，默认自动生成 hotwords.txt")
	parser.add_argument("--hotwords-score", type=float, default=2.5, help="热词增益分数")

	parser.add_argument("--once", action="store_true", help="Run one dialog turn and exit")
	parser.add_argument("--no-play", action="store_true", help="Do not play TTS audio")
	parser.add_argument("--work-dir", default="/tmp/local_voice_chat")
	return parser.parse_args()


def main():
	args = parse_args()

	check_cmd_exists("ffmpeg")
	check_cmd_exists("ffplay")
	check_cmd_exists("curl")

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
	else:  # conformer
		asr_model_path = Path(args.asr_model) if args.asr_model else None

	print("[INIT] building ASR recognizer...")
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
		print(f"[INIT] wake mode: single keyword -> {wakeword_hint_from_bin(wake_keyword_bin)}")
	if args.require_wake_each_turn:
		print("[INIT] dialog mode: require wake word for every turn")
	else:
		print("[INIT] dialog mode: wake once, then continuous conversation")
	print("[INIT] ready.")

	session_awake = False
	idle_rounds = 0
	file_mode = bool(args.wake_audio_file or args.speech_audio_file)

	while True:
		wake_wav = work_dir / "wake.wav"
		user_wav = work_dir / "user.wav"
		reply_wav = work_dir / "reply.wav"

		need_wake = args.require_wake_each_turn or (not session_awake)
		if need_wake:
			if args.wake_audio_file:
				wake_input = Path(args.wake_audio_file)
				if not wake_input.exists():
					raise RuntimeError(f"wake audio file not found: {wake_input}")
				wake_wav = wake_input
				print(f"\n[WAIT] using wake audio file: {wake_wav}")
			else:
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
					print("[MIC] warning: volume is very low, please move closer or increase mic gain")

			if args.wake_any_keyword:
				hit, hit_keyword, raw, matched_bin = detect_wakeup_any(
					kws_root,
					wake_wav,
					wake_keyword_bins,
					wake_filler,
					wake_mlp,
				)
			else:
				hit, hit_keyword, raw = detect_wakeup(
					kws_root,
					wake_wav,
					wake_keyword_bin,
					wake_filler,
					wake_mlp,
				)
				matched_bin = wake_keyword_bin
			if not hit:
				print("[KWS] no wake word detected.")
				if args.wake_any_keyword:
					print("[KWS] tried all built-in wake words. Please retry speaking clearly.")
				else:
					print(f"[KWS] expected wake word: {wakeword_hint_from_bin(wake_keyword_bin)}")
				if args.wake_audio_file:
					print("[EXIT] wake audio file mode finished")
					break
				continue

			print(f"[KWS] wake word detected: {hit_keyword or '(unknown)'}")
			print(f"[KWS] matched model: {matched_bin.name}")
			session_awake = True
			idle_rounds = 0
			if not args.require_wake_each_turn:
				print("[SESSION] wake accepted. Continuous dialog is active.")

		if args.speech_audio_file:
			speech_input = Path(args.speech_audio_file)
			if not speech_input.exists():
				raise RuntimeError(f"speech audio file not found: {speech_input}")
			print(f"[ASR] using speech audio file: {speech_input}")
			convert_to_wav_16k_mono(speech_input, user_wav)
		else:
			if args.require_wake_each_turn:
				print("[ASR] listening for user query...")
			else:
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
		print(f"[ASR] text: {text}")
		if not text:
			idle_rounds += 1
			print(f"[ASR] empty speech ({idle_rounds}/{args.session_idle_rounds})")
			if (not args.require_wake_each_turn) and idle_rounds >= args.session_idle_rounds:
				session_awake = False
				idle_rounds = 0
				print("[SESSION] idle timeout. Wake word is required again.")
			if file_mode:
				print("[EXIT] file mode finished")
				break
			continue

		idle_rounds = 0
		reply, should_exit = make_reply(text)
		print(f"[TTS] reply: {reply}")
		tts_speak(tts, reply, reply_wav, play=not args.no_play)

		if file_mode:
			print("[EXIT] file mode finished")
			break

		if args.once or should_exit:
			print("[EXIT] done")
			break


if __name__ == "__main__":
	main()
