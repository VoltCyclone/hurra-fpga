"""A raw-UTMI model of the real device on TARGET, for the interrupt-OUT relay tests.

One HID interface with an interrupt-IN endpoint it never has data for (every
poll is NAKed, so the IN path is live but quiet) and the interrupt-OUT endpoint
under test. It answers enumeration, records every OUT data packet the host
sends it, and answers each with the next handshake from ``out_script`` (ACK
once the script is empty). It also accepts any no-data control request after
enumeration, so a forwarded control transfer can be interleaved with OUTs --
or STALLs its status stage, for a (bmRequestType, bRequest) in
``stall_requests``.

It runs as a *background* testbench (``serve``), so the bench driving the PC
side or the host's OUT stream can run concurrently with bus traffic.
"""

from collections import deque
from dataclasses import dataclass

from test_end_to_end import (
    ACK,
    DATA0,
    DATA1,
    DEVICE_DESCRIPTOR,
    IN,
    NAK,
    OUT,
    SETUP,
    SOF,
    STALL,
    SetupRequest,
    data_packet,
    decode_data,
    decode_token,
    send_device_packet,
)


def hid_configuration(
    *,
    in_address: int,
    out_address: int,
    in_mps: int,
    out_mps: int,
    interval: int,
    report_length: int,
    configuration_value: int = 1,
) -> bytes:
    """One HID interface (protocol 0) carrying one interrupt IN and one interrupt OUT."""
    interface = bytes([9, 4, 0, 0, 2, 3, 0, 0, 0])
    hid = bytes([9, 0x21, 0x11, 0x01, 0, 1, 0x22, report_length & 0xFF, report_length >> 8])
    endpoint_in = bytes([7, 5, in_address, 3, in_mps, 0, interval])
    endpoint_out = bytes([7, 5, out_address, 3, out_mps, 0, interval])
    body = interface + hid + endpoint_in + endpoint_out
    total = 9 + len(body)
    header = bytes([9, 2, total & 0xFF, total >> 8, 1, configuration_value, 0, 0x80, 50])
    return header + body


@dataclass(frozen=True)
class OutPacket:
    """One interrupt-OUT data packet as it arrived on the TARGET wire."""

    address: int
    endpoint: int
    pid: int
    payload: bytes
    handshake: int


