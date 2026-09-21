import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from smartapp_runtime.config import (
    ConfigError,
    LimitConfig,
    LoggingConfig,
    NetworkConfig,
    PathsConfig,
    ProcessConfig,
    RendererConfig,
    RuntimeConfig,
    TimeoutConfig,
    load_config,
)


class ConfigTests(unittest.TestCase):
    def test_default_protocol_violation_limit_is_five(self):
        with tempfile.TemporaryDirectory() as raw:
            path = self.write_config(Path(raw), "")
            self.assertEqual(load_config(path).process.protocol_violation_limit, 5)

    def test_file_limit_is_configurable_and_requires_exact_positive_integer(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = self.write_config(root, "[limits]\nmax_file_bytes = 123\n")
            self.assertEqual(load_config(path).limits.max_file_bytes, 123)
            for value in ("true", "0", "-1", "1.5"):
                path = self.write_config(root, "[limits]\nmax_file_bytes = " + value + "\n")
                with self.assertRaises(ConfigError):
                    load_config(path)

    def write_config(self, root, content, include_paths=True):
        path = root / "runtime.toml"
        prefix = '[paths]\nroot = "' + str(root) + '"\n' if include_paths else ""
        path.write_text(prefix + content, encoding="utf-8")
        return path

    def test_loads_python38_compatible_toml(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = self.write_config(
                root, '[paths]\nroot = "' + raw + '/data"\n', include_paths=False
            )

            config = load_config(path)

            self.assertEqual(config.paths.root, root / "data")
            self.assertEqual(config.paths.socket, root / "data/run/runtime.sock")
            self.assertEqual(config.paths.log, root / "data/logs/runtime.jsonl")
            self.assertEqual(config.network.static_host, "127.0.0.1")
            self.assertEqual(config.limits.max_message_bytes, 1024 * 1024)

    def test_rejects_unknown_top_level_key(self):
        with tempfile.TemporaryDirectory() as raw:
            path = self.write_config(Path(raw), "[unknown]\nvalue = 1\n")

            with self.assertRaisesRegex(ConfigError, "unknown configuration section"):
                load_config(path)

    def test_rejects_unknown_nested_key(self):
        with tempfile.TemporaryDirectory() as raw:
            path = self.write_config(Path(raw), "[network]\nunknown = 1\n")

            with self.assertRaisesRegex(ConfigError, "unknown configuration key"):
                load_config(path)

    def test_rejects_configuration_sections_that_are_not_tables(self):
        with tempfile.TemporaryDirectory() as raw:
            path = self.write_config(
                Path(raw), 'paths = "not-a-table"\n', include_paths=False
            )

            with self.assertRaisesRegex(ConfigError, "must be a table"):
                load_config(path)

    def test_rejects_non_loopback_static_host(self):
        with tempfile.TemporaryDirectory() as raw:
            path = self.write_config(Path(raw), '[network]\nstatic_host = "192.0.2.1"\n')

            with self.assertRaisesRegex(ConfigError, "loopback"):
                load_config(path)

    def test_rejects_boolean_port_and_ephemeral_toml_port(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            boolean_path = self.write_config(root, "[network]\nstatic_port = true\n")
            with self.assertRaisesRegex(ConfigError, "static_port"):
                load_config(boolean_path)

            zero_path = self.write_config(root, "[network]\nstatic_port = 0\n")
            with self.assertRaisesRegex(ConfigError, "static_port"):
                load_config(zero_path)

    def test_rejects_invalid_timeout_values(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for value in ("true", "nan", "inf", "0"):
                with self.subTest(value=value):
                    path = self.write_config(root, "[timeouts]\ndownload = " + value + "\n")
                    with self.assertRaisesRegex(ConfigError, "timeouts.download"):
                        load_config(path)

    def test_rejects_file_roots_and_unwritable_missing_root_parents(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            root_file = root / "not-a-directory"
            root_file.write_text("content", encoding="utf-8")
            file_path = self.write_config(
                root,
                '[paths]\nroot = "' + str(root_file) + '"\n',
                include_paths=False,
            )
            with self.assertRaisesRegex(ConfigError, "must be a directory"):
                load_config(file_path)

            missing_root = root / "missing" / "runtime"
            parent_path = self.write_config(
                root,
                '[paths]\nroot = "' + str(missing_root) + '"\n',
                include_paths=False,
            )
            with patch("smartapp_runtime.config.os.access", return_value=False):
                with self.assertRaisesRegex(ConfigError, "must be writable"):
                    load_config(parent_path)

    def test_normalizes_log_level_and_validates_command_renderer(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            valid_path = self.write_config(
                root,
                "[logging]\nlevel = \"debug\"\n"
                "[renderer]\nkind = \"command\"\n"
                "load_argv = [\"load\"]\n"
                "send_argv = [\"send\"]\n"
                "stop_argv = [\"stop\"]\n"
                "restore_argv = [\"restore\"]\n",
            )
            self.assertEqual(load_config(valid_path).logging.level, "DEBUG")

            invalid_path = self.write_config(
                root,
                "[renderer]\nkind = \"command\"\nload_argv = [\"load\"]\n",
            )
            with self.assertRaisesRegex(ConfigError, "command renderer"):
                load_config(invalid_path)

    def test_validates_process_renderer(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            valid_path = self.write_config(
                root,
                "[renderer]\nkind = \"process\"\n"
                "process_argv = [\"screen-runtime\", \"{url}\"]\n"
                "restore_argv = [\"restore-expression\"]\n",
            )
            config = load_config(valid_path).renderer
            self.assertEqual(config.kind, "process")
            self.assertEqual(config.process_argv, ("screen-runtime", "{url}"))

            for content in (
                '[renderer]\nkind = "process"\nrestore_argv = ["restore"]\n',
                '[renderer]\nkind = "process"\nprocess_argv = ["screen"]\n',
                '[renderer]\nkind = "process"\nprocess_argv = ["screen"]\n'
                'restore_argv = ["restore"]\nsend_argv = ["send"]\n',
            ):
                with self.subTest(content=content):
                    path = self.write_config(root, content)
                    with self.assertRaisesRegex(ConfigError, "process renderer"):
                        load_config(path)

    def test_rejects_non_positive_limit_and_duplicate_environment_variable(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            limit_path = self.write_config(root, "[limits]\nmax_files = 0\n")
            with self.assertRaisesRegex(ConfigError, "max_files"):
                load_config(limit_path)

            process_path = self.write_config(
                root, '[process]\nenv_passthrough = ["PATH", "PATH"]\n'
            )
            with self.assertRaisesRegex(ConfigError, "env_passthrough"):
                load_config(process_path)

    def test_rejects_invalid_environment_variable_names(self):
        with tempfile.TemporaryDirectory() as raw:
            path = self.write_config(
                Path(raw), '[process]\nenv_passthrough = ["INVALID-NAME"]\n'
            )

            with self.assertRaisesRegex(ConfigError, "env_passthrough"):
                load_config(path)

    def test_rejects_invalid_renderer_kinds_and_argv_values(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for content, pattern in (
                ('[renderer]\nkind = "unsupported"\n', "renderer.kind"),
                ('[renderer]\nload_argv = ["load", 1]\n', "load_argv"),
            ):
                with self.subTest(content=content):
                    path = self.write_config(root, content)
                    with self.assertRaisesRegex(ConfigError, pattern):
                        load_config(path)

    def test_rejects_invalid_logging_level_and_target(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for content, pattern in (
                ('[logging]\nlevel = "TRACE"\n', "logging.level"),
                ('[logging]\ntarget = "stdout"\n', "logging.target"),
            ):
                with self.subTest(content=content):
                    path = self.write_config(root, content)
                    with self.assertRaisesRegex(ConfigError, pattern):
                        load_config(path)

    def test_configuration_dataclasses_are_immutable(self):
        cases = (
            (PathsConfig(), "root", Path("/other")),
            (NetworkConfig(), "static_port", 18082),
            (TimeoutConfig(), "download", 1.0),
            (LimitConfig(), "max_files", 1),
            (ProcessConfig(), "protocol_violation_limit", 1),
            (RendererConfig(), "kind", "command"),
            (LoggingConfig(), "level", "DEBUG"),
            (RuntimeConfig(), "network", NetworkConfig()),
        )
        for config, field, value in cases:
            with self.subTest(config=type(config).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(config, field, value)

    def test_network_config_allows_ephemeral_port_in_memory(self):
        self.assertEqual(NetworkConfig(static_port=0).static_port, 0)


if __name__ == "__main__":
    unittest.main()
