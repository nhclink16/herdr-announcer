import tempfile
import unittest
from pathlib import Path
from unittest import mock

from announcer import config as config_module


SHARED_CONFIG = '''
# shared valid fixture
announce = ["done", "blocked"]
debounce_seconds = 12
summary = "command"
custom_prompt = "keep # inside"
toast = true
'''


class TinyTomlTests(unittest.TestCase):
    def write_config(self, directory, content):
        path = Path(directory) / "config.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_supported_values_comments_and_hash_in_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(directory, SHARED_CONFIG)
            parsed = config_module._load_tiny_toml(path)

        self.assertEqual(parsed["announce"], ["done", "blocked"])
        self.assertEqual(parsed["debounce_seconds"], 12)
        self.assertEqual(parsed["summary"], "command")
        self.assertEqual(parsed["custom_prompt"], "keep # inside")
        self.assertIs(parsed["toast"], True)

    def test_unparseable_lines_are_skipped_with_reasons(self):
        content = '''
summary = "template"
[voice]
float_value = 1.5
announce = [
  "done",
]
toast = false
'''
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(directory, content)
            reasons = []
            parsed = config_module._load_tiny_toml(path, reasons)

        self.assertEqual(parsed, {"summary": "template", "toast": False})
        self.assertEqual(
            reasons,
            [
                "config: skipped line 3",
                "config: skipped line 4",
                "config: skipped line 5",
                "config: skipped line 6",
                "config: skipped line 7",
            ],
        )

    def test_unknown_keys_are_collected_and_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_config(
                directory,
                'summary = "template"\nzebra = 1\nalpha = "x"\n',
            )
            unknown = []
            loaded = config_module.load_config(Path(directory), unknown_keys=unknown)

        self.assertEqual(loaded["summary"], "template")
        self.assertNotIn("zebra", loaded)
        self.assertEqual(unknown, ["alpha", "zebra"])

    @unittest.skipIf(config_module.tomllib is None, "tomllib unavailable")
    def test_tomllib_and_tiny_parser_match_on_shared_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(directory, SHARED_CONFIG)
            tiny = config_module._load_tiny_toml(path)
            with path.open("rb") as handle:
                standard = config_module.tomllib.load(handle)

        self.assertEqual(tiny, standard)

    def test_load_config_threads_tiny_parser_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_config(directory, "[table]\nsummary = \"template\"\n")
            reasons = []
            with mock.patch.object(config_module, "tomllib", None):
                loaded = config_module.load_config(Path(directory), reasons=reasons)

        self.assertEqual(loaded["summary"], "template")
        self.assertEqual(reasons, ["config: skipped line 1"])


if __name__ == "__main__":
    unittest.main()
