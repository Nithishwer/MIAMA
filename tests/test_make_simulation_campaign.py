"""Check campaign selection, transfer paths and progress using local fixtures."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import yaml

from utils import make_simulation_campaign as campaign_script


class CampaignTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        root_patch = patch.object(campaign_script, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        clock_patch = patch.object(campaign_script, "datetime")
        clock = clock_patch.start()
        clock.now.return_value = datetime(
            2026, 10, 26, tzinfo=ZoneInfo("Europe/London")
        )
        self.addCleanup(clock_patch.stop)

        self.compounds = {}
        self.simulations = self.root / "simulations"
        for compound in ("compound_a", "compound_b"):
            self.compounds[compound] = {
                "repeats": {
                    repeat: {
                        "vanilla": {
                            "folder": str(
                                self.simulations / compound / f"vanilla_rep_{repeat}"
                            ),
                            "status": status,
                        },
                        "alchemical_complex": {
                            "folder": str(
                                self.simulations
                                / compound
                                / f"abfe_van{repeat}_hrex_r1/complex"
                            ),
                            "status": status,
                        },
                    }
                    for repeat, status in (
                        (3, "Waiting"),
                        (1, "Waiting"),
                        (2, "Attention required"),
                        (4, "Complete"),
                    )
                },
                "alchemical_ligand": {
                    "folder": str(
                        self.simulations / compound / "abfe_van1_hrex_r1/ligand"
                    ),
                    "status": "Waiting",
                },
            }
        for compound in self.compounds.values():
            for repeat in compound["repeats"].values():
                self.write_production(Path(repeat["vanilla"]["folder"]))
                self.write_alchemical(Path(repeat["alchemical_complex"]["folder"]))
            self.write_alchemical(Path(compound["alchemical_ligand"]["folder"]))
        self.document = {
            "datasets": {
                "example": {"complexes": self.compounds},
                "other": {"complexes": {"unselected": {}}},
            }
        }
        self.save_inventory()

    def write_production(self, folder, mdp="prod.mdp", log="prod.log"):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / mdp).write_text("nsteps = 10000000 ; target\ndt = 0.002 ; ps\n")
        (folder / log).write_text(
            "Step Time\n0 0\nStep Time\n6000000 12500\n"
            "Footer\n999999999 999999999\nStep Time\n7000000"
        )

    def write_alchemical(self, folder):
        for window in ("coul.00", "vdw.01"):
            stage, number = window.split(".")
            self.write_production(
                folder / window / "prod", mdp=f"{stage}.prod.{number}.mdp", log="md.log"
            )

    def save_inventory(self):
        (self.root / "data.yaml").write_text(
            yaml.safe_dump(self.document, sort_keys=False), encoding="utf-8"
        )

    def create_campaign(
        self,
        stage="vanilla",
        cluster="ranv",
        status="waiting",
        window=None,
        skip_progress_check=False,
    ):
        output = io.StringIO()
        arguments = ["-d", "example", "-s", stage, "-c", cluster, "--status", status]
        if window is not None:
            arguments += ["--window", window]
        if skip_progress_check:
            arguments.append("--skip-progress-check")
        with redirect_stdout(output):
            result = campaign_script.main(arguments)
        self.assertEqual(result, 0)
        folder = Path(output.getvalue().strip())
        self.assertEqual(list(folder.iterdir()), [folder / "campaign.yaml"])
        return folder, yaml.safe_load((folder / "campaign.yaml").read_text())

    def assert_creation_fails(self, stage, status, message):
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
            self.create_campaign(stage=stage, status=status)
        self.assertEqual(raised.exception.code, 1)
        self.assertIn(message, errors.getvalue())
        self.assertFalse((self.root / "campaigns").exists())
        return errors.getvalue()

    def test_all_stages_and_clusters_record_expected_runs_and_destinations(self):
        original = (self.root / "data.yaml").read_bytes()
        for stage, short_stage in (
            ("vanilla", "vanilla"),
            ("alchemical_complex", "alc-complex"),
            ("alchemical_ligand", "alc-ligand"),
        ):
            for cluster, base in (
                ("ranv", "/home/reub0138"),
                ("soma", "/home/reub0138"),
                ("archer", "/work/e280/e280-Biggin/shared/reub0138"),
            ):
                with self.subTest(stage=stage, cluster=cluster):
                    folder, metadata = self.create_campaign(stage, cluster)
                    self.assertTrue(
                        folder.name.startswith(f"example_{short_stage}_20261026")
                    )
                    expected_runs = []
                    repeats = (None,) if stage == "alchemical_ligand" else (1, 3)
                    for compound in self.compounds:
                        for repeat in repeats:
                            if stage == "vanilla":
                                relative = f"vanilla_rep_{repeat}"
                            else:
                                leaf = "ligand" if repeat is None else "complex"
                                relative = f"abfe_van{repeat or 1}_hrex_r1/{leaf}"
                            expected_runs.append(
                                {
                                    "compound": compound,
                                    "repeat": repeat,
                                    "source": str(
                                        self.simulations / compound / relative
                                    ),
                                    "destination": f"{base}/{folder.name}/{compound}/{relative}",
                                    "total_production_ns": 20.0,
                                    "completed_production_ns": 12.5,
                                }
                            )
                    expected_metadata = {
                        "dataset": "example",
                        "stage": stage,
                        "cluster": cluster,
                        "created_at": "2026-10-26T00:00:00+00:00",
                        "status": "Waiting",
                        "runs": expected_runs,
                    }
                    if stage != "vanilla":
                        expected_metadata["window"] = "coul.00"
                    self.assertEqual(metadata, expected_metadata)
        self.assertEqual((self.root / "data.yaml").read_bytes(), original)

    def test_status_filter_is_case_insensitive_and_saved_canonically(self):
        for status in (
            "waiting",
            "WAITING",
            "Attention Required",
            "ATTENTION REQUIRED",
        ):
            with self.subTest(status=status):
                _, metadata = self.create_campaign(status=status)
                expected = (
                    "Waiting" if status.lower() == "waiting" else "Attention required"
                )
                self.assertEqual(metadata["status"], expected)
                self.assertEqual(
                    [run["repeat"] for run in metadata["runs"]],
                    [1, 3, 1, 3] if expected == "Waiting" else [2, 2],
                )

    def test_duplicate_campaign_name_is_used_in_every_destination(self):
        first, _ = self.create_campaign()
        second, metadata = self.create_campaign()
        self.assertEqual(second.name, first.name + "_2")
        for run in metadata["runs"]:
            self.assertTrue(
                run["destination"].startswith(f"/home/reub0138/{second.name}/")
            )

    def test_all_missing_selected_paths_are_reported_before_creation(self):
        self.compounds["compound_a"]["repeats"][1]["vanilla"]["folder"] = None
        self.compounds["compound_b"]["repeats"][3]["vanilla"]["folder"] = ""
        self.save_inventory()
        errors = self.assert_creation_fails("vanilla", "waiting", "Unset source paths")
        self.assertIn("compound_a, repeat 1", errors)
        self.assertIn("compound_b, repeat 3", errors)

    def test_missing_shared_ligand_path_is_reported(self):
        self.compounds["compound_a"]["alchemical_ligand"]["folder"] = None
        self.save_inventory()
        self.assert_creation_fails(
            "alchemical_ligand", "waiting", "compound_a, shared ligand"
        )

    def test_missing_unselected_paths_do_not_block_creation(self):
        self.compounds["compound_a"]["repeats"][2]["vanilla"]["folder"] = None
        self.save_inventory()
        self.create_campaign()

    def test_empty_selection_does_not_create_campaign(self):
        self.assert_creation_fails(
            "alchemical_ligand", "attention required", "No alchemical_ligand runs match"
        )

    def test_status_is_required_and_rejects_complete(self):
        arguments = ["-d", "example", "-s", "vanilla", "-c", "ranv"]
        for extra in ([], ["--status", "complete"]):
            with self.subTest(extra=extra):
                with (
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    campaign_script.main(arguments + extra)
                self.assertEqual(raised.exception.code, 2)
                self.assertFalse((self.root / "campaigns").exists())

    def test_configured_ligand_filenames_and_window_override(self):
        for entry in self.compounds.values():
            run = entry["alchemical_ligand"]
            run["production_files"] = {
                "trajectory": "{stage}.prod.{number}.xtc",
                "log": "{stage}.prod.{number}.log",
                "mdp": "{stage}.prod.{number}.mdp",
            }
            production = Path(run["folder"]) / "vdw.01" / "prod"
            (production / "vdw.prod.01.log").write_text("Step Time\n15000000 30000\n")
        self.save_inventory()
        _, metadata = self.create_campaign(stage="alchemical_ligand", window="vdw.01")
        self.assertEqual(metadata["window"], "vdw.01")
        self.assertEqual(len(metadata["runs"]), 2)
        for run in metadata["runs"]:
            self.assertEqual(run["total_production_ns"], 20.0)
            self.assertEqual(run["completed_production_ns"], 30.0)

    def test_missing_log_or_mdp_prevents_campaign_creation(self):
        source = Path(self.compounds["compound_a"]["repeats"][1]["vanilla"]["folder"])
        for filename in ("prod.log", "prod.mdp"):
            with self.subTest(filename=filename):
                path = source / filename
                content = path.read_text()
                path.unlink()
                errors = self.assert_creation_fails("vanilla", "waiting", filename)
                self.assertIn("compound_a, repeat 1", errors)
                path.write_text(content)

    def test_skip_progress_check_accepts_missing_invalid_and_existing_production_files(
        self,
    ):
        original = (self.root / "data.yaml").read_bytes()
        for stage in campaign_script.STAGE_NAMES:
            repeats = (None,) if stage == "alchemical_ligand" else (1, 3)
            expected = [
                (compound, repeat) for compound in self.compounds for repeat in repeats
            ]
            for state in ("existing", "invalid", "missing"):
                with self.subTest(stage=stage, state=state):
                    for compound, repeat in expected:
                        entry = self.compounds[compound]
                        run = (
                            entry[stage]
                            if repeat is None
                            else entry["repeats"][repeat][stage]
                        )
                        folder = Path(run["folder"])
                        if stage == "vanilla":
                            files = [folder / "prod.mdp", folder / "prod.log"]
                        else:
                            production = folder / "coul.00" / "prod"
                            files = [
                                production / "coul.prod.00.mdp",
                                production / "md.log",
                            ]
                        for path in files:
                            if state == "invalid":
                                path.write_text("invalid production data")
                            elif state == "missing":
                                path.unlink()
                    _, metadata = self.create_campaign(
                        stage=stage, skip_progress_check=True
                    )
                    self.assertTrue(metadata["skip_progress_check"])
                    self.assertEqual(
                        [(run["compound"], run["repeat"]) for run in metadata["runs"]],
                        expected,
                    )
                    for run in metadata["runs"]:
                        self.assertIsNone(run["total_production_ns"])
                        self.assertIsNone(run["completed_production_ns"])
        self.assertEqual((self.root / "data.yaml").read_bytes(), original)

    def test_skip_progress_check_still_rejects_unset_source_paths(self):
        self.compounds["compound_a"]["alchemical_ligand"]["folder"] = None
        self.save_inventory()
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
            self.create_campaign(stage="alchemical_ligand", skip_progress_check=True)
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Unset source paths", errors.getvalue())
        self.assertFalse((self.root / "campaigns").exists())

    def test_invalid_mdp_or_log_prevents_campaign_creation(self):
        source = Path(self.compounds["compound_a"]["repeats"][1]["vanilla"]["folder"])
        for filename, content in (
            ("prod.mdp", "dt = 0.002"),
            ("prod.mdp", "nsteps = -1\ndt = 0.002"),
            ("prod.mdp", "nsteps = 10"),
            ("prod.mdp", "nsteps = 10\ndt = invalid"),
            ("prod.mdp", "nsteps = 10\ndt = nan"),
            ("prod.mdp", "nsteps = 10\ndt = 0"),
            ("prod.mdp", "nsteps = 10\ndt = -0.002"),
            ("prod.log", "No step records"),
            ("prod.log", "Step Time\n100 nan"),
            ("prod.log", "Step Time\n100 -1"),
        ):
            with self.subTest(filename=filename, content=content):
                path = source / filename
                original = path.read_text()
                path.write_text(content)
                self.assert_creation_fails("vanilla", "waiting", "compound_a, repeat 1")
                path.write_text(original)

    def test_zero_completed_time_is_recorded(self):
        for entry in self.compounds.values():
            for repeat in entry["repeats"].values():
                (Path(repeat["vanilla"]["folder"]) / "prod.log").write_text(
                    "Step Time\n0 0\n"
                )
        _, metadata = self.create_campaign()
        for run in metadata["runs"]:
            self.assertEqual(run["completed_production_ns"], 0.0)

    def test_invalid_production_filename_configuration_prevents_creation(self):
        self.compounds["compound_a"]["alchemical_ligand"]["production_files"] = None
        self.save_inventory()
        self.assert_creation_fails("alchemical_ligand", "waiting", "production_files")

    def test_invalid_window_is_rejected_before_campaign_creation(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.create_campaign(stage="alchemical_complex", window="../invalid")
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse((self.root / "campaigns").exists())


if __name__ == "__main__":
    unittest.main()
