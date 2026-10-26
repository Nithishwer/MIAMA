"""Check data mistakes and rendering behaviours that affect manual tracking."""

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import yaml

from build import ROOT, build_site, prepare_views, render_page
from data import load_data, validate_data


def example_data():
    alchemical = dict(
        folder=None,
        status="Waiting",
        remark="",
        convergence="unknown",
        total_production_ns=None,
        completed_production_ns=None,
    )
    return {
        "datasets": {
            "example": {
                "label": "Example dataset",
                "complexes": {
                    "ligand_01": {
                        "repeats": {
                            repeat: {
                                "vanilla": {
                                    "folder": None,
                                    "status": "Complete",
                                    "remark": "",
                                },
                                "alchemical_complex": deepcopy(alchemical),
                            }
                            for repeat in (2, 1)
                        },
                        "alchemical_ligand": deepcopy(alchemical),
                    }
                },
            }
        }
    }


def complex_record(document):
    return document["datasets"]["example"]["complexes"]["ligand_01"]


class ValidationTests(unittest.TestCase):
    def test_alchemical_production_files_are_optional_and_preserved(self):
        document = example_data()
        original_views = prepare_views(document)
        compound = complex_record(document)
        for run in (
            compound["repeats"][1]["alchemical_complex"],
            compound["alchemical_ligand"],
        ):
            run["production_files"] = {
                "trajectory": "{stage}.prod.{number}.xtc",
                "log": "{stage}.prod.{number}.log",
                "mdp": "custom.mdp",
            }
        self.assertEqual(validate_data(document), [])
        self.assertEqual(prepare_views(document), original_views)
        self.assertEqual(render_page(document).count('class="run-row"'), 5)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "data.yaml"
            source.write_text(yaml.safe_dump(document))
            self.assertEqual(load_data(source), document)

    def test_invalid_production_files_have_field_specific_errors(self):
        files = {
            "trajectory": "traj_comp.xtc",
            "log": "md.log",
            "mdp": "{stage}.prod.{number}.mdp",
        }
        for value in (None, [], "md.log"):
            document = example_data()
            complex_record(document)["alchemical_ligand"]["production_files"] = value
            self.assertEqual(
                validate_data(document),
                [
                    "datasets.example.complexes.ligand_01.alchemical_ligand.production_files: expected a mapping"
                ],
            )
        for field in files:
            for value in (
                None,
                42,
                True,
                "",
                " ",
                ".",
                "..",
                "/tmp/file",
                "dir/file",
                "dir\\file",
                "file\0",
                "{unknown}.log",
                "{stage.log}",
                "{stage!r}.log",
                "{number:02}.log",
                "{stage",
                "stage}",
            ):
                with self.subTest(field=field, value=value):
                    document = example_data()
                    complex_record(document)["alchemical_ligand"][
                        "production_files"
                    ] = {**files, field: value}
                    errors = validate_data(document)
                    self.assertTrue(
                        any(f".production_files.{field}:" in error for error in errors),
                        errors,
                    )
            document = example_data()
            incomplete_files = files.copy()
            del incomplete_files[field]
            complex_record(document)["alchemical_ligand"]["production_files"] = (
                incomplete_files
            )
            self.assertTrue(
                any(
                    f".production_files.{field}: required" in error
                    for error in validate_data(document)
                )
            )
        document = example_data()
        complex_record(document)["alchemical_ligand"]["production_files"] = {
            **files,
            "extra": "extra.log",
        }
        self.assertTrue(
            any(
                ".production_files.extra: unrecognised" in error
                for error in validate_data(document)
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "data.yaml"
            source.write_text(yaml.safe_dump(document))
            with self.assertRaisesRegex(
                ValueError, r"alchemical_ligand.production_files.extra"
            ):
                load_data(source)

    def test_vanilla_rejects_production_files(self):
        document = example_data()
        complex_record(document)["repeats"][1]["vanilla"]["production_files"] = {
            "trajectory": "prod.xtc",
            "log": "prod.log",
            "mdp": "prod.mdp",
        }
        self.assertTrue(
            any(
                ".vanilla.production_files: unrecognised" in error
                for error in validate_data(document)
            )
        )

    def test_valid_unknowns_and_extensions(self):
        document = example_data()
        run = complex_record(document)["alchemical_ligand"]
        run.update(total_production_ns=10, completed_production_ns=20.5)
        self.assertEqual(validate_data(document), [])

    def test_reports_multiple_fields_with_paths(self):
        document = example_data()
        run = complex_record(document)["alchemical_ligand"]
        run.update(status="Running", convergence="yes", folder="", statsu="Waiting")
        del run["total_production_ns"]
        errors = validate_data(document)
        for field in (
            "status",
            "convergence",
            "folder",
            "statsu",
            "total_production_ns",
        ):
            self.assertTrue(
                any(
                    f"datasets.example.complexes.ligand_01.alchemical_ligand.{field}:"
                    in error
                    for error in errors
                ),
                errors,
            )

    def test_invalid_numeric_values(self):
        for value in (-1, True, False, "10", float("inf"), float("nan"), []):
            with self.subTest(value=value):
                document = example_data()
                complex_record(document)["alchemical_ligand"][
                    "completed_production_ns"
                ] = value
                self.assertTrue(validate_data(document))

    def test_invalid_repeat_keys(self):
        for repeat in (0, -1, True, "1", 1.5):
            with self.subTest(repeat=repeat):
                document = example_data()
                compound = complex_record(document)
                compound["repeats"] = {repeat: compound["repeats"][1]}
                self.assertTrue(
                    any(
                        "positive integer" in error for error in validate_data(document)
                    )
                )

    def test_invalid_hierarchy_and_identifiers(self):
        for document in (
            None,
            [],
            {"datasets": []},
            {"datasets": {"example": {"label": "Example", "complexes": []}}},
            {"datasets": {12: {"label": "Example", "complexes": {}}}},
            {"datasets": {"example": {"label": "", "complexes": {}}}},
        ):
            with self.subTest(document=document):
                self.assertTrue(validate_data(document))

    def test_vanilla_rejects_alchemical_fields(self):
        document = example_data()
        complex_record(document)["repeats"][1]["vanilla"]["convergence"] = "unknown"
        self.assertTrue(
            any("unrecognised field" in error for error in validate_data(document))
        )

    def test_remark_requires_text_for_every_run_type(self):
        for kind in ("vanilla", "alchemical_complex", "alchemical_ligand"):
            document = example_data()
            compound = complex_record(document)
            run = (
                compound[kind]
                if kind == "alchemical_ligand"
                else compound["repeats"][1][kind]
            )
            for value in ("", "Check this run\nBefore extending", None, 12, []):
                with self.subTest(kind=kind, value=value):
                    run["remark"] = value
                    errors = validate_data(document)
                    if isinstance(value, str):
                        self.assertEqual(errors, [])
                    else:
                        self.assertTrue(any(".remark:" in error for error in errors))
            del run["remark"]
            self.assertTrue(
                any(".remark:" in error for error in validate_data(document))
            )

    def test_empty_collections(self):
        for document in (
            {"datasets": {}},
            {"datasets": {"example": {"label": "Example", "complexes": {}}}},
        ):
            self.assertEqual(validate_data(document), [])
            self.assertIn("<!doctype html>", render_page(document))
        document = example_data()
        complex_record(document)["repeats"] = {}
        self.assertEqual(validate_data(document), [])
        self.assertEqual(len(prepare_views(document)[1]), 1)
        self.assertIn("No repeats", render_page(document))

    def test_duplicate_keys_and_syntax_include_line(self):
        for content, expected in (
            ("datasets: {}\ndatasets: {}\n", "duplicate key"),
            (
                "datasets:\n  example:\n    label: First\n    label: Second\n",
                "duplicate key",
            ),
            ("datasets: [\n", "while parsing"),
            ("datasets:\n  ? [complex, key]\n  : {}\n", "mapping keys must be simple"),
        ):
            with (
                self.subTest(content=content),
                tempfile.TemporaryDirectory() as directory,
            ):
                source = Path(directory) / "data.yaml"
                source.write_text(content)
                with self.assertRaises(ValueError) as raised:
                    load_data(source)
                self.assertIn(expected, str(raised.exception))
                self.assertIn("line", str(raised.exception))

    def test_unsafe_yaml_tags_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "data.yaml"
            source.write_text("!!python/object:builtins.dict {}")
            with self.assertRaises(ValueError):
                load_data(source)


class RenderingTests(unittest.TestCase):
    def test_shared_record_count_and_numeric_repeat_order(self):
        datasets, runs = prepare_views(example_data())
        self.assertEqual(len(runs), 5)
        self.assertEqual(datasets[0]["run_count"], 5)
        self.assertEqual(datasets[0]["counts"]["Waiting"], 3)
        self.assertEqual(sum(run["repeat"] is None for run in runs), 1)
        self.assertEqual(
            [run["repeat"] for run in runs if run["kind"] == "vanilla"], [1, 2]
        )

    def test_html_escapes_values_and_embeds_assets(self):
        document = example_data()
        document["datasets"]["example"]["label"] = '<script>alert("label")</script>'
        compound = complex_record(document)
        compound["alchemical_ligand"]["folder"] = (
            '/remote/<script>alert("folder")</script>'
        )
        document["datasets"]["example"]["complexes"] = {'ligand_"<tag>': compound}
        page = render_page(document)
        self.assertNotIn('<script>alert("label")</script>', page)
        self.assertNotIn('<script>alert("folder")</script>', page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("ligand_&#34;&lt;tag&gt;", page)
        self.assertIn("data:font/ttf;base64,", page)
        self.assertNotIn("<script src=", page)
        self.assertNotIn("<link ", page)
        self.assertIn("Website generated:", page)

    def test_remarks_render_for_all_run_types_and_escape_html(self):
        document = example_data()
        compound = complex_record(document)
        compound["repeats"][1]["vanilla"]["remark"] = "Vanilla note"
        compound["repeats"][1]["alchemical_complex"]["remark"] = "Complex note"
        compound["alchemical_ligand"]["remark"] = '<script>alert("remark")</script>'
        _, runs = prepare_views(document)
        self.assertEqual(
            {run["remark"] for run in runs},
            {"", "Vanilla note", "Complex note", '<script>alert("remark")</script>'},
        )
        page = render_page(document)
        self.assertIn('data-column="9" data-numeric="false">Remark', page)
        self.assertEqual(page.count('<td class="remark">'), 5)
        self.assertIn('<td class="remark">Vanilla note</td>', page)
        self.assertIn('<td class="remark">Complex note</td>', page)
        self.assertIn("&lt;script&gt;alert(&#34;remark&#34;)&lt;/script&gt;", page)
        self.assertNotIn('<script>alert("remark")</script>', page)
        self.assertIn('colspan="10"', page)

    def test_failed_validation_preserves_existing_page(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "data.yaml"
            output = Path(directory) / "index.html"
            source.write_text("datasets: []")
            output.write_text("previous page")
            with self.assertRaises(ValueError):
                build_site(source, output)
            self.assertEqual(output.read_text(), "previous page")

    def test_failed_render_preserves_existing_page(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "data.yaml"
            output = Path(directory) / "index.html"
            source.write_text(yaml.safe_dump(example_data()))
            output.write_text("previous page")
            with patch("build.render_page", side_effect=ValueError("render failed")):
                with self.assertRaises(ValueError):
                    build_site(source, output)
            self.assertEqual(output.read_text(), "previous page")

    def test_successful_build_replaces_output_without_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "data.yaml"
            output = Path(directory) / "site/index.html"
            source.write_text(yaml.safe_dump(example_data()))
            self.assertEqual(build_site(source, output), output)
            self.assertEqual(output.read_text().count('class="run-row"'), 5)
            self.assertEqual(list(output.parent.iterdir()), [output])

    def test_seeded_deflorian_inventory(self):
        document = load_data(ROOT / "data.yaml")
        self.assertEqual(list(document["datasets"]), ["deflorian-set1"])
        datasets, runs = prepare_views(document)
        self.assertEqual(len(datasets[0]["compounds"]), 27)
        self.assertEqual(len(runs), 189)
        for kind, expected in (
            ("vanilla", 81),
            ("alchemical_complex", 81),
            ("alchemical_ligand", 27),
        ):
            self.assertEqual(sum(run["kind"] == kind for run in runs), expected)


if __name__ == "__main__":
    unittest.main()
