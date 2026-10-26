"""Check production status parsing and reporting using small simulation fixtures."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from utils.mark_vanilla_complete import check_vanilla, main, read_last_step, read_nsteps


class VanillaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        (self.folder / "prod.xtc").touch()
        (self.folder / "prod.mdp").write_text("nsteps = 100\n")
        (self.folder / "prod.log").write_text("  Step   Time\n  100  0.20000\n")

    def test_exact_step_match_only(self):
        for step, status, reason in (
            (100, "complete", ""),
            (90, "incomplete", "step 90/100"),
            (110, "incomplete", "step 110/100"),
        ):
            with self.subTest(step=step):
                (self.folder / "prod.log").write_text(f"Step Time\n{step} 0.2\n")
                self.assertEqual(check_vanilla(self.folder), (status, reason))

    def test_missing_folder_or_trajectory(self):
        self.assertEqual(check_vanilla(None), ("missing", ""))
        self.assertEqual(check_vanilla(self.folder / "absent"), ("missing", ""))
        (self.folder / "prod.xtc").unlink()
        self.assertEqual(check_vanilla(self.folder), ("missing", ""))

    def test_missing_supporting_files(self):
        for name in ("prod.mdp", "prod.log"):
            with self.subTest(name=name):
                path = self.folder / name
                content = path.read_text()
                path.unlink()
                self.assertEqual(
                    check_vanilla(self.folder), ("incomplete", f"{name} missing")
                )
                path.write_text(content)

    def test_mdp_comments_and_whitespace(self):
        path = self.folder / "prod.mdp"
        path.write_text("; nsteps = 999\ndt = 0.002\n  nsteps\t= 100 ; target\n")
        self.assertEqual(read_nsteps(path), 100)

    def test_invalid_mdp_is_incomplete(self):
        for content in ("", "dt = 0.002\n", "nsteps = nope", "nsteps = -1"):
            with self.subTest(content=content):
                (self.folder / "prod.mdp").write_text(content)
                status, reason = check_vanilla(self.folder)
                self.assertEqual(status, "incomplete")
                self.assertIn("nsteps", reason)

    def test_log_uses_last_record_and_ignores_footer_and_truncated_record(self):
        path = self.folder / "prod.log"
        path.write_text(
            "Step Time\n0 0.0\n"
            "Step Time\n\n100 0.20000\n"
            "Statistics over 101 steps\n999 888\nFinished mdrun\n"
            "Step Time\n110"
        )
        self.assertEqual(read_last_step(path), 100)
        self.assertEqual(check_vanilla(self.folder), ("complete", ""))

    def test_invalid_log_is_incomplete(self):
        for content in (
            "",
            "100 0.2\n",
            "Step Time\n",
            "Step Time\n100\n",
            "Step Time\ninvalid 0.2\n",
            "Step Time\n100 nan\n",
        ):
            with self.subTest(content=content):
                (self.folder / "prod.log").write_text(content)
                self.assertEqual(
                    check_vanilla(self.folder),
                    ("incomplete", "prod.log has no valid step records"),
                )

    def test_unreadable_log_is_incomplete(self):
        with patch(
            "utils.production_status.read_last_step",
            side_effect=PermissionError("prod.log permission denied"),
        ):
            self.assertEqual(
                check_vanilla(self.folder),
                ("incomplete", "prod.log permission denied"),
            )

    def test_reporting_continues_after_bad_run_and_saves_statuses_with_backup(self):
        broken = self.folder / "broken"
        broken.mkdir()
        (broken / "prod.xtc").touch()
        document = {
            "datasets": {
                "example": {
                    "complexes": {
                        "ligand_01": {
                            "repeats": {
                                repeat: {
                                    "vanilla": {
                                        "folder": folder,
                                        "status": status,
                                        "remark": "Keep this manual note",
                                    },
                                    "alchemical_complex": {"status": "Waiting"},
                                }
                                for repeat, folder, status in (
                                    (1, str(broken), "Waiting"),
                                    (2, str(self.folder), "Attention required"),
                                    (3, None, "Complete"),
                                )
                            }
                        }
                    }
                }
            }
        }
        source = self.folder / "data.yaml"
        content = yaml.safe_dump(document)
        source.write_text(content)
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(main(source), 0)
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                f"example\tligand_01\t1\t{broken}\tincomplete (prod.mdp missing)",
                f"example\tligand_01\t2\t{self.folder}\tcomplete",
                "example\tligand_01\t3\t-\tmissing",
            ],
        )
        saved = yaml.safe_load(source.read_text())
        repeats = saved["datasets"]["example"]["complexes"]["ligand_01"]["repeats"]
        self.assertEqual(
            [run["vanilla"]["status"] for run in repeats.values()],
            ["Waiting", "Complete", "Complete"],
        )
        for run in repeats.values():
            self.assertEqual(run["vanilla"]["remark"], "Keep this manual note")
            self.assertEqual(run["alchemical_complex"], {"status": "Waiting"})
        backups = list((self.folder / "archives").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), content)
        self.assertIn(str(backups[0]), errors.getvalue())

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(source), 0)
        self.assertEqual(list((self.folder / "archives").iterdir()), backups)

    def test_missing_and_incomplete_runs_preserve_all_existing_statuses(self):
        (self.folder / "prod.log").write_text("Step Time\n90 0.18\n")
        source = self.folder / "data.yaml"
        for folder in (None, str(self.folder)):
            for status in ("Waiting", "Attention required", "Complete"):
                with self.subTest(folder=folder, status=status):
                    document = {
                        "datasets": {
                            "example": {
                                "complexes": {
                                    "ligand_01": {
                                        "repeats": {
                                            1: {
                                                "vanilla": {
                                                    "folder": folder,
                                                    "status": status,
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    content = yaml.safe_dump(document)
                    source.write_text(content)
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(main(source), 0)
                    self.assertEqual(source.read_text(), content)
                    self.assertFalse((self.folder / "archives").exists())

    def test_update_failure_returns_error(self):
        source = self.folder / "data.yaml"
        source.write_text("datasets: {}\n")
        errors = io.StringIO()
        with (
            patch(
                "utils.mark_vanilla_complete.update_yaml",
                side_effect=PermissionError("backup permission denied"),
            ),
            redirect_stderr(errors),
        ):
            self.assertEqual(main(source), 1)
        self.assertIn("Cannot update", errors.getvalue())
        self.assertEqual(source.read_text(), "datasets: {}\n")

    def test_unreadable_or_malformed_yaml_fails_cleanly(self):
        source = self.folder / "data.yaml"
        for content in (None, "datasets: [\n"):
            with self.subTest(content=content):
                if content is not None:
                    source.write_text(content)
                errors = io.StringIO()
                output = io.StringIO()
                with redirect_stderr(errors), redirect_stdout(output):
                    self.assertEqual(main(source), 1)
                self.assertIn("Cannot load", errors.getvalue())
                self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
