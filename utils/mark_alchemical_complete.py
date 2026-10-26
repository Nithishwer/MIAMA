"""Mark complete or missing alchemical runs, backing up any YAML changes."""

import argparse
import re
import sys
from pathlib import Path

import yaml

if __package__:
    from .production_status import check_production, validate_production_files
    from .update_yaml import update_yaml
else:
    from production_status import check_production, validate_production_files
    from update_yaml import update_yaml

DATA_PATH = Path(__file__).resolve().parents[1] / "data.yaml"
DEFAULT_WINDOW = "coul.00"
DEFAULT_PRODUCTION_FILES = {
    "trajectory": "traj_comp.xtc",
    "log": "md.log",
    "mdp": "{stage}.prod.{number}.mdp",
}
# Distinguish omitted configuration from an invalid YAML null.
UNSET_PRODUCTION_FILES = object()


def parse_window(value):
    if not re.fullmatch(r"(?:rest|coul|vdw)\.[0-9]{2}", value):
        raise argparse.ArgumentTypeError("expected rest.NN, coul.NN or vdw.NN")
    return value


def check_alchemical(
    folder, window=DEFAULT_WINDOW, production_files=UNSET_PRODUCTION_FILES
):
    if production_files is UNSET_PRODUCTION_FILES:
        production_files = DEFAULT_PRODUCTION_FILES
    errors = validate_production_files(production_files)
    if errors:
        return "incomplete", "; ".join(errors)
    if not folder:
        return "missing", ""
    stage, number = window.split(".")
    filenames = {
        field: template.format(stage=stage, number=number)
        for field, template in production_files.items()
    }
    return check_production(Path(folder) / window / "prod", **filenames)


def main(data_path=DATA_PATH, window=DEFAULT_WINDOW):
    try:
        with Path(data_path).open(encoding="utf-8") as stream:
            document = yaml.safe_load(stream)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        print(f"Cannot load {data_path}: {error}", file=sys.stderr)
        return 1

    for dataset_id, dataset in document["datasets"].items():
        for complex_id, compound in dataset["complexes"].items():
            entries = [
                (repeat, "alchemical_complex", runs["alchemical_complex"])
                for repeat, runs in sorted(compound["repeats"].items())
            ]
            entries.append(
                ("Shared", "alchemical_ligand", compound["alchemical_ligand"])
            )
            for repeat, kind, run in entries:
                folder = run["folder"]
                status, reason = check_alchemical(
                    folder, window, run.get("production_files", UNSET_PRODUCTION_FILES)
                )
                if status == "complete":
                    run["status"] = "Complete"
                elif status == "missing":
                    run["status"] = "Attention required"
                result = f"{status} ({reason})" if reason else status
                print(
                    f"{dataset_id}\t{complex_id}\t{repeat}\t{kind}\t"
                    f"{folder or '-'}\t{window}\t{result}"
                )
    try:
        backup = update_yaml(data_path, document)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        print(f"Cannot update {data_path}: {error}", file=sys.stderr)
        return 1
    if backup is not None:
        print(f"Updated {data_path}; backup: {backup}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window",
        type=parse_window,
        default=DEFAULT_WINDOW,
        help="window to check for every run (default: %(default)s)",
    )
    args = parser.parse_args()
    raise SystemExit(main(window=args.window))
