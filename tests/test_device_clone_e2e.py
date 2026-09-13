"""End-to-end tests for the assembled AUX clone device.

A scripted PC drives the clone's raw UTMI bus through descriptor requests,
enumeration, report transfer, and an unsupported vendor request. Each test
uses a fresh top level with the source descriptor store and clone device as
sibling submodules, matching the production wiring.
"""

import pytest
from _descriptor_store_helpers import seed_descriptor
from amaranth import Elaboratable, Module
from amaranth.sim import Simulator
from luna.gateware.interface.utmi import UTMIInterface

from hurra_cynthion.descriptors import DescriptorStore
from hurra_cynthion.device import MouseCloneDevice

SETUP_PID = 0xD
OUT_PID = 0x1
IN_PID = 0x9

DATA0 = 0xC3
DATA1 = 0x4B
ACK = 0xD2
NAK = 0x5A
STALL = 0x1E

GET_DESCRIPTOR = 0x06
SET_ADDRESS = 0x05
SET_CONFIGURATION = 0x09

DEVICE_DESCRIPTOR = bytes([18, 1, 0, 2, 0, 0, 0, 64, 0x09, 0x12, 1, 0, 0, 1, 0, 0, 0, 1])
REPLACEMENT_DEVICE_DESCRIPTOR = bytes(
    [18, 1, 0, 2, 0, 0, 0, 64, 0x09, 0x12, 2, 0, 1, 1, 0, 0, 0, 1]
)
MOUSE_REPORT = [0x01, 0x05, 0xFB, 0x00]

# Keep every simulated transaction bounded so a broken handshake fails quickly.
MAX_NAK_ATTEMPTS = 50
RECEIVE_TIMEOUT_CYCLES = 500
COPY_TIMEOUT_CYCLES = 200


def usb_crc5(payload: int) -> int:
    remainder = 0x1F
    for bit_number in range(11):
        feedback = ((payload >> bit_number) & 1) ^ (remainder & 1)
        remainder >>= 1
        if feedback:
            remainder ^= 0x14
    return remainder ^ 0x1F


def usb_crc16(payload: list[int]) -> int:
    crc = 0xFFFF
    for byte in payload:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc ^ 0xFFFF


def token_packet(pid: int, address: int, endpoint: int) -> list[int]:
    payload = address | (endpoint << 7)
    return [
        pid | ((~pid & 0xF) << 4),
        payload & 0xFF,
        ((payload >> 8) & 0x07) | (usb_crc5(payload) << 3),
    ]


def data_packet(pid_byte: int, payload: list[int]) -> list[int]:
    crc = usb_crc16(payload)
    return [pid_byte, *payload, crc & 0xFF, crc >> 8]


def setup_packet(request_type: int, request: int, value: int, index: int, length: int) -> list[int]:
    return [
        request_type,
        request,
        value & 0xFF,
        (value >> 8) & 0xFF,
        index & 0xFF,
        (index >> 8) & 0xFF,
        length & 0xFF,
        (length >> 8) & 0xFF,
    ]


class _Top(Elaboratable):
    """Elaborate the source store beside the clone, as production does."""

    def __init__(self):
        self.bus = UTMIInterface()
        self.store = DescriptorStore()
        self.dut = MouseCloneDevice(bus=self.bus, store=self.store)

    def elaborate(self, platform):
        del platform
        m = Module()
        m.submodules.store = self.store
        m.submodules.dut = self.dut
        return m


# Scripted PC helpers for the raw UTMI bus.


async def pc_send(ctx, bus, packet: list[int]) -> None:
    ctx.set(bus.rx_active, 1)
    ctx.set(bus.rx_valid, 0)
    await ctx.tick("usb")
    for byte in packet:
        ctx.set(bus.rx_data, byte)
        ctx.set(bus.rx_valid, 1)
        await ctx.tick("usb")
    ctx.set(bus.rx_valid, 0)
    ctx.set(bus.rx_active, 0)
    await ctx.tick("usb")


async def pc_receive(ctx, bus, *, limit: int = RECEIVE_TIMEOUT_CYCLES) -> list[int]:
    for _ in range(limit):
        if ctx.get(bus.tx_valid):
            break
        await ctx.tick("usb")
    else:
        raise AssertionError("timed out waiting for the device to respond on the UTMI bus")

    packet = []
    while ctx.get(bus.tx_valid):
        packet.append(ctx.get(bus.tx_data))
        await ctx.tick("usb")
    return packet


