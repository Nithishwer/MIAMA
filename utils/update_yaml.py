"""Save YAML changes after archiving an exact copy of the previous file."""

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml


def update_yaml(path, document):
    """Return the timestamped backup path, or None when the data is unchanged."""
    path = Path(path)
    original = path.read_bytes()
    if yaml.safe_load(original) == document:
        return None
    content = yaml.safe_dump(document, sort_keys=False)
    archives = path.parent / "archives"
    archives.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = archives / f"{path.stem}_{timestamp}{path.suffix}"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.chmod(path.stat().st_mode)
        # Never replace the source unless its original bytes were backed up.
        with backup.open("xb") as stream:
            stream.write(original)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return backup
