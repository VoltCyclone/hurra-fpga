import re
from dataclasses import dataclass, replace

import pytest
from amaranth import Elaboratable, Module, Signal
from amaranth.back import rtlil
from amaranth.sim import Simulator

from hurra_cynthion.enumerator import MAX_ENUM_ATTEMPTS, BoundedMouseEnumerator
from hurra_cynthion.timing import HostTiming
from hurra_cynthion.types import HostError, TransactionStatus


@dataclass(frozen=True)
class Request:
    address: int
    request_type: int
    request: int
    value: int
    index: int
    length: int
    max_packet_size: int
    payload: bytes = b""
    status: TransactionStatus = TransactionStatus.SUCCESS


class ScriptedControl(Elaboratable):
    def __init__(self) -> None:
        self.start = Signal()
        self.connected = Signal()
        self.address = Signal(7)
        self.request_type = Signal(8)
        self.request = Signal(8)
        self.value = Signal(16)
        self.index = Signal(16)
        self.length = Signal(16)
        self.max_packet_size = Signal(7)
        self.out_payload = Signal(8)
        self.out_index = Signal(16)
        self.busy = Signal()
        self.done = Signal()
        self.status = Signal(3)
        self.transferred = Signal(16)
        self.data_valid = Signal()
        self.data_ready = Signal()
        self.data = Signal(8)
        self.data_first = Signal()
        self.data_last = Signal()
        self.set_address_valid = Signal()
        self.set_address = Signal(7)

    def elaborate(self, platform) -> Module:
        del platform
        return Module()


DEVICE = bytes([18, 1, 0, 2, 0, 0, 0, 64, 0x09, 0x12, 1, 0, 0, 1, 0, 0, 0, 1])


def device_with_strings(manufacturer: int, product: int, serial: int) -> bytes:
    descriptor = bytearray(DEVICE)
    descriptor[14:17] = bytes([manufacturer, product, serial])
    return bytes(descriptor)


def mouse_configuration(
    *,
    interfaces: int = 1,
    endpoint_count: int = 1,
    endpoint_address: int = 0x83,
    endpoint_attributes: int = 3,
    report_length: int = 52,
    extra_endpoint: bool = False,
) -> bytes:
    interface = bytes([9, 4, 2, 0, endpoint_count, 3, 1, 2, 0])
    hid = bytes([9, 0x21, 0x11, 1, 0, 1, 0x22, report_length & 0xFF, report_length >> 8])
    endpoint = bytes([7, 5, endpoint_address, endpoint_attributes, 8, 0, 10])
    body = interface + hid + endpoint + (endpoint if extra_endpoint else b"")
    total = 9 + len(body)
    return bytes([9, 2, total, 0, interfaces, 7, 0, 0x80, 50]) + body


def composite_configuration(interfaces) -> bytes:
    # interfaces: list of (class, subclass, protocol, endpoint_addr, report_len)
    def one(number, iface_class, subclass, protocol, endpoint_addr, report_len) -> bytes:
        interface = bytes([9, 4, number, 0, 1, iface_class, subclass, protocol, 0])
        hid = bytes([9, 0x21, 0x11, 1, 0, 1, 0x22, report_len & 0xFF, report_len >> 8])
        endpoint = bytes([7, 5, endpoint_addr, 3, 8, 0, 10])
        return interface + hid + endpoint

    body = b"".join(one(index, *spec) for index, spec in enumerate(interfaces))
    total = 9 + len(body)
    return bytes([9, 2, total & 0xFF, total >> 8, len(interfaces), 7, 0, 0x80, 50]) + body


# DeathAdder-shaped: boot mouse (interface 0) plus two keyboard interfaces.
DEATHADDER = [(3, 1, 2, 0x81, 52), (3, 1, 1, 0x82, 65), (3, 0, 0, 0x83, 65)]


def composite_requests(interfaces) -> list[Request]:
    config = composite_configuration(interfaces)
    requests = [
        Request(0, 0x80, 6, 0x0100, 0, 8, 8, DEVICE[:8]),
        Request(0, 0x00, 5, 1, 0, 0, 64),
        Request(1, 0x80, 6, 0x0100, 0, 18, 64, DEVICE),
        Request(1, 0x80, 6, 0x0200, 0, 9, 64, config[:9]),
        Request(1, 0x80, 6, 0x0200, 0, len(config), 64, config),
        Request(1, 0x00, 9, 7, 0, 0, 64),
    ]
    for index, spec in enumerate(interfaces):
        report_len = spec[4]
        requests.append(Request(1, 0x81, 6, 0x2200, index, report_len, 64, bytes(report_len)))
    return requests


def string_descriptor(text: str) -> bytes:
    payload = text.encode("utf-16-le")
    return bytes([len(payload) + 2, 3]) + payload


def descriptor_requests(
    *,
    device: bytes = DEVICE,
    configuration: bytes | None = None,
    strings: tuple[bytes, ...] = (),
    language: bytes = b"\x04\x03\x09\x04",
    report: bytes | None = None,
) -> list[Request]:
    configuration = configuration or mouse_configuration()
    report_length = int.from_bytes(configuration[25:27], "little")
    requests = [
        Request(0, 0x80, 6, 0x0100, 0, 8, 8, device[:8]),
        Request(0, 0x00, 5, 1, 0, 0, 64),
        Request(1, 0x80, 6, 0x0100, 0, 18, 64, device),
        Request(1, 0x80, 6, 0x0200, 0, 9, 64, configuration[:9]),
        Request(1, 0x80, 6, 0x0200, 0, len(configuration), 64, configuration),
    ]
    nonzero_indexes = []
    for index in device[14:17]:
        if index and index not in nonzero_indexes:
            nonzero_indexes.append(index)
    if nonzero_indexes:
        requests.append(Request(1, 0x80, 6, 0x0300, 0, 255, 64, language))
        for descriptor_index, descriptor in zip(nonzero_indexes, strings, strict=True):
            requests.append(
                Request(1, 0x80, 6, 0x0300 | descriptor_index, 0x0409, 255, 64, descriptor)
            )
    requests.extend(
        [
            Request(1, 0x00, 9, 7, 0, 0, 64),
            Request(
                1,
                0x81,
                6,
                0x2200,
                2,
                report_length,
                64,
                report if report is not None else bytes(report_length),
            ),
        ]
    )
    return requests


def simulate(bench) -> None:
    timing = HostTiming.simulation()
    control = ScriptedControl()
    dut = BoundedMouseEnumerator(timing=timing, control=control)
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def wrapped(ctx) -> None:
        ctx.set(dut.enable, 1)
        await bench(ctx, dut, control, timing)

    simulation.add_testbench(wrapped)
    simulation.run()


