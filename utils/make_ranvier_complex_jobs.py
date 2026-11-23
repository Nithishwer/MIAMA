"""Write independent-window Ranvier start and continuation jobs for a campaign."""

import argparse
import re
from pathlib import Path

import yaml

SCRIPT_NAMES = ("job_complex_ranv.sh", "job_complex_ranv_contd.sh")
WINDOW_ORDER = {"rest": 0, "coul": 1, "vdw": 2}

JOB_HEADER = """#!/bin/bash
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --ntasks-per-socket=1
#SBATCH --cpus-per-task=8
#SBATCH --gres=gpu:1
#SBATCH --gres-flags=enforce-binding
#SBATCH --time=24:00:00
#SBATCH --job-name=abfe-complex
#SBATCH --partition=gpu4-biggin,gpuL40S-biggin,g-ran
#SBATCH --array=1-{array_size}
#SBATCH --output=output_%x_%A_%a.out

set -euo pipefail

module load GROMACS/2023.1-foss-2022a-CUDA-11.7.0
export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:?}"
cd -- "${SLURM_SUBMIT_DIR:?}"
"""

SELECT_WINDOW = """
task_id="${SLURM_ARRAY_TASK_ID:?}"
if [[ ! "$task_id" =~ ^[1-9][0-9]*$ ]] || (( task_id > ${#windows[@]} )); then
    printf 'Invalid array task ID: %s\\n' "$task_id" >&2
    exit 1
fi
window="${windows[task_id - 1]}"
"""

START_JOB = """
window_type="${window%.*}"
window_number="${window##*.}"
cd -- "$window"

# Each stage starts from the preceding stage's coordinates, without a restart.
coordinates="../../complex_${window_type}.gro"
for stage in enmin nvt npt_b npt_pr prod; do
    cd -- "$stage"
    gmx grompp -f "${window_type}.${stage}.${window_number}.mdp" \\
        -c "$coordinates" -r "$coordinates" \\
        -n "../../index_${window_type}.ndx" \\
        -p "../../complex_${window_type}.top" \\
        -o topol.tpr -maxwarn 2
    gmx mdrun -s topol.tpr -ntmpi 1 -ntomp "$OMP_NUM_THREADS"
    cd ..
    coordinates="../${stage}/confout.gro"
done
"""

CONTINUATION_JOB = """
cd -- "$window/prod"

# Missing restart inputs must never turn a continuation into a fresh simulation.
for input in topol.tpr state.cpt; do
    if [[ ! -f "$input" || ! -s "$input" ]]; then
        printf 'Missing or empty production input: %s/%s\\n' "$PWD" "$input" >&2
        exit 1
    fi
done
gmx mdrun -s topol.tpr -cpi state.cpt -append -ntmpi 1 -ntomp "$OMP_NUM_THREADS"
"""


def read_sources(campaign_path):
    """Read and validate the campaign without depending on transfer utilities."""
    with campaign_path.open(encoding="utf-8") as stream:
        campaign = yaml.safe_load(stream)
    if not isinstance(campaign, dict):
        raise TypeError("campaign YAML must be a mapping")
    if campaign.get("cluster") != "ranv":
        raise ValueError("campaign must specify cluster: ranv")
    if campaign.get("stage") != "alchemical_complex":
        raise ValueError("campaign must specify stage: alchemical_complex")
    runs = campaign.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("campaign must contain a nonempty runs list")

    sources = []
    for number, run in enumerate(runs, start=1):
        source = run.get("source") if isinstance(run, dict) else None
        if not isinstance(source, str) or not source.strip() or "\0" in source:
            raise ValueError(f"run {number}: source must be a nonempty path")
        source = Path(source).expanduser().resolve()
        if not source.is_dir():
            raise ValueError(f"run {number}: source is not a directory: {source}")
        if source not in sources:
            sources.append(source)
    return sources


def render_job(windows, *, continuation=False):
    """Embed the discovered windows so the job needs no external window list."""
    header = JOB_HEADER.replace("{array_size}", str(len(windows)))
    window_array = " ".join(windows)
    return (
        header
        + f"\nwindows=({window_array})\n"
        + SELECT_WINDOW
        + (CONTINUATION_JOB if continuation else START_JOB)
    )


def make_jobs(campaign_path, *, overwrite=False):
    """Validate every source and target before writing any job scripts."""
    jobs = []
    for source in read_sources(Path(campaign_path)):
        windows = [
            path.name
            for path in source.iterdir()
            if re.fullmatch(r"(?:rest|coul|vdw)\.[0-9]{2}", path.name) and path.is_dir()
        ]
        windows.sort(key=lambda name: (WINDOW_ORDER[name.split(".")[0]], name[-2:]))
        if not windows:
            raise ValueError(f"{source}: no rest.NN, coul.NN or vdw.NN directories")
        for number, filename in enumerate(SCRIPT_NAMES):
            target = source / filename
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ValueError(f"{target}: target is not a regular file")
            if target.exists() and not overwrite:
                raise ValueError(
                    f"{target}: already exists; use --overwrite to replace it"
                )
            jobs.append((target, render_job(windows, continuation=number == 1)))

    for target, script in jobs:
        with target.open("w" if overwrite else "x", encoding="utf-8") as stream:
            stream.write(script)
        target.chmod(target.stat().st_mode | 0o111)
        print(target)
    return [target for target, _ in jobs]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_yaml", type=Path, help="path to a campaign.yaml file")
    parser.add_argument(
        "--overwrite", action="store_true", help="replace existing regular job scripts"
    )
    args = parser.parse_args(argv)
    try:
        make_jobs(args.campaign_yaml, overwrite=args.overwrite)
    except (OSError, UnicodeError, TypeError, ValueError, yaml.YAMLError) as error:
        parser.exit(1, f"Cannot generate Ranvier jobs: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
