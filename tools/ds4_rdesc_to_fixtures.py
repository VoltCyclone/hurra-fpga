"""Convert a raw DS4 HID report descriptor capture into C and Python fixtures."""

import argparse
import re
from pathlib import Path


def parse_hex(text: str) -> bytes:
    """Keep pure hex-pair lines, including one line with no separators."""
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
    parser.add_argument("input", type=Path, help="raw descriptor text file")
    parser.add_argument("--length", type=int, default=507, help="expected byte length")
    args = parser.parse_args()

    data = parse_hex(args.input.read_text(encoding="utf-8"))
    assert len(data) == args.length, f"expected {args.length} bytes, got {len(data)}"

    c_rows = [
        "    " + " ".join(f"0x{byte:02X}," for byte in data[start : start + 12])
        for start in range(0, len(data), 12)
    ]
    args.input.with_name("ds4_c.txt").write_text("\n".join(c_rows) + "\n", encoding="utf-8")
    args.input.with_name("ds4_py.txt").write_text(repr(data) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