async def power_attach_and_reset(ctx, dut, timing) -> None:
    await ctx.tick("usb")
    assert ctx.get(dut.target_discharge)
    assert not ctx.get(dut.aux_vbus_en)
    for _ in range(timing.vbus_discharge_cycles - 1):
        await ctx.tick("usb")
        assert ctx.get(dut.target_discharge)
        assert not ctx.get(dut.aux_vbus_en)
    await ctx.tick("usb")
    assert not ctx.get(dut.target_discharge)
    assert ctx.get(dut.aux_vbus_en)

    ctx.set(dut.line_state, 1)
    for _ in range(timing.attach_stable_cycles - 1):
        await ctx.tick("usb")
        assert ctx.get(dut.phy_op_mode) == 0
    await ctx.tick("usb")

    for _ in range(timing.reset_cycles):
        assert ctx.get(dut.phy_xcvr_select) == 0
        assert not ctx.get(dut.phy_term_select)
        assert ctx.get(dut.phy_op_mode) == 2
        assert ctx.get(dut.dp_pulldown)
        assert ctx.get(dut.dm_pulldown)
        await ctx.tick("usb")
    assert ctx.get(dut.phy_xcvr_select) == 1
    assert ctx.get(dut.phy_term_select)
    assert ctx.get(dut.phy_op_mode) == 0


async def serve_request(
    ctx,
    dut,
    control,
    expected: Request,
    *,
    se0_cycles: int = 0,
    se0_before_done: bool = False,
    after_start=None,
) -> None:
    for _ in range(100):
        if ctx.get(control.start):
            break
        await ctx.tick("usb")
    else:
        raise AssertionError(f"enumerator did not issue request {expected}")

    assert ctx.get(control.connected)
    assert ctx.get(control.address) == expected.address
    assert ctx.get(control.request_type) == expected.request_type
    assert ctx.get(control.request) == expected.request
    assert ctx.get(control.value) == expected.value
    assert ctx.get(control.index) == expected.index
    assert ctx.get(control.length) == expected.length
    assert ctx.get(control.max_packet_size) == expected.max_packet_size
    ctx.set(control.busy, 1)
    await ctx.tick("usb")

    # control.start is a single-cycle FSM pulse: it is only guaranteed
    # observable up to the point busy is latched above. Any caller that needs
    # to interleave extra clock ticks (e.g. a descriptor_store lookup, which
    # now requires a synchronous-read tick) between requests must do so here,
    # once the enumerator is safely parked in its busy-servicing state,
    # rather than between this call and the next serve_request/serve_all.
    if after_start is not None:
        await after_start(ctx)

    if se0_cycles:
        ctx.set(dut.line_state, 0)
        for _ in range(se0_cycles):
            await ctx.tick("usb")
            assert ctx.get(dut.connected)
            assert ctx.get(dut.enumerating)
        ctx.set(dut.line_state, 1)
        await ctx.tick("usb")

    for offset, byte in enumerate(expected.payload):
        ctx.set(control.data, byte)
        ctx.set(control.data_valid, 1)
        ctx.set(control.data_first, offset == 0)
        ctx.set(control.data_last, offset == len(expected.payload) - 1)
        for _ in range(20):
            if ctx.get(control.data_ready):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("enumerator did not accept control data")
        await ctx.tick("usb")
    ctx.set(control.data_valid, 0)
    ctx.set(control.data_first, 0)
    ctx.set(control.data_last, 0)
    ctx.set(control.transferred, len(expected.payload))
    ctx.set(control.status, expected.status.value)
    if se0_before_done:
        ctx.set(dut.line_state, 0)
    ctx.set(control.done, 1)
    ctx.set(control.busy, 0)
    await ctx.tick("usb")
    ctx.set(control.done, 0)


async def serve_all(ctx, dut, control, requests: list[Request]) -> None:
    for request in requests:
        await serve_request(ctx, dut, control, request)


async def settle(ctx, cycles: int = 4) -> None:
    for _ in range(cycles):
        await ctx.tick("usb")


async def lookup(ctx, store, key: tuple[int, int, int], offset: int = 0):
    descriptor_type, descriptor_index, w_index = key
    ctx.set(store.lookup_type, descriptor_type)
    ctx.set(store.lookup_index, descriptor_index)
    ctx.set(store.lookup_w_index, w_index)
    ctx.set(store.lookup_offset, offset)
    while not ctx.get(store.lookup_ready):
        await ctx.tick("usb")
    ctx.set(store.lookup_request, 1)
    await ctx.tick("usb")
    ctx.set(store.lookup_request, 0)
    for _ in range(32):
        if ctx.get(store.lookup_response):
            break
        await ctx.tick("usb")
    else:
        raise AssertionError("descriptor lookup did not respond")
    await ctx.tick("usb")
    return ctx.get(store.lookup_found), ctx.get(store.lookup_length), ctx.get(store.lookup_data)


def test_enumerates_mouse_without_strings_and_stores_descriptors() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, descriptor_requests())
        await settle(ctx)
        assert ctx.get(dut.ready)
        assert ctx.get(dut.connected)
        assert not ctx.get(dut.enumerating)
        assert ctx.get(dut.error_code) == HostError.NONE
        assert ctx.get(dut.device_address) == 1
        assert ctx.get(dut.ep0_max_packet) == 64
        assert ctx.get(dut.configuration_value) == 7
        assert ctx.get(dut.ep_count) == 1
        assert ctx.get(dut.ep_interface[0]) == 2
        assert ctx.get(dut.ep_number[0]) == 3
        assert ctx.get(dut.ep_max_packet[0]) == 8
        assert ctx.get(dut.ep_interval[0]) == 10
        assert ctx.get(dut.ep_report_length[0]) == 52
        assert await lookup(ctx, dut.descriptor_store, (1, 0, 0), 17) == (1, 18, 1)
        assert await lookup(ctx, dut.descriptor_store, (2, 0, 0), 1) == (1, 34, 2)
        assert await lookup(ctx, dut.descriptor_store, (0x22, 0, 2), 51) == (1, 52, 0)

    simulate(bench)


def test_enumerates_composite_deathadder_and_captures_all_endpoints() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, composite_requests(DEATHADDER))
        await settle(ctx)
        assert ctx.get(dut.ready)
        assert ctx.get(dut.error_code) == HostError.NONE
        assert ctx.get(dut.ep_count) == 3
        assert [ctx.get(dut.ep_interface[k]) for k in range(3)] == [0, 1, 2]
        assert [ctx.get(dut.ep_number[k]) for k in range(3)] == [1, 2, 3]
        assert [ctx.get(dut.ep_report_length[k]) for k in range(3)] == [52, 65, 65]
        # each interface's report descriptor is stored, keyed by interface number
        assert await lookup(ctx, dut.descriptor_store, (0x22, 0, 0), 0) == (1, 52, 0)
        assert await lookup(ctx, dut.descriptor_store, (0x22, 0, 1), 0) == (1, 65, 0)
        assert await lookup(ctx, dut.descriptor_store, (0x22, 0, 2), 0) == (1, 65, 0)

    simulate(bench)


