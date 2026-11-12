"""Locate drops on disk."""
from __future__ import annotations

import re
from pathlib import Path


def natural_key(name: str) -> list:
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)]


def find_drops(root: Path) -> list[Path]:
    """`root` is either one drop, or a parent holding drop_* folders (returned in natural order)."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"not a directory: {root}")
    children = [p for p in root.iterdir() if p.is_dir() and p.name.startswith("drop_")]
    if children:
        return sorted(children, key=lambda p: natural_key(p.name))
    return [root]
