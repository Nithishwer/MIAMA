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
                                "vanilla": {"folder": None, "status": "Complete"},
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