def test_enumerator_fetches_one_report_descriptor_per_interface() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        # Interface 0 (a mouse) exposes two interrupt-IN endpoints. A report
        # descriptor is a per-interface property, so the enumerator must fetch it
        # exactly once for interface 0 -- not once per endpoint. The request
        # script offers a single interface-0 report GET; a redundant second GET
        # would stall enumeration (dut.ready would never assert).
        interface = bytes([9, 4, 0, 0, 2, 3, 1, 2, 0])
        hid = bytes([9, 0x21, 0x11, 1, 0, 1, 0x22, 52, 0])
        ep_a = bytes([7, 5, 0x81, 3, 8, 0, 10])  # interrupt IN, endpoint 1
        # Endpoint 2, not 8: the number is incidental to this test (any second
        # distinct interrupt-IN endpoint of interface 0 exercises the property),
        # and numbers above RELAY_ENDPOINT_NUMBERS now fail enumeration.
        ep_b = bytes([7, 5, 0x82, 3, 8, 0, 10])  # interrupt IN, endpoint 2
        body = interface + hid + ep_a + ep_b
        total = 9 + len(body)
        config = bytes([9, 2, total & 0xFF, total >> 8, 1, 7, 0, 0x80, 50]) + body
        requests = [
            Request(0, 0x80, 6, 0x0100, 0, 8, 8, DEVICE[:8]),
            Request(0, 0x00, 5, 1, 0, 0, 64),
            Request(1, 0x80, 6, 0x0100, 0, 18, 64, DEVICE),
            Request(1, 0x80, 6, 0x0200, 0, 9, 64, config[:9]),
            Request(1, 0x80, 6, 0x0200, 0, len(config), 64, config),
            Request(1, 0x00, 9, 7, 0, 0, 64),
            Request(1, 0x81, 6, 0x2200, 0, 52, 64, bytes(52)),
        ]
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, requests)
        await settle(ctx)
        assert ctx.get(dut.ready)
        assert ctx.get(dut.error_code) == HostError.NONE
        # Both endpoints captured, both on interface 0.
        assert ctx.get(dut.ep_count) == 2
        assert [ctx.get(dut.ep_interface[k]) for k in range(2)] == [0, 0]
        assert [ctx.get(dut.ep_number[k]) for k in range(2)] == [1, 2]
        # Interface 0's report descriptor is stored once.
        assert await lookup(ctx, dut.descriptor_store, (0x22, 0, 0), 0) == (1, 52, 0)

    simulate(bench)


def test_enumerates_composite_with_vendor_interface_skipped() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        # mouse (interface 0) plus a non-HID vendor interface (class 0xFF, interface 1);
        # the vendor interface and its interrupt-IN endpoint must be skipped, not captured.
        interfaces = [(3, 1, 2, 0x81, 52), (0xFF, 0, 0, 0x82, 65)]
        config = composite_configuration(interfaces)
        requests = [
            Request(0, 0x80, 6, 0x0100, 0, 8, 8, DEVICE[:8]),
            Request(0, 0x00, 5, 1, 0, 0, 64),
            Request(1, 0x80, 6, 0x0100, 0, 18, 64, DEVICE),
            Request(1, 0x80, 6, 0x0200, 0, 9, 64, config[:9]),
            Request(1, 0x80, 6, 0x0200, 0, len(config), 64, config),
            Request(1, 0x00, 9, 7, 0, 0, 64),
            Request(1, 0x81, 6, 0x2200, 0, 52, 64, bytes(52)),
        ]
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, requests)
        await settle(ctx)
        assert ctx.get(dut.ready)
        assert ctx.get(dut.error_code) == HostError.NONE
        assert ctx.get(dut.ep_count) == 1
        assert ctx.get(dut.ep_interface[0]) == 0
        assert ctx.get(dut.ep_number[0]) == 1

    simulate(bench)


def test_enumerator_skips_interrupt_out_and_captures_the_interrupt_in() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        # The keyboard interface (interface 1) carries an interrupt-OUT endpoint
        # (LED reports, address 0x02) ahead of its interrupt-IN (0x82); the OUT
        # must be skipped while the IN is captured, alongside the mouse's IN.
        mouse = (
            bytes([9, 4, 0, 0, 1, 3, 1, 2, 0])
            + bytes([9, 0x21, 0x11, 1, 0, 1, 0x22, 52, 0])
            + bytes([7, 5, 0x81, 3, 8, 0, 10])
        )
        keyboard = (
            bytes([9, 4, 1, 0, 2, 3, 1, 1, 0])
            + bytes([9, 0x21, 0x11, 1, 0, 1, 0x22, 65, 0])
            + bytes([7, 5, 0x02, 3, 8, 0, 10])  # interrupt OUT -> skipped
            + bytes([7, 5, 0x82, 3, 8, 0, 10])  # interrupt IN  -> captured
        )
        body = mouse + keyboard
        total = 9 + len(body)
        config = bytes([9, 2, total & 0xFF, total >> 8, 2, 7, 0, 0x80, 50]) + body
        requests = [
            Request(0, 0x80, 6, 0x0100, 0, 8, 8, DEVICE[:8]),
            Request(0, 0x00, 5, 1, 0, 0, 64),
            Request(1, 0x80, 6, 0x0100, 0, 18, 64, DEVICE),
            Request(1, 0x80, 6, 0x0200, 0, 9, 64, config[:9]),
            Request(1, 0x80, 6, 0x0200, 0, len(config), 64, config),
            Request(1, 0x00, 9, 7, 0, 0, 64),
            Request(1, 0x81, 6, 0x2200, 0, 52, 64, bytes(52)),
            Request(1, 0x81, 6, 0x2200, 1, 65, 64, bytes(65)),
        ]
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, requests)
        await settle(ctx)
        assert ctx.get(dut.ready)
        assert ctx.get(dut.error_code) == HostError.NONE
        assert ctx.get(dut.ep_count) == 2
        assert [ctx.get(dut.ep_interface[k]) for k in range(2)] == [0, 1]
        assert [ctx.get(dut.ep_number[k]) for k in range(2)] == [1, 2]

    simulate(bench)


def test_enumerator_fills_endpoint_table_to_the_maximum() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        # MAX_INTERFACES interfaces, each contributing one interrupt-IN endpoint,
        # fills the endpoint table to MAX_ENDPOINTS and is accepted (not rejected).
        interfaces = [
            (3, 1, 2, 0x81, 20),
            (3, 1, 1, 0x82, 21),
            (3, 1, 1, 0x83, 22),
            (3, 1, 1, 0x84, 23),
        ]
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, composite_requests(interfaces))
        await settle(ctx)
        assert ctx.get(dut.ready)
        assert ctx.get(dut.error_code) == HostError.NONE
        assert ctx.get(dut.ep_count) == 4
        assert [ctx.get(dut.ep_number[k]) for k in range(4)] == [1, 2, 3, 4]
        assert [ctx.get(dut.ep_report_length[k]) for k in range(4)] == [20, 21, 22, 23]

    simulate(bench)


def test_enumerates_and_stores_three_referenced_strings() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        device = device_with_strings(1, 2, 3)
        strings = tuple(string_descriptor(text) for text in ("Maker", "Mouse", "Serial"))
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(
            ctx,
            dut,
            control,
            descriptor_requests(device=device, strings=strings),
        )
        await settle(ctx)
        assert ctx.get(dut.ready)
        assert await lookup(ctx, dut.descriptor_store, (3, 0, 0)) == (1, 4, 4)
        for index, descriptor in enumerate(strings, 1):
            assert await lookup(ctx, dut.descriptor_store, (3, index, 0x0409)) == (
                1,
                len(descriptor),
                descriptor[0],
            )

    simulate(bench)


