"""Stable public status and error values used by the host pipeline."""

from enum import IntEnum


class TransactionStatus(IntEnum):
    """Terminal result of one USB transaction."""

    SUCCESS = 0
    NAK = 1
    STALL = 2
    TIMEOUT = 3
    CRC_ERROR = 4
    OVERFLOW = 5
    DISCONNECTED = 6


class HostError(IntEnum):
    """Stable error exposed by the bounded mouse host."""

    NONE = 0
    UNSUPPORTED_SPEED = 1
    UNSUPPORTED_TOPOLOGY = 2
    MALFORMED_DESCRIPTOR = 3
    OVERSIZED_DESCRIPTOR = 4
    CONTROL_FAILURE = 5
    POLLING_FAILURE = 6
    DISCONNECTED = 7