class OutRelayTarget:
    def __init__(
        self,
        *,
        configuration: bytes,
        report_descriptor: bytes,
        interface: int,
        in_endpoint: int,
        out_endpoint: int,
        device_descriptor: bytes = DEVICE_DESCRIPTOR,
    ) -> None:
        self.device_descriptor = device_descriptor
        self.configuration_descriptor = configuration
        self.report_descriptor = report_descriptor
        self.interface = interface
        self.in_endpoint = in_endpoint
        self.out_endpoint = out_endpoint
        self.ep0_mps = device_descriptor[7]

        self.sof_count = 0
        self.in_polls = 0
        self.out_packets: list[OutPacket] = []
        #: Handshakes for successive OUT data packets; ACK once exhausted.
        self.out_script: deque[int] = deque()
        #: No-data control requests other than SET_ADDRESS/SET_CONFIGURATION.
        self.control_requests: list[SetupRequest] = []
        #: (bmRequestType, bRequest) pairs whose status stage is STALLed.
        self.stall_requests: set[tuple[int, int]] = set()
        self.stalled_requests: list[SetupRequest] = []
        self.bus_reset()

    def bus_reset(self) -> None:
        self.address = 0
        self.configuration = 0
        self.setup: SetupRequest | None = None
        self.pending: tuple[int, int] | None = None
        self.awaiting_ack: str | None = None
        self.control_remaining: bytes | None = None
        self.control_toggle = DATA1

    def descriptor_bytes(self, setup: SetupRequest) -> bytes:
        descriptor_type = setup.value >> 8
        if descriptor_type == 1:
            descriptor = self.device_descriptor
        elif descriptor_type == 2:
            descriptor = self.configuration_descriptor
        elif descriptor_type == 0x22:
            assert setup.request_type == 0x81
            assert setup.index == self.interface
            descriptor = self.report_descriptor
        else:
            raise AssertionError(f"unexpected descriptor type {descriptor_type:#x}")
        return descriptor[: setup.length]

    async def handle_packet(self, ctx, host, packet: list[int]) -> None:
        pid = packet[0]
        if pid == SOF:
            decode_token(packet)
            self.sof_count += 1
            return

        if pid in (SETUP, OUT, IN):
            address, endpoint = decode_token(packet)
            assert address == self.address, f"token for address {address}, not {self.address}"
            if pid in (SETUP, OUT):
                self.pending = (pid, endpoint)
                return

            if endpoint == 0:
                assert self.setup is not None
                if self.setup.length:
                    if self.control_remaining is None:
                        self.control_remaining = self.descriptor_bytes(self.setup)
                        self.control_toggle = DATA1
                    chunk = self.control_remaining[: self.ep0_mps]
                    self.control_remaining = self.control_remaining[self.ep0_mps :]
                    data_pid = self.control_toggle
                    self.control_toggle = DATA0 if data_pid == DATA1 else DATA1
                    if not self.control_remaining:
                        self.control_remaining = None
                    self.awaiting_ack = "control-data"
                    await send_device_packet(ctx, host, data_packet(data_pid, chunk))
                elif (self.setup.request_type, self.setup.request) in self.stall_requests:
                    self.stalled_requests.append(self.setup)
                    self.setup = None
                    await send_device_packet(ctx, host, [STALL])
                else:
                    self.awaiting_ack = "control-status"
                    await send_device_packet(ctx, host, data_packet(DATA1, b""))
                return

            assert endpoint == self.in_endpoint, f"IN to unexpected endpoint {endpoint}"
            assert self.configuration != 0
            self.in_polls += 1
            await send_device_packet(ctx, host, [NAK])
            return

        if pid in (DATA0, DATA1):
            assert self.pending is not None, "DATA packet without a token"
            token_pid, endpoint = self.pending
            self.pending = None
            payload = decode_data(packet)
            if token_pid == OUT and endpoint == self.out_endpoint:
                assert self.configuration != 0
                handshake = self.out_script.popleft() if self.out_script else ACK
                self.out_packets.append(OutPacket(self.address, endpoint, pid, payload, handshake))
                await send_device_packet(ctx, host, [handshake])
                return

            assert endpoint == 0, f"OUT data to unexpected endpoint {endpoint}"
            if token_pid == SETUP:
                assert pid == DATA0
                assert len(payload) == 8
                self.setup = SetupRequest(
                    request_type=payload[0],
                    request=payload[1],
                    value=int.from_bytes(payload[2:4], "little"),
                    index=int.from_bytes(payload[4:6], "little"),
                    length=int.from_bytes(payload[6:8], "little"),
                )
                self.control_remaining = None
            else:
                assert pid == DATA1
                assert payload == b""
                assert self.setup is not None and self.setup.length
                self.setup = None
            await send_device_packet(ctx, host, [ACK])
            return

        assert packet == [ACK], f"unexpected packet {packet}"
        if self.awaiting_ack == "control-status":
            setup = self.setup
            assert setup is not None
            if setup.request_type == 0 and setup.request == 5:
                self.address = setup.value
            elif setup.request_type == 0 and setup.request == 9:
                self.configuration = setup.value
            else:
                self.control_requests.append(setup)
            self.setup = None
        self.awaiting_ack = None


async def serve(ctx, host, target: OutRelayTarget) -> None:
    """Answer every packet the host transmits, forever. Run as a background testbench."""
    utmi = host.utmi
    while True:
        while not ctx.get(utmi.tx_valid):
            await ctx.tick("usb")
        packet = []
        while ctx.get(utmi.tx_valid):
            packet.append(ctx.get(utmi.tx_data))
            await ctx.tick("usb")
        await target.handle_packet(ctx, host, packet)


async def wait_until(ctx, predicate, *, limit: int, what: str) -> None:
    for _ in range(limit):
        if predicate():
            return
        await ctx.tick("usb")
    raise AssertionError(f"timed out waiting for {what}")