async def pc_send_ack_watching(
    ctx, bus, pulse_signal, value_signal, *, settle: int = 100
) -> int | None:
    """Send ACK and capture the value accompanying a one-cycle change pulse.

    LUNA's address and configuration values are valid only while their change
    strobes are high, so watch the signals throughout the handshake.
    """
    captured = None

    def check() -> None:
        nonlocal captured
        if captured is None and ctx.get(pulse_signal):
            captured = ctx.get(value_signal)

    ctx.set(bus.rx_active, 1)
    ctx.set(bus.rx_valid, 0)
    check()
    await ctx.tick("usb")
    check()
    ctx.set(bus.rx_data, ACK)
    ctx.set(bus.rx_valid, 1)
    check()
    await ctx.tick("usb")
    check()
    ctx.set(bus.rx_valid, 0)
    ctx.set(bus.rx_active, 0)
    check()

    for _ in range(settle):
        if captured is not None:
            break
        await ctx.tick("usb")
        check()

    await ctx.tick("usb")
    return captured


async def pc_setup_transaction(ctx, bus, address: int, setup_bytes: list[int]) -> None:
    await pc_send(ctx, bus, token_packet(SETUP_PID, address, 0))
    await pc_send(ctx, bus, data_packet(DATA0, setup_bytes))
    handshake = await pc_receive(ctx, bus)
    assert handshake == [ACK], f"SETUP stage was not ACKed: {handshake!r}"


async def pc_in_transaction(
    ctx, bus, address: int, endpoint: int, *, max_attempts: int = MAX_NAK_ATTEMPTS
) -> list[int]:
    """Issue IN tokens, transparently retrying NAKs, returning the first non-NAK reply."""
    for _ in range(max_attempts):
        await pc_send(ctx, bus, token_packet(IN_PID, address, endpoint))
        response = await pc_receive(ctx, bus)
        if response == [NAK]:
            continue
        return response
    raise AssertionError("device NAKed an IN transaction past the retry budget")


async def control_read(
    ctx,
    bus,
    *,
    address: int,
    request_type: int,
    request: int,
    value: int,
    index: int,
    length: int,
) -> bytes:
    """SETUP + DATA_IN + STATUS_OUT. Returns the payload bytes the device sent."""
    await pc_setup_transaction(
        ctx, bus, address, setup_packet(request_type, request, value, index, length)
    )
    response = await pc_in_transaction(ctx, bus, address, 0)
    assert response and response[0] in (DATA0, DATA1), f"expected a DATA packet, got {response!r}"
    payload = bytes(response[1:-2])
    crc = response[-2] | (response[-1] << 8)
    assert crc == usb_crc16(list(payload)), "descriptor DATA packet failed its CRC16"

    await pc_send(ctx, bus, [ACK])  # ACK the data phase.

    await pc_send(ctx, bus, token_packet(OUT_PID, address, 0))
    await pc_send(ctx, bus, data_packet(DATA1, []))
    status_handshake = await pc_receive(ctx, bus)
    assert status_handshake == [ACK], f"STATUS_OUT stage was not ACKed: {status_handshake!r}"
    return payload


async def control_write_no_data(
    ctx,
    bus,
    *,
    address: int,
    request_type: int,
    request: int,
    value: int,
    index: int,
    pulse_signal,
    value_signal,
) -> int | None:
    """Run a no-data control write and return its committed value."""
    await pc_setup_transaction(
        ctx, bus, address, setup_packet(request_type, request, value, index, 0)
    )
    status = await pc_in_transaction(ctx, bus, address, 0)
    assert status and status[0] in (DATA0, DATA1), f"STATUS_IN was not a DATA packet: {status!r}"
    assert status[1:-2] == [], "STATUS_IN stage was not a zero-length packet"
    crc = status[-2] | (status[-1] << 8)
    assert crc == usb_crc16([])

    return await pc_send_ack_watching(ctx, bus, pulse_signal, value_signal)


def _make_top():
    top = _Top()
    sim = Simulator(top)
    sim.add_clock(1e-6, domain="usb")
    return top, sim


