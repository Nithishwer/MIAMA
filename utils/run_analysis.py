"""Run component analyses selected by dataset and saved analysis status."""

import argparse
import csv
import math
import os
import sys
import tempfile
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data import ANALYSIS_STATUSES, load_data
from utils.analysis_utils import (
    RESULT_COLUMNS,
    analysis_folder,
    common_time,
    dhdl_lines,
    entry_key,
    entry_label,
    expected_windows,
    find_inputs,
    read_last_time,
    refresh_entries,
    save_analysis_statuses,
    select_entries,
    validate_outputs,
)

ROOT = Path(__file__).resolve().parents[1]


def run_analysis(folder, component, threads=-1):
    """Analyse one component from frozen inputs and return the analysed time in ps."""
    # Import only when analysing, so previews and status refreshes stay lightweight.
    os.environ["JAX_PLATFORMS"] = "cpu"
    try:
        from alchemlyb.workflows import ABFE
        from matplotlib import pyplot as plt
    except ImportError as error:
        raise RuntimeError(
            "Install requirements-analysis.txt in the analysis environment"
        ) from error

    inputs = find_inputs(folder, component)
    target = analysis_folder(folder, component).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{component}-", dir=target.parent
    ) as temporary:
        work = Path(temporary) / "results"
        work.mkdir()
        times = {}
        for window, source in inputs.items():
            copied = work / f"dhdl.{window}.xvg"
            with copied.open("wb") as stream:
                stream.writelines(dhdl_lines(source))
            times[window] = read_last_time(copied)
        analysed_time = common_time(times)

        previous_directory = Path.cwd()
        try:
            os.chdir(work)
            workflow = ABFE(
                units="kcal/mol",
                software="GROMACS",
                dir="./",
                prefix="dhdl",
                suffix="xvg",
                T=298,
                outdirectory="./",
            )
            workflow.read(n_jobs=threads)
            workflow.preprocess(
                skiptime=10, uncorr="dhdl", threshold=50, n_jobs=threads
            )
            workflow.estimate(estimators=("BAR", "TI"))
            estimates = workflow.estimator.copy()
            # run() ignores estimator kwargs; apply bootstraps to MBAR explicitly.
            workflow.estimate(estimators=("MBAR",), n_bootstraps=50)
            workflow.estimator.update(estimates)
            workflow.generate_result()
            plt.close(workflow.plot_overlap_matrix("O_MBAR.pdf").figure)
            plt.close(workflow.plot_ti_dhdl().figure)
            plt.close(workflow.plot_dF_state())
            plt.close(
                workflow.plot_dF_state(
                    dF_state="dF_state_long.pdf", orientation="landscape"
                )
            )
            plt.close(
                workflow.check_convergence(
                    10, estimator="MBAR", dF_t="dF_t.pdf", n_bootstraps=50
                ).figure
            )
            workflow.summary.to_csv("ABFE_summary.csv")
            workflow.convergence.to_csv("ABFE_convergence.csv")
        finally:
            os.chdir(previous_directory)
        validate_outputs(work, component)

        # Move the whole result directory, restoring old outputs if publication fails.
        previous = Path(temporary) / "previous"
        if target.exists():
            target.rename(previous)
        try:
            work.rename(target)
        except OSError:
            if previous.exists():
                previous.rename(target)
            raise
    return analysed_time


def cleanup_inputs(entries):
    """Remove only snapshots published by this run, after their YAML save."""
    failed = False
    cleaned = set()
    for entry in entries:
        if "calculated_time" not in entry:
            continue
        directory = entry_key(entry)
        if directory in cleaned:
            continue
        cleaned.add(directory)
        for window in expected_windows(entry["component"]):
            try:
                (directory / f"dhdl.{window}.xvg").unlink()
            except OSError as error:
                failed = True
                print(
                    f"{entry_label(entry)}\tInput cleanup failed: {error}",
                    file=sys.stderr,
                )
    return failed


def compile_results(complex_folder, ligand_folder):
    """Combine complex, ligand and restraint results into abfe_results.csv."""
    complex_directory = analysis_folder(complex_folder, "complex")
    ligand_directory = analysis_folder(ligand_folder, "ligand")
    complex_stages = validate_outputs(complex_directory, "complex")
    ligand_stages = validate_outputs(ligand_directory, "ligand")
    restraint = Path(complex_folder).parent / "boresch/resgen_output.txt"
    correction = None
    with restraint.open(encoding="utf-8") as stream:
        for line in stream:
            if line.startswith("dG_off"):
                try:
                    correction = float(line.split(",")[1].split(":")[1])
                except (IndexError, ValueError):
                    raise ValueError(f"{restraint}: invalid dG_off") from None
                break
    if correction is None or not math.isfinite(correction):
        raise ValueError(f"{restraint}: missing or invalid dG_off")
    rows = [
        ["ligand_coul", *ligand_stages["coul"]],
        ["ligand_vdw", *ligand_stages["vdw"]],
        ["anal_rest", correction, 0, correction, 0, correction, 0],
        ["complex_bonded", *complex_stages["bonded"]],
        ["complex_coul", *complex_stages["coul"]],
        ["complex_vdw", *complex_stages["vdw"]],
    ]
    # Match the reference calculation, including its linear sum of error columns.
    totals = []
    for column in range(1, 7):
        if column % 2 == 0:
            totals.append(sum(row[column] for row in rows))
        else:
            totals.append(
                sum(row[column] for row in rows[:3])
                - sum(row[column] for row in rows[3:])
            )
    rows.append(["total", *totals])
    output = complex_directory.parent / "abfe_results.csv"
    header = ["state", *RESULT_COLUMNS]
    if output.is_file():
        try:
            with output.open(encoding="utf-8", newline="") as stream:
                reader = csv.reader(stream)
                if next(reader, []) == header:
                    existing = [[row[0], *map(float, row[1:])] for row in reader]
                    if existing == rows:
                        return False
        except (ValueError, IndexError, csv.Error):
            pass
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=output.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def validate_inputs(entry):
    """Check the source folder, required inputs and matching window end times."""
    if not entry["folder"]:
        raise ValueError("Source folder is not set")
    inputs = find_inputs(entry["folder"], entry["component"])
    common_time({window: read_last_time(path) for window, path in inputs.items()})


