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
        experimental_dg: null
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
              convergence: unconverged
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

`experimental_dg` is optional at compound level: enter experimental binding ΔG
in kcal/mol as a finite number, or leave it `null` when unknown. Negative values
are valid. All shown run fields except `production_files` are required for alchemical records.
Vanilla records require `folder`, `status` and `remark`, and optionally include
`total_production_ns` and `completed_production_ns`.

| Field | Accepted values |
| --- | --- |
| `folder` | A nonempty folder path string, or `null` if unknown |
| `status` | Exactly `Attention required`, `Complete`, or `Waiting` |
| `remark` | Free-text note for any run; use `""` when blank |
| `production_files` | Optional alchemical-only mapping containing `trajectory`, `log` and `mdp` filenames |
| `convergence` | Exactly `unknown`, `unconverged`, or `converged` |
| `total_production_ns` | Intended production duration from the MDP, in ns; or `null` |
| `completed_production_ns` | Last logged production time, in ns; or `null` |
| `analysis_status` | Optional alchemical-only value: `Missing`, `Incomplete` or `Complete` |
| `analysis_remark` | Optional alchemical-only explanation, displayed alongside the manual remark |
| `total_analysed_ps` | Optional alchemical-only final timestamp used in analysis, in ps |

Times cover production only, excluding minimisation and equilibration. The
production status refresh scripts calculate the MDP duration as
`nsteps × dt / 1000` when `dt` is in ps, and completed time as the last logged
`Time / 1000`. Alchemical times
describe only the selected window (default: `coul.00`), rather than the minimum
across all windows. Zero means no production has completed; `null` means it has
not been recorded. An extension may make completed production exceed the
recorded total; this is allowed. Production is assumed to start at time zero.

**Status and convergence are independent fields.** Edit them manually, or use
the refresh scripts below to update run statuses and production times. The
builder never reads simulation folders, MDP files, trajectories or scheduler
state. Folder paths need not exist on the machine building the console.
Displayed folders are selectable text, including paths to remote locations.

Empty dataset, complex and repeat mappings are supported. Dataset and complex
display order follows the YAML; repeats are ordered numerically. The Run view
shows each shared ligand once, with `Shared` instead of a repeat number. Vanilla
convergence displays `N/A`; production times display their recorded values, or
`Unknown` when absent or `null`.
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

## Refresh vanilla status

```bash
python3 utils/refresh_vanilla_status.py
```

The script reads this repository's `data.yaml` and prints dataset, complex,
repeat, folder and status for every vanilla entry. `missing` means the folder is
unset or `prod.xtc` is absent. With a trajectory present, `complete` means the
last step in `prod.log` exactly equals `nsteps` in `prod.mdp`; all other results
are `incomplete`, with a short reason. Production is assumed to start at step
zero. The script sets the status to `Complete` only for complete runs. Missing
and incomplete runs keep their existing status, including an existing `Complete`.
The script also updates `total_production_ns` and `completed_production_ns` from
`prod.mdp` and `prod.log`, including for incomplete runs and runs without a
trajectory. If either time cannot be read, it reports the error and preserves
both recorded times. Unset folders are skipped. Other fields are preserved.
It can be run from any directory by using its full path; rebuild the website
separately to display updated statuses and times.

`utils/update_yaml.py` provides `update_yaml(path, document)` for saving changes.
Before replacing the YAML, it archives an exact copy of the previous file in
`archives/` beside it, with a UTC timestamp in the filename. The repository's
`archives/` folder is ignored by Git. Unchanged data creates no write or backup.
Changed files are serialized using PyYAML, which does not preserve YAML comments.

## Refresh alchemical status

```bash
python3 utils/refresh_alchemical_status.py
python3 utils/refresh_alchemical_status.py --window vdw.00
python3 utils/refresh_alchemical_status.py --dataset deflorian-set1
```