async def _power_up(ctx, bus, dut) -> None:
    ctx.set(bus.tx_ready, 1)
    ctx.set(bus.line_state, 1)
    assert hasattr(dut, "copy_enable"), "MouseCloneDevice lacks descriptor-copy control"
    ctx.set(dut.copy_enable, 1)
    for _ in range(COPY_TIMEOUT_CYCLES):
        if ctx.get(dut.copy_done):
            break
        await ctx.tick("usb")
    else:
        raise AssertionError("shared descriptor store did not become ready")
    ctx.set(dut.connect, 1)
    await ctx.tick("usb")


def test_get_descriptor_returns_seeded_device_descriptor():
    """GET_DESCRIPTOR is served directly from the host-owned descriptor store."""
    top, sim = _make_top()
    bus, store, dut = top.bus, top.store, top.dut
    assert dut.store is store
    assert dut._std_handler._store is store

    async def bench(ctx):
        await seed_descriptor(ctx, store, dtype=1, index=0, w_index=0, data=DEVICE_DESCRIPTOR)
        await _power_up(ctx, bus, dut)

        descriptor = await control_read(
            ctx,
            bus,
            address=0,
            request_type=0x80,
            request=GET_DESCRIPTOR,
            value=0x0100,
            index=0,
            length=18,
        )
        assert descriptor == DEVICE_DESCRIPTOR
        assert ctx.get(dut.debug_setup_request_type) == 0x80
        assert ctx.get(dut.debug_setup_request) == GET_DESCRIPTOR
        assert ctx.get(dut.debug_setup_value) == 0x0100
        assert ctx.get(dut.debug_setup_index) == 0
        assert ctx.get(dut.debug_setup_length) == 18

    sim.add_testbench(bench)
    sim.run()


def test_descriptor_generation_replacement_requires_rearm_before_reconnect():
    top, sim = _make_top()
    bus, store, dut = top.bus, top.store, top.dut

    async def bench(ctx):
        await seed_descriptor(ctx, store, dtype=1, index=0, w_index=0, data=DEVICE_DESCRIPTOR)
        await _power_up(ctx, bus, dut)
        descriptor = await control_read(
            ctx,
            bus,
            address=0,
            request_type=0x80,
            request=GET_DESCRIPTOR,
            value=0x0100,
            index=0,
            length=18,
        )
        assert descriptor == DEVICE_DESCRIPTOR

        generation = ctx.get(store.descriptor_generation)
        ctx.set(store.clear, 1)
        await ctx.delay(1e-9)
        assert not ctx.get(dut.copy_done)
        assert not ctx.get(bus.term_select)
        await ctx.tick("usb")
        assert ctx.get(store.descriptor_generation) == (generation + 1) & 0xFFFF
        ctx.set(store.clear, 0)

        await seed_descriptor(
            ctx,
            store,
            dtype=1,
            index=0,
            w_index=0,
            data=REPLACEMENT_DEVICE_DESCRIPTOR,
        )
        assert not ctx.get(dut.copy_done)
        assert not ctx.get(bus.term_select)

        ctx.set(dut.copy_enable, 0)
        await ctx.tick("usb")
        ctx.set(dut.copy_enable, 1)
        await ctx.delay(1e-9)
        assert ctx.get(dut.copy_done)
        assert ctx.get(bus.term_select)

        replacement = await control_read(
            ctx,
            bus,
            address=0,
            request_type=0x80,
            request=GET_DESCRIPTOR,
            value=0x0100,
            index=0,
            length=18,
        )
        assert replacement == REPLACEMENT_DEVICE_DESCRIPTOR

    sim.add_testbench(bench)
    sim.run()


def test_set_address_pulses_address_changed():
    """SET_ADDRESS(5) must pulse `interface.address_changed` with `new_address == 5`."""
    top, sim = _make_top()
    bus, dut = top.bus, top.dut

    async def bench(ctx):
        await _power_up(ctx, bus, dut)
        new_address = await control_write_no_data(
            ctx,
            bus,
            address=0,
            request_type=0x00,
            request=SET_ADDRESS,
            value=5,
            index=0,
            pulse_signal=dut.debug_address_changed,
            value_signal=dut.debug_new_address,
        )
        assert new_address == 5, "address_changed did not pulse with new_address == 5"

    sim.add_testbench(bench)
    sim.run()


