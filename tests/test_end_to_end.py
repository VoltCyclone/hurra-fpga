from dataclasses import dataclass, replace

from amaranth.sim import Simulator

from hurra_cynthion.host import BoundedMouseHost
from hurra_cynthion.timing import HostTiming
from hurra_cynthion.types import HostError

OUT = 0xE1
IN = 0x69
SOF = 0xA5
SETUP = 0x2D
DATA0 = 0xC3
DATA1 = 0x4B
ACK = 0xD2
NAK = 0x5A
STALL = 0x1E

DEVICE_DESCRIPTOR = bytes([18, 1, 0, 2, 0, 0, 0, 64, 0x09, 0x12, 1, 0, 0, 1, 0, 0, 0, 1])
REPORT_DESCRIPTOR = bytes(
    [
        0x05,
        0x01,
        0x09,
        0x02,
        0xA1,
        0x01,
        0x09,
        0x01,
        0xA1,
        0x00,
        0x05,
        0x09,
        0x19,
        0x01,
        0x29,
        0x03,
        0x15,
        0x00,
        0x25,
        0x01,
        0x95,
        0x03,
        0x75,
        0x01,
        0x81,
        0x02,
        0x95,
        0x01,
        0x75,
        0x05,
        0x81,
        0x01,
        0x05,
        0x01,
        0x09,
        0x30,
        0x09,
        0x31,
        0x15,
        0x81,
        0x25,
        0x7F,
        0x75,
        0x08,
        0x95,
        0x02,
        0x81,
        0x06,
        0xC0,
        0xC0,
    ]
)

_INTERFACE = bytes([9, 4, 2, 0, 1, 3, 1, 2, 0])
_HID = bytes(
    [
        9,
        0x21,
        0x11,
        0x01,
        0,
        1,
        0x22,
        len(REPORT_DESCRIPTOR) & 0xFF,
        len(REPORT_DESCRIPTOR) >> 8,
    ]
)
_ENDPOINT = bytes([7, 5, 0x83, 3, 8, 0, 1])
_CONFIGURATION_LENGTH = 9 + len(_INTERFACE) + len(_HID) + len(_ENDPOINT)
CONFIGURATION_DESCRIPTOR = (
    bytes([9, 2, _CONFIGURATION_LENGTH, 0, 1, 7, 0, 0x80, 50]) + _INTERFACE + _HID + _ENDPOINT
)

MOUSE_REPORT = bytes([0x01, 0x05, 0xFB])


def usb_crc5(payload: int) -> int:
    remainder = 0x1F
    for bit_number in range(11):
        feedback = ((payload >> bit_number) & 1) ^ (remainder & 1)
        remainder >>= 1
        if feedback:
            remainder ^= 0x14
    return remainder ^ 0x1F


def usb_crc16(payload: bytes | list[int]) -> int:
    crc = 0xFFFF
    for byte in payload:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc ^ 0xFFFF


def data_packet(pid: int, payload: bytes = b"") -> list[int]:
    crc = usb_crc16(payload)
    return [pid, *payload, crc & 0xFF, crc >> 8]


def decode_token(packet: list[int]) -> tuple[int, int]:
    assert len(packet) == 3
    payload = packet[1] | ((packet[2] & 0x07) << 8)
    assert packet[2] >> 3 == usb_crc5(payload)
    return payload & 0x7F, (payload >> 7) & 0x0F


def decode_data(packet: list[int]) -> bytes:
    assert packet[0] in (DATA0, DATA1)
    payload = bytes(packet[1:-2])
    assert int.from_bytes(packet[-2:], "little") == usb_crc16(payload)
    return payload


async def receive_host_packet(ctx, host, *, limit: int = 3000) -> list[int]:
    for _ in range(limit):
        if ctx.get(host.utmi.tx_valid):
            break
        await ctx.tick("usb")
    else:
        raise AssertionError("timed out waiting for a raw host UTMI packet")

    packet = []
    while ctx.get(host.utmi.tx_valid):
        packet.append(ctx.get(host.utmi.tx_data))
        await ctx.tick("usb")
    return packet


async def send_device_packet(ctx, host, packet: list[int]) -> None:
    ctx.set(host.utmi.rx_active, 1)
    ctx.set(host.utmi.rx_valid, 0)
    await ctx.tick("usb")
    for byte in packet:
        ctx.set(host.utmi.rx_data, byte)
        ctx.set(host.utmi.rx_valid, 1)
        await ctx.tick("usb")
    ctx.set(host.utmi.rx_valid, 0)
    ctx.set(host.utmi.rx_active, 0)
    await ctx.tick("usb")


@dataclass(frozen=True)
class SetupRequest:
    request_type: int
    request: int
    value: int
    index: int
    length: int