def test_duplicate_string_indexes_are_fetched_once() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        device = device_with_strings(2, 2, 2)
        descriptor = string_descriptor("Shared")
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(
            ctx,
            dut,
            control,
            descriptor_requests(device=device, strings=(descriptor,)),
        )
        await settle(ctx)
        assert ctx.get(dut.ready)
        assert await lookup(ctx, dut.descriptor_store, (3, 2, 0x0409)) == (
            1,
            len(descriptor),
            descriptor[0],
        )

    simulate(bench)


def test_rejects_stable_low_speed_attachment() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        await ctx.tick("usb")
        for _ in range(timing.vbus_discharge_cycles):
            await ctx.tick("usb")
        ctx.set(dut.line_state, 2)
        for _ in range(timing.attach_stable_cycles + 1):
            await ctx.tick("usb")
        assert ctx.get(dut.error_code) == HostError.UNSUPPORTED_SPEED
        assert not ctx.get(dut.ready)
        assert not ctx.get(control.start)

    simulate(bench)


def test_timing_profiles_bound_detach_deglitching() -> None:
    assert HostTiming.hardware().detach_stable_cycles == 150
    assert HostTiming.hardware(clock_hz=1).detach_stable_cycles == 1
    assert HostTiming.simulation().detach_stable_cycles == 3


def test_reset_and_address_recovery_delays_are_exact() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        requests = descriptor_requests()
        await power_attach_and_reset(ctx, dut, timing)

        assert not ctx.get(dut.traffic_enable)
        await ctx.tick("usb")
        for _ in range(timing.reset_recovery_cycles - 1):
            assert ctx.get(dut.traffic_enable)
            assert not ctx.get(control.start)
            assert ctx.get(dut.phy_xcvr_select) == 1
            assert ctx.get(dut.phy_term_select)
            assert ctx.get(dut.phy_op_mode) == 0
            await ctx.tick("usb")
        assert ctx.get(dut.traffic_enable)
        assert ctx.get(control.start)

        await serve_request(ctx, dut, control, requests[0])
        await serve_request(ctx, dut, control, requests[1])
        assert ctx.get(dut.device_address) == 1
        for _ in range(timing.address_recovery_cycles):
            assert not ctx.get(control.start)
            await ctx.tick("usb")
        assert ctx.get(control.start)
        assert ctx.get(control.address) == 1

    simulate(bench)


def test_reset_recovery_tolerates_brief_nonj_without_losing_progress() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        await power_attach_and_reset(ctx, dut, timing)
        # Accumulate half of the required stable-J interval.
        for _ in range(timing.reset_recovery_cycles // 2):
            await ctx.tick("usb")

        # A SOF-sized disturbance shorter than one frame pauses, but does not
        # reset, the stable-J progress.
        ctx.set(dut.line_state, 0)
        for _ in range(1):
            await ctx.tick("usb")
            assert ctx.get(dut.connected)
            assert ctx.get(dut.traffic_enable)
            assert not ctx.get(control.start)
        ctx.set(dut.line_state, 1)
        retained_remaining = timing.reset_recovery_cycles - (timing.reset_recovery_cycles // 2)
        for _ in range(retained_remaining):
            await ctx.tick("usb")
        assert ctx.get(control.start)

    simulate(bench)


def test_reset_recovery_full_frame_nonj_resets_stable_j_progress() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        await power_attach_and_reset(ctx, dut, timing)
        for _ in range(timing.reset_recovery_cycles // 2):
            await ctx.tick("usb")

        ctx.set(dut.line_state, 0)
        for _ in range(timing.frame_cycles):
            await ctx.tick("usb")
            assert not ctx.get(control.start)
        ctx.set(dut.line_state, 1)
        for _ in range(timing.reset_recovery_cycles):
            assert not ctx.get(control.start)
            await ctx.tick("usb")
        assert ctx.get(control.start)

    simulate(bench)


def test_detach_during_address_recovery_aborts_before_address_one_traffic() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        requests = descriptor_requests()
        await power_attach_and_reset(ctx, dut, timing)
        for _ in range(timing.reset_recovery_cycles):
            await ctx.tick("usb")
        await serve_request(ctx, dut, control, requests[0])
        await serve_request(ctx, dut, control, requests[1])

        ctx.set(dut.line_state, 0)
        for _ in range(timing.detach_stable_cycles):
            await ctx.tick("usb")
        assert not ctx.get(dut.connected)
        assert ctx.get(dut.error_code) == HostError.DISCONNECTED
        assert not ctx.get(control.start)

    simulate(bench)


def test_brief_se0_eop_during_active_control_does_not_disconnect() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        requests = descriptor_requests()
        await power_attach_and_reset(ctx, dut, timing)
        await serve_request(
            ctx,
            dut,
            control,
            requests[0],
            se0_cycles=timing.detach_stable_cycles - 1,
        )
        await serve_all(ctx, dut, control, requests[1:])
        await settle(ctx)
        assert ctx.get(dut.ready)
        assert ctx.get(dut.error_code) == HostError.NONE

    simulate(bench)


def test_brief_se0_eop_from_ready_does_not_disconnect() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, descriptor_requests())
        await settle(ctx)

        ctx.set(dut.line_state, 0)
        for _ in range(timing.detach_stable_cycles - 1):
            await ctx.tick("usb")
            assert ctx.get(dut.ready)
            assert ctx.get(dut.connected)
        ctx.set(dut.line_state, 1)
        await ctx.tick("usb")
        assert ctx.get(dut.ready)
        assert ctx.get(dut.error_code) == HostError.NONE

    simulate(bench)


def test_sustained_se0_from_ready_disconnects_at_qualified_cycle() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, descriptor_requests())
        await settle(ctx)

        ctx.set(dut.line_state, 0)
        for _ in range(timing.detach_stable_cycles - 1):
            await ctx.tick("usb")
            assert ctx.get(dut.ready)
        await ctx.tick("usb")
        assert not ctx.get(dut.ready)
        assert not ctx.get(dut.connected)
        assert ctx.get(dut.error_code) == HostError.DISCONNECTED

    simulate(bench)


def test_sustained_se0_qualification_remains_asserted_across_capture_start_states() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        requests = descriptor_requests()
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, requests[:3])

        async def check_device_descriptor_captured(ctx) -> None:
            # control.start is a one-cycle pulse; this lookup's synchronous
            # read requires its own clock tick, so it must run through
            # serve_request's after_start hook (after busy is latched) and
            # not between serve_all and serve_request, or it would consume
            # the next request's start pulse before serve_request can see it.
            assert await lookup(ctx, dut.descriptor_store, (1, 0, 0)) == (1, 18, 18)

        await serve_request(
            ctx,
            dut,
            control,
            requests[3],
            se0_before_done=True,
            after_start=check_device_descriptor_captured,
        )
        for _ in range(timing.detach_stable_cycles):
            await ctx.tick("usb")

        assert not ctx.get(dut.connected)
        assert not ctx.get(dut.enumerating)
        assert ctx.get(dut.error_code) == HostError.DISCONNECTED
        assert await lookup(ctx, dut.descriptor_store, (1, 0, 0)) == (0, 0, 0)

    simulate(bench)


