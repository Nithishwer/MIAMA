# MIAMA

A small, manually maintained ABFE status console. Edit YAML, rebuild the static
website, and refresh the browser. The console uses ABFlow's H.A.W.T.O.W.N styling.

## Run locally

Requires Python 3.10 or newer. From this repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python build.py
python -m http.server --bind 127.0.0.1 --directory site 8765
```

Open [the local console](http://127.0.0.1:8765). After each edit to `data.yaml`,
run `python build.py` again and refresh the browser. You can also open the
generated `site/index.html` directly: it includes all scripts, styles and fonts
and requires no external services. The generated site is ignored by Git.

## Edit the data

`data.yaml` contains datasets, each dataset's complexes, and each complex's
numbered repeats. Each repeat has a vanilla run and an alchemical complex run.
One alchemical ligand record sits alongside the repeats and is shared across all
of them. Repeat keys are unquoted positive integers; their number is unrestricted.
Dataset and complex keys are nonempty strings. Preserve the indentation when editing.

```yaml
datasets:
  my-dataset:
    label: My dataset
    complexes:
      ligand_01:
        repeats:
          1:
            vanilla:
              folder: /path/to/vanilla_rep_1
              status: Complete
              remark: ""
            alchemical_complex:
              folder: /path/to/abfe_van1_hrex_r1/complex
              production_files:
                trajectory: traj_comp.xtc
                log: md.log
                mdp: "{stage}.prod.{number}.mdp"
              status: Waiting
              remark: "Review production before extending"
              convergence: not_converged
              total_production_ns: 20
              completed_production_ns: 12.5
        alchemical_ligand:
          folder: null
          production_files:
            trajectory: "{stage}.prod.{number}.xtc"
            log: "{stage}.prod.{number}.log"
            mdp: "{stage}.prod.{number}.mdp"
          status: Waiting
          remark: ""
          convergence: unknown
          total_production_ns: null
          completed_production_ns: null