class RawUTMIMouse:
    """Minimal USB device model operating only on raw UTMI packets."""

    def __init__(self) -> None:
        self.sof_count = 0
        self.nak_count = 0
        self.poll_count = 0
        self.report_ack_count = 0
        self.duplicate_ack_count = 0
        self.enumeration_count = 0
        self.bus_reset()

    def bus_reset(self) -> None:
        self.address = 0
        self.configuration = 0
        self.setup = None
        self.pending_out = None
        self.awaiting_ack = None
        self.poll_count = 0

    def descriptor_bytes(self, setup: SetupRequest) -> bytes:
        descriptor_type = setup.value >> 8
        if descriptor_type == 1:
            descriptor = DEVICE_DESCRIPTOR
        elif descriptor_type == 2:
            descriptor = CONFIGURATION_DESCRIPTOR
        elif descriptor_type == 0x22:
            assert setup.request_type == 0x81
            assert setup.index == 2
            descriptor = REPORT_DESCRIPTOR
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
            assert address == self.address
            if pid in (SETUP, OUT):
                self.pending_out = (pid, endpoint)
                return

            if endpoint == 0:
                assert self.setup is not None
                if self.setup.length:
                    payload = self.descriptor_bytes(self.setup)
                    self.awaiting_ack = "control-data"
                else:
                    payload = b""
                    self.awaiting_ack = "control-status"
                await send_device_packet(ctx, host, data_packet(DATA1, payload))
                return

            assert endpoint == 3
            assert self.configuration == 7
            self.poll_count += 1
            if self.poll_count == 1:
                self.nak_count += 1
                await send_device_packet(ctx, host, [NAK])
            elif self.poll_count == 2:
                self.awaiting_ack = "report"
                await send_device_packet(ctx, host, data_packet(DATA0, MOUSE_REPORT))
            elif self.poll_count == 3:
                self.awaiting_ack = "duplicate"
                await send_device_packet(ctx, host, data_packet(DATA0, MOUSE_REPORT))
            else:
                self.nak_count += 1
                await send_device_packet(ctx, host, [NAK])
            return

        if pid in (DATA0, DATA1):
            assert self.pending_out is not None
            token_pid, endpoint = self.pending_out
            assert endpoint == 0
            payload = decode_data(packet)
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
            else:
                assert pid == DATA1
                assert payload == b""
                assert self.setup is not None and self.setup.length
                self.setup = None
            self.pending_out = None
            await send_device_packet(ctx, host, [ACK])
            return

        assert pid == ACK
        assert packet == [ACK]
        assert self.awaiting_ack is not None
        if self.awaiting_ack == "control-status":
            assert self.setup is not None
            if self.setup.request == 5:
                self.address = self.setup.value
            elif self.setup.request == 9:
                self.configuration = self.setup.value
                self.enumeration_count += 1
            else:
                raise AssertionError(f"unexpected no-data request {self.setup.request}")
            self.setup = None
        elif self.awaiting_ack == "report":
            self.report_ack_count += 1
        elif self.awaiting_ack == "duplicate":
            self.duplicate_ack_count += 1
        self.awaiting_ack = None


async def run_bus_until(ctx, host, mouse, predicate, *, packet_limit: int = 500) -> None:
    first_packets = []
    recent_packets = []
    for _ in range(packet_limit):
        if predicate():
            return
        packet = await receive_host_packet(ctx, host)
        if len(first_packets) < 24:
            first_packets.append(packet)
        recent_packets.append(packet)
        recent_packets = recent_packets[-12:]
        await mouse.handle_packet(ctx, host, packet)
    raise AssertionError(
        "raw UTMI bus scenario exceeded its packet bound: "
        f"error={ctx.get(host.error_code)} address={mouse.address} "
        f"configuration={mouse.configuration} setup={mouse.setup} "
        f"first_packets={first_packets} "
        f"recent_packets={recent_packets}"
    )


async def observe_address_recovery(ctx, host, mouse, timing) -> None:
    """Advance recovery without dropping an in-flight scheduler SOF."""
    partial: list[int] = []
    for _ in range(timing.address_recovery_cycles):
        assert not ctx.get(host.control.start)
        if ctx.get(host.utmi.tx_valid):
            partial.append(ctx.get(host.utmi.tx_data))
        elif partial:
            await mouse.handle_packet(ctx, host, partial)
            partial = []
        await ctx.tick("usb")

    while partial or ctx.get(host.utmi.tx_valid):
        if ctx.get(host.utmi.tx_valid):
            partial.append(ctx.get(host.utmi.tx_data))
        elif partial:
            await mouse.handle_packet(ctx, host, partial)
            return
        await ctx.tick("usb")


