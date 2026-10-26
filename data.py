"""Load and validate the manually maintained console data."""

import math
from pathlib import Path

import yaml

from utils.production_status import validate_production_files

STATUSES = ("Attention required", "Complete", "Waiting")
CONVERGENCE_VALUES = ("unknown", "not_converged", "converged")
RUN_TYPES = (
    ("vanilla", "Vanilla"),
    ("alchemical_complex", "Alchemical complex"),
    ("alchemical_ligand", "Alchemical ligand"),
)


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate keys instead of silently discarding earlier values."""

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        "while reading a mapping",
                        node.start_mark,
                        f"duplicate key {key!r}",
                        key_node.start_mark,
                    )
                seen.add(key)
            except TypeError:
                raise yaml.constructor.ConstructorError(
                    "while reading a mapping",
                    node.start_mark,
                    "mapping keys must be simple values",
                    key_node.start_mark,
                ) from None
        return super().construct_mapping(node, deep=deep)


def check_fields(value, required, path, errors, optional=()):
    if not isinstance(value, dict):
        errors.append(f"{path}: expected a mapping")
        return False
    for field in required:
        if field not in value:
            errors.append(f"{path}.{field}: required field is missing")
    for field in value:
        if field not in required and field not in optional:
            errors.append(f"{path}.{field}: unrecognised field")
    return True


def check_collection(value, path, errors):
    if not isinstance(value, dict):
        errors.append(f"{path}: expected a mapping")
        return False
    return True


def check_identifier(value, path, errors):
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}: identifier must be a nonempty string")


def validate_run(run, kind, path, errors):
    fields = ["folder", "status", "remark"]
    if kind != "vanilla":
        fields += ["convergence", "total_production_ns", "completed_production_ns"]
    optional = ("production_files",) if kind != "vanilla" else ()
    if not check_fields(run, fields, path, errors, optional=optional):
        return

    folder = run.get("folder")
    if folder is not None and (not isinstance(folder, str) or not folder.strip()):
        errors.append(f"{path}.folder: expected a nonempty string or null")
    if "status" in run and run["status"] not in STATUSES:
        errors.append(f"{path}.status: expected one of {', '.join(STATUSES)}")
    if "remark" in run and not isinstance(run["remark"], str):
        errors.append(f"{path}.remark: expected a string")
    if kind == "vanilla":
        return
    if "production_files" in run:
        errors.extend(
            f"{path}.{error}"
            for error in validate_production_files(run["production_files"])
        )
    if "convergence" in run and run["convergence"] not in CONVERGENCE_VALUES:
        errors.append(
            f"{path}.convergence: expected one of {', '.join(CONVERGENCE_VALUES)}"
        )
    for field in ("total_production_ns", "completed_production_ns"):
        value = run.get(field)
        if value is None:
            continue
        if (
            type(value) not in (int, float)
            or value < 0
            or (type(value) is float and not math.isfinite(value))
        ):
            errors.append(
                f"{path}.{field}: expected a finite nonnegative number or null"
            )


def validate_data(document):
    """Return field errors together so one edit can fix several problems."""
    errors = []
    if not check_fields(document, ("datasets",), "data", errors):
        return errors
    datasets = document.get("datasets")
    if not check_collection(datasets, "datasets", errors):
        return errors
    for dataset_id, dataset in datasets.items():
        path = f"datasets.{dataset_id}"
        check_identifier(dataset_id, path, errors)
        if not check_fields(dataset, ("label", "complexes"), path, errors):
            continue
        label = dataset.get("label")
        if "label" in dataset and (not isinstance(label, str) or not label.strip()):
            errors.append(f"{path}.label: expected a nonempty string")
        complexes = dataset.get("complexes")
        if not check_collection(complexes, f"{path}.complexes", errors):
            continue
        for complex_id, compound in complexes.items():
            compound_path = f"{path}.complexes.{complex_id}"
            check_identifier(complex_id, compound_path, errors)
            if not check_fields(
                compound, ("repeats", "alchemical_ligand"), compound_path, errors
            ):
                continue
            repeats = compound.get("repeats")
            if check_collection(repeats, f"{compound_path}.repeats", errors):
                for repeat, runs in repeats.items():
                    repeat_path = f"{compound_path}.repeats.{repeat}"
                    if type(repeat) is not int or repeat < 1:
                        errors.append(
                            f"{repeat_path}: repeat must be a positive integer"
                        )
                    if check_fields(
                        runs, ("vanilla", "alchemical_complex"), repeat_path, errors
                    ):
                        for kind in ("vanilla", "alchemical_complex"):
                            if kind in runs:
                                validate_run(
                                    runs[kind], kind, f"{repeat_path}.{kind}", errors
                                )
            if "alchemical_ligand" in compound:
                validate_run(
                    compound["alchemical_ligand"],
                    "alchemical_ligand",
                    f"{compound_path}.alchemical_ligand",
                    errors,
                )
    return errors


def load_data(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            document = yaml.load(stream, Loader=UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise ValueError(str(error)) from None
    errors = validate_data(document)
    if errors:
        raise ValueError("\n".join(errors))
    return document
