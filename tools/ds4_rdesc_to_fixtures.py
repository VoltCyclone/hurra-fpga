"""Convert a raw DS4 HID report descriptor capture into C and Python fixtures.

Accepts either of the two captures the README describes: the hex dump from Linux
debugfs (``/sys/kernel/debug/hid/<device>/rdesc``, hex pairs then a decoded listing)
or saved ``ioreg -l -w0 -r -c IOHIDDevice`` output from macOS, where each device's
descriptor appears as ``"ReportDescriptor" = <0501...>``.
"""

import argparse
import re
from pathlib import Path

IOREG_DESCRIPTOR = re.compile(r'"ReportDescriptor"\s*=\s*<([0-9a-fA-F]+)>')


def parse_hex(text: str, length: int | None = None) -> bytes:
    """Return the one descriptor in ``text``.

    ``ioreg`` output wins when present: the ``ReportDescriptor`` whose length matches
    ``length`` is returned, or the only one when there is one. Otherwise the text is
    read as hex: pure hex-pair lines, or one long run of hex with no separators.
    """
    ioreg = [bytes.fromhex(match) for match in IOREG_DESCRIPTOR.findall(text)]
    if ioreg:
        if len(ioreg) == 1 and (length is None or len(ioreg[0]) == length):
            return ioreg[0]
        matches = [data for data in ioreg if len(data) == length]
        if len(matches) == 1:
            return matches[0]
        found = ", ".join(str(len(data)) for data in ioreg)
        want = f"{length} bytes" if length is not None else "a single entry"
        raise SystemExit(
            f"error: the ioreg output holds {len(ioreg)} ReportDescriptor entries "
            f"({found} bytes) and none matches {want}. Capture one device, for example "
            "ioreg -l -w0 -r -c IOHIDDevice -n 'Wireless Controller', or pass --length."
        )

    pairs: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if re.fullmatch(r"(?:[0-9a-fA-F]{2}(?:\s+|$))+", line):
            pairs.extend(re.findall(r"[0-9a-fA-F]{2}", line))
        elif len(line) > 2 and len(line) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", line):
            pairs.extend(line[index : index + 2] for index in range(0, len(line), 2))
    return bytes.fromhex(" ".join(pairs))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="saved rdesc or ioreg text")
    parser.add_argument("--length", type=int, default=507, help="expected byte length")
    args = parser.parse_args()

    data = parse_hex(args.input.read_text(encoding="utf-8"), args.length)
    if len(data) != args.length:
        raise SystemExit(f"error: expected {args.length} bytes, got {len(data)}")

    c_rows = [
        "    " + " ".join(f"0x{byte:02X}," for byte in data[start : start + 12])
        for start in range(0, len(data), 12)
    ]
    args.input.with_name("ds4_c.txt").write_text("\n".join(c_rows) + "\n", encoding="utf-8")
    args.input.with_name("ds4_py.txt").write_text(repr(data) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
