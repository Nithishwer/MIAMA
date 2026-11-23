"""Create a simulation campaign folder and its YAML metadata."""

import argparse
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

if __package__:
    from .refresh_alchemical_status import (
        DEFAULT_PRODUCTION_FILES,
        DEFAULT_WINDOW,
        parse_window,
    )
    from .production_status import read_production_times, validate_production_files
else:
    from refresh_alchemical_status import (
        DEFAULT_PRODUCTION_FILES,
        DEFAULT_WINDOW,
        parse_window,
    )
    from production_status import read_production_times, validate_production_files

ROOT = Path(__file__).resolve().parents[1]
STAGE_NAMES = {
    "alchemical_complex": "alc-complex",
    "alchemical_ligand": "alc-ligand",
    "vanilla": "vanilla",
}
CLUSTER_PATHS = {
    "ranv": Path("/home/reub0138"),
    "soma": Path("/home/reub0138"),
    "archer": Path("/work/e280/e280-Biggin/shared/reub0138"),
}
STATUS_NAMES = {"waiting": "Waiting", "attention required": "Attention required"}


def read_run_progress(run, stage, window):
    source = Path(run["folder"])
    if stage == "vanilla":
        return read_production_times(source / "prod.mdp", source / "prod.log")
    files = run.get("production_files", DEFAULT_PRODUCTION_FILES)
    errors = validate_production_files(files)
    if errors:
        raise ValueError("; ".join(errors))
    window_stage, number = window.split(".")
    production = source / window / "prod"
    mdp = files["mdp"].format(stage=window_stage, number=number)
    log = files["log"].format(stage=window_stage, number=number)
    return read_production_times(production / mdp, production / log)


def select_runs(
    dataset, stage, status, window=DEFAULT_WINDOW, *, skip_progress_check=False
):
    """Select matching runs, optionally skipping production progress inspection."""
    selected = []
    errors = []
    for compound, entry in dataset["complexes"].items():
        if stage == "alchemical_ligand":
            runs = [(None, entry[stage])]
        else:
            runs = [
                (repeat, stages[stage])
                for repeat, stages in sorted(entry["repeats"].items())
            ]
        for repeat, run in runs:
            if run["status"] != status:
                continue
            label = "shared ligand" if repeat is None else f"repeat {repeat}"
            source = run["folder"]
            if not source or not source.strip():
                errors.append(f"{compound}, {label}: Unset source paths")
                continue
            if skip_progress_check:
                # File progress is unknown when MDP and log inspection is skipped.
                progress = {
                    "total_production_ns": None,
                    "completed_production_ns": None,
                }
            else:
                try:
                    progress = read_run_progress(run, stage, window)
                except (OSError, UnicodeError, ValueError) as error:
                    errors.append(f"{compound}, {label}: {error}")
                    continue
            selected.append(
                {"compound": compound, "repeat": repeat, "source": source, **progress}
            )
    if errors:
        raise ValueError("Cannot prepare campaign runs:\n" + "\n".join(errors))
    if not selected:
        raise ValueError(f"No {stage} runs match status {status!r} in this dataset")
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", "-d", required=True, help="dataset ID from data.yaml"
    )
    parser.add_argument("--stage", "-s", required=True, choices=STAGE_NAMES)
    parser.add_argument("--cluster", "-c", required=True, choices=CLUSTER_PATHS)
    parser.add_argument(
        "--status", required=True, type=str.casefold, choices=STATUS_NAMES
    )
    parser.add_argument(
        "--skip-progress-check",
        action="store_true",
        help=(
            "create the campaign without reading production MDP/log files; "
            "record production times as unknown; simulation files are unchanged"
        ),
    )
    parser.add_argument(
        "--window",
        type=parse_window,
        default=DEFAULT_WINDOW,
        help="alchemical window to read progress from (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        with (ROOT / "data.yaml").open(encoding="utf-8") as stream:
            datasets = yaml.safe_load(stream)["datasets"]
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        parser.exit(1, f"Cannot load data.yaml: {error}\n")
    if args.dataset not in datasets:
        parser.error(
            f"unknown dataset {args.dataset!r}; choose from: {', '.join(datasets)}"
        )

    status = STATUS_NAMES[args.status]
    try:
        runs = select_runs(
            datasets[args.dataset],
            args.stage,
            status,
            args.window,
            skip_progress_check=args.skip_progress_check,
        )
    except ValueError as error:
        parser.exit(1, f"{error}\n")

    created_at = datetime.now(ZoneInfo("Europe/London"))
    # Keep folder names short and safe while preserving the full ID in YAML.
    dataset_name = re.sub(r"[^a-z0-9]+", "-", args.dataset.lower()).strip("-")
    dataset_name = dataset_name[:32].rstrip("-") or "dataset"
    name = f"{dataset_name}_{STAGE_NAMES[args.stage]}_{created_at:%Y%m%d}"
    campaigns = ROOT / "campaigns"

    try:
        campaigns.mkdir(exist_ok=True)
        campaign = campaigns / name
        suffix = 2
        while True:
            try:
                campaign.mkdir()
                break
            except FileExistsError:
                campaign = campaigns / f"{name}_{suffix}"
                suffix += 1

        for run in runs:
            source = Path(run["source"])
            destination = CLUSTER_PATHS[args.cluster] / campaign.name / run["compound"]
            if args.stage != "vanilla":
                destination /= source.parent.name
            run["destination"] = str(destination / source.name)

        metadata = {
            "dataset": args.dataset,
            "stage": args.stage,
            "cluster": args.cluster,
            "created_at": created_at.isoformat(timespec="seconds"),
            "status": status,
            "runs": runs,
        }
        if args.skip_progress_check:
            metadata["skip_progress_check"] = True
        if args.stage != "vanilla":
            metadata["window"] = args.window
        with (campaign / "campaign.yaml").open("x", encoding="utf-8") as stream:
            yaml.safe_dump(metadata, stream, sort_keys=False)
    except OSError as error:
        parser.exit(1, f"Cannot create campaign: {error}\n")

    print(campaign)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
