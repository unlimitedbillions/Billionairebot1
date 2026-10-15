import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import asset_manager as am


class AssetManagerLocalVisualTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_vis, self.old_cache, self.old_manifest = am.VIS, am.CACHE, am.MANIFEST
        self.old_preexisting = set(am.PREEXISTING)
        self.old_attributions = dict(am.ASSET_ATTRIBUTIONS)
        self.old_unlicensed = set(am.UNLICENSED_CACHED_ASSETS)
        am.ASSET_ATTRIBUTIONS.clear()
        am.UNLICENSED_CACHED_ASSETS.clear()
        am.VIS = self.root / "input" / "visuals"
        am.CACHE = self.root / "cache"
        am.MANIFEST = am.CACHE / "assets.json"
        am.PREEXISTING.clear()
        am.usage.clear()
        am.VIS.mkdir(parents=True)
        am.CACHE.mkdir(parents=True)

    def tearDown(self):
        am.VIS, am.CACHE, am.MANIFEST = self.old_vis, self.old_cache, self.old_manifest
        am.PREEXISTING.clear()
        am.PREEXISTING.update(self.old_preexisting)
        am.ASSET_ATTRIBUTIONS.clear()
        am.ASSET_ATTRIBUTIONS.update(self.old_attributions)
        am.UNLICENSED_CACHED_ASSETS.clear()
        am.UNLICENSED_CACHED_ASSETS.update(self.old_unlicensed)
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

    def test_pexels_credits_are_added_to_upload_description_and_manifest(self):
        am.ASSET_ATTRIBUTIONS["pexels-clip.mp4"] = {
            "provider": "pexels", "source_url": "https://www.pexels.com/video/example-123/",
            "photographer": "Example Creator", "photographer_url": "https://www.pexels.com/@example/",
            "media_type": "video", "media_id": 123,
        }
        jobs = [{"id": "bezos-long", "orientation": "portrait", "platform": "youtube",
                 "description": "Original description.",
                 "script": "Narrated business history. [Visual: startup office]"}]
        with patch.object(am, "assign", return_value=("pexels-clip.mp4", "pexels", "video")), patch.object(am, "media_duration", return_value=8.0):
            episodes, rewritten = am.process_jobs(jobs)
        am.apply_attribution_credits(episodes, rewritten)
        scene = episodes[0]["scenes"][0]
        description = rewritten[0]["description"]
        self.assertEqual(scene["attribution"]["provider"], "pexels")
        self.assertIn("https://www.pexels.com/", description)
        self.assertIn("Example Creator", description)
        self.assertIn("https://www.pexels.com/video/example-123/", description)
        self.assertTrue(description.startswith("Original description."))



    def test_license_policy_allows_commercial_adaptable_media_only(self):
        self.assertTrue(am.license_allows_commercial_use("CC0 1.0", "https://creativecommons.org/publicdomain/zero/1.0/"))
        self.assertTrue(am.license_allows_commercial_use("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"))
        self.assertTrue(am.license_allows_commercial_use("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/"))
        self.assertFalse(am.license_allows_commercial_use("CC BY-NC 4.0", "https://creativecommons.org/licenses/by-nc/4.0/"))
        self.assertFalse(am.license_allows_commercial_use("CC BY-ND 4.0", "https://creativecommons.org/licenses/by-nd/4.0/"))
        self.assertFalse(am.license_allows_commercial_use("", ""))

    def test_wikimedia_search_preserves_creator_and_license_metadata(self):
        response = {
            "query": {"pages": {"1": {"index": 1, "title": "File:Example.jpg", "imageinfo": [{
                "mime": "image/jpeg", "size": 12000,
                "thumburl": "https://upload.wikimedia.org/example.jpg",
                "url": "https://upload.wikimedia.org/original-example.jpg",
                "extmetadata": {
                    "LicenseShortName": {"value": "CC BY 4.0"},
                    "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"},
                    "Artist": {"value": "<a href='https://example.com/creator'>Example Creator</a>"},
                },
            }]}}},
        }
        with patch.object(am, "get", return_value=response):
            results = am.wikimedia_images("Example Creator")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["provider"], "wikimedia")
        self.assertEqual(results[0]["creator"], "Example Creator")
        self.assertEqual(results[0]["license"], "CC BY 4.0")
        self.assertIn("commons.wikimedia.org/wiki/File:Example.jpg", results[0]["source_url"])

    def test_wikimedia_rejects_noncommercial_license(self):
        response = {
            "query": {"pages": {"1": {"index": 1, "title": "File:Example.jpg", "imageinfo": [{
                "mime": "image/jpeg", "size": 12000,
                "thumburl": "https://upload.wikimedia.org/example.jpg",
                "url": "https://upload.wikimedia.org/original-example.jpg",
                "extmetadata": {
                    "LicenseShortName": {"value": "CC BY-NC 4.0"},
                    "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-nc/4.0/"},
                    "Artist": {"value": "Example Creator"},
                },
            }]}}},
        }
        with patch.object(am, "get", return_value=response):
            self.assertEqual(am.wikimedia_images("Example Creator"), [])

    def test_openverse_filters_noncommercial_results_and_preserves_credit(self):
        response = {"results": [
            {"url": "https://example.org/by.jpg", "title": "A photo", "creator": "Creator",
             "creator_url": "https://example.org/creator", "foreign_landing_url": "https://example.org/work",
             "license": "by", "license_url": "https://creativecommons.org/licenses/by/4.0/"},
            {"url": "https://example.org/nc.jpg", "title": "NC photo", "creator": "Creator",
             "foreign_landing_url": "https://example.org/nc-work", "license": "by-nc",
             "license_url": "https://creativecommons.org/licenses/by-nc/4.0/"},
        ]}
        with patch.object(am, "get", return_value=response):
            results = am.openverse_images("business office")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["provider"], "openverse")
        self.assertEqual(results[0]["source_url"], "https://example.org/work")
        self.assertEqual(results[0]["license"], "by")

    def test_archive_skips_unknown_license_and_keeps_licensed_metadata(self):
        search = {"response": {"docs": [{"identifier": "licensed-item", "title": "Business footage", "description": "office"}]}}
        metadata = {"metadata": {
            "licenseurl": "https://creativecommons.org/licenses/by/4.0/",
            "creator": "Archive Creator",
        }, "files": [{"name": "business.mp4", "format": "MPEG4", "size": "100000"}]}
        with patch.object(am, "get", side_effect=[search, metadata]):
            results = am.archive_videos("business office")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["provider"], "archive")
        self.assertEqual(results[0]["creator"], "Archive Creator")
        self.assertEqual(results[0]["license_url"], "https://creativecommons.org/licenses/by/4.0/")

        unlicensed = {"metadata": {"creator": "Unknown Creator"},
                      "files": [{"name": "business.mp4", "format": "MPEG4", "size": "100000"}]}
        with patch.object(am, "get", side_effect=[search, unlicensed]):
            self.assertEqual(am.archive_videos("business office"), [])

    def test_legacy_stock_without_license_metadata_is_not_reused(self):
        asset_name = "rv-legacy-unlicensed.mp4"
        (am.VIS / asset_name).write_bytes(b"legacy-video-placeholder")
        am.PREEXISTING.add(asset_name)
        am.MANIFEST.write_text(json.dumps({"episodes": [{"scenes": [{
            "asset_video": f"input/visuals/{asset_name}",
            "source": "archive", "query": "business office", "attribution": None,
        }]}]}), encoding="utf-8")

        am.load_previous_attributions()
        self.assertIn(asset_name, am.UNLICENSED_CACHED_ASSETS)

        query = "business office"
        expected_cached_name = "rv-" + am.h(f"video|{query}|portrait|0") + ".mp4"
        (am.VIS / expected_cached_name).write_bytes(b"cached-unlicensed-video")
        am.PREEXISTING.add(expected_cached_name)
        am.UNLICENSED_CACHED_ASSETS.add(expected_cached_name)
        with patch.object(am, "fetch_video", return_value=("rv-fresh-licensed.mp4", "pexels")):
            result = am.assign(query, "portrait")
        self.assertEqual(result[0], "rv-fresh-licensed.mp4")
        self.assertNotEqual(result[0], expected_cached_name)

    def test_wikimedia_credits_are_added_to_upload_description(self):
        episode = {"scenes": [{"attribution": {
            "provider": "wikimedia", "source_url": "https://commons.wikimedia.org/wiki/File:Example.jpg",
            "title": "File:Example.jpg", "creator": "Example Creator",
            "license": "CC BY 4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/",
        }}]}
        jobs = [{"description": "Original description."}]
        am.apply_attribution_credits([episode], jobs)
        self.assertIn("Original description.", jobs[0]["description"])
        self.assertIn("Example Creator", jobs[0]["description"])
        self.assertIn("CC BY 4.0", jobs[0]["description"])
        self.assertIn("https://commons.wikimedia.org/wiki/File:Example.jpg", jobs[0]["description"])

if __name__ == "__main__":
    unittest.main()
