"""Check coverage decisions, safe execution and alchemlyb thread settings."""

import csv
import io
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import call, patch

import yaml
from test_console import example_data

from data import load_data, validate_run
from utils import analysis_utils
from utils import refresh_analysis_status as refresher
from utils import run_analysis as runner
from utils.analysis_utils import (
    RESULT_COLUMNS,
    analysis_folder,
    dhdl_lines,
    expected_windows,
    find_inputs,
    inspect_analysis,
    read_last_time,
    validate_outputs,
)


def make_inputs(folder, component):
    for window in expected_windows(component):
        path = folder / window / "prod/dhdl.xvg"
        path.parent.mkdir(parents=True)
        path.write_text('# header\n@ title "dhdl"\n0 1 2\n10 2 3\n20 3 4\n')


def write_outputs(directory, component):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "ABFE_summary.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["", "", *RESULT_COLUMNS])
        stages = (
            ("bonded", "coul", "vdw") if component == "complex" else ("coul", "vdw")
        )
        for number, stage in enumerate(stages, 1):
            writer.writerow(
                ["Stages", stage, number, 0.1, number + 1, 0.2, number + 2, 0.3]
            )
    (directory / "ABFE_convergence.csv").write_text(
        ",Forward,Forward_Error,Backward,Backward_Error,data_fraction\n"
        "0,1,0.1,1.1,0.1,0.5\n1,1,0.1,1.1,0.1,1.0\n"
    )


