"""USB token CRC helpers."""


def usb_crc5(payload: int) -> int:
    """Return the transmitted USB CRC-5 for one 11-bit token payload."""
    if not 0 <= payload < (1 << 11):
        raise ValueError("USB token payload must be an 11-bit unsigned value")

    remainder = 0x1F
    for bit_number in range(11):
        feedback = ((payload >> bit_number) & 1) ^ (remainder & 1)
        remainder >>= 1
        if feedback:
            remainder ^= 0x14
    return remainder ^ 0x1F
