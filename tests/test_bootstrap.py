import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from cmcc_app.bootstrap import configured_umask, data_directory, numeric_id, repair_data_permissions


class RuntimeSettingsTests(unittest.TestCase):
    def test_ids_and_masks(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(numeric_id("PUID", 10001), 10001)
            self.assertEqual(configured_umask(), 0o077)
        for value in ("0", "1000", "10001"):
            with patch.dict(os.environ, {"PUID": value}):
                self.assertEqual(numeric_id("PUID", 10001), int(value))
        for value in ("000", "022", "077", "0027"):
            with patch.dict(os.environ, {"UMASK": value}):
                self.assertEqual(configured_umask(), int(value, 8))

    def test_invalid_settings_are_rejected(self):
        for value in ("", "-1", "root", "1000:1000", "1.0", "4294967295", " 0"):
            with self.subTest(uid=value), patch.dict(os.environ, {"PUID": value}):
                with self.assertRaises(ValueError):
                    numeric_id("PUID", 10001)
        for value in ("", "888", "-1", "0o077", "18", "07777"):
            with self.subTest(mask=value), patch.dict(os.environ, {"UMASK": value}):
                with self.assertRaises(ValueError):
                    configured_umask()


@unittest.skipUnless(os.name == "posix", "Linux container permissions")
class DataPathTests(unittest.TestCase):
    def test_refuses_image_and_system_paths(self):
        for value in ("/", "/app", "/etc", "/proc", "/sys", "/dev", "./data"):
            with self.subTest(path=value), patch.dict(os.environ, {"CMCC_DATA_DIR": value}):
                with self.assertRaises(ValueError):
                    data_directory()


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0,
                     "requires root in an isolated Linux container")
class DataPermissionTests(unittest.TestCase):
    def test_repairs_existing_locked_files_without_changing_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            nested = data / "cache"
            nested.mkdir()
            lock = data / "app.lock"
            lock.write_bytes(b"0")
            token = nested / "token.json"
            token.write_bytes(b'{"test":"never-send"}')
            token.chmod(0)
            lock.chmod(0o400)
            nested.chmod(0)
            repair_data_permissions(data, 10001, 10002)
            for path in (data, nested, lock, token):
                self.assertEqual((path.stat().st_uid, path.stat().st_gid), (10001, 10002))
                self.assertEqual(stat.S_IMODE(path.stat().st_mode),
                                 0o700 if path.is_dir() else 0o600)
            self.assertEqual(lock.read_bytes(), b"0")
            self.assertEqual(token.read_bytes(), b'{"test":"never-send"}')
            repair_data_permissions(data, 12000, 12001)
            self.assertEqual(token.stat().st_uid, 12000)
            self.assertEqual(token.read_bytes(), b'{"test":"never-send"}')

    def test_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            outside = base / "outside"
            outside.mkdir()
            file = outside / "unchanged"
            file.write_bytes(b"unchanged")
            data = base / "data"
            data.mkdir()
            (data / "link-file").symlink_to(file)
            (data / "link-dir").symlink_to(outside, target_is_directory=True)
            before = (file.stat(), outside.stat())
            repair_data_permissions(data, 10001, 10001)
            for path, original in zip((file, outside), before):
                after = path.stat()
                self.assertEqual((after.st_uid, after.st_gid, after.st_mode),
                                 (original.st_uid, original.st_gid, original.st_mode))

    def test_rejects_hardlinks_without_chowning_the_target(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            outside = base / "outside"
            outside.write_bytes(b"unchanged")
            data = base / "data"
            data.mkdir()
            os.link(outside, data / "link")
            with self.assertRaises(ValueError):
                repair_data_permissions(data, 10001, 10001)
            self.assertEqual(outside.stat().st_uid, 0)


if __name__ == "__main__":
    unittest.main()
