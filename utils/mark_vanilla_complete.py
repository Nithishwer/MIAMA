"""Update vanilla completion status and production times, backing up YAML changes."""

import sys
from pathlib import Path

import yaml

if __package__:
    from .production_status import (
        check_production,
        read_last_step,
        read_nsteps,
        read_production_times,
    )
    from .update_yaml import update_yaml
else:
    from production_status import (
        check_production,
        read_last_step,
        read_nsteps,
        read_production_times,
    )
    from update_yaml import update_yaml

DATA_PATH = Path(__file__).resolve().parents[1] / "data.yaml"

__all__ = ["check_vanilla", "main", "read_last_step", "read_nsteps"]


def check_vanilla(folder):
    return check_production(folder)


def main(data_path=DATA_PATH):
    try:
        with Path(data_path).open(encoding="utf-8") as stream:
            document = yaml.safe_load(stream)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        print(f"Cannot load {data_path}: {error}", file=sys.stderr)
        return 1

    for dataset_id, dataset in document["datasets"].items():
        for complex_id, compound in dataset["complexes"].items():
            for repeat, runs in sorted(compound["repeats"].items()):
                run = runs["vanilla"]
                folder = run["folder"]
                status, reason = check_vanilla(folder)
                if status == "complete":
                    run["status"] = "Complete"
                if folder:
                    try:
                        production = Path(folder)
                        run.update(
                            read_production_times(
                                production / "prod.mdp", production / "prod.log"
                            )
                        )
                    except (OSError, UnicodeError, ValueError) as error:
                        print(
                            f"{dataset_id}\t{complex_id}\t{repeat}: "
                            f"Cannot read production times: {error}",
                            file=sys.stderr,
                        )
                result = f"{status} ({reason})" if reason else status
                print(
                    f"{dataset_id}\t{complex_id}\t{repeat}\t{folder or '-'}\t{result}"
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
    raise SystemExit(main())
