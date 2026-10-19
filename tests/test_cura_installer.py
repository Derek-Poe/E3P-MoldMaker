import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from plugin_test_support import load_installer

installer = load_installer()


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root / "E3PBlenderMold"

    def test_explicit_fresh_install_exact_files(self):
        result = installer.install(self.destination)
        self.assertIsNone(result["backup"])
        self.assertEqual(set(p.name for p in self.destination.iterdir()), set(installer.FILES))
        for name in installer.FILES:
            self.assertEqual((installer.SOURCE / name).read_bytes(), (self.destination / name).read_bytes())
        metadata = json.loads((self.destination / "plugin.json").read_text())
        self.assertEqual(metadata["supported_sdk_versions"], ["8.12.0"])
        self.assertEqual(metadata["author"], "Derek Poe")

    def test_existing_refused_without_replacement_and_untouched(self):
        installer.install(self.destination)
        marker = self.destination / "local-settings.txt"
        marker.write_bytes(b"user preference fixture")
        with self.assertRaises(FileExistsError):
            installer.install(self.destination)
        self.assertEqual(marker.read_bytes(), b"user preference fixture")
        self.assertFalse(list(self.root.glob("*.zip")))

    def test_replacement_archives_all_old_bytes_without_duplicate_plugin(self):
        installer.install(self.destination)
        (self.destination / "local-settings.txt").write_bytes(b"user preference fixture")
        old = {p.name: p.read_bytes() for p in self.destination.iterdir()}
        result = installer.install(self.destination, replace=True)
        with zipfile.ZipFile(result["backup"]) as archive:
            self.assertEqual({n: archive.read(n) for n in archive.namelist()}, old)
        self.assertFalse((self.destination / "local-settings.txt").exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.is_dir()], ["E3PBlenderMold"])

    def test_install_failure_restores_old_plugin(self):
        installer.install(self.destination)
        (self.destination / "local-settings.txt").write_bytes(b"kept")
        rename = Path.rename

        def fail_payload(path, target):
            if path.name == "payload":
                raise OSError("injected install failure")
            return rename(path, target)

        with patch.object(Path, "rename", fail_payload):
            with self.assertRaisesRegex(OSError, "injected"):
                installer.install(self.destination, replace=True)
        self.assertEqual((self.destination / "local-settings.txt").read_bytes(), b"kept")
        self.assertEqual(len(list(self.root.glob("*.zip"))), 1)

    def test_parent_profile_and_unrecognized_directory_refused(self):
        with self.assertRaises(ValueError):
            installer.install(self.root)
        with self.assertRaises(ValueError):
            installer.install(self.root / "missing" / "E3PBlenderMold")
        self.destination.mkdir()
        (self.destination / "unrelated.txt").write_text("keep")
        with self.assertRaises(ValueError):
            installer.install(self.destination, replace=True)
        self.assertEqual((self.destination / "unrelated.txt").read_text(), "keep")

    def test_linked_destination_refused_where_supported(self):
        target = self.root / "real"
        target.mkdir()
        try:
            self.destination.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks is not available to this account.")
        with self.assertRaises(ValueError):
            installer.install(self.destination, replace=True)
        self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
