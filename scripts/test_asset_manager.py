import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import asset_manager as am


class AssetManagerLocalVisualTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_vis, self.old_cache = am.VIS, am.CACHE
        self.old_preexisting = set(am.PREEXISTING)
        am.VIS = self.root / "input" / "visuals"
        am.CACHE = self.root / "cache"
        am.PREEXISTING.clear()
        am.usage.clear()
        am.VIS.mkdir(parents=True)
        am.CACHE.mkdir(parents=True)

    def tearDown(self):
        am.VIS, am.CACHE = self.old_vis, self.old_cache
        am.PREEXISTING.clear()
        am.PREEXISTING.update(self.old_preexisting)
        am.usage.clear()
        self.tmp.cleanup()

    def test_local_image_is_converted_to_motion_video_for_narrated_scene(self):
        (am.VIS / "hero.jpg").write_bytes(b"image-placeholder")
        def fake_motion(image_path, query, orient, slot):
            (am.VIS / "rv-motion-test.mp4").write_bytes(b"video-placeholder")
            return "rv-motion-test.mp4"
        jobs = [{"id": "bezos-long", "orientation": "portrait", "platform": "youtube",
                 "script": "A narrated scene about business history. [Visual: hero.jpg]"}]
        with patch.object(am, "image_to_motion", side_effect=fake_motion), patch.object(am, "media_duration", return_value=8.0):
            episodes, rewritten = am.process_jobs(jobs)
        scene = episodes[0]["scenes"][0]
        self.assertEqual(scene["type"], "broll")
        self.assertIsNone(scene["asset"])
        self.assertEqual(scene["asset_video"], "input/visuals/rv-motion-test.mp4")
        self.assertIn("[Visual: rv-motion-test.mp4]", rewritten[0]["script"])

    def test_local_video_is_manifested_as_broll(self):
        (am.VIS / "hero.mp4").write_bytes(b"video-placeholder")
        jobs = [{"id": "bezos-long", "orientation": "portrait", "platform": "youtube",
                 "script": "A narrated scene about business history. [Visual: hero.mp4]"}]
        with patch.object(am, "media_duration", return_value=8.0):
            episodes, _ = am.process_jobs(jobs)
        scene = episodes[0]["scenes"][0]
        self.assertEqual(scene["type"], "broll")
        self.assertEqual(scene["asset_video"], "input/visuals/hero.mp4")


if __name__ == "__main__":
    unittest.main()
