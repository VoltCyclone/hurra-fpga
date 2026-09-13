import pytest

from hurra_cynthion.crc import usb_crc5


# Fixed transmitted-bit vectors from the USB-IF white paper
# "Cyclic Redundancy Checks in USB", pp. 5-6:
# https://www.usb.org/sites/default/files/crcdes.pdf
@pytest.mark.parametrize(
    ("payload", "expected_crc"),
    [
        pytest.param(0x710, 0x05, id="sof-frame-710"),
        pytest.param(0x715, 0x1D, id="setup-address-15-endpoint-e"),
        pytest.param(0x53A, 0x07, id="out-address-3a-endpoint-a"),
        pytest.param(0x270, 0x0E, id="in-address-70-endpoint-4"),
        pytest.param(0x001, 0x1D, id="sof-frame-001"),
    ],
)
def test_usb_crc5_matches_usb_if_token_conformance_vectors(payload: int, expected_crc: int) -> None:
    assert usb_crc5(payload) == expected_crc


def reference_usb_crc5(payload: int) -> int:
    """Straightforward USB 2.0 CRC-5 reference, processed LSB first."""
    remainder = 0x1F
    for bit_number in range(11):
        feedback = ((payload >> bit_number) & 1) ^ (remainder & 1)
        remainder >>= 1
        if feedback:
            remainder ^= 0x14
    return remainder ^ 0x1F


def test_usb_crc5_is_correct_for_every_token_payload() -> None:
    for payload in range(1 << 11):
        assert usb_crc5(payload) == reference_usb_crc5(payload)


@pytest.mark.parametrize("payload", [-1, 1 << 11])
def test_usb_crc5_rejects_values_outside_the_token_payload(payload: int) -> None:
    with pytest.raises(ValueError, match="11-bit"):
        usb_crc5(payload)
