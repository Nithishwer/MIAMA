"""Build the static console without inspecting any simulation folders."""

import base64
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from data import RUN_TYPES, STATUSES, load_data

ROOT = Path(__file__).resolve().parent
STATUS_COLORS = {
    "Attention required": "#ff617c",
    "Complete": "#0ad2ff",
    "Waiting": "#ffd166",
}


def make_run(dataset_id, complex_id, kind, repeat, values):
    return dict(
        dataset=dataset_id,
        complex=complex_id,
        kind=kind,
        kind_label=dict(RUN_TYPES)[kind],
        repeat=repeat,
        folder=values["folder"],
        status=values["status"],
        convergence=values.get("convergence"),
        total_production_ns=values.get("total_production_ns"),
        completed_production_ns=values.get("completed_production_ns"),
    )


def prepare_views(document):
    datasets, runs = [], []
    for dataset_id, source in document["datasets"].items():
        compounds, dataset_runs = [], []
        for complex_id, compound in source["complexes"].items():
            groups = {kind: [] for kind, _ in RUN_TYPES}
            for repeat, repeat_runs in sorted(compound["repeats"].items()):
                for kind in ("vanilla", "alchemical_complex"):
                    groups[kind].append(
                        make_run(
                            dataset_id, complex_id, kind, repeat, repeat_runs[kind]
                        )
                    )
            groups["alchemical_ligand"].append(
                make_run(
                    dataset_id,
                    complex_id,
                    "alchemical_ligand",
                    None,
                    compound["alchemical_ligand"],
                )
            )
            compound_runs = [run for group in groups.values() for run in group]
            compounds.append(
                dict(
                    id=complex_id,
                    groups=groups,
                    counts={
                        kind: Counter(run["status"] for run in group)
                        for kind, group in groups.items()
                    },
                )
            )
            dataset_runs.extend(compound_runs)
        datasets.append(
            dict(
                id=dataset_id,
                label=source["label"],
                compounds=compounds,
                run_count=len(dataset_runs),
                counts=Counter(run["status"] for run in dataset_runs),
            )
        )
        runs.extend(dataset_runs)
    return datasets, runs


def render_page(document):
    datasets, runs = prepare_views(document)
    assets = ROOT / "assets"
    font_faces = []
    for family, weight, name in (
        ("Inter", 400, "Inter-Regular.ttf"),
        ("Inter", 600, "Inter-SemiBold.ttf"),
        ("JetBrains Mono", 600, "JetBrainsMono-SemiBold.ttf"),
    ):
        encoded = base64.b64encode((assets / name).read_bytes()).decode("ascii")
        font_faces.append(
            f"@font-face{{font-family:'{family}';font-weight:{weight};"
            f"font-display:swap;src:url(data:font/ttf;base64,{encoded}) format('truetype');}}"
        )
    environment = Environment(
        loader=FileSystemLoader(ROOT),
        autoescape=select_autoescape(("html",)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return environment.get_template("template.html").render(
        datasets=datasets,
        runs=runs,
        run_types=RUN_TYPES,
        statuses=STATUSES,
        status_colors=STATUS_COLORS,
        generated_at=datetime.now(timezone.utc).strftime("%d %b %Y · %H:%M UTC"),
        css="\n".join(font_faces) + "\n" + (assets / "console.css").read_text(),
        javascript=(assets / "console.js").read_text(),
        font_licenses="\n".join(
            (assets / name).read_text()
            for name in ("Inter-LICENSE.txt", "JetBrainsMono-LICENSE.txt")
        ),
    )


def build_site(source=ROOT / "data.yaml", output=ROOT / "site/index.html"):
    content = render_page(load_data(source))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # Replace only after validation and rendering have both succeeded.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output


def main():
    try:
        output = build_site()
    except (ValueError, OSError) as error:
        print(f"Build failed:\n{error}", file=sys.stderr)
        return 1
    print(f"Built {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