async def assert_power_attach_and_reset(ctx, host, timing) -> None:
    await ctx.tick("usb")
    assert ctx.get(host.target_discharge)
    assert not ctx.get(host.aux_vbus_en)
    for _ in range(timing.vbus_discharge_cycles - 1):
        await ctx.tick("usb")
        assert ctx.get(host.target_discharge)
        assert not ctx.get(host.aux_vbus_en)
    await ctx.tick("usb")
    assert not ctx.get(host.target_discharge)
    assert ctx.get(host.aux_vbus_en)

    ctx.set(host.utmi.vbus_valid, 1)
    ctx.set(host.utmi.line_state, 1)
    for _ in range(timing.attach_stable_cycles - 1):
        await ctx.tick("usb")
        assert ctx.get(host.utmi.op_mode) == 0
    await ctx.tick("usb")

    for _ in range(timing.reset_cycles):
        assert ctx.get(host.utmi.xcvr_select) == 0
        assert not ctx.get(host.utmi.term_select)
        assert ctx.get(host.utmi.op_mode) == 2
        assert ctx.get(host.utmi.dp_pulldown)
        assert ctx.get(host.utmi.dm_pulldown)
        await ctx.tick("usb")
    assert ctx.get(host.utmi.xcvr_select) == 1
    assert ctx.get(host.utmi.term_select)
    assert ctx.get(host.utmi.op_mode) == 0
    for _ in range(timing.reset_recovery_cycles):
        assert not ctx.get(host.utmi.tx_valid)
        await ctx.tick("usb")


async def assert_reattach_and_reset(ctx, host, mouse, timing) -> None:
    mouse.bus_reset()
    ctx.set(host.utmi.line_state, 1)
    for _ in range(timing.attach_stable_cycles):
        await ctx.tick("usb")
    for _ in range(timing.reset_cycles):
        assert ctx.get(host.utmi.op_mode) == 2
        await ctx.tick("usb")
    assert ctx.get(host.utmi.op_mode) == 0
    for _ in range(timing.reset_recovery_cycles):
        assert not ctx.get(host.utmi.tx_valid)
        await ctx.tick("usb")


async def lookup(ctx, host, descriptor_type: int, offset: int = 0) -> tuple[int, int, int]:
    ctx.set(host.lookup_type, descriptor_type)
    ctx.set(host.lookup_index, 0)
    ctx.set(host.lookup_w_index, 0)
    ctx.set(host.lookup_offset, offset)
    ctx.set(host.lookup_request, 1)
    await ctx.tick("usb")
    ctx.set(host.lookup_request, 0)
    for _ in range(24):
        if ctx.get(host.lookup_response):
            break
        await ctx.tick("usb")
    else:
        raise AssertionError("host descriptor lookup did not respond")
    await ctx.tick("usb")
    return (
        ctx.get(host.lookup_found),
        ctx.get(host.lookup_length),
        ctx.get(host.lookup_data),
    )


def _composite_interface(
    number: int,
    iface_class: int,
    subclass: int,
    protocol: int,
    endpoint_addr: int,
    report_len: int,
) -> bytes:
    interface = bytes([9, 4, number, 0, 1, iface_class, subclass, protocol, 0])
    hid = bytes([9, 0x21, 0x11, 0x01, 0, 1, 0x22, report_len & 0xFF, report_len >> 8])
    endpoint = bytes([7, 5, endpoint_addr, 3, 8, 0, 1])  # interrupt IN, mps 8, bInterval 1
    return interface + hid + endpoint


# (bInterfaceNumber, class, subclass, protocol, bEndpointAddress, report length)
COMPOSITE_SPECS = [
    (0, 3, 1, 2, 0x81, 5),  # boot mouse   -> interface 0, endpoint 1
    (1, 3, 1, 1, 0x82, 6),  # boot keyboard -> interface 1, endpoint 2
    (2, 3, 0, 0, 0x83, 7),  # consumer/kbd  -> interface 2, endpoint 3
]
_COMPOSITE_BODY = b"".join(_composite_interface(*spec) for spec in COMPOSITE_SPECS)
_COMPOSITE_TOTAL = 9 + len(_COMPOSITE_BODY)
COMPOSITE_CONFIGURATION = (
    bytes([9, 2, _COMPOSITE_TOTAL & 0xFF, _COMPOSITE_TOTAL >> 8, 3, 7, 0, 0x80, 50])
    + _COMPOSITE_BODY
)
# Per-interface report descriptors (keyed by interface number == GET w_index).
COMPOSITE_REPORT_DESCRIPTORS = {
    0: bytes(range(1, 6)),
    1: bytes(range(10, 16)),
    2: bytes(range(20, 27)),
}
# Distinct interrupt-IN report payloads (keyed by endpoint number, <= mps 8).
COMPOSITE_ENDPOINT_REPORTS = {
    1: bytes([0xA0, 0x01]),
    2: bytes([0xB0, 0x02, 0x03]),
    3: bytes([0xC0, 0x04, 0x05, 0x06]),
}