def test_chattering_se0_during_bus_reset_does_not_disconnect_or_change_reset_length() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        await ctx.tick("usb")
        for _ in range(timing.vbus_discharge_cycles):
            await ctx.tick("usb")
        ctx.set(dut.line_state, 1)
        for _ in range(timing.attach_stable_cycles):
            await ctx.tick("usb")

        reset_cycles = 0
        for line_state in (0, 1, 0, 1):
            assert ctx.get(dut.phy_op_mode) == 2
            reset_cycles += 1
            ctx.set(dut.line_state, line_state)
            await ctx.tick("usb")
        assert reset_cycles == timing.reset_cycles
        assert ctx.get(dut.phy_op_mode) == 0
        assert ctx.get(dut.connected)
        ctx.set(dut.line_state, 1)
        await serve_all(ctx, dut, control, descriptor_requests())
        await settle(ctx)
        assert ctx.get(dut.ready)

    simulate(bench)


def test_stable_se1_is_rejected_without_reset_or_enumeration() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        await ctx.tick("usb")
        for _ in range(timing.vbus_discharge_cycles):
            await ctx.tick("usb")
        ctx.set(dut.line_state, 3)
        for _ in range(timing.attach_stable_cycles + 1):
            await ctx.tick("usb")
        assert ctx.get(dut.error_code) == HostError.UNSUPPORTED_SPEED
        assert not ctx.get(dut.connected)
        assert not ctx.get(dut.enumerating)
        assert ctx.get(dut.phy_op_mode) == 0
        assert not ctx.get(control.start)

    simulate(bench)


@pytest.mark.parametrize(
    ("requests", "expected_error"),
    [
        (
            [Request(0, 0x80, 6, 0x0100, 0, 8, 8, DEVICE[:7])],
            HostError.MALFORMED_DESCRIPTOR,
        ),
        (
            descriptor_requests()[:3]
            + [Request(1, 0x80, 6, 0x0200, 0, 9, 64, b"\x09\x02\x01\x04\x01\x07\x00\x80\x32")],
            HostError.OVERSIZED_DESCRIPTOR,
        ),
        (
            descriptor_requests(configuration=mouse_configuration(report_length=2049))[:5],
            HostError.OVERSIZED_DESCRIPTOR,
        ),
        (
            descriptor_requests(configuration=mouse_configuration(interfaces=2))[:5],
            HostError.UNSUPPORTED_TOPOLOGY,
        ),
        (
            descriptor_requests(configuration=mouse_configuration(endpoint_address=3))[:5],
            HostError.UNSUPPORTED_TOPOLOGY,
        ),
        (
            descriptor_requests(configuration=mouse_configuration(endpoint_attributes=2))[:5],
            HostError.UNSUPPORTED_TOPOLOGY,
        ),
    ],
)
def test_enumeration_failures_map_to_stable_errors(
    requests: list[Request], expected_error: HostError
) -> None:
    async def bench(ctx, dut, control, timing) -> None:
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, requests)
        await settle(ctx)
        assert ctx.get(dut.error_code) == expected_error
        assert not ctx.get(dut.ready)
        for _ in range(3):
            await ctx.tick("usb")
            assert ctx.get(dut.error_code) == expected_error

    simulate(bench)


def test_control_failures_exhaust_reenumeration_attempts_before_terminal_error() -> None:
    failure = Request(
        0,
        0x80,
        6,
        0x0100,
        0,
        8,
        8,
        DEVICE[:8],
        TransactionStatus.TIMEOUT,
    )

    async def bench(ctx, dut, control, timing) -> None:
        await power_attach_and_reset(ctx, dut, timing)
        for attempt in range(MAX_ENUM_ATTEMPTS):
            await serve_request(ctx, dut, control, failure)
            if attempt < MAX_ENUM_ATTEMPTS - 1:
                assert ctx.get(dut.enum_attempt) == attempt + 1
                assert ctx.get(dut.enumerating)
                assert ctx.get(dut.error_code) == HostError.NONE

        await settle(ctx)
        assert ctx.get(dut.error_code) == HostError.CONTROL_FAILURE
        assert not ctx.get(dut.ready)
        assert not ctx.get(dut.enumerating)

    simulate(bench)


def configuration_from_descriptors(*descriptors: bytes) -> bytes:
    body = b"".join(descriptors)
    total = 9 + len(body)
    return bytes([9, 2, total & 0xFF, total >> 8, 1, 7, 0, 0x80, 50]) + body


@pytest.mark.parametrize(
    "configuration",
    [
        # endpoint and HID appear before any interface, so nothing is captured
        configuration_from_descriptors(
            mouse_configuration()[27:34],
            mouse_configuration()[18:27],
            mouse_configuration()[9:18],
        ),
        # a second interface declaring a nonzero alternate setting
        configuration_from_descriptors(
            mouse_configuration()[9:18],
            bytes([9, 4, 2, 1, 1, 3, 1, 2, 0]),
            mouse_configuration()[18:27],
            mouse_configuration()[27:34],
        ),
    ],
)
def test_rejects_orphan_and_alternate_interface_descriptors(configuration: bytes) -> None:
    async def bench(ctx, dut, control, timing) -> None:
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(
            ctx,
            dut,
            control,
            descriptor_requests(configuration=configuration)[:5],
        )
        await settle(ctx)
        assert ctx.get(dut.error_code) == HostError.UNSUPPORTED_TOPOLOGY
        assert not ctx.get(dut.ready)

    simulate(bench)


@pytest.mark.parametrize(
    "configuration",
    [
        # HID precedes its interface; the interrupt-IN endpoint then has no report descriptor
        configuration_from_descriptors(
            mouse_configuration()[18:27],
            mouse_configuration()[9:18],
            mouse_configuration()[27:34],
        ),
        # endpoint precedes the HID descriptor of its interface
        configuration_from_descriptors(
            mouse_configuration()[9:18],
            mouse_configuration()[27:34],
            mouse_configuration()[18:27],
        ),
    ],
)
def test_rejects_interrupt_endpoint_without_report_descriptor(configuration: bytes) -> None:
    async def bench(ctx, dut, control, timing) -> None:
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(
            ctx,
            dut,
            control,
            descriptor_requests(configuration=configuration)[:5],
        )
        await settle(ctx)
        assert ctx.get(dut.error_code) == HostError.MALFORMED_DESCRIPTOR
        assert not ctx.get(dut.ready)

    simulate(bench)


def test_rejects_endpoint_address_with_reserved_bits_set() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        configuration = mouse_configuration(endpoint_address=0x93)
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(
            ctx,
            dut,
            control,
            descriptor_requests(configuration=configuration)[:5],
        )
        await settle(ctx)
        assert ctx.get(dut.error_code) == HostError.UNSUPPORTED_TOPOLOGY

    simulate(bench)


def test_rejects_truncated_full_configuration() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        configuration = mouse_configuration()
        requests = descriptor_requests(configuration=configuration)[:4]
        requests.append(Request(1, 0x80, 6, 0x0200, 0, len(configuration), 64, configuration[:-1]))
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, requests)
        await settle(ctx)
        assert ctx.get(dut.error_code) == HostError.MALFORMED_DESCRIPTOR

    simulate(bench)


