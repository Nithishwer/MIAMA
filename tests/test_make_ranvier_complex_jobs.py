"""Check campaign preflight and execute generated jobs with stub GROMACS commands."""

import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

from utils import make_ranvier_complex_jobs as jobs

SCRIPT = Path(__file__).resolve().parents[1] / "utils/make_ranvier_complex_jobs.py"
STAGES = ("enmin", "nvt", "npt_b", "npt_pr", "prod")

GMX_STUB = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with open(os.environ['GMX_CALLS'], 'a') as stream:
    stream.write(json.dumps({'args': args, 'cwd': str(Path.cwd()),
                             'threads': os.environ['OMP_NUM_THREADS']}) + '\\n')
if (Path.cwd().name == os.environ.get('FAIL_STAGE')
        and args[0] == os.environ.get('FAIL_COMMAND')):
    sys.exit(17)

def option(flag):
    return args[args.index(flag) + 1]

if args[0] == 'grompp':
    for flag in ('-f', '-c', '-r', '-n', '-p'):
        if not Path(option(flag)).is_file():
            sys.exit('Missing grompp input: ' + option(flag))
    Path(option('-o')).write_text('compiled input')
elif args[0] == 'mdrun':
    if not Path(option('-s')).is_file():
        sys.exit('Missing TPR')
    if '-cpi' in args and not Path(option('-cpi')).is_file():
        sys.exit('Missing checkpoint')
    Path('confout.gro').write_text('finished coordinates')
else:
    sys.exit('Unexpected command')
