"""Interrupt-OUT capture cases shared by the Python mirror and the gateware.

One table, two parsers: ``test_descriptors.py`` runs every case through
``parse_mouse_configuration`` and ``test_enumerator.py`` through the real
enumerator, and both assert the outcome recorded here. The two cannot then
drift apart case by case, which is how the parsers disagreed before (see the
I4 and nine-byte-endpoint tests in ``test_enumerator.py``).

The rule under test: the FIRST HID interrupt-OUT endpoint of an alternate
setting 0 interface whose number is 1..15, max packet size 1..64 and interval
nonzero is captured. Any other HID interrupt-OUT endpoint -- out of bounds, or
a second one -- is ignored and flagged, never fatal. An OUT endpoint that is
not a candidate at all (non-HID interface, alternate setting, bulk) is skipped
silently, as before.
"""

from dataclasses import dataclass

from _ds4_fixture import DS4_CONFIG_DESCRIPTOR


@dataclass(frozen=True)
class OutCase:
    config: bytes
    #: (interface, report descriptor length) in the order the host fetches them.
    reports: tuple[tuple[int, int], ...]
    #: (interface, endpoint number, max packet size, interval), or None.
    out: tuple[int, int, int, int] | None
    ignored: bool


def endpoint(address: int, *, mps: int = 8, interval: int = 10, attributes: int = 3) -> bytes:
    return bytes([7, 5, address, attributes, mps & 0xFF, mps >> 8, interval])


def hid_interface(
    number: int, *endpoints: bytes, alt: int = 0, iface_class: int = 3, report_length: int = 52
) -> bytes:
    interface = bytes([9, 4, number, alt, len(endpoints), iface_class, 0, 0, 0])
    hid = (
        bytes([9, 0x21, 0x11, 0x01, 0, 1, 0x22, report_length & 0xFF, report_length >> 8])
        if iface_class == 3
        else b""
    )
    return interface + hid + b"".join(endpoints)


def configuration(interfaces: int, *parts: bytes) -> bytes:
    body = b"".join(parts)
    total = 9 + len(body)
    return bytes([9, 2, total & 0xFF, total >> 8, interfaces, 7, 0, 0x80, 50]) + body


def _one(*endpoints: bytes) -> bytes:
    """A single HID interface holding the mouse IN plus ``endpoints``."""
    return configuration(1, hid_interface(0, endpoint(0x81), *endpoints))


_MOUSE_REPORT = ((0, 52),)