```

All shown fields except `production_files` are required. Vanilla records have
only `folder`, `status` and `remark`.

| Field | Accepted values |
| --- | --- |
| `folder` | A nonempty folder path string, or `null` if unknown |
| `status` | Exactly `Attention required`, `Complete`, or `Waiting` |
| `remark` | Free-text note for any run; use `""` when blank |
| `production_files` | Optional alchemical-only mapping containing `trajectory`, `log` and `mdp` filenames |
| `convergence` | Exactly `unknown`, `not_converged`, or `converged` |
| `total_production_ns` | Intended production duration per window from the MDP, in ns; or `null` |
| `completed_production_ns` | Minimum completed production across all expected lambda windows, in ns; or `null` |

Times cover production only, excluding minimisation and equilibration. Enter the
MDP duration manually (`nsteps × dt / 1000` when `dt` is in ps). A completed time
of 10 means every expected window has reached at least 10 ns. Zero means no
production has completed; `null` means it has not been recorded. An extension
may make completed production exceed the recorded total; this is allowed.

**Status and convergence are independent fields.** Edit them manually, or use
the completion checkers below to update run statuses. The builder never
reads simulation folders, MDP files, trajectories or scheduler state. Folder
paths need not exist on the machine building the console. Displayed folders are
selectable text, including paths to remote locations.

Empty dataset, complex and repeat mappings are supported. Dataset and complex
display order follows the YAML; repeats are ordered numerically. The Run view
shows each shared ligand once, with `Shared` instead of a repeat number. Vanilla
convergence and production fields display `N/A`.
Every run has a Remark column in the Run view; remarks are searchable and sortable.

The builder rejects malformed YAML, duplicate keys, missing or misspelled fields,
invalid structures and choices, invalid repeats, and negative or nonfinite times.
Booleans are not accepted as numbers. Errors identify the field path; YAML syntax
and duplicate-key errors also identify the file and line. Field errors are
reported together where possible. A failed build leaves the last generated page
intact. “Website generated” is the build time, not a simulation observation time.

## Initial Deflorian inventory

The only seeded dataset is `deflorian-set1` (Deflorian OX2 set 1), from
`/biggin/b211/reub0138/Projects/orexin/deflorian_set_1_j13_v2_na`.

It contains 27 top-level `OX2_*` complexes, three repeats each, and one shared
alchemical ligand per complex: **189 records** in total. Supporting analysis,
setup and apo folders are excluded. Existing `vanilla_rep_N` folders and
`abfe_vanN_hrex_r1/complex` folders were recorded once during initial setup.
Missing vanilla paths are `null` for manual entry. Shared ligand paths point to
the matching `OX2_*/abfe_van1_hrex_r1/ligand` folders under
`/biggin/b211/reub0138/Projects/orexin/deflorian_set_1_j13_v1`.

**Every initial `Waiting` value is an unreviewed placeholder.** All alchemical
convergence values start as `unknown` and production times as `null`.
Directory existence was not used to infer progress, completion or convergence.
Review and edit the YAML to reflect the actual runs. Rebuilds do not rescan the
dataset or import new complexes automatically.

## Mark completed vanilla production

```bash
python3 utils/mark_vanilla_complete.py
```

The script reads this repository's `data.yaml` and prints dataset, complex,
repeat, folder and status for every vanilla entry. `missing` means the folder is
unset or `prod.xtc` is absent. With a trajectory present, `complete` means the
last step in `prod.log` exactly equals `nsteps` in `prod.mdp`; all other results
are `incomplete`, with a short reason. Production is assumed to start at step
zero. The script sets the status to `Complete` only for complete runs. Missing
and incomplete runs keep their existing status, including an existing `Complete`.
Other fields are preserved. It can be run from any directory by using its full
path; rebuild the website separately to display the updated statuses.

`utils/update_yaml.py` provides `update_yaml(path, document)` for saving changes.
Before replacing the YAML, it archives an exact copy of the previous file in
`archives/` beside it, with a UTC timestamp in the filename. The repository's
`archives/` folder is ignored by Git. Unchanged data creates no write or backup.
Changed files are serialized using PyYAML, which does not preserve YAML comments.

## Mark completed alchemical production

```bash
python3 utils/mark_alchemical_complete.py
python3 utils/mark_alchemical_complete.py --window vdw.00
```

The script checks each alchemical complex repeat and each shared ligand once.
It uses only the selected window, defaulting to `coul.00`; `--window` accepts
`rest.NN`, `coul.NN` or `vdw.NN`, with a two-digit window number. Within
`<folder>/<window>/prod/`, it uses each run's `production_files` mapping to find
the trajectory, log and MDP. For `coul.00`, `{stage}` becomes `coul` and `{number}`
becomes `00`; `--window vdw.01` changes these to `vdw` and `01`.

When provided, `production_files` must contain all three keys: `trajectory`,
`log` and `mdp`. Values must be nonempty filenames without directory paths.
Literal filenames and the `{stage}` and `{number}` placeholders are supported;
other placeholders and formatting expressions are rejected. Entries without
this mapping retain the defaults: `traj_comp.xtc`, `md.log` and
`{stage}.prod.{number}.mdp`. Deflorian ligand records instead configure
`{stage}.prod.{number}.xtc`, `{stage}.prod.{number}.log` and
`{stage}.prod.{number}.mdp`. Vanilla filenames remain fixed.

The checker compares the last valid log step with `nsteps` in the MDP. An exact
match is `complete`; this does not check other windows or convergence.
Production is assumed to start at step zero.

Output columns are dataset, complex, repeat (`Shared` for ligands), run type,
configured folder, window and result, separated by tabs. An unset folder or
absent trajectory is `missing`. Missing or invalid MDP/log files and step
mismatches are `incomplete`, with a short reason. Complete runs are set to
`Complete`, and runs reported as `missing` are set to `Attention required`.
Invalid filename configuration is also reported as `incomplete`, with a reason.
Incomplete runs keep their existing status. All other fields, including filename
configuration, are preserved. Saving uses the same YAML backup helper as the
vanilla checker. Unchanged data creates no write or backup. Unset ligand paths
are not discovered automatically. Rebuild the website separately to display
updated statuses.

## Development

The loader and validation live in `data.py`. `build.py` prepares the three views
and atomically replaces the generated page after rendering `template.html`.
Browser behaviour and styling live in `assets/`. No server application or
database is required.

```bash
python -m unittest discover -s tests -v
python -m pip install ruff
ruff format build.py data.py utils tests
```

Adapted console code retains the source project's GPL-3.0 licence. See `LICENSE`
and `NOTICE`; bundled font licences are retained in `assets/`.