class CompositeUTMIMouse:
    """Raw-UTMI model of a composite HID device (mouse + two keyboards).

    Serves a three-interface configuration, answers a report descriptor GET
    per interface (w_index == interface number), and returns a distinct report
    on each interrupt-IN endpoint. Each endpoint delivers its report once; the
    poller latches it and stops re-issuing until the host drains it.
    """

    def __init__(self, stall_endpoints: tuple[int, ...] = ()) -> None:
        self.sof_count = 0
        self.enumeration_count = 0
        self.report_sent = {1: 0, 2: 0, 3: 0}
        self.report_ack = {1: 0, 2: 0, 3: 0}
        self.stall_endpoints = set(stall_endpoints)
        # Endpoints that NAK every poll (deliver no report); mutable so a test
        # can silence an endpoint after a reconnection.
        self.silent_endpoints: set[int] = set()
        self.bus_reset()

    def bus_reset(self) -> None:
        self.address = 0
        self.configuration = 0
        self.setup = None
        self.pending_out = None
        self.awaiting_ack = None
        self.control_remaining = None
        self.control_toggle = DATA1

    def descriptor_bytes(self, setup: SetupRequest) -> bytes:
        descriptor_type = setup.value >> 8
        if descriptor_type == 1:
            descriptor = DEVICE_DESCRIPTOR
        elif descriptor_type == 2:
            descriptor = COMPOSITE_CONFIGURATION
        elif descriptor_type == 0x22:
            assert setup.request_type == 0x81
            descriptor = COMPOSITE_REPORT_DESCRIPTORS[setup.index]
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
            assert address == self.address
            if pid in (SETUP, OUT):
                self.pending_out = (pid, endpoint)
                return

            if endpoint == 0:
                assert self.setup is not None
                if self.setup.length:
                    # Device->host data stage, split into <=64-byte packets with
                    # an alternating data toggle (a composite config exceeds 64).
                    if self.control_remaining is None:
                        self.control_remaining = self.descriptor_bytes(self.setup)
                        self.control_toggle = DATA1
                    chunk = self.control_remaining[:64]
                    self.control_remaining = self.control_remaining[64:]
                    pid = self.control_toggle
                    self.control_toggle = DATA0 if pid == DATA1 else DATA1
                    self.awaiting_ack = "control-data"
                    if not self.control_remaining:
                        self.control_remaining = None
                    await send_device_packet(ctx, host, data_packet(pid, chunk))
                else:
                    self.awaiting_ack = "control-status"
                    await send_device_packet(ctx, host, data_packet(DATA1, b""))
                return

            assert endpoint in COMPOSITE_ENDPOINT_REPORTS
            assert self.configuration == 7
            if endpoint in self.stall_endpoints:
                await send_device_packet(ctx, host, [STALL])
                return
            if endpoint in self.silent_endpoints:
                await send_device_packet(ctx, host, [NAK])
                return
            self.report_sent[endpoint] += 1
            self.awaiting_ack = ("report", endpoint)
            await send_device_packet(
                ctx, host, data_packet(DATA0, COMPOSITE_ENDPOINT_REPORTS[endpoint])
            )
            return

        if pid in (DATA0, DATA1):
            assert self.pending_out is not None
            token_pid, endpoint = self.pending_out
            assert endpoint == 0
            payload = decode_data(packet)
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
            self.pending_out = None
            await send_device_packet(ctx, host, [ACK])
            return

        assert pid == ACK
        assert packet == [ACK]
        assert self.awaiting_ack is not None
        if self.awaiting_ack == "control-status":
            assert self.setup is not None
            if self.setup.request == 5:
                self.address = self.setup.value
            elif self.setup.request == 9:
                self.configuration = self.setup.value
                self.enumeration_count += 1
            else:
                raise AssertionError(f"unexpected no-data request {self.setup.request}")
            self.setup = None
        elif isinstance(self.awaiting_ack, tuple) and self.awaiting_ack[0] == "report":
            self.report_ack[self.awaiting_ack[1]] += 1
        self.awaiting_ack = None


async def lookup_report(ctx, host, interface: int) -> tuple[int, int]:
    ctx.set(host.lookup_type, 0x22)
    ctx.set(host.lookup_index, 0)
    ctx.set(host.lookup_w_index, interface)
    ctx.set(host.lookup_offset, 0)
    ctx.set(host.lookup_request, 1)
    await ctx.tick("usb")
    ctx.set(host.lookup_request, 0)
    for _ in range(24):
        if ctx.get(host.lookup_response):
            break
        await ctx.tick("usb")
    else:
        raise AssertionError("host report-descriptor lookup did not respond")
    return ctx.get(host.lookup_found), ctx.get(host.lookup_length)


async def drain_tagged_reports(ctx, host, expected: int, *, limit: int = 600) -> dict:
    """Drain the merged stream, grouping bytes into per-interface reports."""
    ctx.set(host.report_ready, 1)
    reports: dict[int, bytes] = {}
    partial: dict[int, list[int]] = {}
    for _ in range(limit):
        if ctx.get(host.report_valid):
            interface = ctx.get(host.report_interface)
            if ctx.get(host.report_first):
                partial[interface] = []
            partial.setdefault(interface, []).append(ctx.get(host.report_data))
            if ctx.get(host.report_last):
                reports[interface] = bytes(partial.pop(interface))
                if len(reports) >= expected:
                    ctx.set(host.report_ready, 0)
                    return reports
        await ctx.tick("usb")
    ctx.set(host.report_ready, 0)
    raise AssertionError(f"drained only {reports}")


