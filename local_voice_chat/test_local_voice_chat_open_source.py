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


if __name__ == "__main__":
    unittest.main()