"""


class RanvierJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.make_source(
            "compound with space's $(literal)", ("vdw.02", "rest.01", "coul.00")
        )
        self.campaign = {
            "cluster": "ranv",
            "stage": "alchemical_complex",
            "runs": [{"source": str(self.source)}],
        }
        self.campaign_path = self.root / "campaign.yaml"
        self.save_campaign()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, content in (
            ("gmx", GMX_STUB),
            ("module", '#!/bin/sh\nexit "${MODULE_EXIT:-0}"\n'),
        ):
            command = self.bin / name
            command.write_text(content)
            command.chmod(0o755)
        self.calls_path = self.root / "gmx_calls.jsonl"

    def make_source(self, name, windows):
        source = self.root / name / "abfe_van1_hrex_r1/complex"
        source.mkdir(parents=True)
        for window in windows:
            kind, number = window.split(".")
            for filename in (
                f"complex_{kind}.gro",
                f"complex_{kind}.top",
                f"index_{kind}.ndx",
            ):
                (source / filename).write_text("original input")
            for stage in STAGES:
                folder = source / window / stage
                folder.mkdir(parents=True)
                (folder / f"{kind}.{stage}.{number}.mdp").write_text("original MDP")
        return source

    def save_campaign(self):
        self.campaign_path.write_text(yaml.safe_dump(self.campaign))

    def generate(self, *options):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(jobs.main([str(self.campaign_path), *options]), 0)
        return [Path(line) for line in output.getvalue().splitlines()]

    def assert_generation_fails(self, reason, *options):
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
            jobs.main([str(self.campaign_path), *options])
        self.assertEqual(raised.exception.code, 1)
        self.assertIn(reason, errors.getvalue())

    def run_job(self, filename, task_id="2", **extra_env):
        self.calls_path.unlink(missing_ok=True)
        environment = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "GMX_CALLS": str(self.calls_path),
            "SLURM_SUBMIT_DIR": str(self.source),
            "SLURM_ARRAY_TASK_ID": task_id,
            "SLURM_CPUS_PER_TASK": "8",
            **extra_env,
        }
        result = subprocess.run(
            ["bash", str(self.source / filename)],
            check=False,
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
        )
        calls = []
        if self.calls_path.exists():
            calls = [
                json.loads(line) for line in self.calls_path.read_text().splitlines()
            ]
        return result, calls

    def write_restart_inputs(self):
        production = self.source / "coul.00/prod"
        for filename in (
            "topol.tpr",
            "state.cpt",
            "md.log",
            "traj_comp.xtc",
            "dhdl.xvg",
        ):
            (production / filename).write_text("original production data")
        return production

    def test_creates_ordered_arrays_with_variable_counts_and_preserves_inputs(self):
        windows = [
            f"{kind}.{number:02}"
            for kind, count in (("rest", 12), ("coul", 11), ("vdw", 21))
            for number in range(count)
        ]
        other = self.make_source("other", reversed(windows))
        self.campaign["runs"].append({"source": str(other)})
        self.save_campaign()
        for name in ("rest.1", "coul.001", "other.00"):
            (self.source / name).mkdir()
        (self.source / "vdw.03").write_text("not a directory")
        previous = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }

        paths = self.generate()

        self.assertEqual(
            paths,
            [
                source / name
                for source in (self.source, other)
                for name in jobs.SCRIPT_NAMES
            ],
        )
        for path, content in previous.items():
            self.assertEqual(path.read_bytes(), content)
        for source, expected in (
            (self.source, ["rest.01", "coul.00", "vdw.02"]),
            (other, windows),
        ):
            for filename in jobs.SCRIPT_NAMES:
                path = source / filename
                content = path.read_text()
                self.assertIn(f"#SBATCH --array=1-{len(expected)}\n", content)
                line = next(
                    line
                    for line in content.splitlines()
                    if line.startswith("windows=(")
                )
                self.assertEqual(shlex.split(line[len("windows=(") : -1]), expected)
                self.assertEqual(path.stat().st_mode & 0o111, 0o111)
                self.assertIn("GROMACS/2023.1-foss-2022a-CUDA-11.7.0", content)
                for setting in (
                    "--ntasks=1",
                    "--cpus-per-task=8",
                    "--gres=gpu:1",
                    "--time=24:00:00",
                ):
                    self.assertIn(setting, content)
                for flag in ("-multidir", "-hrex", "-replex", "-deffnm"):
                    self.assertNotIn(flag, content)
                subprocess.run(
                    ["bash", "-n", str(path)], check=True, capture_output=True
                )

    def test_rejects_invalid_campaign_shapes_and_sources(self):
        valid = self.campaign
        cases = [
            (None, "mapping"),
            ([], "mapping"),
            ({**valid, "cluster": "soma"}, "cluster: ranv"),
            ({**valid, "stage": "alchemical_ligand"}, "stage: alchemical_complex"),
            ({**valid, "runs": []}, "nonempty runs list"),
            ({**valid, "runs": {}}, "nonempty runs list"),
            ({**valid, "runs": [None]}, "nonempty path"),
            ({**valid, "runs": [{}]}, "nonempty path"),
            ({**valid, "runs": [{"source": " "}]}, "nonempty path"),
            ({**valid, "runs": [{"source": 3}]}, "nonempty path"),
            ({**valid, "runs": [{"source": "bad\0path"}]}, "nonempty path"),
            (
                {**valid, "runs": [{"source": str(self.campaign_path)}]},
                "not a directory",
            ),
        ]
        for campaign, reason in cases:
            with self.subTest(campaign=campaign):
                self.campaign = campaign
                self.save_campaign()
                self.assert_generation_fails(reason)
                self.assertFalse(any(self.source.glob("job_complex_ranv*.sh")))
        self.campaign_path.write_text("runs: [")
        self.assert_generation_fails("Cannot generate Ranvier jobs")

    def test_validates_later_sources_before_writing_any_scripts(self):
        missing = self.root / "missing"
        empty = self.root / "empty"
        empty.mkdir()
        for source, reason in ((missing, "not a directory"), (empty, "no rest.NN")):
            with self.subTest(source=source):
                self.campaign["runs"] = [
                    {"source": str(self.source)},
                    {"source": str(source)},
                ]
                self.save_campaign()
                self.assert_generation_fails(reason)
                self.assertFalse(any(self.source.glob("job_complex_ranv*.sh")))

    def test_existing_script_blocks_all_writes_unless_overwrite_is_supplied(self):
        other = self.make_source("other", ["coul.00"])
        self.campaign["runs"].append({"source": str(other)})
        self.save_campaign()
        existing = other / jobs.SCRIPT_NAMES[1]
        existing.write_text("custom job")

        self.assert_generation_fails("use --overwrite")
        self.assertEqual(existing.read_text(), "custom job")
        self.assertFalse(any(self.source.glob("job_complex_ranv*.sh")))
        self.assertFalse((other / jobs.SCRIPT_NAMES[0]).exists())

        self.assertEqual(len(self.generate("--overwrite")), 4)
        self.assertIn("-cpi state.cpt -append", existing.read_text())

    def test_overwrite_rejects_symlinks_and_directory_targets(self):
        target = self.source / jobs.SCRIPT_NAMES[1]
        preserved = self.root / "preserved"
        preserved.write_text("keep this file")
        target.symlink_to(preserved)
        self.assert_generation_fails("not a regular file", "--overwrite")
        self.assertEqual(preserved.read_text(), "keep this file")
        target.unlink()
        target.mkdir()
        self.assert_generation_fails("not a regular file", "--overwrite")
        self.assertFalse((self.source / jobs.SCRIPT_NAMES[0]).exists())

    def test_cli_accepts_an_absolute_campaign_path_from_another_directory(self):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), str(self.campaign_path)],
            check=False,
            cwd=self.bin,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 2)

    def test_start_runs_all_stages_for_only_the_selected_window(self):
        self.generate()
        self.write_restart_inputs()

        result, calls = self.run_job(jobs.SCRIPT_NAMES[0])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 10)
        for index, stage in enumerate(STAGES):
            compile_call, run_call = calls[2 * index : 2 * index + 2]
            self.assertEqual(Path(compile_call["cwd"]), self.source / "coul.00" / stage)
            self.assertEqual(run_call["cwd"], compile_call["cwd"])
            args = compile_call["args"]
            self.assertEqual(args[0], "grompp")
            self.assertEqual(args[args.index("-f") + 1], f"coul.{stage}.00.mdp")
            coordinates = (
                "../../complex_coul.gro"
                if index == 0
                else f"../{STAGES[index - 1]}/confout.gro"
            )
            self.assertEqual(args[args.index("-c") + 1], coordinates)
            self.assertEqual(args[args.index("-r") + 1], coordinates)
            self.assertEqual(
                run_call["args"],
                ["mdrun", "-s", "topol.tpr", "-ntmpi", "1", "-ntomp", "8"],
            )
            self.assertEqual(run_call["threads"], "8")
        self.assertFalse((self.source / "rest.01/enmin/topol.tpr").exists())
        self.assertFalse((self.source / "vdw.02/enmin/topol.tpr").exists())

    def test_continuation_only_resumes_production_without_recompilation(self):
        self.generate()
        production = self.write_restart_inputs()
        previous = {path: path.read_bytes() for path in production.iterdir()}

        result, calls = self.run_job(jobs.SCRIPT_NAMES[1])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(Path(calls[0]["cwd"]), production)
        self.assertEqual(
            calls[0]["args"],
            [
                "mdrun",
                "-s",
                "topol.tpr",
                "-cpi",
                "state.cpt",
                "-append",
                "-ntmpi",
                "1",
                "-ntomp",
                "8",
            ],
        )
        for path, content in previous.items():
            self.assertEqual(path.read_bytes(), content)

    def test_continuation_never_starts_with_missing_or_empty_restart_inputs(self):
        self.generate()
        for filename in ("topol.tpr", "state.cpt"):
            for empty in (False, True):
                with self.subTest(filename=filename, empty=empty):
                    production = self.write_restart_inputs()
                    path = production / filename
                    if empty:
                        path.write_bytes(b"")
                    else:
                        path.unlink()
                    result, calls = self.run_job(jobs.SCRIPT_NAMES[1])
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(
                        f"Missing or empty production input: {path}", result.stderr
                    )
                    self.assertEqual(calls, [])

    def test_start_stops_immediately_after_failed_commands(self):
        self.generate()
        for stage, command, count in (("nvt", "grompp", 3), ("npt_b", "mdrun", 6)):
            with self.subTest(stage=stage, command=command):
                result, calls = self.run_job(
                    jobs.SCRIPT_NAMES[0], FAIL_STAGE=stage, FAIL_COMMAND=command
                )
                self.assertEqual(result.returncode, 17)
                self.assertEqual(len(calls), count)
        result, calls = self.run_job(jobs.SCRIPT_NAMES[0], MODULE_EXIT="7")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(calls, [])

    def test_invalid_array_indices_never_run_gromacs(self):
        self.generate()
        for task_id in ("0", "4", "-1", "x", "02", ""):
            with self.subTest(task_id=task_id):
                result, calls = self.run_job(jobs.SCRIPT_NAMES[0], task_id=task_id)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
