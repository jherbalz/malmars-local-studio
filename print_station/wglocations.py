"""Where the app keeps its files.

A frozen app (one file, unpacked to a temporary folder at start) has two
places: the bundle, which is read only, and the folder the app sits in,
which is where anything the app writes belongs. If that folder cannot be
written to - Program Files, for instance - the app falls back to the
user's own app data.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def bundle_dir() -> Path:
    """Where the read-only app files are."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def data_dir() -> Path:
    """Where the app keeps the things it writes."""
    configured = os.environ.get("PRINT_STATION_DATA")
    if not configured and os.environ.get("JL_STUDIO_DATA"):
        configured = str(Path(os.environ["JL_STUDIO_DATA"]) / "print-station")
    if not configured:
        studio_root = next(
            (parent for parent in Path(__file__).resolve().parents
             if (parent / "license_config.json").is_file()),
            None,
        )
        if studio_root:
            configured = str(studio_root / "data" / "print-station")
    if configured:
        target = Path(configured).resolve()
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return target
    base = Path(sys.executable).resolve().parent \
        if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return base
    except OSError:
        fallback = Path(os.environ.get("LOCALAPPDATA", "") or str(Path.home()))
        fallback = fallback / "Malmars" / "PrintStation"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