@pytest.mark.parametrize(
    ("payload_delta", "expected_error"),
    [(-1, HostError.MALFORMED_DESCRIPTOR), (1, HostError.OVERSIZED_DESCRIPTOR)],
)
def test_rejects_truncated_or_oversized_report(
    payload_delta: int, expected_error: HostError
) -> None:
    async def bench(ctx, dut, control, timing) -> None:
        requests = descriptor_requests()
        report_request = requests[-1]
        requests[-1] = Request(
            report_request.address,
            report_request.request_type,
            report_request.request,
            report_request.value,
            report_request.index,
            report_request.length,
            report_request.max_packet_size,
            bytes(report_request.length + payload_delta),
        )
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, requests)
        await settle(ctx)
        assert ctx.get(dut.error_code) == expected_error

    simulate(bench)


def test_rejects_oversized_full_device_descriptor() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        requests = descriptor_requests()[:3]
        full_device = requests[-1]
        requests[-1] = Request(
            full_device.address,
            full_device.request_type,
            full_device.request,
            full_device.value,
            full_device.index,
            full_device.length,
            full_device.max_packet_size,
            DEVICE + b"\x00",
        )
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, requests)
        await settle(ctx)
        assert ctx.get(dut.error_code) == HostError.OVERSIZED_DESCRIPTOR

    simulate(bench)


def test_rejects_malformed_string_descriptor() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        device = device_with_strings(1, 0, 0)
        requests = descriptor_requests(device=device, strings=(b"\x0a\x03B\x00a\x00d\x00",))
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, requests[:-2])
        await settle(ctx)
        assert ctx.get(dut.error_code) == HostError.MALFORMED_DESCRIPTOR

    simulate(bench)


def test_rejects_oversized_string_descriptor() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        device = device_with_strings(1, 0, 0)
        oversized_string = bytes([254, 3]) + bytes(254)
        requests = descriptor_requests(device=device, strings=(oversized_string,))
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, requests[:-2])
        await settle(ctx)
        assert ctx.get(dut.error_code) == HostError.OVERSIZED_DESCRIPTOR

    simulate(bench)


def test_detach_during_enumeration_aborts_and_clears_store() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        await power_attach_and_reset(ctx, dut, timing)
        await serve_request(ctx, dut, control, descriptor_requests()[0])
        ctx.set(dut.line_state, 0)
        await settle(ctx)
        assert ctx.get(dut.error_code) == HostError.DISCONNECTED
        assert not ctx.get(dut.connected)
        assert not ctx.get(dut.ready)
        assert await lookup(ctx, dut.descriptor_store, (1, 0, 0)) == (0, 0, 0)

    simulate(bench)


def test_detach_from_ready_clears_then_reenumerates() -> None:
    async def bench(ctx, dut, control, timing) -> None:
        requests = descriptor_requests()
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, requests)
        await settle(ctx)
        assert ctx.get(dut.ready)

        ctx.set(dut.line_state, 0)
        await settle(ctx)
        assert ctx.get(dut.error_code) == HostError.DISCONNECTED
        assert await lookup(ctx, dut.descriptor_store, (1, 0, 0)) == (0, 0, 0)

        ctx.set(dut.line_state, 1)
        for _ in range(timing.attach_stable_cycles + timing.reset_cycles):
            await ctx.tick("usb")
        await serve_all(ctx, dut, control, requests)
        await settle(ctx)
        assert ctx.get(dut.ready)
        assert ctx.get(dut.error_code) == HostError.NONE

    simulate(bench)


def test_enumerator_elaborates_to_bounded_logic_and_one_descriptor_memory() -> None:
    control = ScriptedControl()
    dut = BoundedMouseEnumerator(timing=HostTiming.hardware(), control=control)
    netlist = rtlil.convert(
        dut,
        ports=[
            dut.enable,
            dut.line_state,
            dut.target_discharge,
            dut.aux_vbus_en,
            dut.dp_pulldown,
            dut.dm_pulldown,
            dut.phy_xcvr_select,
            dut.phy_term_select,
            dut.phy_op_mode,
            dut.connected,
            dut.enumerating,
            dut.ready,
            dut.error_code,
            dut.device_address,
            dut.ep0_max_packet,
            dut.configuration_value,
            dut.ep_count,
            *dut.ep_interface,
            *dut.ep_number,
            *dut.ep_max_packet,
            *dut.ep_interval,
            *dut.ep_report_length,
        ],
    )
    assert len(re.findall(r"(?m)^\s*memory width 8 size 4096", netlist)) == 1
    assert len(netlist) < 2_000_000


def test_endpoint_number_above_the_relay_range_fails_enumeration() -> None:
    # The clone serves the captured descriptors verbatim, so an endpoint
    # number ReportRelay cannot serve would be advertised to the PC and then
    # NAK forever. Reject it where an operator can attribute it instead.
    requests = descriptor_requests(configuration=mouse_configuration(endpoint_address=0x85))[:5]

    async def bench(ctx, dut, control, timing) -> None:
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, requests)
        await settle(ctx)
        assert ctx.get(dut.error_code) == HostError.UNSUPPORTED_TOPOLOGY, (
            f"endpoint 5 enumerated: error_code={ctx.get(dut.error_code)} "
            f"ready={ctx.get(dut.ready)} ep_count={ctx.get(dut.ep_count)} "
            f"ep_number[0]={ctx.get(dut.ep_number[0])}"
        )
        assert not ctx.get(dut.ready)

    simulate(bench)


def test_endpoint_number_fifteen_fails_enumeration() -> None:
    # 0x8F is the top of the 4-bit endpoint field; guards the comparison width.
    requests = descriptor_requests(configuration=mouse_configuration(endpoint_address=0x8F))[:5]

    async def bench(ctx, dut, control, timing) -> None:
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, requests)
        await settle(ctx)
        assert ctx.get(dut.error_code) == HostError.UNSUPPORTED_TOPOLOGY
        assert not ctx.get(dut.ready)

    simulate(bench)


@pytest.mark.parametrize("endpoint_address", [0x81, 0x82, 0x83, 0x84])
def test_endpoint_numbers_one_through_four_still_enumerate(endpoint_address: int) -> None:
    # Non-regression against an over-tight predicate.
    requests = descriptor_requests(
        configuration=mouse_configuration(endpoint_address=endpoint_address)
    )

    async def bench(ctx, dut, control, timing) -> None:
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, requests)
        await settle(ctx)
        assert ctx.get(dut.error_code) == HostError.NONE
        assert ctx.get(dut.ready)
        assert ctx.get(dut.ep_count) == 1
        assert ctx.get(dut.ep_number[0]) == endpoint_address & 0x0F

    simulate(bench)


@pytest.mark.parametrize("endpoint_address", [0x80, 0x90])
def test_endpoint_zero_and_reserved_bits_still_fail(endpoint_address: int) -> None:
    # The two pre-existing rejections must be untouched by the new clause.
    requests = descriptor_requests(
        configuration=mouse_configuration(endpoint_address=endpoint_address)
    )[:5]

    async def bench(ctx, dut, control, timing) -> None:
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, requests)
        await settle(ctx)
        assert ctx.get(dut.error_code) == HostError.UNSUPPORTED_TOPOLOGY
        assert not ctx.get(dut.ready)

    simulate(bench)


