"""The DS4 capture converter reads both captures the README describes."""

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "tools" / "ds4_rdesc_to_fixtures.py"
_SPEC = importlib.util.spec_from_file_location("ds4_rdesc_to_fixtures", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
parse_hex = _MODULE.parse_hex

IOREG = """\
+-o Wireless Controller  <class IOHIDDevice, id 0x1000, registered>
    {
      "ReportDescriptor" = <05010905a101850109300931>
      "VendorID" = 1356
    }
+-o Apple Internal Keyboard  <class IOHIDDevice>
    {
      "ReportDescriptor" = <05010906a101>
    }
"""


def test_ioreg_entry_is_chosen_by_length():
    assert parse_hex(IOREG, 12) == bytes.fromhex("05010905a101850109300931")
    assert parse_hex(IOREG, 6) == bytes.fromhex("05010906a101")


def test_ioreg_with_no_matching_length_names_what_it_found():
    with pytest.raises(SystemExit, match=r"2 ReportDescriptor entries \(12, 6 bytes\)"):
        parse_hex(IOREG, 507)


def test_single_ioreg_entry_needs_no_length():
    one = '"ReportDescriptor" = <05010905a101>\n'
    assert parse_hex(one) == bytes.fromhex("05010905a101")


def test_debugfs_rdesc_keeps_hex_lines_and_drops_the_listing():
    rdesc = "05 01 09 05 a1 01\n85 01\n\n  Usage Page (Generic Desktop)\n  Report ID (1)\n"
    assert parse_hex(rdesc, 8) == bytes.fromhex("05010905a1018501")


def test_unseparated_hex_run():
    assert parse_hex("05010905a101\n") == bytes.fromhex("05010905a101")
