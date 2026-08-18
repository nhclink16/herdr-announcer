import os
import unittest
from pathlib import Path
from unittest import mock

from announcer import paths


class ResolveDirsTests(unittest.TestCase):
    def setUp(self):
        self.config_dir = Path("/plugin/config")
        self.state_dir = Path("/plugin/state")
        self.fallback = (Path("/fallback/config"), Path("/fallback/state"))

    def test_both_environment_dirs_avoid_fallback_probe(self):
        environment = {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config_dir),
            "HERDR_PLUGIN_STATE_DIR": str(self.state_dir),
        }
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            paths, "resolve_dirs_without_env"
        ) as fallback:
            result = paths.resolve_dirs()

        self.assertEqual(result, (self.config_dir, self.state_dir))
        fallback.assert_not_called()

    def test_private_resolver_name_aliases_public_api(self):
        self.assertIs(paths._resolve_dirs_without_env, paths.resolve_dirs_without_env)

    def test_missing_state_dir_never_falls_back_to_cwd(self):
        environment = {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config_dir),
            "HERDR_PLUGIN_STATE_DIR": "",
        }
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            paths, "resolve_dirs_without_env", return_value=self.fallback
        ):
            result = paths.resolve_dirs()

        self.assertEqual(result, (self.config_dir, self.fallback[1]))
        self.assertNotEqual(result[1], Path("."))

    def test_missing_config_dir_preserves_environment_state_dir(self):
        environment = {
            "HERDR_PLUGIN_CONFIG_DIR": "",
            "HERDR_PLUGIN_STATE_DIR": str(self.state_dir),
        }
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            paths, "resolve_dirs_without_env", return_value=self.fallback
        ):
            result = paths.resolve_dirs()

        self.assertEqual(result, (self.fallback[0], self.state_dir))

    def test_empty_environment_values_use_both_fallbacks(self):
        environment = {
            "HERDR_PLUGIN_CONFIG_DIR": "",
            "HERDR_PLUGIN_STATE_DIR": "",
        }
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            paths, "resolve_dirs_without_env", return_value=self.fallback
        ):
            result = paths.resolve_dirs()

        self.assertEqual(result, self.fallback)


if __name__ == "__main__":
    unittest.main()
