"""DualShock 4 descriptor fixtures.

**These are SYNTHETIC**, built to the documented DS4 (VID 054c, PID 09cc)
topology rather than captured from hardware. They exercise exactly the three
things that made a DS4 unenumerable before phase A:

1. three USB-Audio interfaces alongside the HID one,
2. alternate settings 1 and 2 on the AudioStreaming interface,
3. nine-byte USB Audio 1.0 isochronous endpoint descriptors.

Replace with a real capture when a controller is on the bench -- this needs
no console, just a PC::

    lsusb -v -d 054c:09cc          # configuration descriptor
    # plus usbmon/Wireshark for the HID report descriptor

A real capture will differ in the audio class-specific descriptors (which
this fixture omits, since the parser skips unknown types anyway) and in the
exact report descriptor bytes. Neither affects what these tests assert.
"""

# Interface numbers follow the real device: 0-2 audio, 3 HID.
_AUDIO_CONTROL = bytes([9, 4, 0, 0, 0, 1, 1, 0, 0])

# AudioStreaming, three descriptions of ONE interface: alt 0 has no endpoint,
# alts 1 and 2 each carry a nine-byte isochronous endpoint descriptor
# (bLength 9 = the USB Audio 1.0 form, with bRefresh and bSynchAddress).
_AUDIO_STREAM_ALT0 = bytes([9, 4, 1, 0, 0, 1, 2, 0, 0])
_AUDIO_STREAM_ALT1 = bytes([9, 4, 1, 1, 1, 1, 2, 0, 0]) + bytes(
    [9, 5, 0x01, 0x09, 0x84, 0x01, 0x01, 0x00, 0x00]
)
_AUDIO_STREAM_ALT2 = bytes([9, 4, 2, 0, 0, 1, 2, 0, 0])
_AUDIO_STREAM_ALT2_ALT = bytes([9, 4, 2, 1, 1, 1, 2, 0, 0]) + bytes(
    [9, 5, 0x82, 0x0D, 0x22, 0x00, 0x01, 0x00, 0x00]
)

# The HID interface. bInterfaceProtocol is 0 -- NOT 2 -- which is precisely
# what the old boot-mouse gate rejected.
_HID_INTERFACE = bytes([9, 4, 3, 0, 2, 3, 0, 0, 0])
_HID_DESCRIPTOR = bytes([9, 0x21, 0x11, 0x01, 0x00, 0x01, 0x22, 0xFB, 0x01])
_HID_ENDPOINT_IN = bytes([7, 5, 0x84, 3, 64, 0, 5])
_HID_ENDPOINT_OUT = bytes([7, 5, 0x03, 3, 64, 0, 5])

_BODY = (
    _AUDIO_CONTROL
    + _AUDIO_STREAM_ALT0
    + _AUDIO_STREAM_ALT1
    + _AUDIO_STREAM_ALT2
    + _AUDIO_STREAM_ALT2_ALT
    + _HID_INTERFACE
    + _HID_DESCRIPTOR
    + _HID_ENDPOINT_IN
    + _HID_ENDPOINT_OUT
)

_TOTAL = 9 + len(_BODY)

#: bNumInterfaces is 4 -- the count of interfaces, not of descriptors. Five
#: interface descriptors appear above because two are alternate settings.
DS4_CONFIG_DESCRIPTOR = bytes([9, 2, _TOTAL & 0xFF, _TOTAL >> 8, 4, 1, 0, 0xC0, 0xFA]) + _BODY

#: The real DS4 report descriptor is 507 bytes (0x01FB, as declared above).
#: Byte-exact capture from the bench (docs/PAD_INJECTION.md section 4); until it
#: lands this is a zero placeholder of exactly the right size. Generate with
#: ``python3 tools/ds4_rdesc_to_fixtures.py <rdesc.txt>``, paste, flip the flag.
#: ``DS4_REPORT_DESCRIPTOR_CAPTURED`` gates the tests that read its contents.
#: It must stay identical to ``firmware/mcxn947/test/hid_fixtures.h``'s
#: ``HID_FIXTURE_DS4``; ``tests/test_mcu_compiled_map_e2e.py`` checks that.
DS4_REPORT_DESCRIPTOR_CAPTURED = False
DS4_REPORT_DESCRIPTOR = bytes(507)  # fill from capture: paste ds4_py.txt here
assert DS4_REPORT_DESCRIPTOR_CAPTURED == any(DS4_REPORT_DESCRIPTOR), (  # noqa: SIM300
    "flip DS4_REPORT_DESCRIPTOR_CAPTURED when the bytes land (and only then)"
)
