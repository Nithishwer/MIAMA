"""Shared helpers for analysis inputs, results and recorded statuses."""

import csv
import math
import os
import re
import sys
from pathlib import Path

from utils.update_yaml import update_yaml

WINDOW_COUNTS = {
    "complex": {"rest": 11, "coul": 10, "vdw": 21},
    "ligand": {"coul": 11, "vdw": 21},
}
RESULT_COLUMNS = ("MBAR", "MBAR_Error", "BAR", "BAR_Error", "TI", "TI_Error")


def select_entries(document, dataset_id, data_directory):
    """Collect complex repeats and shared ligands, optionally for one dataset."""
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
    """Format the dataset, compound, repeat and component for console output."""
    repeat = entry["repeat"] if entry["repeat"] is not None else "Shared"
    return f"{entry['dataset']}\t{entry['compound']}\t{repeat}\t{entry['component']}"


def entry_key(entry):
    """Return the resolved output folder, or the entry label if its path is unset."""
    if entry["folder"]:
        return analysis_folder(entry["folder"], entry["component"]).resolve()
    return entry_label(entry)


def expected_windows(component):
    """List the lambda windows required for a complex or ligand analysis."""
    return [
        f"{stage}.{number:02d}"
        for stage, count in WINDOW_COUNTS[component].items()
        for number in range(count)
    ]


def analysis_folder(folder, component):
    """Return the component's analysis directory beside its simulation folder."""
    return Path(folder).parent / "analysis" / component


def read_lambda_state(path):
    """Read lambda names and values from an XVG subtitle."""
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.lstrip().startswith("@ subtitle"):
                match = re.search(r"\(([^()]*)\)\s*=\s*\(([^()]*)\)", line)
                if match:
                    names = [name.strip() for name in match[1].split(",")]
                    values = [float(value) for value in match[2].split(",")]
                    if len(names) == len(values) and all(
                        math.isfinite(value) for value in values
                    ):
                        return dict(zip(names, values))
            if line.strip() and not line.lstrip().startswith(("@", "#")):
                break
    raise ValueError(f"{path}: cannot read lambda coordinates")


def source_windows(folder, component):
    """List simulation windows, excluding verified duplicate stage endpoints."""
    folder = Path(folder)
    windows = {
        path.name
        for path in folder.iterdir()
        if path.is_dir() and re.fullmatch(r"(?:rest|coul|vdw)\.[0-9]+", path.name)
    }
    if component == "complex":
        # The reference workflow uses the next stage's copy of each boundary state.
        for endpoint, next_stage in (("rest.11", "coul.00"), ("coul.10", "vdw.00")):
            if endpoint not in windows:
                continue
            try:
                boundary_state = read_lambda_state(folder / endpoint / "prod/dhdl.xvg")
                next_state = read_lambda_state(folder / next_stage / "prod/dhdl.xvg")
            except (OSError, UnicodeError, ValueError):
                continue  # Unverified extra windows still block analysis.
            if boundary_state == next_state:
                windows.remove(endpoint)
    return windows


def find_inputs(folder, component):
    """Require the whole reference layout before starting a calculation."""
    folder = Path(folder)
    windows = expected_windows(component)
    actual = source_windows(folder, component)
    missing = set(windows) - actual
    extra = actual - set(windows)
    if missing or extra:
        details = []
        if missing:
            details.append("missing windows: " + ", ".join(sorted(missing)))
        if extra:
            details.append("unexpected windows: " + ", ".join(sorted(extra)))
        raise ValueError("; ".join(details))
    inputs = {window: folder / window / "prod/dhdl.xvg" for window in windows}
    absent = [window for window, path in inputs.items() if not path.is_file()]
    if absent:
        raise ValueError("missing dhdl.xvg: " + ", ".join(absent))
    return inputs


def dhdl_lines(path):
    """Yield complete XVG lines, validating numeric records before copying."""
    columns = None
    previous_time = -math.inf
    with Path(path).open("rb") as stream:
        # Freeze the byte limit so a live simulation cannot extend this read forever.
        remaining = os.fstat(stream.fileno()).st_size
        while remaining:
            line = stream.readline(remaining)
            if not line:
                break
            remaining -= len(line)
            if not line.endswith(b"\n"):
                break
            fields = line.split()
            if not fields or line.lstrip().startswith((b"#", b"@")):
                yield line
                continue
            try:
                timestamp = float(fields[0])
                if not all(math.isfinite(float(field)) for field in fields[1:]):
                    raise ValueError
            except ValueError:
                raise ValueError(f"{path}: invalid numeric record") from None
            if columns is None:
                columns = len(fields)
            if (
                len(fields) < 2
                or len(fields) != columns
                or not math.isfinite(timestamp)
                or timestamp < previous_time
            ):
                raise ValueError(f"{path}: invalid or out-of-order record")
            previous_time = timestamp
            yield line