def test_composite_mouse_streams_tagged_reports_from_all_endpoints() -> None:
    timing = HostTiming.simulation()
    host = BoundedMouseHost(timing=timing)
    simulation = Simulator(host)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        mouse = CompositeUTMIMouse()
        ctx.set(host.utmi.tx_ready, 1)
        ctx.set(host.utmi.vbus_valid, 0)
        ctx.set(host.utmi.line_state, 0)
        ctx.set(host.report_ready, 0)

        await assert_power_attach_and_reset(ctx, host, timing)
        await run_bus_until(ctx, host, mouse, lambda: mouse.address == 1)
        await observe_address_recovery(ctx, host, mouse, timing)
        await run_bus_until(
            ctx,
            host,
            mouse,
            lambda: bool(ctx.get(host.enumerated) and mouse.configuration == 7),
        )
        assert ctx.get(host.error_code) == HostError.NONE
        assert mouse.enumeration_count == 1

        # All three interrupt-IN endpoints captured into the table.
        assert ctx.get(host.ep_count) == 3
        assert ctx.get(host.ep_number[0]) == 1
        assert ctx.get(host.ep_number[1]) == 2
        assert ctx.get(host.ep_number[2]) == 3
        assert ctx.get(host.ep_interface[1]) == 1

        # A report descriptor was fetched and stored per interface, keyed by
        # interface number.
        for interface, descriptor in COMPOSITE_REPORT_DESCRIPTORS.items():
            found, length = await lookup_report(ctx, host, interface)
            assert found == 1
            assert length == len(descriptor)

        # Every endpoint is polled; wait until each poller holds its report.
        await run_bus_until(
            ctx,
            host,
            mouse,
            lambda: all(ctx.get(host.pollers[k].report_valid) for k in range(3)),
        )

        # Drain the merged, interface-tagged stream: one report per interface.
        collected = await drain_tagged_reports(ctx, host, expected=3)
        assert collected[0] == COMPOSITE_ENDPOINT_REPORTS[1]
        assert collected[1] == COMPOSITE_ENDPOINT_REPORTS[2]
        assert collected[2] == COMPOSITE_ENDPOINT_REPORTS[3]
        assert ctx.get(host.error_code) == HostError.NONE

    simulation.add_testbench(bench)
    simulation.run()


def test_composite_endpoint_stall_fails_the_whole_device() -> None:
    timing = HostTiming.simulation()
    host = BoundedMouseHost(timing=timing)
    simulation = Simulator(host)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        # Endpoint 3 (interface 2) STALLs when polled: its poller fails, and the
        # device-wide OR of polling_failed drops `enumerated` for the whole
        # device even though the other two endpoints enumerated cleanly.
        mouse = CompositeUTMIMouse(stall_endpoints=(3,))
        ctx.set(host.utmi.tx_ready, 1)
        ctx.set(host.utmi.vbus_valid, 0)
        ctx.set(host.utmi.line_state, 0)
        ctx.set(host.report_ready, 0)

        await assert_power_attach_and_reset(ctx, host, timing)
        await run_bus_until(ctx, host, mouse, lambda: mouse.address == 1)
        await observe_address_recovery(ctx, host, mouse, timing)
        await run_bus_until(ctx, host, mouse, lambda: mouse.configuration == 7)
        assert ctx.get(host.ep_count) == 3

        await run_bus_until(ctx, host, mouse, lambda: bool(ctx.get(host.polling_failed)))
        assert ctx.get(host.pollers[2].failed)
        assert not ctx.get(host.enumerated)
        assert ctx.get(host.error_code) == HostError.POLLING_FAILURE

    simulation.add_testbench(bench)
    simulation.run()


def test_composite_survives_disconnect_while_streaming_and_reenumerates() -> None:
    timing = HostTiming.simulation()
    host = BoundedMouseHost(timing=timing)
    simulation = Simulator(host)
    simulation.add_clock(1e-6, domain="usb")

    async def enumerate_and_fill(ctx, mouse) -> None:
        await run_bus_until(ctx, host, mouse, lambda: mouse.address == 1)
        await observe_address_recovery(ctx, host, mouse, timing)
        await run_bus_until(
            ctx,
            host,
            mouse,
            lambda: bool(ctx.get(host.enumerated) and mouse.configuration == 7),
        )
        await run_bus_until(
            ctx,
            host,
            mouse,
            lambda: all(ctx.get(host.pollers[k].report_valid) for k in range(3)),
        )

    async def bench(ctx) -> None:
        mouse = CompositeUTMIMouse()
        ctx.set(host.utmi.tx_ready, 1)
        ctx.set(host.utmi.vbus_valid, 0)
        ctx.set(host.utmi.line_state, 0)
        ctx.set(host.report_ready, 0)

        await assert_power_attach_and_reset(ctx, host, timing)
        await enumerate_and_fill(ctx, mouse)
        assert ctx.get(host.error_code) == HostError.NONE
        assert mouse.enumeration_count == 1

        # Every poller holds a report; the merge is locked on one of them
        # (report_valid high) but undrained. Note which interface it holds.
        assert ctx.get(host.report_valid)
        locked_interface = ctx.get(host.report_interface)
        assert locked_interface in (0, 1, 2)

        # Unplug while the merge holds that report. The locked poller's buffer
        # clears (disconnect) without a last byte ever being seen.
        ctx.set(host.utmi.line_state, 0)
        for _ in range(timing.detach_stable_cycles):
            await ctx.tick("usb")
        assert not ctx.get(host.connected)
        assert not ctx.get(host.enumerated)
        assert ctx.get(host.error_code) == HostError.DISCONNECTED

        # After the reconnect, silence the endpoint the merge had been locked on.
        # If the merge had stayed wedged on that (now idle) interface, it would
        # offer report_ready to nobody else and the still-active interfaces could
        # never drain; the release fix must let them through.
        silent_interface = locked_interface
        active_interfaces = [i for i in range(3) if i != silent_interface]
        mouse.silent_endpoints = {silent_interface + 1}  # interface i -> endpoint i+1

        await assert_reattach_and_reset(ctx, host, mouse, timing)
        await run_bus_until(ctx, host, mouse, lambda: mouse.address == 1)
        await observe_address_recovery(ctx, host, mouse, timing)
        await run_bus_until(
            ctx,
            host,
            mouse,
            lambda: bool(ctx.get(host.enumerated) and mouse.configuration == 7),
        )
        assert mouse.enumeration_count == 2
        await run_bus_until(
            ctx,
            host,
            mouse,
            lambda: all(ctx.get(host.pollers[i].report_valid) for i in active_interfaces),
        )

        collected = await drain_tagged_reports(ctx, host, expected=2)
        assert set(collected) == set(active_interfaces)
        for interface in active_interfaces:
            assert collected[interface] == COMPOSITE_ENDPOINT_REPORTS[interface + 1]

    simulation.add_testbench(bench)
    simulation.run()


