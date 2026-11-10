"""Refresh recorded analysis statuses and recover supported analysis timestamps."""

import argparse
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data import load_data
from utils.analysis_utils import (
    refresh_entries,
    save_analysis_statuses,
    select_entries,
)

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None, data_path=ROOT / "data.yaml"):
    """Inspect selected datasets and safely save their analysis statuses."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", help="dataset ID; defaults to all datasets")
    args = parser.parse_args(argv)
    data_path = Path(data_path).resolve()
    try:
        original_data = data_path.read_bytes()
        document = load_data(data_path)
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(1, f"Cannot load {data_path}: {error}\n")
    if args.dataset and args.dataset not in document["datasets"]:
        parser.error(
            f"unknown dataset {args.dataset!r}; choose from: {', '.join(document['datasets'])}"
        )
    entries = select_entries(document, args.dataset, data_path.parent)
    inspection_failed = refresh_entries(entries)
    try:
        save_analysis_statuses(data_path, document, original_data)
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(1, f"Cannot update {data_path}: {error}\n")
    return int(inspection_failed)


if __name__ == "__main__":
    raise SystemExit(main())