def test_full_speed_device_never_reports_high_speed() -> None:
    """The Full Speed fallback guarantee, pinned before the chirp FSM exists.

    ``high_speed`` is the single source of truth for the negotiated TARGET
    speed; the scheduler, poller, transaction engine and the AUX mirror all key
    off it. A device that never chirps must leave it deasserted for the whole
    session, so the relay behaves exactly as it does today.

    This is written to survive the chirp FSM landing: the scripted device here
    drives a plain Full Speed attach and never chirps K during reset, which is
    what most HID devices do. If a future chirp implementation asserts
    ``high_speed`` speculatively -- on entering the reset, say, rather than on a
    completed K-J-K-J-K-J handshake -- this fails.
    """

    async def bench(ctx, dut, control, timing) -> None:
        assert not ctx.get(dut.high_speed)
        await power_attach_and_reset(ctx, dut, timing)
        assert not ctx.get(dut.high_speed)
        await serve_all(ctx, dut, control, descriptor_requests())
        await settle(ctx)
        assert ctx.get(dut.ready)
        assert not ctx.get(dut.high_speed)

    simulate(bench)


def test_endpoint_interval_is_captured_raw_not_decoded() -> None:
    """``ep_interval`` holds the descriptor byte verbatim, in the device's encoding.

    bInterval means different things at the two speeds -- a count of 1 ms
    frames at Full Speed, an exponent giving ``2**(bInterval-1)`` microframes at
    High Speed. Decoding it here would be lossy: the AUX clone hands this same
    byte to the PC and needs the original encoding to stay faithful. So the
    decode belongs at the point of use in the poller, and this value stays raw.
    """

    async def bench(ctx, dut, control, timing) -> None:
        await power_attach_and_reset(ctx, dut, timing)
        await serve_all(ctx, dut, control, descriptor_requests())
        await settle(ctx)
        # The scripted mouse descriptor declares bInterval = 10.
        assert ctx.get(dut.ep_interval[0]) == 10

    simulate(bench)


# --- High Speed chirp handshake (USB 2.0 7.1.7.5) ---------------------------

CHIRP_TIMING = replace(
    HostTiming.simulation(),
    reset_cycles=400,
    chirp_detect_cycles=2,
    chirp_drive_cycles=3,
    chirp_timeout_cycles=20,
)


def simulate_with(timing, bench) -> None:
    control = ScriptedControl()
    dut = BoundedMouseEnumerator(timing=timing, control=control)
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def wrapped(ctx) -> None:
        ctx.set(dut.enable, 1)
        await bench(ctx, dut, control, timing)

    simulation.add_testbench(wrapped)
    simulation.run()


async def power_and_attach(ctx, dut, timing) -> None:
    """Advance to the start of BUS_RESET, leaving the reset window untouched.

    Waits on the condition rather than counting cycles, so it stays correct
    across timing profiles.
    """
    ctx.set(dut.line_state, 1)
    bound = timing.vbus_discharge_cycles + timing.attach_stable_cycles + 16
    for _ in range(bound):
        await ctx.tick("usb")
        if ctx.get(dut.phy_op_mode) == 2:
            return
    raise AssertionError("never entered bus reset")


async def collect_host_chirp(ctx, dut, timing, *, limit=600) -> list[int]:
    """Return the host's chirp as a list of driven bytes, one per cycle.

    The host keeps alternating until the reset tail, so this runs to ``limit``
    or until the alternation stops, whichever comes first.
    """
    driven: list[int] = []
    for _ in range(limit):
        if ctx.get(dut.chirp_tx_valid):
            driven.append(ctx.get(dut.chirp_tx_data))
        elif driven:
            break
        await ctx.tick("usb")
    return driven


def _runs(values: list[int]) -> list[tuple[int, int]]:
    """Collapse a sample list into (value, run length) pairs."""
    runs: list[tuple[int, int]] = []
    for value in values:
        if runs and runs[-1][0] == value:
            runs[-1] = (value, runs[-1][1] + 1)
        else:
            runs.append((value, 1))
    return runs


def test_host_answers_a_device_chirp_and_enters_high_speed() -> None:
    """Device chirps K, host answers with an unbroken K-J alternation, link is HS.

    An all-zeroes byte transmitted with op_mode 2 is a chirp K and an all-ones
    byte is a J, because that mode disables NRZI encoding and bit stuffing and
    passes the bits through to the line.
    """

    async def bench(ctx, dut, control, timing) -> None:
        await power_and_attach(ctx, dut, timing)
        assert not ctx.get(dut.high_speed)

        ctx.set(dut.line_state, 0b10)
        for _ in range(timing.chirp_detect_cycles * 4):
            await ctx.tick("usb")
        ctx.set(dut.line_state, 0b00)

        driven = await collect_host_chirp(ctx, dut, timing)
        runs = _runs(driven)

        # Alternating, starting with K, each of the configured width.
        expected_head = [
            (0x00, timing.chirp_drive_cycles),
            (0xFF, timing.chirp_drive_cycles),
            (0x00, timing.chirp_drive_cycles),
            (0xFF, timing.chirp_drive_cycles),
            (0x00, timing.chirp_drive_cycles),
            (0xFF, timing.chirp_drive_cycles),
        ]
        assert runs[:6] == expected_head, runs[:10]
        assert ctx.get(dut.high_speed), "three pairs completed but speed not latched"

    simulate_with(CHIRP_TIMING, bench)


def test_host_keeps_chirping_past_the_three_minimum_pairs() -> None:
    """The alternation must continue to the reset tail, not stop at three pairs.

    USB 2.0 section 7.1.7.5: the host alternates K and J until 100-500 us
    before the reset ends, with no idle between chirps.

    Stopping at three pairs is a trap that looks like success locally. The
    device switches to High Speed within 500 us of seeing them, and a High
    Speed device reads 3 ms of squelch as a reset -- so holding SE0 for the
    remaining ~49.7 ms of a 50 ms reset knocks the device it just trained back
    down to Full Speed. On hardware that presented as a completed handshake
    followed by enumeration failing and retrying into Full Speed.
    """

    async def bench(ctx, dut, control, timing) -> None:
        await power_and_attach(ctx, dut, timing)
        ctx.set(dut.line_state, 0b10)
        for _ in range(timing.chirp_detect_cycles * 4):
            await ctx.tick("usb")
        ctx.set(dut.line_state, 0b00)

        driven = await collect_host_chirp(ctx, dut, timing)
        runs = _runs(driven)

        assert len(runs) > 6, f"stopped at the three-pair minimum: {len(runs)} runs"
        # Strictly alternating, with no idle between chirps.
        values = [value for value, _ in runs]
        assert values == [0x00 if i % 2 == 0 else 0xFF for i in range(len(values))], values
        # Every chirp but the last (which the tail truncates) is full width.
        assert all(length == timing.chirp_drive_cycles for _, length in runs[:-1]), runs

    simulate_with(CHIRP_TIMING, bench)


