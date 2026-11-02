"""Check coverage decisions, safe execution and four-worker scheduling."""

import csv
import io
import json
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
from unittest.mock import patch

import yaml
from test_console import example_data

from data import load_data
from utils import analyse_missing as runner
from utils.analysis_status import (
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


def parallel_probe(folder, component):
    """Keep the first four processes busy together, then let the fifth run."""
    folder = Path(folder)
    started = time.monotonic()
    (folder / "started").write_text(str(started))
    deadline = started + 5
    while len(list(folder.parent.parent.glob("*/complex/started"))) < 4:
        if time.monotonic() > deadline:
            raise RuntimeError("workers did not overlap")
        time.sleep(0.01)
    time.sleep(0.1)
    (folder / "finished.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "start": started,
                "end": time.monotonic(),
            }
        )
    )
    if folder.parent.name == "run0":
        raise ValueError("one component failed")


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name) / "run/complex"
        make_inputs(self.folder, "complex")

    def test_missing_and_partial_outputs(self):
        self.assertEqual(inspect_analysis(None, "complex")[0], "Missing")
        self.assertEqual(inspect_analysis(self.folder, "complex")[0], "Missing")
        directory = analysis_folder(self.folder, "complex")
        directory.mkdir(parents=True)
        (directory / "ABFE_summary.csv").write_text("broken\n")
        self.assertEqual(inspect_analysis(self.folder, "complex")[0], "Incomplete")

    def test_complete_coverage_ignores_production_completion_and_filtering(self):
        make_analysis(self.folder, "complex")
        # No MDP, trajectory or production log is needed to inspect current inputs.
        self.assertEqual(inspect_analysis(self.folder, "complex"), ("Complete", ""))

    def test_later_timestamp_in_any_window_makes_analysis_incomplete(self):
        make_analysis(self.folder, "complex")
        path = self.folder / "vdw.20/prod/dhdl.xvg"
        with path.open("a") as stream:
            stream.write("30 4 5\n")
        status, remark = inspect_analysis(self.folder, "complex")
        self.assertEqual(status, "Incomplete")
        self.assertIn("vdw.20", remark)
        self.assertIn("available through 30 ps", remark)

    def test_equal_final_time_with_more_samples_is_complete(self):
        make_analysis(self.folder, "complex")
        (self.folder / "coul.09/prod/dhdl.xvg").write_text(
            "0 1 2\n5 1 2\n10 2 3\n20 3 4\n"
        )
        self.assertEqual(inspect_analysis(self.folder, "complex"), ("Complete", ""))

    def test_unavailable_evidence_is_complete_with_remark(self):
        directory = make_analysis(self.folder, "complex")
        (directory / "dhdl.rest.00.xvg").unlink()
        status, remark = inspect_analysis(self.folder, "complex")
        self.assertEqual(status, "Complete")
        self.assertIn("Data not verified", remark)
        self.assertIn("rest.00", remark)

    def test_proven_gap_wins_over_unverifiable_window(self):
        directory = make_analysis(self.folder, "complex")
        (directory / "dhdl.rest.00.xvg").unlink()
        with (self.folder / "vdw.20/prod/dhdl.xvg").open("a") as stream:
            stream.write("30 4 5\n")
        self.assertEqual(inspect_analysis(self.folder, "complex")[0], "Incomplete")

    def test_missing_source_or_newer_copies_cannot_verify_coverage(self):
        directory = make_analysis(self.folder, "complex")
        copied = directory / "dhdl.rest.00.xvg"
        future = time.time_ns() + 1_000_000_000
        os.utime(copied, ns=(future, future))
        self.assertIn("newer than results", inspect_analysis(self.folder, "complex")[1])
        shutil.rmtree(self.folder)
        status, remark = inspect_analysis(self.folder, "complex")
        self.assertEqual(status, "Complete")
        self.assertIn("Data not verified", remark)

    def test_missing_and_unexpected_windows_block_execution(self):
        make_analysis(self.folder, "complex")
        shutil.rmtree(self.folder / "rest.10")
        (self.folder / "coul.10").mkdir()
        with self.assertRaisesRegex(
            ValueError, "missing windows: rest.10; unexpected windows: coul.10"
        ):
            find_inputs(self.folder, "complex")
        status, remark = inspect_analysis(self.folder, "complex")
        self.assertEqual(status, "Incomplete")
        self.assertIn("Unexpected window layout", remark)

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
        self.assertEqual(inspect_analysis(self.folder, "complex"), ("Complete", ""))

        path = self.folder / "rest.11/prod/dhdl.xvg"
        path.write_text(path.read_text().replace("(0, 0, 1)", "(0, 0, 0.9)"))
        with self.assertRaisesRegex(ValueError, "unexpected windows: rest.11"):
            find_inputs(self.folder, "complex")
        self.assertEqual(inspect_analysis(self.folder, "complex")[0], "Incomplete")

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
        self.assertEqual(inspect_analysis(self.folder, "complex")[0], "Incomplete")
        convergence.write_text(original)
        summary = directory / "ABFE_summary.csv"
        summary.write_text(
            "\n".join(
                line
                for line in summary.read_text().splitlines()
                if "bonded" not in line
            )
        )
        self.assertIn("missing stages", inspect_analysis(self.folder, "complex")[1])

    def test_symlinked_component_analysis_is_inspected(self):
        directory = make_analysis(self.folder, "complex")
        moved = directory.with_name("retained")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        self.assertEqual(inspect_analysis(self.folder, "complex"), ("Complete", ""))


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

    def test_worker_uses_frozen_inputs_and_reference_settings(self):
        original_directory = Path.cwd()
        with (self.folder / "coul.00/prod/dhdl.xvg").open("ab") as stream:
            stream.write(b"30 4")
        modules, calls = self.fake_workflow()
        with modules:
            runner.run_analysis(self.folder, "complex")
        self.assertEqual(Path.cwd(), original_directory)
        self.assertNotEqual(calls["cwd"], self.folder)
        self.assertEqual(len(calls["inputs"]), 42)
        self.assertEqual(calls["constructor"]["T"], 298)
        self.assertEqual(calls["read"], {"n_jobs": 1})
        self.assertEqual(
            calls["preprocess"],
            {"skiptime": 10, "uncorr": "dhdl", "threshold": 50, "n_jobs": 1},
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
        self.assertEqual(inspect_analysis(self.folder, "complex"), ("Complete", ""))
        copied = analysis_folder(self.folder, "complex") / "dhdl.coul.00.xvg"
        self.assertTrue(copied.read_bytes().endswith(b"20 3 4\n"))

    def test_worker_preserves_previous_outputs_on_failure(self):
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

    def test_data_added_during_analysis_is_left_incomplete(self):
        modules, _ = self.fake_workflow(advance=True)
        with modules:
            runner.run_analysis(self.folder, "complex")
        self.assertEqual(inspect_analysis(self.folder, "complex")[0], "Incomplete")

    def test_four_jobs_overlap_deduplicate_and_continue_after_failure(self):
        document = example_data()
        compound = document["datasets"]["example"]["complexes"]["ligand_01"]
        template = deepcopy(compound["repeats"][1])
        compound["repeats"] = {}
        for index in range(5):
            folder = self.root / f"run{index}/complex"
            make_inputs(folder, "complex")
            runs = deepcopy(template)
            runs["alchemical_complex"]["folder"] = str(folder)
            compound["repeats"][index + 1] = runs
        entries = runner.select_entries(document, None, self.root)[:-1]
        entries.append(entries[0])
        with (
            patch.object(runner, "run_analysis", parallel_probe),
            redirect_stdout(io.StringIO()),
        ):
            failures = runner.run_jobs(entries)
        self.assertEqual(len(failures), 1)
        self.assertIn("one component failed", next(iter(failures.values())))
        records = [
            json.loads(path.read_text())
            for path in self.root.glob("run*/complex/finished.json")
        ]
        self.assertEqual(len(records), 5)
        self.assertEqual(len({record["pid"] for record in records}), 4)
        events = sorted(
            [(r["start"], 1) for r in records] + [(r["end"], -1) for r in records]
        )
        active = peak = 0
        for _, change in events:
            active += change
            peak = max(peak, active)
        self.assertEqual(peak, 4)

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

    def command(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = runner.main(args, self.data_path)
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
        self.save()
        before = self.data_path.read_bytes()
        result, output, _ = self.command()
        self.assertEqual(result, 0)
        self.assertIn("Blocked: Source folder is not set", output)
        self.assertEqual(self.data_path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.data_path])
        self.assertEqual(self.command("--update-status")[0], 0)
        updated = load_data(self.data_path)
        ligand = updated["datasets"]["example"]["complexes"]["ligand_01"][
            "alchemical_ligand"
        ]
        self.assertEqual(ligand["analysis_status"], "Missing")
        self.assertEqual(ligand["remark"], "Manual note")
        for field in (
            "status",
            "convergence",
            "total_production_ns",
            "completed_production_ns",
        ):
            self.assertEqual(ligand[field], self.compound["alchemical_ligand"][field])
        backups = list((self.root / "archives").iterdir())
        self.assertEqual(backups[0].read_bytes(), before)
        self.command("--update-status")
        self.assertEqual(list((self.root / "archives").iterdir()), backups)

    def test_dataset_filter_and_invalid_arguments(self):
        self.document["datasets"]["other"] = deepcopy(
            self.document["datasets"]["example"]
        )
        self.save()
        self.command("--dataset", "example", "--update-status")
        other = load_data(self.data_path)["datasets"]["other"]
        self.assertNotIn(
            "analysis_status", other["complexes"]["ligand_01"]["alchemical_ligand"]
        )
        for arguments in (("--dataset", "missing"), ("--run", "--update-status")):
            with (
                self.subTest(arguments=arguments),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                runner.main(arguments, self.data_path)

    def test_compilation_only_and_updates_when_shared_summary_changes(self):
        self.prepare_components()
        before = self.data_path.read_bytes()
        self.assertIn("Would compile", self.command()[1])
        self.assertEqual(self.data_path.read_bytes(), before)
        self.assertEqual(list(self.root.glob("repeat*/analysis/abfe_results.csv")), [])
        with patch.object(
            runner,
            "ProcessPoolExecutor",
            side_effect=AssertionError("no analysis needed"),
        ):
            result, output, _ = self.command("--run")
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
        self.assertEqual(self.command("--run")[0], 0)
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
        self.assertEqual(self.command("--run")[0], 0)
        self.assertNotEqual(final.stat().st_mtime_ns, before)

    def test_failures_are_reported_and_statuses_still_saved(self):
        self.save()
        result, _, errors = self.command("--run")
        self.assertEqual(result, 1)
        self.assertIn("Analysis failed", errors)
        self.assertIn("analysis_status", self.data_path.read_text())

    def test_edits_during_analysis_are_not_overwritten(self):
        self.save()

        def edit_yaml(entries):
            self.compound["alchemical_ligand"]["remark"] = "Edited during calculation"
            self.save()
            return {}

        with patch.object(runner, "run_jobs", side_effect=edit_yaml):
            with self.assertRaises(SystemExit) as raised:
                self.command("--run")
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Edited during calculation", self.data_path.read_text())
        self.assertFalse((self.root / "archives").exists())

    def test_compilation_failure_preserves_component_statuses_and_old_result(self):
        self.prepare_components()
        folder = Path(self.compound["repeats"][1]["alchemical_complex"]["folder"])
        final = folder.parent / "analysis/abfe_results.csv"
        final.write_text("previous result")
        (folder.parent / "boresch/resgen_output.txt").unlink()
        result, _, errors = self.command("--run")
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
