#!/usr/bin/env python3

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.merge_mcxn947_images import merge_images


def expect_failure(fn, text: str) -> None:
    try:
        fn()
    except ValueError as exc:
        assert text in str(exc), str(exc)
    else:
        raise AssertionError(f"expected ValueError containing {text!r}")


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        core0 = root / "core0.bin"
        core1 = root / "core1.bin"
        merged = root / "merged.bin"
        core0.write_bytes(b"ABC")
        core1.write_bytes(b"xy")

        merge_images(core0, core1, 8, merged, 16)
        assert merged.read_bytes() == b"ABC" + (b"\xff" * 5) + b"xy"

        core0.write_bytes(b"0" * 9)
        expect_failure(lambda: merge_images(core0, core1, 8, merged, 16), "overruns")

        core0.write_bytes(b"ABC")
        core1.write_bytes(b"1" * 9)
        expect_failure(lambda: merge_images(core0, core1, 8, merged, 16), "exceeds")

    print("merge_mcxn947_images_test: ok")


if __name__ == "__main__":
    main()
