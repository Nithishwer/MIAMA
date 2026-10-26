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
            alchemical_complex:
              folder: /path/to/abfe_van1_hrex_r1/complex
              status: Waiting
              convergence: not_converged
              total_production_ns: 20
              completed_production_ns: 12.5
        alchemical_ligand:
          folder: null
          status: Waiting
          convergence: unknown
          total_production_ns: null
          completed_production_ns: null
```

All shown fields are required. Vanilla records have only `folder` and `status`.

| Field | Accepted values |
| --- | --- |
| `folder` | A nonempty folder path string, or `null` if unknown |
| `status` | Exactly `Attention required`, `Complete`, or `Waiting` |
| `convergence` | Exactly `unknown`, `not_converged`, or `converged` |
| `total_production_ns` | Intended production duration per window from the MDP, in ns; or `null` |
| `completed_production_ns` | Minimum completed production across all expected lambda windows, in ns; or `null` |

Times cover production only, excluding minimisation and equilibration. Enter the
MDP duration manually (`nsteps × dt / 1000` when `dt` is in ps). A completed time
of 10 means every expected window has reached at least 10 ns. Zero means no
production has completed; `null` means it has not been recorded. An extension
may make completed production exceed the recorded total; this is allowed.

**Status and convergence are independent manual fields.** The builder never
reads simulation folders, MDP files, trajectories or scheduler state. Folder
paths need not exist on the machine building the console. Displayed folders are
selectable text, including paths to remote locations.

Empty dataset, complex and repeat mappings are supported. Dataset and complex
display order follows the YAML; repeats are ordered numerically. The Run view
shows each shared ligand once, with `Shared` instead of a repeat number. Vanilla
convergence and production fields display `N/A`.

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
Missing vanilla paths and all shared ligand paths are `null` for manual entry.

**Every initial `Waiting` value is an unreviewed placeholder.** All alchemical
convergence values start as `unknown` and production times as `null`.
Directory existence was not used to infer progress, completion or convergence.
Review and edit the YAML to reflect the actual runs. Rebuilds do not rescan the
dataset or import new complexes automatically.

## Development

The loader and validation live in `data.py`. `build.py` prepares the three views
and atomically replaces the generated page after rendering `template.html`.
Browser behaviour and styling live in `assets/`. No server application or
database is required.

```bash
python -m unittest discover -s tests -v
python -m pip install ruff
ruff format build.py data.py tests
```

Adapted console code retains the source project's GPL-3.0 licence. See `LICENSE`
and `NOTICE`; bundled font licences are retained in `assets/`.