Use `-d` or `--dataset` to check and update only that dataset, skipping production
file reads for all others. Omit it to check all datasets. An unknown dataset ID
exits with an error. Dataset selection can be combined with `--window`.

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
Incomplete runs keep their existing status. Total and completed production times
are updated from the selected window's configured MDP and log, including for
incomplete runs and runs without a trajectory. If either time cannot be read,
the script reports the error and preserves both recorded times. Unset folders
and invalid filename configurations are skipped. Convergence and all other
fields, including filename configuration, are preserved. Saving uses the same
YAML backup helper as the vanilla checker. Unchanged data creates no write or
backup. Unset ligand paths are not discovered automatically. Rebuild the
website separately to display updated statuses and times.

## Track and run alchemical analysis

The Runs table has a separate **Analysis status** column. Complex rows describe
complex analysis; the shared ligand row describes ligand analysis. Vanilla rows
show `N/A`. Analysis status describes coverage independently of convergence;
refreshing analysis updates both fields without changing production status.
Older records without analysis fields display `Missing`, with remark
`Analysis status not checked`, until explicitly refreshed.

```bash
# Refresh statuses and recover analysis timestamps from retained copies:
python3 utils/refresh_analysis_status.py --dataset deflorian-set1

# Preview matching analyses without changing any files:
python3 utils/run_analysis.py --dataset deflorian-set1 --analysis-status Incomplete --dry-run

# Run matching analyses, compile results and save refreshed statuses:
python3 utils/run_analysis.py --dataset deflorian-set1 --analysis-status Incomplete --threads 8

# Rebuild the website to display recorded changes:
python3 build.py
```

`refresh_analysis_status.py` checks all datasets when `--dataset` is omitted.
It inspects and saves analysis fields without running calculations, compiling
results or deleting retained inputs.

Both status refresh and analysis execution assign `convergence` from each complex
or shared ligand's `ABFE_convergence.csv`. The unique `data_fraction = 0.5` row
compares MBAR estimates from the first and last halves of the analysed data.
An absolute difference between `Forward` and `Backward` strictly below
0.5 kcal/mol is `converged`; a difference at or above 0.5 is `unconverged`.
Missing, unreadable or invalid convergence results, including a missing or
duplicate half-data row, reset the label to `unknown`. Saved convergence is
assessed even when the summary is unavailable or newer simulation data makes
analysis status `Incomplete`; it describes the saved analysis, not current
coverage. Refresh replaces any previous convergence label.

`run_analysis.py` requires both `--dataset` and `--analysis-status`. Status choices
are exactly `Missing`, `Incomplete` and `Complete`. Selection uses the status saved
in `data.yaml`, with absent statuses counted as `Missing`; refresh first when you
need current statuses. By default both legs are eligible. Use `--leg ligand`
for shared solvent legs or `--leg complex` for complex repeats. This filter also
applies to `--dry-run`. Every matching component is analysed, including verified
Complete components. The runner executes immediately unless `--dry-run` is set.
An empty selection exits successfully without writing files. Dry runs print a
single table headed `Runs to be analysed:`, with dataset, compound, component,
repeat, saved analysis status, total and finished production times in ns, and
input readiness. Unknown times display `Unknown`; shared ligands use `Shared`.
Blocked inputs return a nonzero exit code. Preview does not compile results or
change files; use the refresh script to inspect current analysis statuses.

Paths come from `data.yaml`; relative paths are interpreted relative to that file.
The website builder never inspects simulation folders.

Each alchemical complex repeat and shared ligand can record one analysis time:

```yaml
analysis_status: Complete
analysis_remark: ""
total_analysed_ps: 20000.0
```

`total_analysed_ps` is the final simulation timestamp included across all selected
windows and stages, before equilibration removal and decorrelation. Zero is valid;
an absent or null value means unknown. Strings and booleans are rejected, and
vanilla records cannot use this field.

`Missing` means no component analysis outputs exist. `Incomplete` means outputs
are partial/invalid, coverage cannot be verified, or current window endpoints do
not match the analysed time. `Complete` requires valid summary and convergence
results and every selected current window ending at `total_analysed_ps`.
Unknown coverage has a `Data not verified` remark. The timestamp check reads only
the last 10 lines of each input, ignoring comments and incomplete or invalid
trailing records. It never infers the analysed time from current inputs alone.

All selected windows must end at the same time. Different endpoints identify the
windows in an error, preserve previous results and recorded analysis time, and
return a nonzero exit code in every mode. Neither script trims inputs or chooses
the minimum endpoint. Independent components continue processing.

