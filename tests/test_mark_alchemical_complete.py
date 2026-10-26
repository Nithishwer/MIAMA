"""Check alchemical window selection, reporting and safe status updates."""

import argparse
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import yaml

from utils.mark_alchemical_complete import check_alchemical, main, parse_window

SCRIPT = Path(__file__).resolve().parents[1] / "utils" / "mark_alchemical_complete.py"
LIGAND_FILES = {
    "trajectory": "{stage}.prod.{number}.xtc",
    "log": "{stage}.prod.{number}.log",
    "mdp": "{stage}.prod.{number}.mdp",
}


def write_production(folder, window="coul.00", step=100, filenames=None):
    stage, number = window.split(".")
    production = folder / window / "prod"
    production.mkdir(parents=True, exist_ok=True)
    if filenames is None:
        filenames = {
            "trajectory": "traj_comp.xtc",
            "log": "md.log",
            "mdp": f"{stage}.prod.{number}.mdp",
        }
    (production / filenames["trajectory"]).touch()
    (production / filenames["mdp"]).write_text("nsteps = 100\n")
    (production / filenames["log"]).write_text(f"Step Time\n{step} 0.2\n")
    return production


def make_run(folder, status="Waiting"):
    return {
        "folder": str(folder) if folder is not None else None,
        "status": status,
        "remark": "Keep this manual note",
        "convergence": "not_converged",
        "total_production_ns": 20,
        "completed_production_ns": 12.5,
    }


class AlchemicalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.run_folder = self.folder / "complex"
        self.production = write_production(self.run_folder)

    def test_default_window_requires_exact_step_match(self):
        for step, expected in (
            (100, ("complete", "")),
            (90, ("incomplete", "step 90/100")),
            (110, ("incomplete", "step 110/100")),
        ):
            with self.subTest(step=step):
                (self.production / "md.log").write_text(f"Step Time\n{step} 0.2\n")
                self.assertEqual(check_alchemical(self.run_folder), expected)

    def test_ligand_templates_follow_selected_window(self):
        for window, stem in (
            ("coul.00", "coul.prod.00"),
            ("vdw.01", "vdw.prod.01"),
            ("rest.02", "rest.prod.02"),
        ):
            with self.subTest(window=window):
                folder = self.folder / f"ligand_{window}"
                production = write_production(
                    folder,
                    window,
                    filenames={
                        "trajectory": f"{stem}.xtc",
                        "log": f"{stem}.log",
                        "mdp": f"{stem}.mdp",
                    },
                )
                self.assertEqual(
                    check_alchemical(folder, window, LIGAND_FILES), ("complete", "")
                )
                (production / f"{stem}.log").write_text("Step Time\n90 0.18\n")
                self.assertEqual(
                    check_alchemical(folder, window, LIGAND_FILES),
                    ("incomplete", "step 90/100"),
                )

    def test_literal_filenames_and_missing_configured_files(self):
        files = {"trajectory": "custom.xtc", "log": "custom.log", "mdp": "custom.mdp"}
        write_production(self.run_folder, step=90, filenames=files)
        self.assertEqual(
            check_alchemical(self.run_folder, production_files=files),
            ("incomplete", "step 90/100"),
        )
        for field, expected in (
            ("trajectory", ("missing", "")),
            ("log", ("incomplete", "custom.log missing")),
            ("mdp", ("incomplete", "custom.mdp missing")),
        ):
            with self.subTest(field=field):
                path = self.production / files[field]
                content = path.read_bytes()
                path.unlink()
                self.assertEqual(
                    check_alchemical(self.run_folder, production_files=files), expected
                )
                path.write_bytes(content)
        self.assertEqual(check_alchemical(self.run_folder), ("complete", ""))

    def test_invalid_filename_configuration_reports_incomplete(self):
        for files in (
            None,
            [],
            {},
            {**LIGAND_FILES, "other": "extra.log"},
            {**LIGAND_FILES, "log": "../md.log"},
            {**LIGAND_FILES, "log": "{unknown}.log"},
            {**LIGAND_FILES, "log": "{stage!r}.log"},
        ):
            with self.subTest(files=files):
                status, reason = check_alchemical(
                    self.run_folder, production_files=files
                )
                self.assertEqual(status, "incomplete")
                self.assertIn("production_files", reason)

    def test_override_checks_only_selected_window_and_matching_mdp(self):
        for window in ("rest.01", "coul.01", "vdw.00"):
            with self.subTest(window=window):
                production = write_production(self.run_folder, window, step=90)
                # Unrelated MDP and log names must not determine completion.
                (production / "prod.mdp").write_text("nsteps = 90\n")
                (production / "prod.log").write_text("Step Time\n100 0.2\n")
                self.assertEqual(
                    check_alchemical(self.run_folder, window),
                    ("incomplete", "step 90/100"),
                )
                self.assertEqual(check_alchemical(self.run_folder), ("complete", ""))

    def test_missing_folder_window_or_trajectory(self):
        for folder, window in (
            (None, "coul.00"),
            (self.folder / "absent", "coul.00"),
            (self.run_folder, "vdw.01"),
        ):
            with self.subTest(folder=folder, window=window):
                self.assertEqual(check_alchemical(folder, window), ("missing", ""))
        (self.production / "traj_comp.xtc").unlink()
        (self.production / "prod.xtc").touch()
        self.assertEqual(check_alchemical(self.run_folder), ("missing", ""))

    def test_missing_supporting_files_are_incomplete(self):
        for name in ("coul.prod.00.mdp", "md.log"):
            with self.subTest(name=name):
                path = self.production / name
                content = path.read_text()
                path.unlink()
                self.assertEqual(
                    check_alchemical(self.run_folder),
                    ("incomplete", f"{name} missing"),
                )
                path.write_text(content)

    def test_invalid_mdp_reports_actual_filename(self):
        for content, reason in (
            ("dt = 0.002\n", "has no nsteps"),
            ("nsteps = nope\n", "has invalid nsteps"),
            ("nsteps = -1\n", "has negative nsteps"),
        ):
            with self.subTest(content=content):
                (self.production / "coul.prod.00.mdp").write_text(content)
                self.assertEqual(
                    check_alchemical(self.run_folder),
                    ("incomplete", f"coul.prod.00.mdp {reason}"),
                )

    def test_invalid_or_undecodable_log_is_incomplete(self):
        for content in (b"", b"Step Time\n100 nan\n", b"\xff"):
            with self.subTest(content=content):
                (self.production / "md.log").write_bytes(content)
                status, reason = check_alchemical(self.run_folder)
                self.assertEqual(status, "incomplete")
                self.assertTrue(reason)

    def test_all_records_reported_once_and_statuses_updated(self):
        ligand_folder = self.folder / "ligand"
        write_production(
            ligand_folder,
            filenames={
                "trajectory": "coul.prod.00.xtc",
                "log": "coul.prod.00.log",
                "mdp": "coul.prod.00.mdp",
            },
        )
        incomplete_folder = self.folder / "incomplete"
        write_production(incomplete_folder, step=90)
        broken_folder = self.folder / "broken"
        production = write_production(broken_folder)
        (production / "md.log").unlink()
        vanilla = {"folder": "/unused", "status": "Waiting", "remark": "Vanilla note"}
        document = {
            "datasets": {
                "first": {
                    "label": "First dataset",
                    "complexes": {
                        "ligand_01": {
                            "repeats": {
                                repeat: {
                                    "vanilla": deepcopy(vanilla),
                                    "alchemical_complex": make_run(folder, status),
                                }
                                for repeat, folder, status in (
                                    (3, self.run_folder, "Attention required"),
                                    (1, broken_folder, "Waiting"),
                                    (2, incomplete_folder, "Attention required"),
                                )
                            },
                            "alchemical_ligand": make_run(ligand_folder),
                        }
                    },
                },
                "second": {
                    "label": "Second dataset",
                    "complexes": {
                        "ligand_02": {
                            "repeats": {},
                            "alchemical_ligand": make_run(None, "Complete"),
                        }
                    },
                },
            }
        }
        compound = document["datasets"]["first"]["complexes"]["ligand_01"]
        compound["alchemical_ligand"]["production_files"] = deepcopy(LIGAND_FILES)
        compound["repeats"][3]["alchemical_complex"]["production_files"] = {
            "trajectory": "traj_comp.xtc",
            "log": "md.log",
            "mdp": "{stage}.prod.{number}.mdp",
        }
        source = self.folder / "data.yaml"
        content = yaml.safe_dump(document, sort_keys=False)
        source.write_text(content)
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(main(source), 0)
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                f"first\tligand_01\t1\talchemical_complex\t{broken_folder}\tcoul.00\tincomplete (md.log missing)",
                f"first\tligand_01\t2\talchemical_complex\t{incomplete_folder}\tcoul.00\tincomplete (step 90/100)",
                f"first\tligand_01\t3\talchemical_complex\t{self.run_folder}\tcoul.00\tcomplete",
                f"first\tligand_01\tShared\talchemical_ligand\t{ligand_folder}\tcoul.00\tcomplete",
                "second\tligand_02\tShared\talchemical_ligand\t-\tcoul.00\tmissing",
            ],
        )
        expected = deepcopy(document)
        compound = expected["datasets"]["first"]["complexes"]["ligand_01"]
        compound["repeats"][3]["alchemical_complex"]["status"] = "Complete"
        compound["alchemical_ligand"]["status"] = "Complete"
        expected["datasets"]["second"]["complexes"]["ligand_02"]["alchemical_ligand"][
            "status"
        ] = "Attention required"
        self.assertEqual(yaml.safe_load(source.read_text()), expected)
        backups = list((self.folder / "archives").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), content)
        self.assertIn(str(backups[0]), errors.getvalue())

        saved = source.read_bytes()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(source), 0)
        self.assertEqual(source.read_bytes(), saved)
        self.assertEqual(list((self.folder / "archives").iterdir()), backups)

    def test_main_uses_window_override_for_shared_ligand(self):
        for window, stem, step in (
            ("coul.00", "coul.prod.00", 100),
            ("vdw.01", "vdw.prod.01", 90),
        ):
            write_production(
                self.run_folder,
                window,
                step=step,
                filenames={
                    "trajectory": f"{stem}.xtc",
                    "log": f"{stem}.log",
                    "mdp": f"{stem}.mdp",
                },
            )
        source = self.folder / "data.yaml"
        for window, expected_status in (("vdw.01", "Waiting"), ("coul.00", "Complete")):
            document = {
                "datasets": {
                    "example": {
                        "complexes": {
                            "ligand_01": {
                                "repeats": {},
                                "alchemical_ligand": make_run(self.run_folder),
                            }
                        }
                    }
                }
            }
            run = document["datasets"]["example"]["complexes"]["ligand_01"][
                "alchemical_ligand"
            ]
            run["production_files"] = deepcopy(LIGAND_FILES)
            source.write_text(yaml.safe_dump(document))
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                self.assertEqual(main(source, window=window), 0)
            saved = yaml.safe_load(source.read_text())
            run = saved["datasets"]["example"]["complexes"]["ligand_01"][
                "alchemical_ligand"
            ]
            self.assertEqual(run["status"], expected_status)
            self.assertIn(f"\t{window}\t", output.getvalue())

    def test_incomplete_preserves_every_existing_status_without_writes(self):
        (self.production / "md.log").write_text("Step Time\n90 0.18\n")
        source = self.folder / "data.yaml"
        for status in ("Waiting", "Attention required", "Complete"):
            with self.subTest(status=status):
                document = {
                    "datasets": {
                        "example": {
                            "complexes": {
                                "ligand_01": {
                                    "repeats": {
                                        1: {
                                            "alchemical_complex": make_run(
                                                self.run_folder, status
                                            )
                                        }
                                    },
                                    "alchemical_ligand": make_run(
                                        self.run_folder, status
                                    ),
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

    def test_invalid_configuration_preserves_statuses_and_yaml_without_writes(self):
        source = self.folder / "data.yaml"
        for folder in (None, self.run_folder):
            for status in ("Waiting", "Attention required", "Complete"):
                for files in (None, {**LIGAND_FILES, "log": "{unknown}.log"}):
                    with self.subTest(folder=folder, status=status, files=files):
                        run = make_run(folder, status)
                        run["production_files"] = files
                        document = {
                            "datasets": {
                                "example": {
                                    "complexes": {
                                        "ligand_01": {
                                            "repeats": {
                                                1: {"alchemical_complex": deepcopy(run)}
                                            },
                                            "alchemical_ligand": run,
                                        }
                                    }
                                }
                            }
                        }
                        content = yaml.safe_dump(document)
                        source.write_text(content)
                        output = io.StringIO()
                        with redirect_stdout(output):
                            self.assertEqual(main(source), 0)
                        self.assertEqual(
                            output.getvalue().count("incomplete (production_files"), 2
                        )
                        self.assertEqual(source.read_text(), content)
                        self.assertFalse((self.folder / "archives").exists())

    def test_missing_marks_every_existing_status_attention_required_with_backup(self):
        (self.production / "traj_comp.xtc").unlink()
        no_window = self.folder / "no_window"
        no_window.mkdir()
        folders = (None, self.folder / "absent", no_window, self.run_folder)
        for folder_index, folder in enumerate(folders):
            for status_index, status in enumerate(
                ("Waiting", "Attention required", "Complete")
            ):
                with self.subTest(folder=folder, status=status):
                    case_folder = self.folder / f"case_{folder_index}_{status_index}"
                    case_folder.mkdir()
                    source = case_folder / "data.yaml"
                    document = {
                        "datasets": {
                            "example": {
                                "complexes": {
                                    "ligand_01": {
                                        "repeats": {
                                            1: {
                                                "alchemical_complex": make_run(
                                                    folder, status
                                                )
                                            }
                                        },
                                        "alchemical_ligand": make_run(folder, status),
                                    }
                                }
                            }
                        }
                    }
                    content = yaml.safe_dump(document)
                    source.write_text(content)
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        self.assertEqual(main(source), 0)
                    expected = deepcopy(document)
                    compound = expected["datasets"]["example"]["complexes"]["ligand_01"]
                    compound["repeats"][1]["alchemical_complex"]["status"] = (
                        "Attention required"
                    )
                    compound["alchemical_ligand"]["status"] = "Attention required"
                    self.assertEqual(yaml.safe_load(source.read_text()), expected)
                    backups = list((case_folder / "archives").glob("*"))
                    self.assertEqual(len(backups), int(status != "Attention required"))
                    if backups:
                        self.assertEqual(backups[0].read_text(), content)
                    else:
                        self.assertEqual(source.read_text(), content)
                    saved = source.read_bytes()
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        self.assertEqual(main(source), 0)
                    self.assertEqual(source.read_bytes(), saved)
                    self.assertEqual(
                        list((case_folder / "archives").glob("*")), backups
                    )

    def test_load_and_save_failures_return_errors(self):
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
        source.write_text("datasets: {}\n")
        errors = io.StringIO()
        with (
            patch(
                "utils.mark_alchemical_complete.update_yaml",
                side_effect=PermissionError("backup permission denied"),
            ),
            redirect_stderr(errors),
        ):
            self.assertEqual(main(source), 1)
        self.assertIn("Cannot update", errors.getvalue())
        self.assertEqual(source.read_text(), "datasets: {}\n")

    def test_window_validation_and_cli_reject_invalid_window_before_processing(self):
        for window in ("rest.00", "coul.01", "vdw.99"):
            self.assertEqual(parse_window(window), window)
        for window in (
            "coul",
            "coul.0",
            "coul.000",
            "other.00",
            "../coul.00",
            "coul.00/prod",
        ):
            with self.subTest(window=window):
                with self.assertRaises(argparse.ArgumentTypeError):
                    parse_window(window)
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), "--window", window],
                    check=False,
                    cwd=self.folder,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("expected rest.NN, coul.NN or vdw.NN", result.stderr)

    def test_script_help_from_another_directory(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            check=False,
            cwd=self.folder,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--window", result.stdout)
        self.assertIn("coul.00", result.stdout)


if __name__ == "__main__":
    unittest.main()