def test_host_stops_chirping_before_the_reset_ends() -> None:
    """The chirp must stop, leaving an SE0 tail so the device sees squelch.

    The device waits for squelch to know the chirp is over and high-speed
    traffic is coming. Chirping to the very end of reset would leave it waiting.
    """

    async def bench(ctx, dut, control, timing) -> None:
        await power_and_attach(ctx, dut, timing)
        ctx.set(dut.line_state, 0b10)
        for _ in range(timing.chirp_detect_cycles * 4):
            await ctx.tick("usb")
        ctx.set(dut.line_state, 0b00)

        # Run past the end of the reset window and confirm the drive stops.
        saw_quiet_after_chirp = False
        chirped = False
        for _ in range(timing.reset_cycles + 64):
            if ctx.get(dut.chirp_tx_valid):
                chirped = True
            elif chirped:
                saw_quiet_after_chirp = True
                break
            await ctx.tick("usb")

        assert chirped, "never chirped at all"
        assert saw_quiet_after_chirp, "chirp never stopped before the reset ended"
        assert ctx.get(dut.high_speed)

    simulate_with(CHIRP_TIMING, bench)


def test_high_speed_link_leaves_reset_in_the_high_speed_phy_configuration() -> None:
    """After the handshake the PHY must come out of reset configured for HS.

    High Speed is transceiver 0 with termination 0 -- no Full Speed pull-up --
    and op_mode NORMAL. Leaving the Full Speed default in place would complete
    the handshake and then talk Full Speed anyway.
    """

    async def bench(ctx, dut, control, timing) -> None:
        await power_and_attach(ctx, dut, timing)
        ctx.set(dut.line_state, 0b10)
        for _ in range(timing.chirp_detect_cycles * 4):
            await ctx.tick("usb")
        ctx.set(dut.line_state, 0b00)
        await collect_host_chirp(ctx, dut, timing)
        assert ctx.get(dut.high_speed)

        # Run out the rest of the reset window.
        for _ in range(timing.reset_cycles + 4):
            await ctx.tick("usb")
            if ctx.get(dut.phy_op_mode) == 0:
                break
        assert ctx.get(dut.phy_op_mode) == 0, "still driving chirp mode"
        assert ctx.get(dut.phy_xcvr_select) == 0, "not on the high-speed transceiver"
        assert not ctx.get(dut.phy_term_select), "full-speed pull-up still presented"

    simulate_with(CHIRP_TIMING, bench)


def test_momentary_k_shorter_than_the_qualifier_is_not_a_chirp() -> None:
    """A K briefly glimpsed during reset must not start the handshake."""

    async def bench(ctx, dut, control, timing) -> None:
        await power_and_attach(ctx, dut, timing)
        # One cycle of K, below the chirp_detect_cycles=2 qualifier.
        ctx.set(dut.line_state, 0b10)
        await ctx.tick("usb")
        ctx.set(dut.line_state, 0b00)
        for _ in range(timing.chirp_timeout_cycles * 3):
            await ctx.tick("usb")
            assert not ctx.get(dut.chirp_tx_valid), "answered a glitch"
        assert not ctx.get(dut.high_speed)

    simulate_with(CHIRP_TIMING, bench)


def test_high_speed_se0_is_idle_and_not_a_disconnect() -> None:
    """The regression that would otherwise break the instant HS engages.

    At Full Speed a sustained SE0 means the device dropped its pull-up. At High
    Speed SE0 is where the bus rests between packets, so reusing that test would
    report a disconnect immediately after a successful handshake.
    """

    async def bench(ctx, dut, control, timing) -> None:
        await power_and_attach(ctx, dut, timing)
        ctx.set(dut.line_state, 0b10)
        for _ in range(timing.chirp_detect_cycles * 4):
            await ctx.tick("usb")
        ctx.set(dut.line_state, 0b00)
        await collect_host_chirp(ctx, dut, timing)
        assert ctx.get(dut.high_speed)

        # Sit at SE0 far longer than the Full Speed disconnect qualifier.
        ctx.set(dut.host_disconnect, 0)
        for _ in range(timing.detach_stable_cycles * 20):
            await ctx.tick("usb")
            assert ctx.get(dut.connected), "SE0 read as a disconnect at High Speed"

    simulate_with(CHIRP_TIMING, bench)


async def chirp_into_high_speed(ctx, dut, timing) -> None:
    """Run the full handshake and leave the link idle in High Speed."""
    await power_and_attach(ctx, dut, timing)
    ctx.set(dut.line_state, 0b10)
    for _ in range(timing.chirp_detect_cycles * 4):
        await ctx.tick("usb")
    # SE0 is the High Speed idle, so this is where the bus rests from here on.
    ctx.set(dut.line_state, 0b00)
    await collect_host_chirp(ctx, dut, timing)
    assert ctx.get(dut.high_speed)
    for _ in range(timing.reset_cycles + 8):
        await ctx.tick("usb")
        if ctx.get(dut.phy_op_mode) == 0:
            return
    raise AssertionError("reset window never ended")


def test_enumerates_a_mouse_over_a_high_speed_link() -> None:
    """End to end at High Speed: chirp, recover, enumerate, capture.

    Recovery is the part that would otherwise fail silently. It waits for a
    stable idle bus, and idle is SE0 at High Speed rather than the J it is at
    Full Speed -- so a Full Speed idle test would never be satisfied, time out
    after its whole budget, and retry enumeration forever.
    """

    async def bench(ctx, dut, control, timing) -> None:
        await chirp_into_high_speed(ctx, dut, timing)
        await serve_all(ctx, dut, control, descriptor_requests())
        await settle(ctx)
        assert ctx.get(dut.ready)
        assert ctx.get(dut.connected)
        assert ctx.get(dut.high_speed), "speed lost during enumeration"
        assert ctx.get(dut.error_code) == HostError.NONE
        assert ctx.get(dut.device_address) == 1
        assert ctx.get(dut.ep_count) == 1
        assert ctx.get(dut.ep_interval[0]) == 10

    simulate_with(CHIRP_TIMING, bench)


def test_high_speed_disconnect_comes_from_the_phy() -> None:
    """With SE0 unusable as a disconnect, the PHY's host_disconnect ends it."""

    async def bench(ctx, dut, control, timing) -> None:
        await chirp_into_high_speed(ctx, dut, timing)
        await serve_all(ctx, dut, control, descriptor_requests())
        await settle(ctx)
        assert ctx.get(dut.ready)
        assert ctx.get(dut.connected)

        ctx.set(dut.host_disconnect, 1)
        for _ in range(timing.detach_stable_cycles * 8 + 16):
            await ctx.tick("usb")
            if not ctx.get(dut.connected):
                break
        assert not ctx.get(dut.connected), "PHY disconnect did not end the session"
        assert not ctx.get(dut.high_speed), "speed not cleared on disconnect"

    simulate_with(CHIRP_TIMING, bench)


def test_force_full_speed_suppresses_the_chirp_entirely() -> None:
    """The runtime override keeps the link Full Speed even against an HS device.

    The escape hatch for a device that misbehaves at High Speed. The register
    declaring it predates the chirp and had nothing to drive until now.
    """

    async def bench(ctx, dut, control, timing) -> None:
        ctx.set(dut.force_full_speed, 1)
        await power_and_attach(ctx, dut, timing)

        # A device chirping K as hard as it likes.
        ctx.set(dut.line_state, 0b10)
        for _ in range(timing.chirp_timeout_cycles * 3):
            await ctx.tick("usb")
            assert not ctx.get(dut.chirp_tx_valid), "answered a chirp while pinned to FS"
        assert not ctx.get(dut.high_speed)

    simulate_with(CHIRP_TIMING, bench)