Inputs are `<folder>/<window>/prod/dhdl.xvg`; outputs are in the sibling
`analysis/complex` or `analysis/ligand` folder. Existing analysis symlinks are
followed. Coverage checks compare current input endpoints with the saved analysis
time, independently of the window selected when refreshing production status.
Expected complex windows are `rest.00`–`rest.10`, `coul.00`–`coul.09` and
`vdw.00`–`vdw.20`; ligand windows are
`coul.00`–`coul.10` and `vdw.00`–`vdw.20`. Missing inputs and unexpected layouts
block calculation rather than silently omitting windows. Complex `rest.11` and
`coul.10` folders are allowed when their XVG lambda coordinates match `coul.00`
and `vdw.00`, respectively. Those duplicate boundary states are omitted, matching
the reference script's input selection. Other extra windows still block analysis.
Unfinished trailing input records are ignored. Equilibration removal and
decorrelation are intentional analysis filtering, not missing coverage.

Status refresh and analysis execution print `Checking` before each component.
Coverage checks seek directly to the end of each input and compare the final valid
timestamps from its last 10 lines. They do not count samples or detect differences
earlier in the files, including extra samples within the same time range.
Component analyses run one at a time. `--threads` sets alchemlyb's `n_jobs` for
reading and preprocessing; its default of `-1` uses all available CPUs. Use a
positive integer to limit those workers. Estimation and convergence retain their
existing settings because those steps do not support `n_jobs`. Input snapshots
retain full numeric validation before calculation.

Install the analysis dependencies in your environment before running calculations.

```bash
python3 -m pip install -r requirements-analysis.txt
```