def make_analysis(folder, component):
    directory = analysis_folder(folder, component)
    directory.mkdir(parents=True)
    for window, source in find_inputs(folder, component).items():
        shutil.copyfile(source, directory / f"dhdl.{window}.xvg")
    write_outputs(directory, component)
    return directory


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name) / "run/complex"
        make_inputs(self.folder, "complex")

    def test_missing_and_partial_outputs(self):
        self.assertEqual(inspect_analysis(None, "complex")["status"], "Missing")
        self.assertEqual(inspect_analysis(self.folder, "complex")["status"], "Missing")
        directory = analysis_folder(self.folder, "complex")
        directory.mkdir(parents=True)
        (directory / "ABFE_summary.csv").write_text("broken\n")
        self.assertEqual(
            inspect_analysis(self.folder, "complex")["status"], "Incomplete"
        )

    def test_complete_coverage_ignores_production_completion_and_filtering(self):
        make_analysis(self.folder, "complex")
        # No MDP, trajectory or production log is needed to inspect current inputs.
        self.assertEqual(
            inspect_analysis(self.folder, "complex"),
            {
                "status": "Complete",
                "remark": "",
                "total_analysed_ps": 20,
                "input_error": "",
                "convergence": "converged",
            },
        )

    def test_later_timestamp_in_any_window_makes_analysis_incomplete(self):
        make_analysis(self.folder, "complex")
        path = self.folder / "vdw.20/prod/dhdl.xvg"
        with path.open("a") as stream:
            stream.write("30 4 5\n")
        inspection = inspect_analysis(self.folder, "complex")
        status, remark = inspection["status"], inspection["remark"]
        self.assertEqual(status, "Incomplete")
        self.assertIn("vdw.20", remark)
        self.assertIn("Window end times differ", remark)

    def test_equal_final_time_with_more_samples_is_complete(self):
        make_analysis(self.folder, "complex")
        (self.folder / "coul.09/prod/dhdl.xvg").write_text(
            "0 1 2\n5 1 2\n10 2 3\n20 3 4\n"
        )
        self.assertEqual(
            inspect_analysis(self.folder, "complex"),
            {
                "status": "Complete",
                "remark": "",
                "total_analysed_ps": 20,
                "input_error": "",
                "convergence": "converged",
            },
        )

    def test_unavailable_evidence_is_incomplete_with_remark(self):
        directory = make_analysis(self.folder, "complex")
        (directory / "dhdl.rest.00.xvg").unlink()
        inspection = inspect_analysis(self.folder, "complex")
        status, remark = inspection["status"], inspection["remark"]
        self.assertEqual(status, "Incomplete")
        self.assertIn("Data not verified", remark)
        self.assertIn("rest.00", remark)

    def test_proven_gap_wins_over_unverifiable_window(self):
        directory = make_analysis(self.folder, "complex")
        (directory / "dhdl.rest.00.xvg").unlink()
        with (self.folder / "vdw.20/prod/dhdl.xvg").open("a") as stream:
            stream.write("30 4 5\n")
        self.assertEqual(
            inspect_analysis(self.folder, "complex")["status"], "Incomplete"
        )

    def test_missing_source_or_newer_copies_cannot_verify_coverage(self):
        directory = make_analysis(self.folder, "complex")
        copied = directory / "dhdl.rest.00.xvg"
        future = time.time_ns() + 1_000_000_000
        os.utime(copied, ns=(future, future))
        inspection = inspect_analysis(self.folder, "complex")
        status, remark = inspection["status"], inspection["remark"]
        self.assertEqual(status, "Incomplete")
        self.assertIn("newer than results", remark)
        shutil.rmtree(self.folder)
        inspection = inspect_analysis(self.folder, "complex")
        status, remark = inspection["status"], inspection["remark"]
        self.assertEqual(status, "Incomplete")
        self.assertIn("Data not verified", remark)

    def test_missing_and_unexpected_windows_block_execution(self):
        make_analysis(self.folder, "complex")
        shutil.rmtree(self.folder / "rest.10")
        (self.folder / "coul.10").mkdir()
        with self.assertRaisesRegex(
            ValueError, "missing windows: rest.10; unexpected windows: coul.10"
        ):
            find_inputs(self.folder, "complex")
        inspection = inspect_analysis(self.folder, "complex")
        status, remark = inspection["status"], inspection["remark"]
        self.assertEqual(status, "Incomplete")
        self.assertIn("unexpected windows", remark)

    def test_duplicate_stage_endpoints_are_allowed_but_not_selected(self):
        for endpoint, next_stage, coordinates in (
            ("rest.11", "coul.00", "0, 0, 1"),
            ("coul.10", "vdw.00", "1, 0, 1"),
        ):
            header = f'@ subtitle "T = 298.15 (K) state 12: (coul-lambda, vdw-lambda, bonded-lambda) = ({coordinates})"\n'
            for window in (endpoint, next_stage):
                path = self.folder / window / "prod/dhdl.xvg"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(header + "0 1 2\n10 2 3\n20 3 4\n")
        inputs = find_inputs(self.folder, "complex")
        self.assertEqual(len(inputs), 42)
        self.assertNotIn("rest.11", inputs)
        self.assertNotIn("coul.10", inputs)
        make_analysis(self.folder, "complex")
        self.assertEqual(
            inspect_analysis(self.folder, "complex"),
            {
                "status": "Complete",
                "remark": "",
                "total_analysed_ps": 20,
                "input_error": "",
                "convergence": "converged",
            },
        )

        path = self.folder / "rest.11/prod/dhdl.xvg"
        path.write_text(path.read_text().replace("(0, 0, 1)", "(0, 0, 0.9)"))
        with self.assertRaisesRegex(ValueError, "unexpected windows: rest.11"):
            find_inputs(self.folder, "complex")
        self.assertEqual(
            inspect_analysis(self.folder, "complex")["status"], "Incomplete"
        )

    def test_dhdl_ignores_unfinished_tail_and_rejects_invalid_records(self):
        path = self.folder / "coul.00/prod/dhdl.xvg"
        with path.open("ab") as stream:
            stream.write(b"30 4")
        self.assertEqual(read_last_time(path), 20)
        path.write_text("0 1 2\n10 bad 3\n")
        self.assertEqual(read_last_time(path), 0)
        with self.assertRaisesRegex(ValueError, "invalid numeric record"):
            list(dhdl_lines(path))
        path.write_text("bad 2 3\n")
        with self.assertRaisesRegex(ValueError, "no complete numeric samples"):
            read_last_time(path)

    def test_timestamp_check_only_uses_the_last_ten_lines(self):
        path = self.folder / "coul.00/prod/dhdl.xvg"
        path.write_text("123 1 2\n" + "# footer\n" * 10)
        with self.assertRaisesRegex(ValueError, "last 10 lines"):
            read_last_time(path)
        path.write_text("broken earlier record\n" * 20 + "20 1 2\n# footer\n")
        self.assertEqual(read_last_time(path), 20)

    def test_timestamp_check_seeks_to_the_tail_of_large_files(self):
        stream = io.BytesIO(b"earlier data\n" * 100_000 + b"20 1 2\n" * 10)
        with patch.object(stream, "read", wraps=stream.read) as read:
            with patch.object(Path, "open", return_value=stream):
                self.assertEqual(read_last_time(Path("large.xvg")), 20)
        self.assertLessEqual(sum(call.args[0] for call in read.call_args_list), 8192)

    def test_invalid_convergence_and_missing_stages_are_incomplete(self):
        directory = make_analysis(self.folder, "complex")
        convergence = directory / "ABFE_convergence.csv"
        original = convergence.read_text()
        convergence.write_text(original.replace("1.0", "nan"))
        self.assertEqual(
            inspect_analysis(self.folder, "complex")["status"], "Incomplete"
        )
        convergence.write_text(original)
        summary = directory / "ABFE_summary.csv"
        summary.write_text(
            "\n".join(
                line
                for line in summary.read_text().splitlines()
                if "bonded" not in line
            )
        )
        self.assertIn(
            "missing stages", inspect_analysis(self.folder, "complex")["remark"]
        )

    def test_saved_time_verifies_outputs_without_retained_copies(self):
        directory = analysis_folder(self.folder, "complex")
        write_outputs(directory, "complex")
        for saved in (None, 20, 10, 30):
            with self.subTest(saved=saved):
                inspection = inspect_analysis(self.folder, "complex", saved)
                self.assertEqual(
                    inspection["status"], "Complete" if saved == 20 else "Incomplete"
                )
                self.assertEqual(inspection["total_analysed_ps"], saved)
        self.assertEqual(list(directory.glob("dhdl.*.xvg")), [])

    def test_zero_time_is_valid_and_null_is_unknown(self):
        write_outputs(analysis_folder(self.folder, "complex"), "complex")
        for path in find_inputs(self.folder, "complex").values():
            path.write_text("0 1 2\n")
        self.assertEqual(
            inspect_analysis(self.folder, "complex", 0)["status"], "Complete"
        )
        self.assertEqual(
            inspect_analysis(self.folder, "complex", None)["status"], "Incomplete"
        )

    def test_legacy_snapshot_recovers_time_ahead_of_yaml(self):
        directory = make_analysis(self.folder, "complex")
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        inspection = inspect_analysis(self.folder, "complex", 10)
        self.assertEqual(inspection["total_analysed_ps"], 20)
        self.assertEqual(inspection["status"], "Complete")
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})

    def test_unreliable_legacy_snapshots_fall_back_to_yaml(self):
        directory = make_analysis(self.folder, "complex")
        copied = directory / "dhdl.rest.00.xvg"
        original = copied.read_bytes()
        original_time = copied.stat().st_mtime_ns
        for content in (b"0 1 2\n10 2 3\n", b"broken\n", None):
            with self.subTest(content=content):
                if content is None:
                    copied.unlink()
                else:
                    copied.write_bytes(content)
                    os.utime(copied, ns=(original_time, original_time))
                unknown = inspect_analysis(self.folder, "complex")
                self.assertEqual(unknown["status"], "Incomplete")
                self.assertIsNone(unknown["total_analysed_ps"])
                saved = inspect_analysis(self.folder, "complex", 20)
                self.assertEqual(saved["status"], "Complete")
                self.assertEqual(saved["total_analysed_ps"], 20)
                copied.write_bytes(original)
                os.utime(copied, ns=(original_time, original_time))
        future = time.time_ns() + 1_000_000_000
        os.utime(copied, ns=(future, future))
        self.assertEqual(
            inspect_analysis(self.folder, "complex", 20)["status"], "Complete"
        )

    def test_unequal_live_endpoints_preserve_recorded_time(self):
        make_analysis(self.folder, "complex")
        with (self.folder / "coul.09/prod/dhdl.xvg").open("a") as stream:
            stream.write("30 4 5\n")
        inspection = inspect_analysis(self.folder, "complex", 10)
        self.assertEqual(inspection["total_analysed_ps"], 10)
        self.assertEqual(inspection["status"], "Incomplete")
        self.assertIn("coul.09", inspection["input_error"])
        self.assertIn("30 ps", inspection["input_error"])

    def test_uniform_production_growth_uses_saved_time(self):
        write_outputs(analysis_folder(self.folder, "complex"), "complex")
        for path in find_inputs(self.folder, "complex").values():
            with path.open("a") as stream:
                stream.write("30 4 5\n")
        inspection = inspect_analysis(self.folder, "complex", 20)
        self.assertEqual(inspection["total_analysed_ps"], 20)
        self.assertEqual(inspection["status"], "Incomplete")
        self.assertIn("available through 30 ps", inspection["remark"])
        self.assertEqual(inspection["input_error"], "")
        self.assertEqual(inspection["convergence"], "converged")

    def test_convergence_uses_half_data_with_a_strict_cutoff(self):
        directory = make_analysis(self.folder, "complex")
        for forward, backward, expected in (
            (1, 1.499, "converged"),
            (1, 1.5, "unconverged"),
            (1, 1.501, "unconverged"),
            (-1, -1.499, "converged"),
            (-1, -1.5, "unconverged"),
            (-1.501, -1, "unconverged"),
            (-0.2, 0.2, "converged"),
        ):
            with self.subTest(forward=forward, backward=backward):
                # Full-data agreement must not determine the half-data verdict.
                full_backward = 10 if expected == "converged" else 1
                (directory / "ABFE_convergence.csv").write_text(
                    ",Forward,Forward_Error,Backward,Backward_Error,data_fraction\n"
                    f"0,{forward},0.1,{backward},0.1,0.5\n"
                    f"1,1,0.1,{full_backward},0.1,1.0\n"
                )
                inspection = inspect_analysis(self.folder, "complex", 20)
                self.assertEqual(inspection["convergence"], expected)
                self.assertEqual(inspection["status"], "Complete")

    def test_missing_or_invalid_convergence_is_unknown(self):
        directory = make_analysis(self.folder, "complex")
        path = directory / "ABFE_convergence.csv"
        original = path.read_bytes()
        invalid = [
            None,
            b"",
            b"\xff",
            original.replace(b"Forward,", b"Missing,"),
            original.replace(b"0,1,0.1,1.1,0.1,0.5", b"0,1,0.1"),
            original.replace(b"0,1,", b"0,bad,"),
            original.replace(b"0,1,", b"0,nan,"),
            original.replace(b",1.1,", b",inf,"),
            original.replace(b",0.1,", b",-0.1,"),
            original.replace(b",1.0\n", b",nan\n"),
            original.replace(b",1.0\n", b",0.9\n"),
            original.replace(b",0.5\n", b",0.4\n"),
            original.replace(
                b"1,1,0.1,1.1,0.1,1.0",
                b"1,1,0.1,1.1,0.1,0.5\n2,1,0.1,1.1,0.1,1.0",
            ),
        ]
        for content in invalid:
            with self.subTest(content=content):
                if content is None:
                    path.unlink()
                else:
                    path.write_bytes(content)
                self.assertEqual(
                    inspect_analysis(self.folder, "complex", 20)["convergence"],
                    "unknown",
                )
        with patch.object(
            analysis_utils, "read_convergence", side_effect=PermissionError("denied")
        ):
            inspection = inspect_analysis(self.folder, "complex", 20)
        self.assertEqual(inspection["convergence"], "unknown")
        self.assertEqual(inspection["status"], "Incomplete")

    def test_convergence_does_not_require_summary_or_coverage(self):
        directory = make_analysis(self.folder, "complex")
        summary = directory / "ABFE_summary.csv"
        for content in ("broken\n", None):
            with self.subTest(summary=content):
                if content is None:
                    summary.unlink()
                else:
                    summary.write_text(content)
                inspection = inspect_analysis(self.folder, "complex")
                self.assertEqual(inspection["status"], "Incomplete")
                self.assertEqual(inspection["convergence"], "converged")
        shutil.rmtree(self.folder)
        self.assertEqual(
            inspect_analysis(self.folder, "complex")["convergence"], "converged"
        )

    def test_missing_or_duplicate_half_row_keeps_analysis_coverage_complete(self):
        directory = make_analysis(self.folder, "complex")
        path = directory / "ABFE_convergence.csv"
        original = path.read_text()
        half_row = "0,1,0.1,1.1,0.1,0.5\n"
        for replacement in ("", half_row * 2):
            with self.subTest(replacement=replacement):
                path.write_text(original.replace(half_row, replacement))
                inspection = inspect_analysis(self.folder, "complex", 20)
                self.assertEqual(inspection["status"], "Complete")
                self.assertEqual(inspection["convergence"], "unknown")

    def test_optional_timestamp_validation(self):
        compound = example_data()["datasets"]["example"]["complexes"]["ligand_01"]
        for kind, record in (
            ("alchemical_complex", compound["repeats"][1]["alchemical_complex"]),
            ("alchemical_ligand", compound["alchemical_ligand"]),
        ):
            for value in (
                0,
                20000.0,
                None,
                -1,
                float("nan"),
                float("inf"),
                -float("inf"),
                True,
                False,
                "20",
            ):
                with self.subTest(kind=kind, value=value):
                    errors = []
                    validate_run(
                        dict(record, total_analysed_ps=value), kind, "run", errors
                    )
                    valid = (
                        value is None
                        or type(value) in (int, float)
                        and value >= 0
                        and value < float("inf")
                    )
                    self.assertEqual(not errors, valid)
        vanilla = compound["repeats"][1]["vanilla"]
        for value in (None, 0, 20):
            errors = []
            validate_run(
                dict(vanilla, total_analysed_ps=value), "vanilla", "run", errors
            )
            self.assertTrue(any("total_analysed_ps" in error for error in errors))

    def test_symlinked_component_analysis_is_inspected(self):
        directory = make_analysis(self.folder, "complex")
        moved = directory.with_name("retained")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        self.assertEqual(
            inspect_analysis(self.folder, "complex"),
            {
                "status": "Complete",
                "remark": "",
                "total_analysed_ps": 20,
                "input_error": "",
                "convergence": "converged",
            },
        )


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "run/complex"
        make_inputs(self.folder, "complex")

    def fake_workflow(self, advance=False, fail=False):
        folder = self.folder
        calls = {}

        class Result:
            def to_csv(self, filename):
                write_outputs(Path.cwd(), "complex")

        class Workflow:
            def __init__(self, **kwargs):
                calls["constructor"] = kwargs
                calls["cwd"] = Path.cwd()
                calls["inputs"] = list(Path.cwd().glob("dhdl.*.xvg"))
                self.summary = Result()
                self.convergence = Result()

            def read(self, **kwargs):
                calls["read"] = kwargs

            def preprocess(self, **kwargs):
                calls["preprocess"] = kwargs
                if advance:
                    with (folder / "vdw.20/prod/dhdl.xvg").open("a") as stream:
                        stream.write("30 4 5\n")
                if fail:
                    raise ValueError("workflow failed")

            def estimate(self, **kwargs):
                calls.setdefault("estimate", []).append(kwargs)
                self.estimator = dict.fromkeys(kwargs["estimators"])

            def generate_result(self):
                calls["estimators"] = list(self.estimator)

            def plot_overlap_matrix(self, filename):
                return types.SimpleNamespace(figure=None)

            def plot_ti_dhdl(self):
                return types.SimpleNamespace(figure=None)

            def plot_dF_state(self, **kwargs):
                return None

            def check_convergence(self, fractions, **kwargs):
                calls["convergence"] = (fractions, kwargs)
                return types.SimpleNamespace(figure=None)

        module = types.ModuleType("alchemlyb.workflows")
        module.ABFE = Workflow
        pyplot = types.ModuleType("matplotlib.pyplot")
        pyplot.close = lambda figure: None
        matplotlib = types.ModuleType("matplotlib")
        matplotlib.pyplot = pyplot
        return patch.dict(
            sys.modules,
            {
                "alchemlyb": types.ModuleType("alchemlyb"),
                "alchemlyb.workflows": module,
                "matplotlib": matplotlib,
                "matplotlib.pyplot": pyplot,
            },
        ), calls

    def test_analysis_uses_frozen_inputs_and_reference_settings(self):
        original_directory = Path.cwd()
        with (self.folder / "coul.00/prod/dhdl.xvg").open("ab") as stream:
            stream.write(b"30 4")
        modules, calls = self.fake_workflow()
        with modules:
            analysed_time = runner.run_analysis(self.folder, "complex")
        self.assertEqual(analysed_time, 20)
        self.assertEqual(Path.cwd(), original_directory)
        self.assertNotEqual(calls["cwd"], self.folder)
        self.assertEqual(len(calls["inputs"]), 42)
        self.assertEqual(calls["constructor"]["T"], 298)
        self.assertEqual(calls["read"], {"n_jobs": -1})
        self.assertEqual(
            calls["preprocess"],
            {"skiptime": 10, "uncorr": "dhdl", "threshold": 50, "n_jobs": -1},
        )
        self.assertEqual(
            calls["estimate"],
            [
                {"estimators": ("BAR", "TI")},
                {"estimators": ("MBAR",), "n_bootstraps": 50},
            ],
        )
        self.assertEqual(calls["estimators"], ["MBAR", "BAR", "TI"])
        self.assertEqual(
            calls["convergence"],
            (10, {"estimator": "MBAR", "dF_t": "dF_t.pdf", "n_bootstraps": 50}),
        )
        self.assertEqual(
            inspect_analysis(self.folder, "complex"),
            {
                "status": "Complete",
                "remark": "",
                "total_analysed_ps": 20,
                "input_error": "",
                "convergence": "converged",
            },
        )
        copied = analysis_folder(self.folder, "complex") / "dhdl.coul.00.xvg"
        self.assertTrue(copied.read_bytes().endswith(b"20 3 4\n"))

    def test_analysis_passes_threads_to_supported_steps(self):
        modules, calls = self.fake_workflow()
        with modules:
            runner.run_analysis(self.folder, "complex", threads=3)
        self.assertEqual(calls["read"], {"n_jobs": 3})
        self.assertEqual(calls["preprocess"]["n_jobs"], 3)

    def test_analysis_preserves_previous_outputs_on_failure(self):
        directory = make_analysis(self.folder, "complex")
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        modules, _ = self.fake_workflow(fail=True)
        previous_directory = Path.cwd()
        with modules, self.assertRaisesRegex(ValueError, "workflow failed"):
            runner.run_analysis(self.folder, "complex")
        self.assertEqual(Path.cwd(), previous_directory)
        self.assertEqual(
            before, {path.name: path.read_bytes() for path in directory.iterdir()}
        )
        self.assertEqual(list(directory.parent.iterdir()), [directory])

    def test_failed_publication_restores_previous_directory(self):
        directory = make_analysis(self.folder, "complex")
        marker = directory / "previous-result"
        marker.write_text("keep")
        rename = Path.rename

        def fail_publication(path, target):
            if path.name == "results":
                raise OSError("publication failed")
            return rename(path, target)

        modules, _ = self.fake_workflow()
        with modules, patch.object(Path, "rename", fail_publication):
            with self.assertRaisesRegex(OSError, "publication failed"):
                runner.run_analysis(self.folder, "complex")
        self.assertEqual(marker.read_text(), "keep")

    def test_unequal_snapshot_fails_before_calculation_and_preserves_results(self):
        directory = make_analysis(self.folder, "complex")
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        with (self.folder / "coul.09/prod/dhdl.xvg").open("a") as stream:
            stream.write("30 4 5\n")
        modules, calls = self.fake_workflow()
        with (
            modules,
            self.assertRaisesRegex(
                ValueError, "Window end times differ.*30 ps: coul.09"
            ),
        ):
            runner.run_analysis(self.folder, "complex")
        self.assertNotIn("constructor", calls)
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})

    def test_data_added_during_analysis_is_left_incomplete(self):
        modules, _ = self.fake_workflow(advance=True)
        with modules:
            runner.run_analysis(self.folder, "complex")
        self.assertEqual(
            inspect_analysis(self.folder, "complex")["status"], "Incomplete"
        )

    def test_symlinked_publication_keeps_the_symlink(self):
        directory = make_analysis(self.folder, "complex")
        moved = directory.with_name("shared-results")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        modules, _ = self.fake_workflow()
        with modules:
            runner.run_analysis(self.folder, "complex")
        self.assertTrue(directory.is_symlink())
        validate_outputs(moved, "complex")


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.document = example_data()
        self.compound = self.document["datasets"]["example"]["complexes"]["ligand_01"]
        self.data_path = self.root / "data.yaml"

    def save(self):
        self.data_path.write_text(yaml.safe_dump(self.document, sort_keys=False))

    def run_command(self, *args, status="Missing", dataset="example"):
        return self.command(
            runner, "--dataset", dataset, "--analysis-status", status, *args
        )

    def refresh_command(self, *args):
        return self.command(refresher, *args)

    def command(self, module, *args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = module.main(args, self.data_path)
        return result, output.getvalue(), errors.getvalue()

    def prepare_components(self):
        ligand = self.root / "shared/ligand"
        make_inputs(ligand, "ligand")
        make_analysis(ligand, "ligand")
        self.compound["alchemical_ligand"]["folder"] = str(ligand)
        for repeat, runs in self.compound["repeats"].items():
            folder = self.root / f"repeat{repeat}/complex"
            make_inputs(folder, "complex")
            make_analysis(folder, "complex")
            restraint = folder.parent / "boresch/resgen_output.txt"
            restraint.parent.mkdir()
            restraint.write_text("dG_off: -2, dG_on: 2\n")
            runs["alchemical_complex"]["folder"] = str(folder)
        self.save()

    def test_preview_writes_nothing_and_status_refresh_preserves_manual_data(self):
        self.compound["alchemical_ligand"]["remark"] = "Manual note"
        self.compound["alchemical_ligand"]["convergence"] = "converged"
        self.save()
        before = self.data_path.read_bytes()
        result, output, _ = self.run_command("--dry-run")
        self.assertEqual(result, 1)
        self.assertIn("Blocked: Source folder is not set", output)
        self.assertEqual(self.data_path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.data_path])
        self.assertEqual(self.refresh_command()[0], 0)
        updated = load_data(self.data_path)
        ligand = updated["datasets"]["example"]["complexes"]["ligand_01"][
            "alchemical_ligand"
        ]
        self.assertEqual(ligand["analysis_status"], "Missing")
        self.assertEqual(ligand["remark"], "Manual note")
        self.assertEqual(ligand["convergence"], "unknown")
        for field in (
            "status",
            "total_production_ns",
            "completed_production_ns",
        ):
            self.assertEqual(ligand[field], self.compound["alchemical_ligand"][field])
        backups = list((self.root / "archives").iterdir())
        self.assertEqual(backups[0].read_bytes(), before)
        self.refresh_command()
        self.assertEqual(list((self.root / "archives").iterdir()), backups)

    def test_refresh_dataset_filter_and_all_datasets_default(self):
        self.prepare_components()
        self.document["datasets"]["other"] = deepcopy(
            self.document["datasets"]["example"]
        )
        self.save()
        self.refresh_command("--dataset", "example")
        other = load_data(self.data_path)["datasets"]["other"]
        self.assertEqual(
            other["complexes"]["ligand_01"]["alchemical_ligand"]["convergence"],
            "unknown",
        )
        self.assertEqual(self.first_saved_record()["convergence"], "converged")
        self.assertNotIn(
            "analysis_status", other["complexes"]["ligand_01"]["alchemical_ligand"]
        )
        self.assertEqual(self.refresh_command()[0], 0)
        other = load_data(self.data_path)["datasets"]["other"]
        self.assertEqual(
            other["complexes"]["ligand_01"]["alchemical_ligand"]["analysis_status"],
            "Complete",
        )
        self.assertEqual(
            other["complexes"]["ligand_01"]["alchemical_ligand"]["convergence"],
            "converged",
        )

    def test_refresh_saves_convergence_and_resets_invalid_results(self):
        self.prepare_components()
        before = self.data_path.read_bytes()
        self.assertEqual(self.refresh_command()[0], 0)
        updated = load_data(self.data_path)["datasets"]["example"]["complexes"][
            "ligand_01"
        ]
        for repeat, runs in updated["repeats"].items():
            record = runs["alchemical_complex"]
            self.assertEqual(record["convergence"], "converged")
            for field in (
                "status",
                "remark",
                "total_production_ns",
                "completed_production_ns",
            ):
                self.assertEqual(
                    record[field],
                    self.compound["repeats"][repeat]["alchemical_complex"][field],
                )
        self.assertEqual(updated["alchemical_ligand"]["convergence"], "converged")
        backups = list((self.root / "archives").iterdir())
        self.assertEqual(backups[0].read_bytes(), before)
        unchanged = self.data_path.read_bytes(), self.data_path.stat().st_mtime_ns
        self.assertEqual(self.refresh_command()[0], 0)
        self.assertEqual(
            (self.data_path.read_bytes(), self.data_path.stat().st_mtime_ns), unchanged
        )
        self.assertEqual(list((self.root / "archives").iterdir()), backups)

        complex_folder = Path(updated["repeats"][1]["alchemical_complex"]["folder"])
        path = analysis_folder(complex_folder, "complex") / "ABFE_convergence.csv"
        path.write_text(path.read_text().replace("0,1,0.1,1.1", "0,-1,0.1,-1.5"))
        ligand_folder = Path(updated["alchemical_ligand"]["folder"])
        (analysis_folder(ligand_folder, "ligand") / "ABFE_convergence.csv").unlink()
        self.assertEqual(self.refresh_command()[0], 0)
        self.assertEqual(self.first_saved_record()["convergence"], "unconverged")
        updated = load_data(self.data_path)["datasets"]["example"]["complexes"][
            "ligand_01"
        ]
        self.assertEqual(updated["alchemical_ligand"]["convergence"], "unknown")

    def test_runner_requires_valid_dataset_and_status_flags(self):
        self.save()
        arguments = (
            (),
            ("--dataset", "example"),
            ("--analysis-status", "Missing"),
            ("--dataset", "example", "--analysis-status", "Waiting"),
            ("--dataset", "missing", "--analysis-status", "Missing"),
            ("--dataset", "example", "--analysis-status", "Missing", "--run"),
            ("--dataset", "example", "--analysis-status", "Missing", "--update-status"),
            (
                "--dataset",
                "example",
                "--analysis-status",
                "Missing",
                "--n_concurrent",
                "2",
            ),
        )
        for arguments in arguments:
            with (
                self.subTest(arguments=arguments),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                runner.main(arguments, self.data_path)
            self.assertEqual(raised.exception.code, 2)

    def test_refresh_rejects_unknown_dataset_and_execution_flags(self):
        self.save()
        for arguments in (("--dataset", "missing"), ("--run",), ("--update-status",)):
            with (
                self.subTest(arguments=arguments),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                refresher.main(arguments, self.data_path)
            self.assertEqual(raised.exception.code, 2)

    def test_runs_in_order_deduplicate_and_continue_after_failure(self):
        self.prepare_components()
        self.compound["repeats"][3] = deepcopy(self.compound["repeats"][1])
        self.save()
        entries = analysis_utils.select_entries(self.document, "example", self.root)
        first_folder = entries[0]["folder"]

        def analyse(folder, component, threads):
            if folder == first_folder:
                raise ValueError("one component failed")
            return 20.0

        with patch.object(runner, "run_analysis", side_effect=analyse) as analysis:
            result, _, errors = self.run_command("--threads", "2")
        self.assertEqual(result, 1)
        self.assertIn("one component failed", errors)
        self.assertEqual(
            analysis.call_args_list,
            [
                call(entry["folder"], entry["component"], threads=2)
                for entry in (entries[0], entries[1], entries[3])
            ],
        )
        self.assertIn(
            "one component failed", self.first_saved_record()["analysis_remark"]
        )

    def test_shared_ligand_returns_one_timestamp_to_all_references(self):
        self.prepare_components()
        ligand = self.compound["alchemical_ligand"]
        ligand["analysis_status"] = "Incomplete"
        self.document["datasets"]["example"]["complexes"]["ligand_02"] = deepcopy(
            self.compound
        )
        self.save()
        with patch.object(runner, "run_analysis", return_value=20.0) as analysis:
            self.assertEqual(
                self.run_command("--threads", "2", status="Incomplete")[0], 0
            )
        analysis.assert_called_once_with(Path(ligand["folder"]), "ligand", threads=2)
        compounds = load_data(self.data_path)["datasets"]["example"]["complexes"]
        for compound in compounds.values():
            self.assertEqual(compound["alchemical_ligand"]["total_analysed_ps"], 20)
            self.assertEqual(compound["alchemical_ligand"]["convergence"], "converged")
            self.assertEqual(
                compound["alchemical_ligand"]["analysis_status"], "Complete"
            )

    def test_saved_status_selects_only_matching_dataset_and_complex(self):
        self.prepare_components()
        first = self.compound["repeats"][1]["alchemical_complex"]
        first["analysis_status"] = "Incomplete"
        second = self.compound["repeats"][2]["alchemical_complex"]
        second["analysis_status"] = "Complete"
        self.compound["alchemical_ligand"]["analysis_status"] = "Complete"
        self.document["datasets"]["other"] = deepcopy(
            self.document["datasets"]["example"]
        )
        self.save()
        before = load_data(self.data_path)
        # Live inspection is Complete, but selection must use saved Incomplete.
        self.assertEqual(
            inspect_analysis(Path(first["folder"]), "complex")["status"], "Complete"
        )
        with patch.object(runner, "run_analysis", return_value=20.0) as analysis:
            result, output, _ = self.run_command(status="Incomplete")
        self.assertEqual(result, 0)
        analysis.assert_called_once_with(Path(first["folder"]), "complex", threads=-1)
        self.assertEqual(output.count("Compiled abfe_results.csv"), 1)
        self.assertTrue(
            (Path(first["folder"]).parent / "analysis/abfe_results.csv").is_file()
        )
        self.assertFalse(
            (Path(second["folder"]).parent / "analysis/abfe_results.csv").exists()
        )
        after = load_data(self.data_path)
        expected = deepcopy(before)
        expected["datasets"]["example"]["complexes"]["ligand_01"]["repeats"][1][
            "alchemical_complex"
        ].update(
            analysis_status="Complete",
            analysis_remark="",
            total_analysed_ps=20.0,
            convergence="converged",
        )
        self.assertEqual(after, expected)

    def test_missing_filter_includes_absent_statuses(self):
        self.prepare_components()
        self.compound["repeats"][1]["alchemical_complex"]["analysis_status"] = "Missing"
        self.compound["alchemical_ligand"]["analysis_status"] = "Incomplete"
        self.save()
        with patch.object(runner, "run_analysis", return_value=20.0) as analysis:
            self.assertEqual(self.run_command()[0], 0)
        self.assertEqual(
            analysis.call_args_list,
            [
                call(Path(runs["alchemical_complex"]["folder"]), "complex", threads=-1)
                for _, runs in sorted(self.compound["repeats"].items())
            ],
        )
        saved_ligand = load_data(self.data_path)["datasets"]["example"]["complexes"][
            "ligand_01"
        ]["alchemical_ligand"]
        self.assertEqual(saved_ligand, self.compound["alchemical_ligand"])

    def test_complete_selection_reruns_verified_complete_component(self):
        self.prepare_components()
        record = self.compound["repeats"][1]["alchemical_complex"]
        record["analysis_status"] = "Complete"
        self.save()
        folder = Path(record["folder"])
        self.assertEqual(inspect_analysis(folder, "complex")["status"], "Complete")
        with patch.object(runner, "run_analysis", return_value=20.0) as analysis:
            self.assertEqual(self.run_command(status="Complete")[0], 0)
        analysis.assert_called_once_with(folder, "complex", threads=-1)
        self.assertEqual(self.first_saved_record()["analysis_status"], "Complete")

    def test_empty_selection_does_not_inspect_or_write(self):
        self.save()
        before = self.data_path.read_bytes(), self.data_path.stat().st_mtime_ns
        with (
            patch.object(
                runner, "run_analysis", side_effect=AssertionError("no analyses")
            ),
            patch.object(
                analysis_utils,
                "inspect_analysis",
                side_effect=AssertionError("no inspection"),
            ),
        ):
            for arguments in ((), ("--dry-run",)):
                with self.subTest(arguments=arguments):
                    result, output, _ = self.run_command(
                        *arguments, status="Incomplete"
                    )
                    self.assertEqual(result, 0)
                    self.assertIn("No analyses match", output)
        self.assertEqual(
            (self.data_path.read_bytes(), self.data_path.stat().st_mtime_ns), before
        )
        self.assertEqual(list(self.root.iterdir()), [self.data_path])

    def test_complex_preview_skips_ligand_with_missing_inputs(self):
        self.prepare_components()
        self.compound["alchemical_ligand"]["folder"] = None
        self.save()

        result, output, _ = self.run_command("--leg", "complex", "--dry-run")
        self.assertEqual(result, 0)
        self.assertEqual(output.count("Ready"), 2)
        self.assertNotIn("Shared", output)

    def test_ligand_selection_compiles_all_its_complex_repeats(self):
        self.prepare_components()
        for runs in self.compound["repeats"].values():
            runs["alchemical_complex"]["analysis_status"] = "Incomplete"
        ligand = self.compound["alchemical_ligand"]
        ligand["analysis_status"] = "Incomplete"
        self.save()

        def update_ligand(folder, component, threads=-1):
            self.assertEqual((folder, component), (Path(ligand["folder"]), "ligand"))
            summary = (
                analysis_folder(Path(ligand["folder"]), "ligand") / "ABFE_summary.csv"
            )
            summary.write_text(
                summary.read_text().replace("Stages,coul,1,", "Stages,coul,2,")
            )
            return 20.0

        with patch.object(runner, "run_analysis", side_effect=update_ligand):
            result, output, _ = self.run_command("--leg", "ligand", status="Incomplete")
        self.assertEqual(result, 0)
        self.assertEqual(output.count("Compiled abfe_results.csv"), 2)
        saved = load_data(self.data_path)["datasets"]["example"]["complexes"][
            "ligand_01"
        ]
        self.assertEqual(saved["repeats"], self.compound["repeats"])
        for runs in self.compound["repeats"].values():
            final = (
                Path(runs["alchemical_complex"]["folder"]).parent
                / "analysis/abfe_results.csv"
            )
            with final.open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(float(rows[-1]["MBAR"]), 0)

    def test_missing_counterpart_blocks_compilation_without_running_it(self):
        self.prepare_components()
        self.compound["repeats"][1]["alchemical_complex"]["analysis_status"] = (
            "Incomplete"
        )
        self.compound["alchemical_ligand"]["folder"] = None
        self.save()
        with patch.object(runner, "run_analysis", return_value=20.0) as analysis:
            result, _, errors = self.run_command(status="Incomplete")
        self.assertEqual(result, 1)
        self.assertIn("Compilation failed", errors)
        analysis.assert_called_once_with(
            Path(self.compound["repeats"][1]["alchemical_complex"]["folder"]),
            "complex",
            threads=-1,
        )
        self.assertEqual(self.first_saved_record()["analysis_status"], "Complete")

    def test_failed_ligand_does_not_compile_dependent_results(self):
        self.prepare_components()
        self.compound["alchemical_ligand"]["analysis_status"] = "Incomplete"
        self.save()
        finals = [
            Path(runs["alchemical_complex"]["folder"]).parent
            / "analysis/abfe_results.csv"
            for runs in self.compound["repeats"].values()
        ]
        for final in finals:
            final.write_text("previous result")
        with patch.object(
            runner, "run_analysis", side_effect=ValueError("ligand calculation failed")
        ):
            result, _, errors = self.run_command(status="Incomplete")
        self.assertEqual(result, 1)
        self.assertIn("ligand calculation failed", errors)
        self.assertEqual(
            [final.read_text() for final in finals], ["previous result"] * 2
        )

    def test_refresh_protects_concurrent_edits_and_keeps_outputs(self):
        self.prepare_components()
        inspect = analysis_utils.inspect_analysis

        def inspect_and_edit(*args):
            self.compound["alchemical_ligand"]["remark"] = "Edited during refresh"
            self.save()
            return inspect(*args)

        with patch.object(
            analysis_utils, "inspect_analysis", side_effect=inspect_and_edit
        ):
            with self.assertRaises(SystemExit) as raised:
                self.refresh_command()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Edited during refresh", self.data_path.read_text())
        self.assertFalse((self.root / "archives").exists())
        self.assertEqual(len(list(self.root.rglob("dhdl.*.xvg"))), 116)

    def test_refresh_changes_unverified_complete_to_incomplete(self):
        self.prepare_components()
        record = self.compound["repeats"][1]["alchemical_complex"]
        record.update(analysis_status="Complete", remark="Manual note")
        self.save()
        directory = analysis_folder(Path(record["folder"]), "complex")
        (directory / "dhdl.rest.00.xvg").unlink()
        self.assertIn("Complete", self.run_command("--dry-run", status="Complete")[1])
        self.assertEqual(self.refresh_command()[0], 0)
        document = load_data(self.data_path)
        updated = document["datasets"]["example"]["complexes"]["ligand_01"]["repeats"][
            1
        ]["alchemical_complex"]
        self.assertEqual(updated["analysis_status"], "Incomplete")
        self.assertIn("Data not verified", updated["analysis_remark"])
        self.assertEqual(updated["remark"], "Manual note")

    def test_run_passes_threads_to_analysis(self):
        self.prepare_components()
        record = self.compound["repeats"][1]["alchemical_complex"]
        record["analysis_status"] = "Complete"
        self.save()
        for arguments, expected in (
            ((), -1),
            (("--threads", "2"), 2),
            (("--threads", "-1"), -1),
            (("--threads", "1"), 1),
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(
                    runner, "run_analysis", side_effect=self.publish_analysis
                ) as analysis,
            ):
                self.assertEqual(self.run_command(*arguments, status="Complete")[0], 0)
            analysis.assert_called_once_with(
                Path(record["folder"]), "complex", threads=expected
            )

    def test_invalid_threads_are_rejected_before_loading_data(self):
        for value in ("0", "-2", "1.5", "abc"):
            with (
                self.subTest(value=value),
                redirect_stderr(io.StringIO()),
                patch.object(runner, "load_data") as load,
                self.assertRaises(SystemExit) as raised,
            ):
                runner.main(
                    [
                        "--dataset",
                        "example",
                        "--analysis-status",
                        "Missing",
                        "--threads",
                        value,
                    ],
                    self.data_path,
                )
            self.assertEqual(raised.exception.code, 2)
            load.assert_not_called()

    def test_combined_results_update_when_shared_summary_changes(self):
        self.prepare_components()
        self.enterContext(patch.object(runner, "run_analysis", return_value=20.0))
        self.enterContext(patch.object(runner, "cleanup_inputs", return_value=False))
        before = self.data_path.read_bytes()
        self.assertIn("Runs to be analysed:", self.run_command("--dry-run")[1])
        self.assertEqual(self.data_path.read_bytes(), before)
        self.assertEqual(list(self.root.glob("repeat*/analysis/abfe_results.csv")), [])
        result, output, _ = self.run_command()
        self.assertEqual(result, 0)
        self.assertEqual(output.count("Compiled abfe_results.csv"), 2)
        folder = Path(self.compound["repeats"][1]["alchemical_complex"]["folder"])
        final = folder.parent / "analysis/abfe_results.csv"
        with final.open() as stream:
            rows = list(csv.DictReader(stream))
        # Ligand 1+2, restraint +2, complex -(1+2+3), with linear error sums.
        self.assertAlmostEqual(float(rows[-1]["MBAR"]), -1)
        self.assertAlmostEqual(float(rows[-1]["MBAR_Error"]), 0.5)
        self.assertAlmostEqual(float(rows[-1]["BAR"]), -2)
        self.assertAlmostEqual(float(rows[-1]["TI"]), -3)
        before = final.stat().st_mtime_ns
        self.assertEqual(self.run_command(status="Complete")[0], 0)
        self.assertEqual(final.stat().st_mtime_ns, before)
        summary = (
            analysis_folder(
                Path(self.compound["alchemical_ligand"]["folder"]), "ligand"
            )
            / "ABFE_summary.csv"
        )
        summary.write_text(
            summary.read_text().replace("Stages,coul,1,", "Stages,coul,2,")
        )
        self.assertEqual(self.run_command(status="Complete")[0], 0)
        self.assertNotEqual(final.stat().st_mtime_ns, before)

    def test_failures_are_reported_and_statuses_still_saved(self):
        self.save()
        result, _, errors = self.run_command()
        self.assertEqual(result, 1)
        self.assertIn("Analysis failed", errors)
        self.assertIn("analysis_status", self.data_path.read_text())

    def test_edits_during_analysis_are_not_overwritten(self):
        self.prepare_components()

        def edit_yaml(folder, component, threads=-1):
            self.compound["alchemical_ligand"]["remark"] = "Edited during calculation"
            self.save()
            return 20.0

        with patch.object(runner, "run_analysis", side_effect=edit_yaml):
            with self.assertRaises(SystemExit) as raised:
                self.run_command()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Edited during calculation", self.data_path.read_text())
        self.assertFalse((self.root / "archives").exists())

    def publish_analysis(self, folder, component, threads=-1):
        directory = analysis_folder(folder, component)
        shutil.rmtree(directory)
        make_analysis(folder, component)
        (directory / "plot.pdf").write_bytes(b"plot")
        return 20.0

    def first_saved_record(self):
        return load_data(self.data_path)["datasets"]["example"]["complexes"][
            "ligand_01"
        ]["repeats"][1]["alchemical_complex"]

    def test_successful_run_saves_before_deleting_only_new_inputs(self):
        self.prepare_components()
        record = self.compound["repeats"][1]["alchemical_complex"]
        record.update(
            total_analysed_ps=10,
            remark="Keep manual note",
            analysis_status="Incomplete",
        )
        self.save()
        before = self.data_path.read_bytes()
        directory = analysis_folder(Path(record["folder"]), "complex")
        save_yaml = analysis_utils.update_yaml

        def check_save(path, document):
            self.assertEqual(len(list(directory.glob("dhdl.*.xvg"))), 42)
            self.assertEqual(self.first_saved_record()["total_analysed_ps"], 10)
            return save_yaml(path, document)

        with (
            patch.object(runner, "run_analysis", side_effect=self.publish_analysis),
            patch.object(analysis_utils, "update_yaml", side_effect=check_save),
        ):
            self.assertEqual(self.run_command(status="Incomplete")[0], 0)
        saved = self.first_saved_record()
        self.assertEqual(saved["total_analysed_ps"], 20)
        self.assertEqual(saved["analysis_status"], "Complete")
        self.assertEqual(saved["remark"], "Keep manual note")
        self.assertEqual(saved["convergence"], "converged")
        for field in (
            "status",
            "total_production_ns",
            "completed_production_ns",
        ):
            self.assertEqual(saved[field], record[field])
        self.assertEqual(list(directory.glob("dhdl.*.xvg")), [])
        self.assertEqual((directory / "plot.pdf").read_bytes(), b"plot")
        validate_outputs(directory, "complex")
        self.assertEqual(next((self.root / "archives").iterdir()).read_bytes(), before)
        ligand = analysis_folder(
            Path(self.compound["alchemical_ligand"]["folder"]), "ligand"
        )
        self.assertEqual(len(list(ligand.glob("dhdl.*.xvg"))), 32)
        self.assertEqual(self.refresh_command()[0], 0)
        self.assertEqual(self.first_saved_record()["analysis_status"], "Complete")

    def test_failed_yaml_save_retains_snapshot_for_later_recovery(self):
        self.prepare_components()
        record = self.compound["repeats"][1]["alchemical_complex"]
        record["total_analysed_ps"] = 10
        self.save()
        before = self.data_path.read_bytes()
        with (
            patch.object(runner, "run_analysis", side_effect=self.publish_analysis),
            patch.object(
                analysis_utils, "update_yaml", side_effect=OSError("save failed")
            ),
        ):
            with self.assertRaises(SystemExit) as raised:
                self.run_command()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(self.data_path.read_bytes(), before)
        directory = analysis_folder(Path(record["folder"]), "complex")
        self.assertEqual(len(list(directory.glob("dhdl.*.xvg"))), 42)
        before_refresh = {p.name: p.read_bytes() for p in directory.iterdir()}
        self.assertEqual(self.refresh_command()[0], 0)
        self.assertEqual(self.first_saved_record()["total_analysed_ps"], 20)
        self.assertEqual(
            before_refresh, {p.name: p.read_bytes() for p in directory.iterdir()}
        )

    def test_calculation_failure_keeps_previous_results_and_recorded_time(self):
        self.prepare_components()
        record = self.compound["repeats"][1]["alchemical_complex"]
        record.update(total_analysed_ps=20, analysis_status="Incomplete")
        self.save()
        folder = Path(record["folder"])
        directory = analysis_folder(folder, "complex")
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        for path in find_inputs(folder, "complex").values():
            with path.open("a") as stream:
                stream.write("30 4 5\n")
        with patch.object(
            runner, "run_analysis", side_effect=ValueError("calculation failed")
        ) as analysis:
            result, _, errors = self.run_command(status="Incomplete")
        analysis.assert_called_once_with(folder, "complex", threads=-1)
        self.assertEqual(result, 1)
        self.assertIn("calculation failed", errors)
        self.assertEqual(self.first_saved_record()["total_analysed_ps"], 20)
        self.assertEqual(self.first_saved_record()["analysis_status"], "Incomplete")
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})

    def test_inventory_edit_keeps_published_inputs_and_user_edits(self):
        self.prepare_components()

        def publish_and_edit(folder, component, threads=-1):
            analysed_time = self.publish_analysis(folder, component, threads)
            self.compound["alchemical_ligand"]["remark"] = "Concurrent edit"
            self.save()
            return analysed_time

        with patch.object(runner, "run_analysis", side_effect=publish_and_edit):
            with self.assertRaises(SystemExit):
                self.run_command()
        self.assertIn("Concurrent edit", self.data_path.read_text())
        directory = analysis_folder(
            Path(self.compound["repeats"][1]["alchemical_complex"]["folder"]), "complex"
        )
        self.assertEqual(len(list(directory.glob("dhdl.*.xvg"))), 42)
        self.assertNotIn("total_analysed_ps", self.first_saved_record())

    def test_cleanup_failure_keeps_valid_results_and_saved_time(self):
        self.prepare_components()
        folder = Path(self.compound["repeats"][1]["alchemical_complex"]["folder"])
        directory = analysis_folder(folder, "complex")
        blocked = directory / "dhdl.rest.00.xvg"
        unlink = Path.unlink

        def fail_one_copy(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError("cannot delete input")
            return unlink(path, *args, **kwargs)

        with (
            patch.object(runner, "run_analysis", side_effect=self.publish_analysis),
            patch.object(Path, "unlink", fail_one_copy),
        ):
            result, _, errors = self.run_command()
        self.assertEqual(result, 1)
        self.assertIn("Input cleanup failed", errors)
        saved = self.first_saved_record()
        self.assertEqual(saved["total_analysed_ps"], 20)
        self.assertEqual(saved["analysis_status"], "Complete")
        self.assertTrue(blocked.is_file())
        validate_outputs(directory, "complex")

    def test_production_advancing_keeps_calculated_time_without_rerunning(self):
        self.prepare_components()

        def publish_and_advance(folder, component, threads=-1):
            analysed_time = self.publish_analysis(folder, component, threads)
            for path in find_inputs(folder, component).values():
                with path.open("a") as stream:
                    stream.write("30 4 5\n")
            return analysed_time

        with patch.object(
            runner, "run_analysis", side_effect=publish_and_advance
        ) as analysis:
            self.assertEqual(self.run_command()[0], 0)
        self.assertEqual(analysis.call_count, 3)
        saved = self.first_saved_record()
        self.assertEqual(saved["total_analysed_ps"], 20)
        self.assertEqual(saved["analysis_status"], "Incomplete")
        self.assertIn("available through 30 ps", saved["analysis_remark"])

    def test_unequal_inputs_block_one_component_and_preserve_saved_time(self):
        self.prepare_components()
        record = self.compound["repeats"][1]["alchemical_complex"]
        record["total_analysed_ps"] = 10
        self.save()
        folder = Path(record["folder"])
        directory = analysis_folder(folder, "complex")
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        with (folder / "vdw.20/prod/dhdl.xvg").open("a") as stream:
            stream.write("30 4 5\n")
        original = self.data_path.read_bytes()
        self.assertEqual(self.run_command("--dry-run")[0], 1)
        self.assertEqual(self.data_path.read_bytes(), original)
        self.assertEqual(self.refresh_command()[0], 1)
        self.assertEqual(self.first_saved_record()["total_analysed_ps"], 10)
        with patch.object(
            runner,
            "run_analysis",
            side_effect=AssertionError("blocked analysis must not start"),
        ) as analysis:
            result, _, errors = self.run_command(status="Incomplete")
        analysis.assert_not_called()
        self.assertEqual(result, 1)
        self.assertIn("vdw.20", errors)
        self.assertEqual(self.first_saved_record()["total_analysed_ps"], 10)
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})
        ligand = load_data(self.data_path)["datasets"]["example"]["complexes"][
            "ligand_01"
        ]["alchemical_ligand"]
        self.assertEqual(ligand["analysis_status"], "Complete")
        self.assertEqual(ligand["total_analysed_ps"], 20)

    def test_preview_recovers_nothing_on_disk_and_refresh_keeps_legacy_copies(self):
        self.prepare_components()
        self.compound["repeats"][1]["alchemical_complex"].update(
            total_production_ns=20, completed_production_ns=12.5
        )
        self.compound["repeats"][2]["alchemical_complex"].update(
            total_production_ns=0, completed_production_ns=0
        )
        self.save()
        before = self.data_path.read_bytes()
        files = {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}
        with patch.object(
            analysis_utils,
            "inspect_analysis",
            side_effect=AssertionError("preview uses saved statuses"),
        ):
            result, output, errors = self.run_command("--dry-run")
        self.assertEqual(result, 0)
        self.assertTrue(output.startswith("Runs to be analysed:\n\n"))
        self.assertIn("Total production (ns)", output)
        self.assertIn("Finished production (ns)", output)
        self.assertEqual(
            [line.split() for line in output.splitlines()[3:]],
            [
                [
                    "example",
                    "ligand_01",
                    "complex",
                    "1",
                    "Missing",
                    "20",
                    "12.5",
                    "Ready",
                ],
                ["example", "ligand_01", "complex", "2", "Missing", "0", "0", "Ready"],
                [
                    "example",
                    "ligand_01",
                    "ligand",
                    "Shared",
                    "Missing",
                    "Unknown",
                    "Unknown",
                    "Ready",
                ],
            ],
        )
        self.assertNotIn("Checking", output)
        self.assertNotIn("Would compile", output)
        self.assertEqual(errors, "")
        self.assertEqual(self.data_path.read_bytes(), before)
        self.assertEqual(
            files,
            {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()},
        )
        self.assertEqual(self.refresh_command()[0], 0)
        self.assertEqual(self.first_saved_record()["total_analysed_ps"], 20)
        self.assertTrue(all(path.exists() for path in files))
        for path, modified in files.items():
            if path != self.data_path:
                self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_compilation_failure_preserves_component_statuses_and_old_result(self):
        self.prepare_components()
        folder = Path(self.compound["repeats"][1]["alchemical_complex"]["folder"])
        final = folder.parent / "analysis/abfe_results.csv"
        final.write_text("previous result")
        (folder.parent / "boresch/resgen_output.txt").unlink()
        with patch.object(runner, "run_analysis", return_value=20.0):
            result, _, errors = self.run_command()
        self.assertEqual(result, 1)
        self.assertIn("Compilation failed", errors)
        self.assertEqual(final.read_text(), "previous result")
        document = load_data(self.data_path)
        run = document["datasets"]["example"]["complexes"]["ligand_01"]["repeats"][1][
            "alchemical_complex"
        ]
        self.assertEqual(run["analysis_status"], "Complete")


if __name__ == "__main__":
    unittest.main()
