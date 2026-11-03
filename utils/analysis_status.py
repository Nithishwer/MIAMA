"""Inspect component results and compare retained inputs with available data."""

import csv
import math
import os
import re
from pathlib import Path

WINDOW_COUNTS = {
    "complex": {"rest": 11, "coul": 10, "vdw": 21},
    "ligand": {"coul": 11, "vdw": 21},
}
RESULT_COLUMNS = ("MBAR", "MBAR_Error", "BAR", "BAR_Error", "TI", "TI_Error")


def expected_windows(component):
    return [
        f"{stage}.{number:02d}"
        for stage, count in WINDOW_COUNTS[component].items()
        for number in range(count)
    ]


def analysis_folder(folder, component):
    return Path(folder).parent / "analysis" / component


def read_lambda_state(path):
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
    """Yield complete XVG lines and their timestamps; headers have no timestamp."""
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
                yield line, None
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
            yield line, timestamp


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


def validate_outputs(directory, component):
    read_summary(directory / "ABFE_summary.csv", component)
    path = directory / "ABFE_convergence.csv"
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
        fractions = []
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
            fractions.append(values[4])
        if not fractions or fractions[-1] != 1:
            raise ValueError(f"{path}: missing full-data convergence result")


def inspect_analysis(folder, component):
    if not folder:
        return "Missing", "Source folder is not set"
    directory = analysis_folder(folder, component)
    try:
        if not directory.exists() or not any(directory.iterdir()):
            return "Missing", "No analysis outputs"
        validate_outputs(directory, component)
    except (OSError, UnicodeError, ValueError, csv.Error) as error:
        return "Incomplete", str(error)

    unverified = []
    try:
        extra = source_windows(folder, component) - set(expected_windows(component))
        if extra:
            return "Incomplete", "Unexpected window layout: " + ", ".join(sorted(extra))
        find_inputs(folder, component)
    except (OSError, ValueError) as error:
        unverified.append(str(error))
    for window in expected_windows(component):
        source = Path(folder) / window / "prod/dhdl.xvg"
        copied = directory / f"dhdl.{window}.xvg"
        try:
            available_time = read_last_time(source)
            analysed_time = read_last_time(copied)
            if available_time > analysed_time:
                return "Incomplete", (
                    f"{window}: analysed through {analysed_time:g} ps; "
                    f"available through {available_time:g} ps"
                )
            if available_time != analysed_time:
                unverified.append(
                    f"{window}: source coverage differs from retained input"
                )
            output_time = min(
                (directory / filename).stat().st_mtime_ns
                for filename in ("ABFE_summary.csv", "ABFE_convergence.csv")
            )
            if copied.stat().st_mtime_ns > output_time:
                unverified.append(f"{window}: retained input is newer than results")
        except (OSError, UnicodeError, ValueError) as error:
            unverified.append(f"{window}: {error}")
    if unverified:
        # Keep the table readable; the first reason is enough to explain uncertainty.
        return "Incomplete", "Data not verified: " + unverified[0]
    return "Complete", ""