PyMBAR uses the official `4.2.0` GitHub release because PyPI still provides
`4.0.3`, which has a subsampling index error fixed in
[PyMBAR #558](https://github.com/choderalab/pymbar/pull/558).
`requirements-analysis.txt` selects this release.

Preview, status refresh and website building do not import those dependencies.
Shared output folders are analysed once per invocation. Only components matching
the dataset, saved analysis status and optional leg filters are selected; inspecting a different
live status does not change the selection. Production need not have finished,
and there is no 18 ns cutoff.
Insufficient usable samples are reported as a calculation failure.

The alchemlyb settings match the reference scripts: GROMACS, 298 K, kcal/mol,
MBAR/BAR/TI, `skiptime=10` (ps), `uncorr="dhdl"`, threshold 50, 50 bootstraps,
10 forward/reverse fractions, and overlap and breakdown plots.
The script uses the estimator API to apply 50 MBAR bootstraps to both the summary
and convergence estimates: alchemlyb's automatic `run()` ignores that keyword.
This can change MBAR error estimates compared with the older scripts.
JAX is restricted to CPU. Each calculation uses fresh, frozen input copies in a
separate temporary directory. Previous results are retained if analysis or output
validation fails, including when the frozen windows have unequal endpoints.
Successful results replace the component directory and return their common
endpoint. The runner saves `total_analysed_ps` and refreshed statuses through the
YAML backup helper before deleting only that calculation's input copies.
Plots and result CSVs remain. If YAML saving fails or the inventory was
edited during calculation, those published copies remain for recovery. A cleanup
failure is reported with a nonzero exit code; valid results and the saved time
remain. If production advances during analysis, the analysed time is retained and
the refreshed status is Incomplete; there is no immediate rerun.

Existing retained input copies can recover `total_analysed_ps` without calculation
when every required copy is readable, all endpoints agree, outputs validate, and
no copy is newer than the results. A valid retained snapshot takes precedence over
the saved value, recovering results published before a failed YAML save. Unequal
live endpoints preserve the saved value until corrected. If recovery evidence is
incomplete or unreliable, the saved value is used instead.

To migrate existing analyses, run `refresh_analysis_status.py`, check the saved
timestamps and remarks in `data.yaml`, and rebuild with `python3 build.py`.
Preview and status refresh never delete retained copies. After confirming that
a component has the correct saved `total_analysed_ps`, manually remove only its legacy
`dhdl.<window>.xvg` files from `analysis/complex` or `analysis/ligand`. Keep its
plots, CSVs and original `<folder>/<window>/prod/dhdl.xvg` simulation inputs.
Do not remove recovery copies before their timestamp has been saved successfully.

Once component outputs are usable, the runner creates or updates each affected
complex repeat's `analysis/abfe_results.csv`, using its configured shared ligand
summary and its own `boresch/resgen_output.txt`. A selected complex affects its
own combined CSV; a selected ligand affects all its complex repeats' combined
CSVs. Existing counterpart summaries are used without analysing unselected
components. Missing counterpart folders or unusable summaries block compilation.
The calculation preserves the reference behavior, including its linear sum of
estimator errors and its extraction of the second comma-separated correction
(`dG_on`) from the `dG_off` line. Missing restraint inputs or compilation errors
do not change component analysis status.

Independent calculations continue after failures. The runner returns a nonzero
exit code when selected work is blocked or fails, and still saves inspected
statuses. The runner saves YAML using the existing backup helper, and only
selected records have their analysis fields refreshed. Generated
`analysis_remark` and convergence values are refreshed without overwriting manual
`remark` values or production fields. Status refresh is a snapshot; run it again after
additional simulation data arrives. No-change refreshes create no backup.

## Plot ABFE results against experiment

With the MIAMA environment activated:

```bash
python utils/plot_fep_results.py
python utils/plot_fep_results.py --dataset yuan-dor-c1 --method MBAR
python utils/plot_fep_results.py --dataset yuan-dor-c2 --method TI --output-dir plots
python utils/plot_fep_results.py --dataset yuan-dor-c1 --collect
```

The utility prints an aligned table with `Compound`, `Repeat`, `Leg`, and
`Status` columns. Legs are `complex`, `ligand`, or `boresch`; statuses are
`present`, `created`, or `error`. By default, `complex` rows describe existing
`analysis/abfe_results.csv` files; missing files show `error` and are skipped.
Use `--collect` to rebuild and overwrite every selected repeat's
CSV before plotting the new results, including identical CSVs. Compilation uses
that repeat's complex analysis, its configured shared ligand analysis, and its
`boresch/resgen_output.txt` restraint correction. Collection reports each validated
leg as `present`, or the analytical Boresch fallback as `created`. Failed legs
show `error`. A concise `Failures:` summary follows the table, listing each failed
compound, repeat and leg with its path and reason. Unset folders show `<unset>`.
Successful paths, correction values, skip messages, and save messages are omitted.
Component analyses need valid `ABFE_summary.csv` and `ABFE_convergence.csv` files.

If `boresch/resgen_output.txt` is absent, compilation estimates the analytical
harmonic Boresch correction from `boresch/BoreschRestraint.top`, or its copy at
`complex/BoreschRestraint.top` when the first topology is absent. It uses the
saved B-state bond, angle and dihedral parameters, converting angle references
from degrees to radians. Temperature comes from `ref-t` (or `ref_t`) in
`complex/rest.00/prod/rest.prod.00.mdp`; all thermostat groups must share one
finite positive temperature. The release formula uses R = 0.008314472 kJ/mol/K
and V° = 1.66 nm³, then divides by 4.184 for kcal/mol
([Boresch, 2024](https://pmc.ncbi.nlm.nih.gov/articles/PMC11094717/)).
MIAMA's `anal_rest` uses **minus the release correction**, matching its existing
`dG_on` convention, with zero estimator error. The fallback shows `created` and does
not generate a replacement Boresch output or rerun simulation or restraint fitting.
Missing or invalid fallback inputs cause compilation to fail without replacing
the previous CSV. Existing Boresch output takes precedence; invalid existing
output is reported rather than replaced by an estimate.

The plot uses the unique `total` row. It defaults to all datasets and MBAR;
`--method` also accepts BAR and TI. Invalid CSVs and compilation failures are
reported and skipped, processing continues, and the command returns a nonzero
exit code. Failed collection preserves the previous CSV and excludes its stale
values from that invocation's plot. Existing malformed CSVs are only rebuilt
with `--collect`. Saved analysis and convergence statuses do not filter results.

Enter each compound's `experimental_dg` in `data.yaml` to include it in a plot.
With `--collect`, compilation also runs when experimental values are unset, but
those compounds are excluded from plots. Each compound is plotted
as the mean of its available valid repeats, with sample SD as the vertical error
bar. A single available repeat has no vertical error bar. Horizontal error bars
are fixed at ±1 kcal/mol to match the reference figure; they are an assumed
uncertainty, not measured experimental errors. The square plots include the
identity line, ±1/2/5 kcal/mol shaded bands, MUE, RMSE, Pearson R and Spearman ρ.
Undefined correlations display `N/A`.

Each dataset with plottable compounds produces `plots/<dataset>_<method>.png`,
or writes into `--output-dir` when supplied. Datasets without points are skipped.
Compilation combines existing leg-analysis outputs; it does not rerun component
analysis, change the inventory, or open a GUI.

## Create a simulation campaign

```bash
python3 utils/make_simulation_campaign.py \
  --dataset deflorian-set1 --stage alchemical_complex --cluster ranv \
  --status "attention required"
```

Use a dataset ID from `data.yaml`. Stages are `alchemical_complex`,
`alchemical_ligand` and `vanilla`; clusters are `ranv`, `soma` and `archer`.
The required `--status` accepts `waiting` or `"attention required"`,
case-insensitively. Only runs matching the dataset, stage and status are selected:
each matching vanilla or complex repeat, or each shared ligand once.
The script creates a folder such as
`campaigns/deflorian-set1_alc-complex_20261026/` under this repository,
regardless of the current working directory. Dataset names are shortened to
32 characters for folder names. Existing names receive a numeric suffix
(`_2`, `_3`, etc.). The entire `campaigns/` directory is ignored by Git.

Each folder contains one `campaign.yaml` with the full dataset ID, stage,
cluster, an ISO creation timestamp in Europe/London time, the canonical status
(`Waiting` or `Attention required`) and a `runs` list. Each run records its
`compound`, `repeat`, `source`, `destination`, `total_production_ns` and
`completed_production_ns`; shared ligands use `repeat: null`.
The source is the configured `folder` in `data.yaml`. Destinations use these bases:

| Cluster | Base path |
| --- | --- |
| `ranv`, `soma` | `/home/reub0138` |
| `archer` | `/work/e280/e280-Biggin/shared/reub0138` |

An alchemical destination is
`<base>/<campaign>/<compound>/<abfe-run>/complex` or ends in `/ligand`,
preserving the source's run and stage folder names. Vanilla destinations use
`<base>/<campaign>/<compound>/<vanilla-run>`. The campaign component includes
any numeric suffix added to avoid an existing folder.

By default, production times are read directly from the source files when
creating the campaign. Intended time is `nsteps × dt / 1000` from the MDP, and
completed time is the last valid `Time` beneath a `Step Time` header in the log,
divided by 1000. Both values are in ns and assume production starts at time zero. Log
footers and truncated final records are ignored. Completed time may exceed the
MDP target; zero is recorded as zero.

Vanilla runs use `prod.mdp` and `prod.log` in the source folder. Alchemical runs
use one window, defaulting to `coul.00`, under `<source>/<window>/prod/`, with
each run's `production_files` filenames and the production status refresh
script's defaults.
Use `--window vdw.01` to select a different window; the selected window is saved
in the campaign YAML. These times describe that window only. The option has no
effect on vanilla runs. No trajectory file is required for reading progress.

To create a campaign without checking existing production progress, add
`--skip-progress-check`:

```bash
python3 utils/make_simulation_campaign.py \
  --dataset yuan-dor-c1 --stage alchemical_ligand --cluster soma \
  --status "Attention required" --skip-progress-check
```

This includes all runs matching the dataset, stage and status filters without
reading or validating their MDP/log files, regardless of whether those files
exist or contain valid progress. Both production time fields are saved as `null`,
and the campaign YAML records `skip_progress_check: true`. Source paths must
still be set.
This flag only affects campaign metadata; it does not reset simulation files.

Without `--skip-progress-check`, if any selected source is unset, or its MDP/log
is missing, unreadable or invalid, the script reports the affected compounds and
repeats and creates no campaign. An empty selection also creates no campaign. Reading progress
requires simulation files to be readable locally. This creates metadata only;
simulation folders are not copied or submitted, and `data.yaml` is unchanged.

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

