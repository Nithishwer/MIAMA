"""Plot existing ABFE results, optionally collecting them from leg analyses."""

import argparse
import csv
import math
import sys
from pathlib import Path
from statistics import mean, stdev

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data import load_data
from utils.run_analysis import compile_results

ROOT = Path(__file__).resolve().parents[1]


def print_status(compound, repeat, leg, status):
    """Print one compact status row without filesystem paths."""
    print(f"{compound:<18}{repeat!s:<8}{leg:<10}{status}")


def failure_message(compound, repeat, leg, path, error):
    """Keep the actual failed path and a single-line reason, without a traceback."""
    if isinstance(error, OSError):
        detail = f"{error.filename or path or '<unset>'}: {error.strerror or error}"
    else:
        reason = str(error)
        detail = reason if reason.startswith("/") else f"{path or '<unset>'}: {reason}"
    return f"{compound} {repeat} {leg}: {' '.join(detail.splitlines())}"


def read_total(path, method):
    """Read the selected estimator from exactly one total row."""
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if not {"state", method}.issubset(reader.fieldnames or []):
            raise ValueError(f"required columns: state, {method}")
        totals = [row[method] for row in reader if row["state"] == "total"]
    if len(totals) != 1:
        raise ValueError("expected exactly one total row")
    try:
        value = float(totals[0])
    except (TypeError, ValueError):
        raise ValueError(f"invalid {method} total") from None
    if not math.isfinite(value):
        raise ValueError(f"nonfinite {method} total")
    return value


def collect_results(dataset, data_directory, method, collect=False, failures=None):
    """Group existing estimates by compound, optionally rebuilding every CSV."""
    points = []
    failed = False
    if failures is None:
        failures = []
    for compound_id, compound in dataset["complexes"].items():
        values = []
        for repeat, runs in sorted(compound["repeats"].items()):
            folder = runs["alchemical_complex"]["folder"]
            path = (
                (data_directory / folder).parent / "analysis/abfe_results.csv"
                if folder
                else None
            )
            found = path is not None and path.is_file()
            if not collect and not found:
                print_status(compound_id, repeat, "complex", "error")
                reason = "result CSV not found" if path else "folder is not configured"
                failures.append(
                    failure_message(compound_id, repeat, "complex", path, reason)
                )
                continue
            ligand_folder = compound["alchemical_ligand"]["folder"]
            if collect and (not folder or not ligand_folder):
                leg = "complex" if not folder else "ligand"
                print_status(compound_id, repeat, leg, "error")
                failures.append(
                    failure_message(
                        compound_id, repeat, leg, None, "folder is not configured"
                    )
                )
                failed = True
                continue
            statuses = {} if collect else {"complex": "present"}
            try:
                if collect:
                    compile_results(
                        data_directory / folder,
                        data_directory / ligand_folder,
                        force=True,
                        report_status=statuses.__setitem__,
                    )
                values.append(read_total(path, method))
            except (OSError, UnicodeError, ValueError, csv.Error) as error:
                leg = "complex"
                error_path = path
                if collect:
                    # The first unreported leg is the input that failed validation.
                    leg = next(
                        (
                            leg
                            for leg in ("complex", "ligand", "boresch")
                            if leg not in statuses
                        ),
                        "complex",
                    )
                    if leg not in statuses:
                        complex_folder = data_directory / folder
                        error_path = {
                            "complex": complex_folder,
                            "ligand": data_directory / ligand_folder,
                            "boresch": complex_folder.parent / "boresch",
                        }[leg]
                statuses[leg] = "error"
                failures.append(
                    failure_message(compound_id, repeat, leg, error_path, error)
                )
                failed = True
            for leg, status in statuses.items():
                print_status(compound_id, repeat, leg, status)

        experimental = compound.get("experimental_dg")
        if experimental is not None and values:
            # Repeat variability is separate from each estimator's reported error.
            error = stdev(values) if len(values) > 1 else None
            points.append((experimental, mean(values), error))
    return points, failed


def plot_results(points, label, method, output):
    """Save a square comparison plot with repeat SD and reference-style statistics."""
    # Load plotting dependencies only when there are points to draw.
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    from scipy.stats import pearsonr, spearmanr

    experimental, predicted, errors = zip(*points)
    padding = max(3.0, max(error or 0 for error in errors))
    limits = [
        min(*experimental, *predicted) - padding,
        max(*experimental, *predicted) + padding,
    ]
    figure, axis = plt.subplots(figsize=(4, 4))
    for width, alpha in ((5, 0.07), (2, 0.175), (1, 0.28)):
        axis.fill_between(
            limits,
            [value - width for value in limits],
            [value + width for value in limits],
            color="orange",
            alpha=alpha,
        )
    axis.plot(limits, limits, color="gray", linestyle="--", linewidth=1)
    for x, y, error in points:
        axis.errorbar(
            x,
            y,
            xerr=1.0,
            yerr=error,
            fmt="o",
            color="black",
            markersize=5,
            elinewidth=1.2,
        )

    mue = mean(abs(y - x) for x, y in zip(experimental, predicted))
    rmse = math.sqrt(mean((y - x) ** 2 for x, y in zip(experimental, predicted)))
    pearson = spearman = "N/A"
    if len(set(experimental)) > 1 and len(set(predicted)) > 1:
        pearson = f"{pearsonr(experimental, predicted).statistic:.2f}"
        spearman = f"{spearmanr(experimental, predicted).statistic:.2f}"
    axis.text(
        0.05,
        0.95,
        f"MUE = {mue:.2f}\nRMSE = {rmse:.2f}\n"
        f"Pearson R = {pearson}\nSpearman ρ = {spearman}",
        transform=axis.transAxes,
        va="top",
        fontsize=10,
    )
    axis.set(
        xlim=limits,
        ylim=limits,
        xlabel="Experimental ΔG (kcal/mol)",
        ylabel="Predicted ΔG (kcal/mol)",
        title=f"{label} ({method})",
        aspect="equal",
    )
    figure.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=300)
    plt.close(figure)


def main(argv=None, data_path=ROOT / "data.yaml"):
    """Plot inventory datasets, collecting new results only when requested."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", help="dataset ID; defaults to all datasets")
    parser.add_argument("--method", choices=("MBAR", "BAR", "TI"), default="MBAR")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "plots")
    parser.add_argument(
        "--collect",
        action="store_true",
        help="rebuild and overwrite every selected CSV from leg analyses before plotting",
    )
    args = parser.parse_args(argv)
    data_path = Path(data_path).resolve()
    try:
        document = load_data(data_path)
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(1, f"Cannot load {data_path}: {error}\n")
    datasets = document["datasets"]
    if args.dataset:
        if args.dataset not in datasets:
            parser.error(
                f"unknown dataset {args.dataset!r}; choose from: {', '.join(datasets)}"
            )
        datasets = {args.dataset: datasets[args.dataset]}

    failed = False
    failures = []
    print_status("Compound", "Repeat", "Leg", "Status")
    for dataset_id, dataset in datasets.items():
        points, invalid = collect_results(
            dataset,
            data_path.parent,
            args.method,
            collect=args.collect,
            failures=failures,
        )
        failed = failed or invalid
        if not points:
            continue
        output = args.output_dir / f"{dataset_id}_{args.method}.png"
        plot_results(points, dataset["label"], args.method, output)
    if failures:
        print("\nFailures:")
        print("\n".join(failures))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
