"""Preview, refresh or run missing/outdated component analyses with four workers."""

import argparse
import csv
import math
import os
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data import load_data
from utils.analysis_status import (
    RESULT_COLUMNS,
    analysis_folder,
    dhdl_lines,
    find_inputs,
    inspect_analysis,
    read_last_time,
    read_summary,
    validate_outputs,
)
from utils.update_yaml import update_yaml

ROOT = Path(__file__).resolve().parents[1]


def select_entries(document, dataset_id, data_directory):
    entries = []
    for dataset, source in document["datasets"].items():
        if dataset_id and dataset != dataset_id:
            continue
        for compound, records in source["complexes"].items():
            components = [
                (repeat, "complex", runs["alchemical_complex"])
                for repeat, runs in sorted(records["repeats"].items())
            ]
            components.append((None, "ligand", records["alchemical_ligand"]))
            for repeat, component, run in components:
                folder = (
                    (data_directory / run["folder"]).resolve()
                    if run["folder"]
                    else None
                )
                entries.append(
                    {
                        "dataset": dataset,
                        "compound": compound,
                        "repeat": repeat,
                        "component": component,
                        "folder": folder,
                        "record": run,
                    }
                )
    return entries


def entry_label(entry):
    repeat = entry["repeat"] if entry["repeat"] is not None else "Shared"
    return f"{entry['dataset']}\t{entry['compound']}\t{repeat}\t{entry['component']}"


def output_key(entry):
    return analysis_folder(entry["folder"], entry["component"]).resolve()


def run_analysis(folder, component):
    # Import only in workers, so inspection and website builds stay lightweight.
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
        for window, source in inputs.items():
            copied = work / f"dhdl.{window}.xvg"
            with copied.open("wb") as stream:
                for line, _ in dhdl_lines(source):
                    stream.write(line)
            read_last_time(copied)

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
            workflow.read(n_jobs=1)
            workflow.preprocess(skiptime=10, uncorr="dhdl", threshold=50, n_jobs=1)
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


def run_jobs(entries):
    jobs = {}
    failures = {}
    for entry in entries:
        print(f"Checking {entry_label(entry)}", file=sys.stderr, flush=True)
        status, _ = inspect_analysis(entry["folder"], entry["component"])
        if status == "Complete":
            continue
        if not entry["folder"]:
            failures[entry_label(entry)] = "Source folder is not set"
            continue
        key = output_key(entry)
        if key in jobs or key in failures:
            continue
        try:
            find_inputs(entry["folder"], entry["component"])
        except (OSError, ValueError) as error:
            failures[key] = str(error)
            continue
        jobs[key] = entry
    if jobs:
        with ProcessPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(run_analysis, entry["folder"], entry["component"]): key
                for key, entry in jobs.items()
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    future.result()
                    print(f"{entry_label(jobs[key])}\tAnalysis finished", flush=True)
                except Exception as error:
                    failures[key] = str(error)
    return failures


def compile_results(complex_folder, ligand_folder, *, write=True):
    complex_directory = analysis_folder(complex_folder, "complex")
    ligand_directory = analysis_folder(ligand_folder, "ligand")
    validate_outputs(complex_directory, "complex")
    validate_outputs(ligand_directory, "ligand")
    complex_stages = read_summary(complex_directory / "ABFE_summary.csv", "complex")
    ligand_stages = read_summary(ligand_directory / "ABFE_summary.csv", "ligand")
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
    if not write:
        return True
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


def compile_selected(entries, failures, *, preview=False):
    ligands = {
        (entry["dataset"], entry["compound"]): entry
        for entry in entries
        if entry["component"] == "ligand"
    }
    failed = False
    for entry in entries:
        if entry["component"] != "complex":
            continue
        ligand = ligands[(entry["dataset"], entry["compound"])]
        if not entry["folder"] or not ligand["folder"]:
            continue  # Already reported as blocked component work.
        if output_key(entry) in failures or output_key(ligand) in failures:
            continue
        try:
            if compile_results(entry["folder"], ligand["folder"], write=not preview):
                action = "Would compile" if preview else "Compiled"
                print(f"{entry_label(entry)}\t{action} abfe_results.csv")
        except (OSError, UnicodeError, ValueError, csv.Error) as error:
            failed = True
            action = "Compilation blocked" if preview else "Compilation failed"
            print(f"{entry_label(entry)}\t{action}: {error}", file=sys.stderr)
    return failed


def main(argv=None, data_path=ROOT / "data.yaml"):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", help="dataset ID; defaults to all datasets")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--update-status",
        action="store_true",
        help="save inspected statuses without analysing",
    )
    modes.add_argument(
        "--run",
        action="store_true",
        help="run Missing/Incomplete analyses with four workers and save statuses",
    )
    args = parser.parse_args(argv)
    data_path = Path(data_path).resolve()
    try:
        original_data = data_path.read_bytes()
        document = load_data(data_path)
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(1, f"Cannot load {data_path}: {error}\n")
    if args.dataset and args.dataset not in document["datasets"]:
        parser.error(
            f"unknown dataset {args.dataset!r}; choose from: {', '.join(document['datasets'])}"
        )
    entries = select_entries(document, args.dataset, data_path.parent)
    failures = run_jobs(entries) if args.run else {}
    compilation_failed = compile_selected(entries, failures) if args.run else False
    for entry in entries:
        print(f"Checking {entry_label(entry)}", file=sys.stderr, flush=True)
        status, remark = inspect_analysis(entry["folder"], entry["component"])
        failure_key = output_key(entry) if entry["folder"] else entry_label(entry)
        if failure_key in failures:
            remark = "; ".join(
                filter(None, (remark, f"Analysis failed: {failures[failure_key]}"))
            )
            print(
                f"{entry_label(entry)}\tAnalysis failed: {failures[failure_key]}",
                file=sys.stderr,
            )
        result = status + (f" ({remark})" if remark else "")
        action = ""
        if not (args.run or args.update_status):
            action = "Would skip" if status == "Complete" else "Would analyse"
            if status != "Complete":
                try:
                    if not entry["folder"]:
                        raise ValueError("Source folder is not set")
                    find_inputs(entry["folder"], entry["component"])
                except (OSError, ValueError) as error:
                    action = f"Blocked: {error}"
        print(
            f"{entry_label(entry)}\t{result}" + (f"\t{action}" if action else ""),
            flush=True,
        )
        entry["record"].update(analysis_status=status, analysis_remark=remark)
    if args.run or args.update_status:
        try:
            # Long calculations must not overwrite edits made while they were running.
            if data_path.read_bytes() != original_data:
                raise ValueError(
                    "data.yaml changed during analysis; rerun --update-status to refresh without overwriting edits"
                )
            backup = update_yaml(data_path, document)
        except (OSError, UnicodeError, ValueError) as error:
            parser.exit(1, f"Cannot update {data_path}: {error}\n")
        if backup:
            print(f"Updated {data_path}; backup: {backup}")
    else:
        compile_selected(entries, {}, preview=True)
    return int(bool(failures) or compilation_failed)


if __name__ == "__main__":
    raise SystemExit(main())