def preview_entries(entries):
    """Print selected runs, saved production times and input blockers as a table."""
    headers = (
        "Dataset",
        "Compound",
        "Component",
        "Repeat",
        "Analysis status",
        "Total production (ns)",
        "Finished production (ns)",
        "Input check",
    )
    rows = [headers]
    failures = {}
    for entry in entries:
        input_check = "Ready"
        try:
            validate_inputs(entry)
        except (OSError, UnicodeError, ValueError) as error:
            failures[entry_key(entry)] = str(error)
            input_check = f"Blocked: {error}"
        record = entry["record"]
        times = [
            f"{record[field]:g}" if record.get(field) is not None else "Unknown"
            for field in ("total_production_ns", "completed_production_ns")
        ]
        rows.append(
            (
                entry["dataset"],
                entry["compound"],
                entry["component"],
                str(entry["repeat"]) if entry["repeat"] is not None else "Shared",
                record.get("analysis_status", "Missing"),
                *times,
                input_check,
            )
        )
    widths = [max(len(row[column]) for row in rows) for column in range(len(headers))]
    print("Runs to be analysed:\n")
    for row in rows:
        print(
            "  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip()
        )
    return failures


def compile_selected(entries, selected, failures):
    """Compile combined results affected by the selected components."""
    ligands = {
        (entry["dataset"], entry["compound"]): entry
        for entry in entries
        if entry["component"] == "ligand"
    }
    selected_labels = {entry_label(entry) for entry in selected}
    failed = False
    for entry in entries:
        if entry["component"] != "complex":
            continue
        ligand = ligands[(entry["dataset"], entry["compound"])]
        if (
            entry_label(entry) not in selected_labels
            and entry_label(ligand) not in selected_labels
        ):
            continue
        if entry_key(entry) in failures or entry_key(ligand) in failures:
            continue
        try:
            if not entry["folder"] or not ligand["folder"]:
                raise ValueError("Complex or shared ligand source folder is not set")
            if compile_results(entry["folder"], ligand["folder"]):
                print(f"{entry_label(entry)}\tCompiled abfe_results.csv")
        except (OSError, UnicodeError, ValueError, csv.Error) as error:
            failed = True
            print(f"{entry_label(entry)}\tCompilation failed: {error}", file=sys.stderr)
    return failed


def main(argv=None, data_path=ROOT / "data.yaml"):
    """Select analyses from CLI flags, then preview or run them and save statuses."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, help="dataset ID from data.yaml")
    parser.add_argument(
        "--analysis-status",
        required=True,
        choices=ANALYSIS_STATUSES,
        help="saved analysis status to select; absent statuses count as Missing",
    )
    parser.add_argument(
        "--leg",
        choices=("ligand", "complex"),
        help="analyse only the selected leg (default: both legs)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preview selected work without writing files",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=-1,
        help="alchemlyb n_jobs for reading and preprocessing; -1 uses all CPUs (default: -1)",
    )
    args = parser.parse_args(argv)
    if args.threads != -1 and args.threads < 1:
        parser.error("--threads must be -1 or a positive integer")
    data_path = Path(data_path).resolve()
    try:
        original_data = data_path.read_bytes()
        document = load_data(data_path)
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(1, f"Cannot load {data_path}: {error}\n")
    if args.dataset not in document["datasets"]:
        parser.error(
            f"unknown dataset {args.dataset!r}; choose from: {', '.join(document['datasets'])}"
        )
    entries = select_entries(document, args.dataset, data_path.parent)
    selected = [
        entry
        for entry in entries
        if (args.leg is None or entry["component"] == args.leg)
        and entry["record"].get("analysis_status", "Missing") == args.analysis_status
    ]
    if not selected:
        print("No analyses match the selected filters.")
        return 0
    if args.dry_run:
        failures = preview_entries(selected)
        return int(bool(failures))

    calculated_times = {}
    failures = {}
    for entry in selected:
        print(f"Checking {entry_label(entry)}", file=sys.stderr, flush=True)
        key = entry_key(entry)
        if key in failures:
            continue
        if key not in calculated_times:
            try:
                validate_inputs(entry)
                calculated_times[key] = run_analysis(
                    entry["folder"], entry["component"], threads=args.threads
                )
            except Exception as error:
                failures[key] = str(error)
                continue
            print(f"{entry_label(entry)}\tAnalysis finished", flush=True)
        entry["calculated_time"] = calculated_times[key]

    compilation_failed = compile_selected(entries, selected, failures)
    inspection_failed = refresh_entries(selected, failures)
    try:
        save_analysis_statuses(data_path, document, original_data)
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(1, f"Cannot update {data_path}: {error}\n")
    # Keep published inputs for recovery if the inventory save above fails.
    cleanup_failed = cleanup_inputs(selected)
    return int(
        bool(failures) or compilation_failed or inspection_failed or cleanup_failed
    )


if __name__ == "__main__":
    raise SystemExit(main())
