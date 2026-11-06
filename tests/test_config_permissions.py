import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from broker.config import ensure_config, load_config


def default_config_in(directory):
    return patch("broker.config.os.path.expanduser", side_effect=lambda path: (
        str(Path(directory) / ".broker" / "config.yaml") if path == "~/.broker/config.yaml" else path
    ))


class ConfigPermissionTests(unittest.TestCase):
    def test_default_creation_is_private_even_with_permissive_umask(self):
        with tempfile.TemporaryDirectory() as directory, default_config_in(directory):
            previous_umask = os.umask(0)
            try:
                path = Path(ensure_config())
            finally:
                os.umask(previous_umask)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIn("broker", load_config(str(path)))

    def test_existing_default_permissions_repaired_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory, default_config_in(directory):
            parent = Path(directory) / ".broker"
            parent.mkdir()
            path = parent / "config.yaml"
            content = "broker:\n  mcp_port: 9000\n"
            path.write_text(content)
            parent.chmod(0o755)
            path.chmod(0o644)
            ensure_config(str(path))
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_text(), content)

    def test_custom_parent_permissions_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o755)
            path = Path(ensure_config(str(parent / "custom.yaml")))
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_default_symlinks_rejected_without_changing_target_permissions(self):
        with tempfile.TemporaryDirectory() as directory, default_config_in(directory):
            root = Path(directory)
            target = root / "shared"
            target.mkdir()
            target.chmod(0o755)
            parent = root / ".broker"
            parent.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                ensure_config()
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
            parent.unlink()
            parent.mkdir()
            config_target = target / "config.yaml"
            config_target.write_text("{}")
            config_target.chmod(0o644)
            (parent / "config.yaml").symlink_to(config_target)
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                ensure_config()
            self.assertEqual(stat.S_IMODE(config_target.stat().st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()
