import importlib
import inspect
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import local_voice_chat


class OpenSourceWakeCleanupTest(unittest.TestCase):
    def test_legacy_kws_helpers_removed(self):
        source = inspect.getsource(local_voice_chat)
        for needle in (
            "_resolve_legacy_kws_root",
            "ivw_demo",
            "keyword_*.bin",
            "res_shuffnet_v2",
        ):
            self.assertNotIn(needle, source)


class WakeLoopNoiseGateTest(unittest.TestCase):
    def test_wake_min_level_dbfs_argument_exists(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        old_argv = sys.argv[:]
        try:
            sys.argv = ["voice_bridge.py", "--wake-min-level-dbfs", "-40"]
            args = voice_bridge.parse_args()
            self.assertEqual(args.wake_min_level_dbfs, -40.0)
        finally:
            sys.argv = old_argv

    def test_default_wake_words_are_in_hotword_list(self):
        model_mod = importlib.import_module("local_voice_chat.local_voice_chat")
        self.assertIn("小远同学", model_mod.DEFAULT_HOTWORDS)
        self.assertIn("xiaoyuan", model_mod.DEFAULT_HOTWORDS)

        bridge_mod = importlib.import_module("local_voice_chat.voice_bridge")
        old_argv = sys.argv[:]
        try:
            sys.argv = ["voice_bridge.py"]
            args = bridge_mod.parse_args()
        finally:
            sys.argv = old_argv
        self.assertGreaterEqual(args.hotwords_score, 3.0)

    def test_download_media_library_name_is_correct(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        self.assertTrue(hasattr(voice_bridge, "DOWNLOAD_MEDIA_LIBRARY"))
        self.assertTrue(hasattr(voice_bridge, "_DOWNLOAD_MEDIA_LIBRARY"))
        self.assertIs(voice_bridge._DOWNLOAD_MEDIA_LIBRARY, voice_bridge.DOWNLOAD_MEDIA_LIBRARY)

    def test_play_aliases_compatibility_name_exists(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        self.assertTrue(hasattr(voice_bridge, "PLAY_ALIASES"))
        self.assertTrue(hasattr(voice_bridge, "_PLAY_ALIASES"))
        self.assertIs(voice_bridge._PLAY_ALIASES, voice_bridge.PLAY_ALIASES)

    def test_download_failure_message_is_explicit(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")

        class DummyArgs:
            jellyfin_api_key = None

        msg = voice_bridge._download_failed_play_test_video(
            DummyArgs(),
            "外部视频源无法解析",
        )
        self.assertIn("我已经尝试下载", msg)
        self.assertIn("没有写入 Movies 文件夹", msg)
        self.assertIn("外部视频源无法解析", msg)


class AudioGainNormalizeTest(unittest.TestCase):
    """低电平麦克风必须被补偿到 ASR 可用响度。"""

    def _dbfs(self, samples):
        import numpy as np

        rms = float(np.sqrt(np.mean(np.square(samples.astype("float32")))))
        return 20.0 * __import__("math").log10(max(rms, 1e-9))

    def test_quiet_clip_is_boosted_toward_target(self):
        import numpy as np

        model_mod = importlib.import_module("local_voice_chat.local_voice_chat")
        t = np.arange(16000, dtype="float32") / 16000.0
        # 约 -70 dBFS，模拟实测的极低电平录音
        quiet = (0.0003 * np.sin(2 * np.pi * 220 * t)).astype("float32")
        boosted, gain_db = model_mod.normalize_audio_gain(quiet)

        self.assertGreater(gain_db, 20.0)
        self.assertGreater(self._dbfs(boosted), self._dbfs(quiet) + 20.0)

    def test_boost_never_clips(self):
        import numpy as np

        model_mod = importlib.import_module("local_voice_chat.local_voice_chat")
        t = np.arange(16000, dtype="float32") / 16000.0
        quiet = (0.0003 * np.sin(2 * np.pi * 220 * t)).astype("float32")
        boosted, _ = model_mod.normalize_audio_gain(quiet)
        self.assertLessEqual(float(np.max(np.abs(boosted))), 1.0)

    def test_loud_clip_is_not_attenuated(self):
        import numpy as np

        model_mod = importlib.import_module("local_voice_chat.local_voice_chat")
        t = np.arange(16000, dtype="float32") / 16000.0
        loud = (0.5 * np.sin(2 * np.pi * 220 * t)).astype("float32")
        boosted, gain_db = model_mod.normalize_audio_gain(loud)
        self.assertEqual(gain_db, 0.0)
        self.assertTrue(np.array_equal(boosted, loud))

    def test_empty_clip_is_safe(self):
        import numpy as np

        model_mod = importlib.import_module("local_voice_chat.local_voice_chat")
        boosted, gain_db = model_mod.normalize_audio_gain(np.array([], dtype="float32"))
        self.assertEqual(gain_db, 0.0)
        self.assertEqual(boosted.size, 0)

    def test_runtime_env_gain_overrides_are_respected(self):
        import os
        import numpy as np

        model_mod = importlib.import_module("local_voice_chat.local_voice_chat")
        old = {
            "VOICE_ASR_GAIN_TARGET_DBFS": os.environ.get("VOICE_ASR_GAIN_TARGET_DBFS"),
            "VOICE_ASR_TARGET_RMS_DBFS": os.environ.get("VOICE_ASR_TARGET_RMS_DBFS"),
            "VOICE_ASR_MAX_GAIN_DB": os.environ.get("VOICE_ASR_MAX_GAIN_DB"),
            "VOICE_ASR_PEAK_CEILING_DBFS": os.environ.get("VOICE_ASR_PEAK_CEILING_DBFS"),
        }
        try:
            os.environ["VOICE_ASR_GAIN_TARGET_DBFS"] = "-10.0"
            os.environ["VOICE_ASR_TARGET_RMS_DBFS"] = "-10.0"
            os.environ["VOICE_ASR_MAX_GAIN_DB"] = "10.0"
            os.environ["VOICE_ASR_PEAK_CEILING_DBFS"] = "-3.0"

            t = np.arange(16000, dtype="float32") / 16000.0
            quiet = (0.0003 * np.sin(2 * np.pi * 220 * t)).astype("float32")
            _, gain_db = model_mod.normalize_audio_gain(quiet)
            self.assertLess(gain_db, 15.0)
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value




class ConversationGuardRegressionTest(unittest.TestCase):
    def test_short_style_only_text_is_incomplete(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        self.assertTrue(voice_bridge._looks_like_incomplete_command("日系"))
        self.assertTrue(voice_bridge._looks_like_incomplete_command("复古"))
        self.assertTrue(voice_bridge._is_irrelevant_speech("日系"))

    def test_pending_filter_does_not_reuse_stale_style(self):
        import unittest.mock as mock

        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        pending = {"target": "旅行", "style": "vintage", "dry": True, "attempts": 0}

        with mock.patch.object(
            voice_bridge,
            "_run_image_batch_reply",
            side_effect=AssertionError("stale pending filter should not execute"),
        ):
            reply, new_pending = voice_bridge._consume_pending_image_filter(
                "把家庭相册的照片加个滤镜",
                pending,
            )

        self.assertIn("风格", reply)
        self.assertEqual(new_pending["target"], "家庭相册")
        self.assertIsNone(new_pending["style"])

    def test_tts_cut_uses_sentence_boundary(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        reply = "已处理完成：家庭相册已按复古风格处理，成功23张，输出到各原目录/复古风格/。"
        spoken = voice_bridge._truncate_tts_text(reply, 18)
        self.assertTrue(spoken.endswith("。"))
        self.assertLessEqual(len(spoken), 22)
        self.assertNotIn("23张", spoken)

    def test_immich_skips_file_management_queries(self):
        import unittest.mock as mock

        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("Immich should not run")):
            self.assertIsNone(voice_bridge._fast_local_immich_reply(None, "家庭相册里有哪些文件"))

    def test_directory_listing_query_uses_local_filesystem(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        reply = voice_bridge._fast_local_directory_listing_reply(None, "家庭相册里有哪些文件")
        self.assertIsNotNone(reply)
        self.assertIn("家庭相册", reply)
        self.assertTrue("文件" in reply or "有" in reply)

    def test_download_filename_is_sanitized(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        self.assertEqual(voice_bridge._sanitize_download_filename("oceans [oceans].mp4"), "oceans.mp4")


if __name__ == "__main__":
    unittest.main()