def test_real_utmi_mouse_end_to_end_disconnect_and_reenumeration() -> None:
    timing = HostTiming.simulation()
    host = BoundedMouseHost(timing=timing)
    simulation = Simulator(host)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        mouse = RawUTMIMouse()
        ctx.set(host.utmi.tx_ready, 1)
        ctx.set(host.utmi.vbus_valid, 0)
        ctx.set(host.utmi.line_state, 0)
        ctx.set(host.report_ready, 0)

        await assert_power_attach_and_reset(ctx, host, timing)
        await run_bus_until(ctx, host, mouse, lambda: mouse.address == 1)
        await observe_address_recovery(ctx, host, mouse, timing)
        await run_bus_until(
            ctx,
            host,
            mouse,
            lambda: bool(ctx.get(host.enumerated) and mouse.configuration == 7),
        )
        assert ctx.get(host.error_code) == HostError.NONE
        assert mouse.address == 1
        assert mouse.enumeration_count == 1

        await run_bus_until(ctx, host, mouse, lambda: mouse.report_ack_count == 1)
        assert mouse.sof_count > 0
        assert mouse.nak_count >= 1

        for _ in range(20):
            if ctx.get(host.report_valid):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("poller did not expose the accepted report")

        held = ctx.get(host.report_data)
        assert ctx.get(host.report_first)
        for _ in range(3):
            await ctx.tick("usb")
            assert ctx.get(host.report_valid)
            assert ctx.get(host.report_data) == held

        ctx.set(host.report_ready, 1)
        streamed = []
        boundaries = []
        for _ in range(10):
            if ctx.get(host.report_valid):
                streamed.append(ctx.get(host.report_data))
                boundaries.append((ctx.get(host.report_first), ctx.get(host.report_last)))
            await ctx.tick("usb")
            if not ctx.get(host.report_valid):
                break
        ctx.set(host.report_ready, 0)
        assert bytes(streamed) == MOUSE_REPORT
        assert boundaries == [(1, 0), (0, 0), (0, 1)]

        await run_bus_until(ctx, host, mouse, lambda: mouse.duplicate_ack_count == 1)
        for _ in range(5):
            await ctx.tick("usb")
            assert not ctx.get(host.report_valid)

        assert await lookup(ctx, host, 1, 17) == (1, 18, 1)
        ctx.set(host.utmi.line_state, 0)
        for _ in range(timing.detach_stable_cycles):
            await ctx.tick("usb")
        assert not ctx.get(host.connected)
        assert not ctx.get(host.enumerated)
        assert ctx.get(host.error_code) == HostError.DISCONNECTED
        assert await lookup(ctx, host, 1) == (0, 0, 0)

        await assert_reattach_and_reset(ctx, host, mouse, timing)
        await run_bus_until(ctx, host, mouse, lambda: mouse.address == 1)
        await observe_address_recovery(ctx, host, mouse, timing)
        await run_bus_until(
            ctx,
            host,
            mouse,
            lambda: bool(ctx.get(host.enumerated) and mouse.configuration == 7),
        )
        assert mouse.enumeration_count == 2
        assert ctx.get(host.error_code) == HostError.NONE

    simulation.add_testbench(bench)
    simulation.run()


