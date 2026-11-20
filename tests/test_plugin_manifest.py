import unittest

from core.plugin_manifest import normalize_manifest, validate_manifest


class PluginManifestTest(unittest.TestCase):
    def test_valid_manifest(self):
        manifest = normalize_manifest({
            "id": "demo_skill",
            "name": "Demo",
            "version": "1.0.0",
            "api_version": 1,
            "min_host_version": "0.9.0",
            "entry": "plugin.py:PLUGIN",
        })
        self.assertEqual(validate_manifest(manifest, expected_id="demo_skill"), [])

    def test_invalid_id_and_api(self):
        manifest = normalize_manifest({
            "id": "../evil",
            "name": "Bad",
            "version": "1",
            "api_version": 99,
            "entry": "plugin.py",
        })
        errors = validate_manifest(manifest, expected_id="demo")
        self.assertTrue(any("id" in item for item in errors))
        self.assertTrue(any("api_version" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