def read_last_time(path):
    """Read the final complete numeric timestamp from the last ten physical lines."""
    with Path(path).open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        tail = b""
        # Seek backwards rather than reading the whole file to collect its tail.
        while position and tail.count(b"\n") <= 10:
            size = min(position, 8192)
            position -= size
            stream.seek(position)
            tail = stream.read(size) + tail
    for line in reversed(tail.splitlines(keepends=True)[-10:]):
        if not line.endswith(b"\n") or line.lstrip().startswith((b"#", b"@")):
            continue
        try:
            values = [float(field) for field in line.split()]
        except ValueError:
            continue
        if len(values) >= 2 and all(math.isfinite(value) for value in values):
            return values[0]
    raise ValueError(f"{path}: no complete numeric samples in the last 10 lines")


def read_summary(path, component):
    """Read and validate the required stage estimates from ABFE_summary.csv."""
    required = ("bonded", "coul", "vdw") if component == "complex" else ("coul", "vdw")
    stages = {}
    with Path(path).open(encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream)
        header = next(reader, [])
        if header[2:] != list(RESULT_COLUMNS):
            raise ValueError(f"{path}: invalid summary columns")
        for row in reader:
            if len(row) < 2 or row[0] != "Stages" or row[1] not in required:
                continue
            if len(row) != 8 or row[1] in stages:
                raise ValueError(f"{path}: invalid or duplicate stage {row[1]}")
            try:
                values = [float(value) for value in row[2:]]
            except ValueError:
                raise ValueError(f"{path}: invalid stage values") from None
            if not all(math.isfinite(value) for value in values) or any(
                value < 0 for value in values[1::2]
            ):
                raise ValueError(f"{path}: invalid stage values")
            stages[row[1]] = values
    if set(stages) != set(required):
        raise ValueError(
            f"{path}: missing stages: {', '.join(sorted(set(required) - set(stages)))}"
        )
    return stages


def read_convergence(path):
    """Validate convergence results and compare first-half and last-half estimates."""
    half_differences = []
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = (
            "Forward",
            "Forward_Error",
            "Backward",
            "Backward_Error",
            "data_fraction",
        )
        if not set(fields).issubset(reader.fieldnames or []):
            raise ValueError(f"{path}: invalid convergence columns")
        last_fraction = None
        for row in reader:
            try:
                values = [float(row[field]) for field in fields]
            except (ValueError, TypeError):
                raise ValueError(f"{path}: invalid convergence values") from None
            if (
                not all(math.isfinite(value) for value in values)
                or values[1] < 0
                or values[3] < 0
                or not 0 < values[4] <= 1
            ):
                raise ValueError(f"{path}: invalid convergence values")
            last_fraction = values[4]
            if last_fraction == 0.5:
                half_differences.append(abs(values[0] - values[2]))
        if last_fraction != 1:
            raise ValueError(f"{path}: missing full-data convergence result")
    if len(half_differences) != 1:
        return "unknown"
    return "converged" if half_differences[0] < 0.5 else "unconverged"


def validate_outputs(directory, component):
    """Validate result CSVs and return their stage estimates."""
    stages = read_summary(directory / "ABFE_summary.csv", component)
    read_convergence(directory / "ABFE_convergence.csv")
    return stages


def common_time(times):
    """Require one nonnegative endpoint across every selected window."""
    if any(timestamp < 0 for timestamp in times.values()):
        raise ValueError("Negative final timestamp")
    if len(set(times.values())) != 1:
        groups = {}
        for window, timestamp in times.items():
            groups.setdefault(timestamp, []).append(window)
        details = "; ".join(
            f"{timestamp:g} ps: {', '.join(windows)}"
            for timestamp, windows in groups.items()
        )
        raise ValueError("Window end times differ: " + details)
    return next(iter(times.values()))