class AlwaysFreshMouse(RawUTMIMouse):
    """A mouse with a new report ready for every poll, at any poll rate.

    ``RawUTMIMouse`` NAKs after its third poll, which is what its own test
    wants. Measuring the *host's* cadence needs a device that is never the
    limit, or the number measured is the model's rate and not the host's.
    """

    def __init__(self) -> None:
        super().__init__()
        self.toggle = 0
        self.fresh_reports = 0

    async def handle_packet(self, ctx, host, packet: list[int]) -> None:
        if packet[0] == IN:
            _address, endpoint = decode_token(packet)
            if endpoint == 3 and self.configuration == 7:
                self.poll_count += 1
                self.awaiting_ack = "report"
                self.fresh_reports += 1
                pid = DATA1 if self.toggle else DATA0
                self.toggle ^= 1
                payload = bytes([0x01, self.fresh_reports & 0xFF, 0xFB])
                await send_device_packet(ctx, host, data_packet(pid, payload))
                return
        await super().handle_packet(ctx, host, packet)


async def chirp_into_high_speed(ctx, host, timing) -> None:
    """Power, attach, chirp K, and ride the host's chirp out of reset at HS."""
    for _ in range(timing.vbus_discharge_cycles + 2):
        await ctx.tick("usb")
    ctx.set(host.utmi.vbus_valid, 1)
    ctx.set(host.utmi.line_state, 1)
    for _ in range(timing.attach_stable_cycles + 2):
        await ctx.tick("usb")
    for _ in range(10_000):
        if ctx.get(host.utmi.op_mode) == 2:
            break
        await ctx.tick("usb")
    else:
        raise AssertionError("host never entered chirp mode")
    ctx.set(host.utmi.line_state, 0b10)
    for _ in range(timing.chirp_detect_cycles * 4):
        await ctx.tick("usb")
    # SE0 is the High Speed idle, so the bus rests here from now on.
    ctx.set(host.utmi.line_state, 0b00)
    for _ in range(timing.reset_cycles * 4):
        await ctx.tick("usb")
        if ctx.get(host.utmi.op_mode) == 0 and ctx.get(host.high_speed):
            return
    raise AssertionError(
        f"never left reset in High Speed: op_mode={ctx.get(host.utmi.op_mode)} "
        f"high_speed={ctx.get(host.high_speed)}"
    )


def test_high_speed_polls_once_per_microframe_end_to_end() -> None:
    """At High Speed with bInterval=1 the host must poll every microframe.

    The cadence is the whole point of High Speed here, and it is invisible to
    every other test: enumeration succeeding, the speed status bit reading 1,
    and reports arriving all look identical whether the host polls every
    microframe or every fourth one. Only the period says which.

    Measured against SOF, not against wall time, so it does not depend on the
    timing profile's absolute numbers.

    ``sof_ticks`` and ``polls_issued`` are asserted alongside the observed
    tokens because they are what hardware bring-up reads; a counter that does
    not match the bus it counts is worse than no counter.
    """
    timing = replace(
        HostTiming.hardware(),
        frame_cycles=6000,
        microframe_cycles=750,
        attach_stable_cycles=4,
        vbus_discharge_cycles=4,
        reset_cycles=600,
        reset_recovery_cycles=4,
        address_recovery_cycles=4,
        detach_stable_cycles=3,
        chirp_detect_cycles=2,
        chirp_drive_cycles=3,
        chirp_timeout_cycles=300,
        chirp_tail_cycles=8,
    )
    host = BoundedMouseHost(timing=timing)
    simulation = Simulator(host)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        mouse = AlwaysFreshMouse()
        ctx.set(host.utmi.tx_ready, 1)
        ctx.set(host.utmi.vbus_valid, 0)
        ctx.set(host.utmi.line_state, 0)
        ctx.set(host.utmi.host_disconnect, 0)
        # The consumer never backpressures, so what is measured is the host.
        ctx.set(host.report_ready, 1)

        await chirp_into_high_speed(ctx, host, timing)
        assert ctx.get(host.high_speed)
        await run_bus_until(ctx, host, mouse, lambda: mouse.address == 1)
        await observe_address_recovery(ctx, host, mouse, timing)
        await run_bus_until(
            ctx,
            host,
            mouse,
            lambda: bool(ctx.get(host.enumerated) and mouse.configuration == 7),
        )
        assert ctx.get(host.error_code) == HostError.NONE
        assert ctx.get(host.high_speed), "speed lost during enumeration"

        # The window is opened and closed on a SOF so that it spans whole
        # microframes; sampling the counters at an arbitrary point would split
        # one microframe across the boundary and lose a poll to rounding.
        before: tuple[int, int] | None = None
        after: tuple[int, int] | None = None
        cycle = 0
        in_starts: list[int] = []
        sof_starts: list[int] = []
        while cycle < 400_000:
            if ctx.get(host.utmi.tx_valid):
                start = cycle
                packet = []
                while ctx.get(host.utmi.tx_valid):
                    packet.append(ctx.get(host.utmi.tx_data))
                    await ctx.tick("usb")
                    cycle += 1
                if packet[0] == IN:
                    if before is not None:
                        in_starts.append(start)
                elif packet[0] == SOF:
                    if before is None:
                        before = ctx.get(host.polls_issued)
                    else:
                        sof_starts.append(start)
                        if len(sof_starts) >= 20:
                            after = ctx.get(host.polls_issued)
                            break
                await mouse.handle_packet(ctx, host, packet)
                continue
            await ctx.tick("usb")
            cycle += 1
        assert before is not None and after is not None, "never observed enough SOFs"

        assert len(in_starts) >= 19, f"too few polls observed: {len(in_starts)}"
        # Cadence is asserted against SOF rather than against a cycle count:
        # the loop above does not see the ticks the device model spends
        # replying, so both series are short by the same constant and only
        # their equality is meaningful. Equality is also the actual property --
        # one poll per SOF is what bInterval=1 means at High Speed.
        periods = [b - a for a, b in zip(in_starts, in_starts[1:], strict=False)]
        sof_periods = [b - a for a, b in zip(sof_starts, sof_starts[1:], strict=False)]
        assert len(set(periods)) == 1, f"poll cadence is not periodic: {periods}"
        assert (
            periods[: len(sof_periods)] == sof_periods
        ), f"poll cadence does not track SOF: polls={periods} sofs={sof_periods}"
        # The counters are exact, and are what hardware bring-up actually reads;
        # a counter that disagrees with the bus it counts is worse than none.
        # One poll issued per SOF tick is the whole claim.
        assert after - before == len(
            sof_starts
        ), f"polls {after - before} != SOF tokens {len(sof_starts)}"
        assert ctx.get(host.poll_naks) == 0

    simulation.add_testbench(bench)
    simulation.run()


