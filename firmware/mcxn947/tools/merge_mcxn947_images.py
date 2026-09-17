#!/usr/bin/env python3
"""Merge core0 and core1 raw binaries into one bounded flash image.

Usage: merge_mcxn947_images.py CORE0.bin CORE1.bin CORE1_OFFSET OUT.bin FLASH_LIMIT
"""

from __future__ import annotations

import pathlib
import sys


def merge_images(
    core0_path: pathlib.Path,
    core1_path: pathlib.Path,
    core1_offset: int,
    out_path: pathlib.Path,
    flash_limit: int,
) -> None:
    core0 = pathlib.Path(core0_path).read_bytes()
    core1 = pathlib.Path(core1_path).read_bytes()

    if len(core0) > core1_offset:
        raise ValueError(
            f"core0 image ({len(core0)} B) overruns core1 offset {core1_offset:#x}"
        )

    merged_size = core1_offset + len(core1)
    if merged_size > flash_limit:
        raise ValueError(
            f"merged image ({merged_size} B) exceeds flash limit {flash_limit:#x}"
        )

    image = bytearray(b"\xff" * core1_offset)
    image[: len(core0)] = core0
    image += core1
    pathlib.Path(out_path).write_bytes(image)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 5:
        raise SystemExit(__doc__)

    core0, core1, offset_text, output, limit_text = args
    try:
        offset = int(offset_text, 0)
        limit = int(limit_text, 0)
        merge_images(pathlib.Path(core0), pathlib.Path(core1), offset, pathlib.Path(output), limit)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    size = pathlib.Path(output).stat().st_size
    print(f"merged: core0 + core1 -> {output} ({size} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
