"""Check that YAML updates preserve the original file in timestamped backups."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from utils.update_yaml import update_yaml


class UpdateYamlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / "data.yaml"
        self.original = b"# Original formatting\r\nstatus: Waiting\r\nother: 42\r\n"
        self.source.write_bytes(self.original)

    def test_update_creates_exact_timestamped_backup(self):
        mode = self.source.stat().st_mode
        document = {"status": "Complete", "other": 42}
        backup = update_yaml(self.source, document)
        self.assertEqual(backup.parent, self.folder / "archives")
        self.assertRegex(backup.name, r"^data_\d{8}T\d{12}Z\.yaml$")
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(yaml.safe_load(self.source.read_text()), document)
        self.assertEqual(self.source.stat().st_mode, mode)

    def test_unchanged_data_keeps_bytes_and_creates_no_archive(self):
        self.assertIsNone(update_yaml(self.source, {"status": "Waiting", "other": 42}))
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse((self.folder / "archives").exists())

    def test_each_change_gets_its_own_backup(self):
        first = update_yaml(self.source, {"status": "Complete", "other": 42})
        previous = self.source.read_bytes()
        second = update_yaml(self.source, {"status": "Waiting", "other": 42})
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), self.original)
        self.assertEqual(second.read_bytes(), previous)

    def test_backup_failure_does_not_replace_source(self):
        (self.folder / "archives").write_text("Cannot create a directory here")
        with self.assertRaises(OSError):
            update_yaml(self.source, {"status": "Complete"})
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_replace_failure_keeps_source_and_backup_and_removes_temporary(self):
        with (
            patch("utils.update_yaml.os.replace", side_effect=OSError("write failed")),
            self.assertRaises(OSError),
        ):
            update_yaml(self.source, {"status": "Complete"})
        self.assertEqual(self.source.read_bytes(), self.original)
        backups = list((self.folder / "archives").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertEqual(
            set(self.folder.iterdir()), {self.source, self.folder / "archives"}
        )


if __name__ == "__main__":
    unittest.main()
