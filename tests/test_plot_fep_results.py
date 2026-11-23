"""Check result selection, partial repeats and experimental comparison plots."""

import csv
import io
import math
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import yaml
from test_analysis import write_outputs
from test_boresch import TOPOLOGY
from test_console import complex_record, example_data

from utils import plot_fep_results as plotter


class ResultTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "abfe_results.csv"

    def test_reads_only_the_total_for_the_selected_estimator(self):
        self.path.write_text(
            "state,MBAR,MBAR_Error,BAR,BAR_Error,TI,TI_Error\n"
            "ligand_coul,100,99,200,99,300,99\n"
            "total,-8,0.1,-9,0.2,-10,0.3\n"
        )
        for method, expected in (("MBAR", -8), ("BAR", -9), ("TI", -10)):
            with self.subTest(method=method):
                self.assertEqual(plotter.read_total(self.path, method), expected)

    def test_rejects_missing_duplicate_or_invalid_totals(self):
        for contents in (
            "state,BAR\ntotal,-8\n",
            "state,MBAR\nligand_coul,-8\n",
            "state,MBAR\ntotal,-8\ntotal,-9\n",
            "state,MBAR\ntotal\n",
            "state,MBAR\ntotal,text\n",
            "state,MBAR\ntotal,nan\n",
            "state,MBAR\ntotal,inf\n",
            'state,MBAR\ntotal,"unterminated\n',
        ):
            with self.subTest(contents=contents):
                self.path.write_text(contents)
                with self.assertRaises((ValueError, plotter.csv.Error)):
                    plotter.read_total(self.path, "MBAR")


class InventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.document = example_data()
        self.compound = complex_record(self.document)
        self.compound["experimental_dg"] = -8.0
        for repeat, runs in self.compound["repeats"].items():
            # Cover both inventory-relative and absolute simulation paths.
            folder = Path(f"repeat{repeat}/complex")
            runs["alchemical_complex"]["folder"] = str(
                self.root / folder if repeat == 2 else folder
            )
        self.output = io.StringIO()
        self.errors = io.StringIO()

    def write_result(self, repeat, contents):
        folder = self.compound["repeats"][repeat]["alchemical_complex"]["folder"]
        path = (self.root / folder).parent / "analysis/abfe_results.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        return path

    def write_components(self, repeat, correction=-4.0):
        folder = (
            self.root / self.compound["repeats"][repeat]["alchemical_complex"]["folder"]
        )
        write_outputs(folder.parent / "analysis/complex", "complex")
        self.compound["alchemical_ligand"]["folder"] = "shared/ligand"
        write_outputs(self.root / "shared/analysis/ligand", "ligand")
        restraint = folder.parent / "boresch/resgen_output.txt"
        restraint.parent.mkdir(parents=True, exist_ok=True)
        restraint.write_text(f"dG_off: 999, dG_on: {correction}\n")
        return restraint

    def collect(self, method="MBAR", collect=False):
        with redirect_stdout(self.output), redirect_stderr(self.errors):
            return plotter.collect_results(
                "example",
                self.document["datasets"]["example"],
                self.root,
                method,
                collect=collect,
            )

    def test_averages_repeats_and_uses_sample_sd(self):
        self.write_result(1, "state,MBAR\ntotal,-8\n")
        self.write_result(2, "state,MBAR\ntotal,-10\n")
        with patch.object(plotter, "compile_results") as compile_results:
            points, failed = self.collect()
        compile_results.assert_not_called()
        self.assertFalse(failed)
        self.assertEqual(points[0][:2], (-8, -9))
        self.assertAlmostEqual(points[0][2], math.sqrt(2))
        self.assertEqual(self.output.getvalue().count("\tFound"), 2)

    def test_missing_repeat_leaves_one_point_without_sd(self):
        path = self.write_result(1, "state,MBAR\ntotal,-9\n")
        points, failed = self.collect()
        self.assertFalse(failed)
        self.assertEqual(points, [(-8, -9, None)])
        self.assertIn(str(path), self.output.getvalue())
        self.assertIn("example\tligand_01\t2\t", self.output.getvalue())
        self.assertIn("\tMissing", self.output.getvalue())
        self.assertEqual(self.errors.getvalue(), "")

    def test_unset_folder_is_reported_as_missing(self):
        self.compound["repeats"][1]["alchemical_complex"]["folder"] = None
        points, failed = self.collect()
        self.assertEqual(points, [])
        self.assertFalse(failed)
        self.assertIn("example\tligand_01\t1\t-\tMissing", self.output.getvalue())
        self.assertEqual(self.errors.getvalue(), "")

    def test_invalid_repeat_is_reported_and_other_values_are_kept(self):
        path = self.write_result(1, "state,MBAR\ntotal,nan\n")
        self.write_result(2, "state,MBAR\ntotal,-9\n")
        points, failed = self.collect()
        self.assertTrue(failed)
        self.assertEqual(points, [(-8, -9, None)])
        self.assertIn(str(path), self.errors.getvalue())
        self.assertIn("Invalid:", self.errors.getvalue())

    def test_missing_experiment_still_reports_result_availability(self):
        del self.compound["experimental_dg"]
        self.write_result(1, "state,MBAR\ntotal,-9\n")
        self.write_result(2, "state,MBAR\ntotal,-10\n")
        points, failed = self.collect()
        self.assertFalse(failed)
        self.assertEqual(points, [])
        self.assertIn("\tFound", self.output.getvalue())
        self.assertIn("experimental_dg is not set", self.output.getvalue())

    def test_cli_filters_dataset_and_leaves_inputs_unchanged(self):
        del self.compound["repeats"][2]
        result = self.write_result(1, "state,MBAR,BAR,TI\ntotal,-8,-9,-10\n")
        self.document["datasets"]["other"] = deepcopy(
            self.document["datasets"]["example"]
        )
        data_path = self.root / "data.yaml"
        data_path.write_text(yaml.safe_dump(self.document))
        originals = {path: path.read_bytes() for path in (data_path, result)}
        with (
            redirect_stdout(self.output),
            patch.object(plotter, "plot_results") as plot,
        ):
            status = plotter.main(
                [
                    "--dataset",
                    "example",
                    "--method",
                    "TI",
                    "--output-dir",
                    str(self.root),
                ],
                data_path,
            )
        self.assertEqual(status, 0)
        plot.assert_called_once_with(
            [(-8, -10, None)], "Example dataset", "TI", self.root / "example_TI.png"
        )
        self.assertNotIn("other\t", self.output.getvalue())
        for path, contents in originals.items():
            self.assertEqual(path.read_bytes(), contents)

    def test_cli_keeps_datasets_separate_and_returns_failure_after_invalid_input(self):
        self.write_result(1, "state,MBAR\ntotal,-9\n")
        self.write_result(2, "state,MBAR\ntotal,nan\n")
        self.document["datasets"]["other"] = deepcopy(
            self.document["datasets"]["example"]
        )
        data_path = self.root / "data.yaml"
        data_path.write_text(yaml.safe_dump(self.document))
        with (
            redirect_stdout(self.output),
            redirect_stderr(self.errors),
            patch.object(plotter, "plot_results") as plot,
        ):
            status = plotter.main([], data_path)
        self.assertEqual(status, 1)
        self.assertEqual(plot.call_count, 2)
        self.assertEqual(plot.call_args_list[0].args[0], [(-8, -9, None)])
        self.assertEqual(plot.call_args_list[1].args[0], [(-8, -9, None)])
        self.assertNotEqual(
            plot.call_args_list[0].args[-1], plot.call_args_list[1].args[-1]
        )

    def test_cli_skips_empty_datasets_without_creating_output(self):
        data_path = self.root / "data.yaml"
        data_path.write_text(yaml.safe_dump(self.document))
        output_dir = self.root / "plots"
        with redirect_stdout(self.output), redirect_stderr(self.errors):
            status = plotter.main(["--output-dir", str(output_dir)], data_path)
        self.assertEqual(status, 0)
        self.assertFalse(output_dir.exists())
        self.assertIn("no plottable compounds", self.output.getvalue())

    def test_missing_results_are_compiled_using_the_shared_ligand(self):
        results = [
            self.write_components(repeat).parent.parent / "analysis/abfe_results.csv"
            for repeat in (1, 2)
        ]
        for ligand_folder in ("shared/ligand", str(self.root / "shared/ligand")):
            with self.subTest(ligand_folder=ligand_folder):
                self.compound["alchemical_ligand"]["folder"] = ligand_folder
                for path in results:
                    path.unlink(missing_ok=True)
                points, failed = self.collect(collect=True)
                self.assertFalse(failed)
                # Ligand (1 + 2) + correction (-4) - complex (1 + 2 + 3).
                self.assertEqual(points, [(-8, -7, 0)])
                for path in results:
                    self.assertEqual(plotter.read_total(path, "MBAR"), -7)
                    self.assertEqual(plotter.read_total(path, "TI"), -9)
        self.assertEqual(self.output.getvalue().count("\tMissing"), 4)
        self.assertEqual(self.output.getvalue().count("\tCompiled"), 4)

    def test_cli_collect_rebuilds_existing_and_malformed_results(self):
        self.write_components(1, correction=-4)
        self.write_components(2, correction=-6)
        data_path = self.root / "data.yaml"
        data_path.write_text(yaml.safe_dump(self.document))
        for contents in ("state,MBAR\ntotal,-99\n", "state,MBAR\ntotal,nan\n"):
            with self.subTest(contents=contents):
                paths = [self.write_result(repeat, contents) for repeat in (1, 2)]
                with (
                    redirect_stdout(self.output),
                    redirect_stderr(self.errors),
                    patch.object(plotter, "plot_results") as plot,
                ):
                    status = plotter.main(["--collect"], data_path)
                self.assertEqual(status, 0)
                self.assertEqual(plot.call_args.args[0][0][:2], (-8, -8))
                self.assertAlmostEqual(plot.call_args.args[0][0][2], math.sqrt(2))
                self.assertEqual(plotter.read_total(paths[0], "MBAR"), -7)
                self.assertEqual(plotter.read_total(paths[1], "MBAR"), -9)

    def test_failed_collection_preserves_csv_and_plots_other_repeats(self):
        for failure in ("missing summary", "invalid correction", "invalid topology"):
            with self.subTest(failure=failure):
                restraint = self.write_components(1)
                self.write_components(2)
                previous = self.write_result(1, "state,MBAR\ntotal,-99\n")
                original = previous.read_bytes()
                self.write_result(2, "state,MBAR\ntotal,-90\n")
                if failure == "missing summary":
                    (
                        restraint.parent.parent / "analysis/complex/ABFE_summary.csv"
                    ).unlink()
                elif failure == "invalid correction":
                    restraint.write_text("dG_off: 999, dG_on: invalid\n")
                else:
                    restraint.unlink()
                    restraint.with_name("BoreschRestraint.top").write_text(
                        "invalid topology"
                    )
                    mdp = (
                        restraint.parent.parent
                        / "complex/rest.00/prod/rest.prod.00.mdp"
                    )
                    mdp.parent.mkdir(parents=True)
                    mdp.write_text("ref-t = 298.15 298.15\n")
                data_path = self.root / "data.yaml"
                data_path.write_text(yaml.safe_dump(self.document))
                with (
                    redirect_stdout(self.output),
                    redirect_stderr(self.errors),
                    patch.object(plotter, "plot_results") as plot,
                ):
                    status = plotter.main(["--collect"], data_path)
                self.assertEqual(status, 1)
                self.assertEqual(previous.read_bytes(), original)
                self.assertEqual(plot.call_args.args[0], [(-8, -7, None)])
        self.assertIn("Compilation failed:", self.errors.getvalue())

    def test_compiles_without_experimental_values(self):
        del self.compound["experimental_dg"]
        results = [
            self.write_components(repeat).parent.parent / "analysis/abfe_results.csv"
            for repeat in (1, 2)
        ]
        points, failed = self.collect(collect=True)
        self.assertFalse(failed)
        self.assertEqual(points, [])
        self.assertTrue(all(path.is_file() for path in results))
        self.assertIn("experimental_dg is not set", self.output.getvalue())

    def test_unset_shared_ligand_prevents_compilation(self):
        self.write_components(1)
        self.compound["alchemical_ligand"]["folder"] = None
        points, failed = self.collect(collect=True)
        self.assertTrue(failed)
        self.assertEqual(points, [])
        self.assertIn("shared ligand source folder is not set", self.errors.getvalue())

    def test_collect_estimates_missing_boresch_output_and_plots_new_total(self):
        del self.compound["repeats"][2]
        restraint = self.write_components(1)
        restraint.unlink()
        topology = restraint.with_name("BoreschRestraint.top")
        topology.write_text(TOPOLOGY)
        mdp = restraint.parent.parent / "complex/rest.00/prod/rest.prod.00.mdp"
        mdp.parent.mkdir(parents=True)
        mdp.write_text("ref-t = 298.15 298.15\n")
        data_path = self.root / "data.yaml"
        data_path.write_text(yaml.safe_dump(self.document))
        with (
            redirect_stdout(self.output),
            redirect_stderr(self.errors),
            patch.object(plotter, "plot_results") as plot,
        ):
            status = plotter.main(["--collect"], data_path)
        self.assertEqual(status, 0)
        correction = 9.23382693797829
        # MIAMA uses dG_on = -release, then adds ligand stages minus complex stages.
        self.assertAlmostEqual(plot.call_args.args[0][0][1], correction - 3)
        result = restraint.parent.parent / "analysis/abfe_results.csv"
        with result.open() as stream:
            rows = {row["state"]: row for row in csv.DictReader(stream)}
        for method in ("MBAR", "BAR", "TI"):
            self.assertAlmostEqual(float(rows["anal_rest"][method]), correction)
            self.assertEqual(float(rows["anal_rest"][f"{method}_Error"]), 0)
        self.assertFalse(restraint.exists())
        self.assertEqual(topology.read_text(), TOPOLOGY)
        self.assertIn("Analytical Boresch dG_on", self.output.getvalue())

    def test_default_does_not_compile_even_with_ready_component_inputs(self):
        self.write_components(1)
        restraint = self.write_components(2)
        missing = restraint.parent.parent / "analysis/abfe_results.csv"
        existing = self.write_result(1, "state,MBAR\ntotal,-99\n")
        original = existing.read_bytes()
        with patch.object(plotter, "compile_results") as compile_results:
            points, failed = self.collect()
        compile_results.assert_not_called()
        self.assertFalse(failed)
        self.assertEqual(points, [(-8, -99, None)])
        self.assertEqual(existing.read_bytes(), original)
        self.assertFalse(missing.exists())

    def test_collect_overwrites_even_identical_results(self):
        results = [
            self.write_components(repeat).parent.parent / "analysis/abfe_results.csv"
            for repeat in (1, 2)
        ]
        points, failed = self.collect(collect=True)
        self.assertFalse(failed)
        originals = {path: (path.read_bytes(), path.stat().st_ino) for path in results}
        updated_points, failed = self.collect(collect=True)
        self.assertFalse(failed)
        self.assertEqual(updated_points, points)
        for path, (contents, inode) in originals.items():
            self.assertEqual(path.read_bytes(), contents)
            self.assertNotEqual(path.stat().st_ino, inode)