def retained_analysis_time(directory, component):
    """Recover coverage only from a complete snapshot predating valid results."""
    output_time = min(
        (directory / filename).stat().st_mtime_ns
        for filename in ("ABFE_summary.csv", "ABFE_convergence.csv")
    )
    times = {}
    for window in expected_windows(component):
        copied = directory / f"dhdl.{window}.xvg"
        if copied.stat().st_mtime_ns > output_time:
            raise ValueError(f"{window}: retained input is newer than results")
        times[window] = read_last_time(copied)
    return common_time(times)


def inspect_analysis(folder, component, total_analysed_ps=None):
    """Inspect saved convergence results and current analysis coverage separately."""
    result = {
        "status": "Missing",
        "remark": "Source folder is not set",
        "total_analysed_ps": total_analysed_ps,
        "input_error": "",
        "convergence": "unknown",
    }
    if not folder:
        return result

    available_time = None
    source_error = ""
    try:
        inputs = find_inputs(folder, component)
        times = {window: read_last_time(path) for window, path in inputs.items()}
        try:
            available_time = common_time(times)
        except ValueError as error:
            # Unequal endpoints block this component even when outputs are absent.
            result["input_error"] = str(error)
            source_error = str(error)
    except (OSError, UnicodeError, ValueError) as error:
        source_error = str(error)

    directory = analysis_folder(folder, component)
    # Saved convergence remains useful when the summary or current inputs are missing.
    try:
        result["convergence"] = read_convergence(directory / "ABFE_convergence.csv")
    except (OSError, UnicodeError, ValueError, csv.Error):
        pass
    try:
        if not directory.exists() or not any(directory.iterdir()):
            result["remark"] = "No analysis outputs"
            return result
        result["status"] = "Incomplete"
        validate_outputs(directory, component)
    except (OSError, UnicodeError, ValueError, csv.Error) as error:
        result.update(status="Incomplete", remark=str(error))
        return result

    recovery_error = ""
    try:
        recovered_time = retained_analysis_time(directory, component)
        # Published snapshots recover a failed YAML save; unequal live inputs do not
        # replace a previously recorded time during a status refresh.
        if not result["input_error"]:
            result["total_analysed_ps"] = recovered_time
    except (OSError, UnicodeError, ValueError) as error:
        recovery_error = str(error)

    analysed_time = result["total_analysed_ps"]
    if source_error:
        result["remark"] = "Data not verified: " + source_error
    elif analysed_time is None:
        result["remark"] = "Data not verified: " + recovery_error
    elif available_time != analysed_time:
        result["remark"] = (
            f"Analysed through {analysed_time:g} ps; "
            f"available through {available_time:g} ps"
        )
    else:
        result.update(status="Complete", remark="")
    return result


def refresh_entries(entries, failures=None):
    """Update records with inspected statuses, timestamps and analysis failures."""
    failures = failures or {}
    inspection_failed = False
    for entry in entries:
        print(f"Checking {entry_label(entry)}", file=sys.stderr, flush=True)
        record = entry["record"]
        if "calculated_time" in entry:
            record["total_analysed_ps"] = entry["calculated_time"]
        inspection = inspect_analysis(
            entry["folder"], entry["component"], record.get("total_analysed_ps")
        )
        status, remark = inspection["status"], inspection["remark"]
        if inspection["total_analysed_ps"] is not None:
            record["total_analysed_ps"] = inspection["total_analysed_ps"]
        if inspection["input_error"]:
            inspection_failed = True
            print(f"{entry_label(entry)}\t{inspection['input_error']}", file=sys.stderr)
        failure_key = entry_key(entry)
        if failure_key in failures:
            remark = "; ".join(
                filter(None, (remark, f"Analysis failed: {failures[failure_key]}"))
            )
            print(
                f"{entry_label(entry)}\tAnalysis failed: {failures[failure_key]}",
                file=sys.stderr,
            )
        result = status + (f" ({remark})" if remark else "")
        print(f"{entry_label(entry)}\t{result}", flush=True)
        record.update(
            analysis_status=status,
            analysis_remark=remark,
            convergence=inspection["convergence"],
        )
    return inspection_failed


def save_analysis_statuses(data_path, document, original_data):
    """Back up and save changes unless the inventory changed during processing."""
    # Do not overwrite inventory edits made during inspection or calculation.
    if data_path.read_bytes() != original_data:
        raise ValueError(
            "data.yaml changed during analysis; run refresh_analysis_status.py "
            "to refresh without overwriting edits"
        )
    backup = update_yaml(data_path, document)
    if backup:
        print(f"Updated {data_path}; backup: {backup}")
