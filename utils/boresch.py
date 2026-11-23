"""Calculate the harmonic Boresch release correction from saved GROMACS inputs."""

import math
from pathlib import Path


def calculate_boresch_release(topology, temperature):
    """Return the B-state analytical release correction in kcal/mol."""
    if not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("Boresch temperature must be finite and positive")
    # Atom counts, harmonic function types and required restraint counts.
    layout = {"bonds": (2, "6", 1), "angles": (3, "1", 2), "dihedrals": (4, "2", 3)}
    parameters = {section: [] for section in layout}
    section = None
    with Path(topology).open(encoding="utf-8") as stream:
        for line in stream:
            line = line.split(";", 1)[0].strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip().lower()
            elif section in layout:
                fields = line.split()
                atom_count, function_type, _ = layout[section]
                if len(fields) != atom_count + 5 or fields[atom_count] != function_type:
                    raise ValueError(
                        f"{topology}: expected harmonic A/B {section} parameters"
                    )
                try:
                    reference, force = map(float, fields[-2:])
                except ValueError:
                    raise ValueError(
                        f"{topology}: invalid B-state {section} parameters"
                    ) from None
                if (
                    not math.isfinite(reference)
                    or not math.isfinite(force)
                    or force <= 0
                ):
                    raise ValueError(
                        f"{topology}: {section} requires finite references and positive B-state forces"
                    )
                parameters[section].append((reference, force))
    if any(
        len(parameters[section]) != count for section, (_, _, count) in layout.items()
    ):
        raise ValueError(
            f"{topology}: expected one bond, two angles and three dihedrals"
        )
    distance = parameters["bonds"][0][0]
    angles = [reference for reference, _ in parameters["angles"]]
    if distance <= 0 or any(not 0 < angle < 180 for angle in angles):
        raise ValueError(
            f"{topology}: distance must be positive and angles strictly between 0 and 180 degrees"
        )
    forces = [force for rows in parameters.values() for _, force in rows]
    rt = 0.008314472 * temperature
    # Evaluate the dimensionless ratio in log space to avoid large products.
    log_ratio = (
        math.log(8 * math.pi**2 * 1.66)
        + sum(math.log(force) for force in forces) / 2
        - 2 * math.log(distance)
        - sum(math.log(math.sin(math.radians(angle))) for angle in angles)
        - 3 * math.log(2 * math.pi * rt)
    )
    return -rt * log_ratio / 4.184


def estimate_boresch_release(complex_folder):
    """Read the saved restraint topology and production temperature for one run."""
    complex_folder = Path(complex_folder)
    topology = complex_folder.parent / "boresch/BoreschRestraint.top"
    if not topology.exists():
        topology = complex_folder / "BoreschRestraint.top"
    mdp = complex_folder / "rest.00/prod/rest.prod.00.mdp"
    temperatures = []
    with mdp.open(encoding="utf-8") as stream:
        for line in stream:
            key, separator, value = line.split(";", 1)[0].partition("=")
            if separator and key.strip().lower().replace("_", "-") == "ref-t":
                try:
                    temperatures.extend(map(float, value.split()))
                except ValueError:
                    raise ValueError(f"{mdp}: invalid ref-t temperature") from None
    if (
        not temperatures
        or any(not math.isfinite(value) or value <= 0 for value in temperatures)
        or len(set(temperatures)) != 1
    ):
        raise ValueError(
            f"{mdp}: requires one finite positive ref-t temperature shared by all groups"
        )
    return calculate_boresch_release(topology, temperatures[0])
