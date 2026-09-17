#!/usr/bin/env python3
"""Shared filesystem helpers (read/write/copy).

Single home for the small helpers previously duplicated between
``start.py`` and ``txgen.py`` so the CLI modules stay thin.
"""

from __future__ import annotations

import glob
import os
import shutil
from typing import List


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def edit(path: str, text: str) -> None:
    write(path, text)


def copy(src: str, dst: str) -> None:
    """Copy ``src`` to ``dst``, raising a clear error when missing."""
    # Local import: keeps this module importable without pulling the
    # whole daemon layer into every importer at module load time, and
    # avoids a top-level cycle (services -> files -> proc is fine, but
    # files must not force proc on importers that only need file I/O).
    import proc

    if not os.path.isfile(src):
        raise proc.DaemonError("required file not found: %s" % src)
    shutil.copy2(src, dst)


def copy_glob(pattern: str, dst_dir: str) -> List[str]:
    """Copy every file matching ``pattern`` into ``dst_dir``."""
    import proc

    matches = sorted(glob.glob(pattern))
    if not matches:
        raise proc.DaemonError("nothing matched %s" % pattern)
    for path in matches:
        copy(path, os.path.join(dst_dir, os.path.basename(path)))
    return matches