OUT_CASES = {
    # The DualShock 4: EP 0x03 OUT, mps 64, bInterval 5, beside EP 0x84 IN.
    "ds4": OutCase(DS4_CONFIG_DESCRIPTOR, ((3, 507),), (3, 3, 64, 5), False),
    # A keyboard's LED endpoint, ahead of its own IN, on the second interface.
    "keyboard_led": OutCase(
        configuration(
            2,
            hid_interface(0, endpoint(0x81)),
            hid_interface(1, endpoint(0x02), endpoint(0x82), report_length=65),
        ),
        ((0, 52), (1, 65)),
        (1, 2, 8, 10),
        False,
    ),
    "out_before_in": OutCase(
        configuration(1, hid_interface(0, endpoint(0x02), endpoint(0x81))),
        _MOUSE_REPORT,
        (0, 2, 8, 10),
        False,
    ),
    # bLength 9 puts bSynchAddress last; bInterval is still offset 6.
    "nine_byte_form": OutCase(
        _one(bytes([9, 5, 0x02, 3, 8, 0, 10, 0, 7])), _MOUSE_REPORT, (0, 2, 8, 10), False
    ),
    "mps_64": OutCase(
        _one(endpoint(0x04, mps=64, interval=1)), _MOUSE_REPORT, (0, 4, 64, 1), False
    ),
    # Any number 1..15: the clone serves the OUT endpoint on a runtime number.
    "number_five": OutCase(_one(endpoint(0x05)), _MOUSE_REPORT, (0, 5, 8, 10), False),
    "number_fifteen": OutCase(_one(endpoint(0x0F)), _MOUSE_REPORT, (0, 15, 8, 10), False),
    # IN and OUT are different endpoints even with one number [USB2.0 9.6.6].
    "shares_the_in_number": OutCase(_one(endpoint(0x01)), _MOUSE_REPORT, (0, 1, 8, 10), False),
    # Out of bounds: ignored and flagged, enumeration unaffected.
    "endpoint_zero": OutCase(_one(endpoint(0x00)), _MOUSE_REPORT, None, True),
    "reserved_address_bits": OutCase(_one(endpoint(0x12)), _MOUSE_REPORT, None, True),
    "mps_65": OutCase(_one(endpoint(0x02, mps=65)), _MOUSE_REPORT, None, True),
    "mps_zero": OutCase(_one(endpoint(0x02, mps=0)), _MOUSE_REPORT, None, True),
    # High Speed additional-transaction bits make this 0x0840, not 64.
    "mps_high_bandwidth_bits": OutCase(_one(endpoint(0x02, mps=0x0840)), _MOUSE_REPORT, None, True),
    "interval_zero": OutCase(_one(endpoint(0x02, interval=0)), _MOUSE_REPORT, None, True),
    # Exactly one OUT is relayed: the first valid one wins.
    "second_out": OutCase(
        _one(endpoint(0x02), endpoint(0x03, mps=16)), _MOUSE_REPORT, (0, 2, 8, 10), True
    ),
    "invalid_then_valid": OutCase(
        _one(endpoint(0x05, interval=0), endpoint(0x02)), _MOUSE_REPORT, (0, 2, 8, 10), True
    ),
    # Not candidates at all: skipped without a flag.
    "non_hid_interface": OutCase(
        configuration(
            2, hid_interface(0, endpoint(0x81)), hid_interface(1, endpoint(0x02), iface_class=0xFF)
        ),
        _MOUSE_REPORT,
        None,
        False,
    ),
    "alternate_setting": OutCase(
        configuration(1, hid_interface(0, endpoint(0x81)), hid_interface(0, endpoint(0x02), alt=1)),
        _MOUSE_REPORT,
        None,
        False,
    ),
    "bulk_out": OutCase(_one(endpoint(0x02, attributes=2)), _MOUSE_REPORT, None, False),
    "no_out": OutCase(_one(), _MOUSE_REPORT, None, False),
}

#: A HID interface whose only endpoint is OUT: nothing to relay to the PC, so
#: enumeration must still refuse it exactly as it refuses no endpoint at all.
OUT_ONLY_CONFIGURATION = configuration(1, hid_interface(0, endpoint(0x02)))


@dataclass(frozen=True)
class InCase:
    config: bytes
    reports: tuple[tuple[int, int], ...]
    #: The captured IN endpoint numbers, in capture order; None when the
    #: device must be refused as UNSUPPORTED_TOPOLOGY.
    numbers: tuple[int, ...] | None


#: Interrupt-IN capture by endpoint NUMBER, shared the same way: numbers are
#: 1..15, the four relay slots bind theirs at runtime, and two captured IN
#: endpoints may not share one -- both would answer the PC's token.
IN_CASES = {
    "number_five": InCase(configuration(1, hid_interface(0, endpoint(0x85))), _MOUSE_REPORT, (5,)),
    "number_fifteen": InCase(
        configuration(1, hid_interface(0, endpoint(0x8F))), _MOUSE_REPORT, (15,)
    ),
    "four_high_numbers": InCase(
        configuration(
            4,
            *(hid_interface(k, endpoint(0x8C + k)) for k in range(4)),
        ),
        tuple((k, 52) for k in range(4)),
        (12, 13, 14, 15),
    ),
    "duplicate_in_one_interface": InCase(
        configuration(1, hid_interface(0, endpoint(0x81), endpoint(0x81))), _MOUSE_REPORT, None
    ),
    "duplicate_across_interfaces": InCase(
        configuration(2, hid_interface(0, endpoint(0x82)), hid_interface(1, endpoint(0x82))),
        ((0, 52), (1, 52)),
        None,
    ),
    # Only captured endpoints count: an alternate setting's copy is skipped.
    "same_number_in_an_alternate_setting": InCase(
        configuration(1, hid_interface(0, endpoint(0x81)), hid_interface(0, endpoint(0x81), alt=1)),
        _MOUSE_REPORT,
        (1,),
    ),
}