def test_enumeration_latches_configured_and_relays_report_to_ep1():
    """SET_ADDRESS(5) + SET_CONFIGURATION(1) latch `configured`; a pushed report reaches EP1 IN."""
    top, sim = _make_top()
    bus, dut = top.bus, top.dut

    async def bench(ctx):
        await _power_up(ctx, bus, dut)
        new_address = await control_write_no_data(
            ctx,
            bus,
            address=0,
            request_type=0x00,
            request=SET_ADDRESS,
            value=5,
            index=0,
            pulse_signal=dut.debug_address_changed,
            value_signal=dut.debug_new_address,
        )
        assert new_address == 5

        new_config = await control_write_no_data(
            ctx,
            bus,
            address=5,
            request_type=0x00,
            request=SET_CONFIGURATION,
            value=1,
            index=0,
            pulse_signal=dut.debug_config_changed,
            value_signal=dut.debug_new_config,
        )
        assert new_config == 1
        # configured is registered one cycle after config_changed.
        await ctx.tick("usb")
        assert ctx.get(dut.configured) == 1

        # Select EP1 before checking whether its relay FIFO is ready.
        ctx.set(dut.report_endpoint, 1)
        for index, byte in enumerate(MOUSE_REPORT):
            ctx.set(dut.report_data, byte)
            ctx.set(dut.report_first, 1 if index == 0 else 0)
            ctx.set(dut.report_last, 1 if index == len(MOUSE_REPORT) - 1 else 0)
            assert ctx.get(dut.report_ready), f"report FIFO not ready for byte {index}"
            ctx.set(dut.report_valid, 1)
            await ctx.tick("usb")
        ctx.set(dut.report_valid, 0)
        ctx.set(dut.report_first, 0)
        ctx.set(dut.report_last, 0)

        report_packet = await pc_in_transaction(ctx, bus, 5, 1)
        assert report_packet[0] in (DATA0, DATA1), f"expected a DATA packet: {report_packet!r}"
        payload = list(report_packet[1:-2])
        crc = report_packet[-2] | (report_packet[-1] << 8)
        assert crc == usb_crc16(payload), "EP1 report packet failed its CRC16"
        assert payload == MOUSE_REPORT
        await pc_send(ctx, bus, [ACK])

    sim.add_testbench(bench)
    sim.run()


def test_unclaimed_vendor_request_is_stalled_not_hung():
    """An unclaimed vendor request must STALL instead of hanging."""
    top, sim = _make_top()
    bus, dut = top.bus, top.dut

    async def bench(ctx):
        await _power_up(ctx, bus, dut)

        request_type = 0xC0  # device-to-host | VENDOR | device recipient
        await pc_setup_transaction(ctx, bus, 0, setup_packet(request_type, 0x01, 0, 0, 0))
        response = await pc_in_transaction(ctx, bus, 0, 0, max_attempts=10)
        assert response == [STALL], f"unclaimed VENDOR request was not STALLed: {response!r}"

    sim.add_testbench(bench)
    sim.run()


@pytest.mark.parametrize("n_decoys", range(12))
def test_get_descriptor_at_every_directory_slot(n_decoys: int):
    """The target descriptor must be served from any directory slot.

    ``serve_response`` fires at ``prepare + 3 + slot``, so which slot a descriptor
    lands in decides whether the store's answer collides with the IN token's
    ``start``. Slot allocation is capture-ordered, so for a given captured device
    the outcome is deterministic: that descriptor either always works or always
    times out. Slot 8 failed before the start replay landed.
    """
    top, sim = _make_top()
    bus, store, dut = top.bus, top.store, top.dut

    async def bench(ctx):
        for index in range(n_decoys):
            await seed_descriptor(
                ctx, store, dtype=3, index=index, w_index=0x0409, data=bytes([index] * 4)
            )
        await seed_descriptor(ctx, store, dtype=1, index=0, w_index=0, data=DEVICE_DESCRIPTOR)
        await _power_up(ctx, bus, dut)

        descriptor = await control_read(
            ctx,
            bus,
            address=0,
            request_type=0x80,
            request=GET_DESCRIPTOR,
            value=0x0100,
            index=0,
            length=18,
        )
        assert descriptor == DEVICE_DESCRIPTOR, f"slot {n_decoys}"

    sim.add_testbench(bench)
    sim.run()