class PlotTests(unittest.TestCase):
    def test_saves_plot_and_handles_undefined_correlations(self):
        import matplotlib

        matplotlib.use("Agg")
        from matplotlib import pyplot as plt

        scenarios = (
            ([(-8, -9, None)], "N/A"),
            ([(-8, -9, 0.5), (-8, -7, 0.5)], "N/A"),
            ([(-8, -9, 0.5), (-6, -9, 0.5)], "N/A"),
            ([(-8, -9, 0.5), (-6, -7, 0.5)], "1.00"),
        )
        with tempfile.TemporaryDirectory() as directory:
            for points, correlation in scenarios:
                with self.subTest(points=points):
                    output = Path(directory) / "plots/example_MBAR.png"
                    figure, axis = plt.subplots(figsize=(4, 4))
                    with (
                        patch.object(plt, "subplots", return_value=(figure, axis)),
                        patch.object(axis, "errorbar", wraps=axis.errorbar) as errorbar,
                    ):
                        plotter.plot_results(points, "Example", "MBAR", output)
                    self.assertTrue(
                        output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
                    )
                    self.assertEqual(axis.get_xlim(), axis.get_ylim())
                    self.assertIn(
                        f"Pearson R = {correlation}", axis.texts[0].get_text()
                    )
                    self.assertIn(
                        f"Spearman ρ = {correlation}", axis.texts[0].get_text()
                    )
                    for call, point in zip(errorbar.call_args_list, points):
                        self.assertEqual(call.kwargs["yerr"], point[2])
                        self.assertEqual(call.kwargs["xerr"], 1.0)


if __name__ == "__main__":
    unittest.main()