def test_high_speed_device_that_stops_answering_is_detached_and_reenumerated() -> None:
    """A device that vanishes at High Speed must not wedge the host forever.

    ``detached`` is the only way out of READY -- and out of ERROR -- so it is
    the single recovery path for the whole design. At Full Speed it is driven
    by sustained SE0, which the host's own pull-downs guarantee the moment the
    device removes its pull-up. At High Speed SE0 is the *idle* state, so the
    only trigger left is the PHY's ``host_disconnect``. On hardware that signal
    did not assert on unplug, and the host stayed in READY with polling
    permanently failed: the poller's ``failed`` latch clears only when
    ``enable`` drops, ``enable`` follows ``connected``, and ``connected`` needed
    the detach that never came. Replugging could not recover it; only
    reconfiguring the FPGA did.

    Three consecutive timeouts is independent evidence that the device is gone,
    and it does not depend on the PHY reporting anything.
    """
    timing = replace(
        HostTiming.hardware(),
        frame_cycles=6000,
        microframe_cycles=750,
        attach_stable_cycles=4,
        vbus_discharge_cycles=4,
        reset_cycles=600,
        reset_recovery_cycles=4,
        address_recovery_cycles=4,
        detach_stable_cycles=3,
        transaction_timeout_cycles=400,
        chirp_detect_cycles=2,
        chirp_drive_cycles=3,
        chirp_timeout_cycles=300,
        chirp_tail_cycles=8,
    )
    host = BoundedMouseHost(timing=timing)
    simulation = Simulator(host)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        mouse = AlwaysFreshMouse()
        ctx.set(host.utmi.tx_ready, 1)
        ctx.set(host.utmi.vbus_valid, 0)
        ctx.set(host.utmi.line_state, 0)
        # The PHY never reports a disconnect -- exactly the hardware case.
        ctx.set(host.utmi.host_disconnect, 0)
        ctx.set(host.report_ready, 1)

        await chirp_into_high_speed(ctx, host, timing)
        await run_bus_until(ctx, host, mouse, lambda: mouse.address == 1)
        await observe_address_recovery(ctx, host, mouse, timing)
        await run_bus_until(
            ctx,
            host,
            mouse,
            lambda: bool(ctx.get(host.enumerated) and mouse.configuration == 7),
        )
        assert ctx.get(host.connected)
        assert ctx.get(host.high_speed)

        # The device goes away. Nothing answers, and the bus rests at SE0 --
        # which at High Speed is indistinguishable from a healthy idle bus.
        for _ in range(60_000):
            await ctx.tick("usb")
            if not ctx.get(host.connected):
                break
        else:
            raise AssertionError(
                "host never detached from a device that stopped answering: "
                f"connected={ctx.get(host.connected)} "
                f"enumerated={ctx.get(host.enumerated)} "
                f"polling_failed={ctx.get(host.polling_failed)} "
                f"error={ctx.get(host.error_code)}"
            )

        assert ctx.get(host.error_code) == HostError.DISCONNECTED

        # And the replug must be picked up on its own. A returning device
        # presents its Full Speed pull-up again, so the line goes back to J and
        # the host should reset and re-run the speed handshake.
        ctx.set(host.utmi.line_state, 1)
        for _ in range(40_000):
            await ctx.tick("usb")
            if ctx.get(host.utmi.op_mode) == 2:
                break
        else:
            raise AssertionError(
                f"host never re-entered reset after replug: "
                f"connected={ctx.get(host.connected)} "
                f"op_mode={ctx.get(host.utmi.op_mode)}"
            )

    simulation.add_testbench(bench)
    simulation.run()
