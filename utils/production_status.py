"""Check production completion from a trajectory, MDP and log."""

import math
from pathlib import Path


def validate_production_files(files):
    """Return field-specific errors for production filename templates."""
    if not isinstance(files, dict):
        return ["production_files: expected a mapping"]
    errors = []
    fields = ("trajectory", "log", "mdp")
    for field in fields:
        path = f"production_files.{field}"
        if field not in files:
            errors.append(f"{path}: required field is missing")
            continue
        value = files[field]
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{path}: expected a nonempty filename")
            continue
        if value in (".", "..") or any(
            character in value for character in ("/", "\\", "\0")
        ):
            errors.append(f"{path}: expected a filename without a directory path")
        # Only the selected window's stage and number can be substituted.
        literal = value.replace("{stage}", "").replace("{number}", "")
        if "{" in literal or "}" in literal:
            errors.append(
                f"{path}: only {{stage}} and {{number}} placeholders are supported"
            )
    for field in files:
        if field not in fields:
            errors.append(f"production_files.{field}: unrecognised field")
    return errors


def read_nsteps(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            key, separator, value = line.split(";", 1)[0].partition("=")
            if separator and key.strip() == "nsteps":
                try:
                    nsteps = int(value.strip())
                except ValueError:
                    raise ValueError(f"{path.name} has invalid nsteps") from None
                if nsteps < 0:
                    raise ValueError(f"{path.name} has negative nsteps")
                return nsteps
    raise ValueError(f"{path.name} has no nsteps")


def read_last_step(path):
    last_step = None
    awaiting_step = False
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            fields = line.split()
            if fields == ["Step", "Time"]:
                awaiting_step = True
                continue
            if not awaiting_step or not fields:
                continue
            # Only accept the numeric row beneath a header, never footer statistics.
            awaiting_step = False
            if len(fields) != 2:
                continue
            try:
                step = int(fields[0])
                time = float(fields[1])
            except ValueError:
                continue
            if step >= 0 and math.isfinite(time):
                last_step = step
    if last_step is None:
        raise ValueError(f"{path.name} has no valid step records")
    return last_step


def check_production(folder, *, trajectory="prod.xtc", mdp="prod.mdp", log="prod.log"):
    if not folder:
        return "missing", ""
    folder = Path(folder)
    try:
        if not (folder / trajectory).is_file():
            return "missing", ""
        nsteps = read_nsteps(folder / mdp)
        last_step = read_last_step(folder / log)
    except FileNotFoundError as error:
        return "incomplete", f"{Path(error.filename).name} missing"
    except (OSError, UnicodeError, ValueError) as error:
        return "incomplete", str(error)
    if last_step == nsteps:
        return "complete", ""
    return "incomplete", f"step {last_step}/{nsteps}"
