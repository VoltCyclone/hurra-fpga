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
MAX_PACKET_BYTES = 1 + 64 + 2


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
        # PID + 64 bytes + CRC16 is the largest packet this device may send.
        # A transmitter that never ends one must fail here, not hang.
        assert len(packet) <= MAX_PACKET_BYTES, "device babbled: packet exceeds 67 bytes"
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


async def pc_in_data_packet(ctx, bus, address: int) -> tuple[int, bytes]:
    """One data-stage IN transaction, NAKs retried. Returns (PID byte, payload).

    Does not ACK: the caller decides, so a lost ACK can be modelled.
    """
    response = await pc_in_transaction(ctx, bus, address, 0)
    assert response and response[0] in (DATA0, DATA1), f"expected a DATA packet, got {response!r}"
    payload = bytes(response[1:-2])
    crc = response[-2] | (response[-1] << 8)
    assert crc == usb_crc16(list(payload)), "DATA packet failed its CRC16"
    return response[0], payload


async def pc_status_out(ctx, bus, address: int) -> None:
    """Status stage of a control read: an OUT ZLP the device must ACK."""
    await pc_send(ctx, bus, token_packet(OUT_PID, address, 0))
    await pc_send(ctx, bus, data_packet(DATA1, []))
    handshake = await pc_receive(ctx, bus)
    assert handshake == [ACK], f"STATUS_OUT stage was not ACKed: {handshake!r}"


async def pc_status_in(ctx, bus, address: int, *, max_attempts: int = MAX_NAK_ATTEMPTS) -> None:
    """Status stage of a no-data request: an IN the device answers, after any
    NAKs, with a zero-length DATA1 -- never a bare handshake -- which we ACK."""
    status = await pc_in_transaction(ctx, bus, address, 0, max_attempts=max_attempts)
    assert status and status[0] == DATA1, f"status IN was not DATA1: {status!r}"
    assert status[1:-2] == [], "status IN stage was not a zero-length packet"
    await pc_send(ctx, bus, [ACK])


async def control_in_packets(
    ctx, bus, *, setup_bytes: list[int], mps: int, address: int = 0
) -> list[tuple[int, bytes]]:
    """Run a whole control read as a host does, returning every data packet.

    The data stage ends the way USB 2.0 s8.5.3.2 ends it: on a packet shorter
    than bMaxPacketSize0 -- a ZLP included -- or once wLength bytes are in.
    Returning the packets rather than the joined payload is the point: the
    boundaries and PIDs are what these tests assert on.
    """
    await pc_setup_transaction(ctx, bus, address, setup_bytes)
    length = setup_bytes[6] | (setup_bytes[7] << 8)
    packets: list[tuple[int, bytes]] = []
    received = 0
    while True:
        assert len(packets) < 16, f"data stage did not terminate: {packets!r}"
        pid, payload = await pc_in_data_packet(ctx, bus, address)
        await pc_send(ctx, bus, [ACK])
        packets.append((pid, payload))
        received += len(payload)
        if len(payload) < mps or received >= length:
            break
    await pc_status_out(ctx, bus, address)
    return packets


def alternating_pids(count: int) -> list[int]:
    """DATA1, DATA0, DATA1, ...: a control data stage starts on DATA1 [USB2.0 8.5.3]."""
    return [DATA1 if index % 2 == 0 else DATA0 for index in range(count)]


def assert_packets(packets: list[tuple[int, bytes]], *, sizes: list[int], data: bytes) -> None:
    """A data stage's packet boundaries, its PID sequence, and its bytes."""
    assert [len(payload) for _pid, payload in packets] == sizes, f"packet sizes: {packets!r}"
    pids = [pid for pid, _payload in packets]
    assert pids == alternating_pids(len(sizes)), f"PIDs must alternate from DATA1: {pids}"
    assert b"".join(payload for _pid, payload in packets) == data


async def push_report(ctx, dut, endpoint: int, report: list[int]) -> None:
    """Queue one report on a relay endpoint, as the injection plane would."""
    # Select the endpoint before checking whether its relay FIFO is ready.
    ctx.set(dut.report_endpoint, endpoint)
    for index, byte in enumerate(report):
        ctx.set(dut.report_data, byte)
        ctx.set(dut.report_first, 1 if index == 0 else 0)
        ctx.set(dut.report_last, 1 if index == len(report) - 1 else 0)
        assert ctx.get(dut.report_ready), f"report FIFO not ready for byte {index}"
        ctx.set(dut.report_valid, 1)
        await ctx.tick("usb")
    ctx.set(dut.report_valid, 0)
    ctx.set(dut.report_first, 0)
    ctx.set(dut.report_last, 0)


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


#: The IN endpoint numbers _power_up binds the clone's relay slots to.
DEFAULT_IN_ENDPOINTS = (1, 2, 3, 4)


async def _power_up(
    ctx, bus, dut, *, mps: int | None = None, in_endpoints: tuple[int, ...] = DEFAULT_IN_ENDPOINTS
) -> None:
    ctx.set(bus.tx_ready, 1)
    ctx.set(bus.line_state, 1)
    # In production the host's captured endpoint table drives these, settled
    # before the clone connects. The clone registers them.
    ctx.set(dut.in_endpoint_count, len(in_endpoints))
    for slot, number in enumerate(in_endpoints):
        ctx.set(dut.in_endpoint_number[slot], number)
    if mps is not None:
        # In production the host's enumerator drives this from the real
        # device's bMaxPacketSize0 before the clone is ever connected.
        ctx.set(dut.ep0_max_packet, mps)
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

        await push_report(ctx, dut, 1, MOUSE_REPORT)

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


HID_SET_IDLE = 0x0A


def test_hid_set_idle_status_stage_is_a_zero_length_packet():
    """SET_IDLE is a no-data host->device request, so its status stage is IN.

    USB 2.0 s8.5.3: the device answers a status-stage IN token with a
    zero-length DATA1 packet. A bare ACK handshake is not a valid answer to an
    IN token -- the host is waiting for a data packet. LUNA's own SET_ADDRESS
    and CDC-ACM SET_LINE_CODING handlers both send_zlp() here.

    This is checked on the wire rather than at the handler's outputs, because
    at the handler both answers look like "a handshake signal went high".
    """
    top, sim = _make_top()
    bus, dut = top.bus, top.dut

    async def bench(ctx):
        await _power_up(ctx, bus, dut)
        await pc_setup_transaction(ctx, bus, 0, setup_packet(0x21, HID_SET_IDLE, 0, 0, 0))
        status = await pc_in_transaction(ctx, bus, 0, 0)
        assert status and status[0] in (
            DATA0,
            DATA1,
        ), f"SET_IDLE status stage was not a DATA packet: {status!r}"
        assert status[1:-2] == [], "SET_IDLE status stage was not zero-length"

    sim.add_testbench(bench)
    sim.run()


# --- The relayed interrupt-OUT endpoint -----------------------------------------

#: Long enough for LUNA's handshake generator to answer a packet several times
#: over; an endpoint that has not answered by then is not going to.
HANDSHAKE_WINDOW_CYCLES = 64


async def pc_handshake_or_none(ctx, bus, *, window: int = HANDSHAKE_WINDOW_CYCLES):
    """Return the device's reply to a packet, or None if it stays silent."""
    for _ in range(window):
        if ctx.get(bus.tx_valid):
            return await pc_receive(ctx, bus)
        await ctx.tick("usb")
    return None


async def pc_out(ctx, bus, address: int, endpoint: int, pid: int, payload: bytes):
    """One interrupt-OUT transaction: token, data, and whatever handshake comes back."""
    await pc_send(ctx, bus, token_packet(OUT_PID, address, endpoint))
    await pc_send(ctx, bus, data_packet(pid, list(payload)))
    return await pc_handshake_or_none(ctx, bus)


async def drain_out_stream(ctx, dut, *, idle: int = 8, limit: int = 400) -> list[bytes]:
    """Read the clone's OUT stream dry, splitting it into packets on ``last``."""
    ctx.set(dut.out_ready, 1)
    packets: list[bytes] = []
    current: list[int] = []
    quiet = 0
    for _ in range(limit):
        if ctx.get(dut.out_valid):
            quiet = 0
            current.append(ctx.get(dut.out_data))
            if ctx.get(dut.out_last):
                packets.append(bytes(current))
                current = []
        else:
            quiet += 1
            if quiet > idle:
                break
        await ctx.tick("usb")
    ctx.set(dut.out_ready, 0)
    assert not current, f"stream ended mid-packet: {current!r}"
    return packets


async def _power_up_with_out_endpoint(ctx, bus, dut, number: int, *, present: bool = True) -> None:
    # In production this comes from the host's enumerator, settled before the
    # clone connects. The clone registers it, so give it a cycle.
    ctx.set(dut.out_present, present)
    ctx.set(dut.out_endpoint_number, number)
    await ctx.tick("usb")
    await _power_up(ctx, bus, dut)


@pytest.mark.parametrize("number", [1, 2, 3, 4])
def test_out_to_the_runtime_endpoint_number_is_acked_and_streamed(number: int):
    top, sim = _make_top()
    bus, dut = top.bus, top.dut

    async def bench(ctx):
        await _power_up_with_out_endpoint(ctx, bus, dut, number)
        assert await pc_out(ctx, bus, 0, number, DATA0, b"\x05\x01\x02") == [ACK]
        assert await pc_out(ctx, bus, 0, number, DATA1, b"\x05\x03") == [ACK]
        assert await drain_out_stream(ctx, dut) == [b"\x05\x01\x02", b"\x05\x03"]

    sim.add_testbench(bench)
    sim.run()


def test_out_to_any_other_endpoint_number_gets_no_handshake():
    top, sim = _make_top()
    bus, dut = top.bus, top.dut

    async def bench(ctx):
        await _power_up_with_out_endpoint(ctx, bus, dut, 2)
        for endpoint in (1, 3, 4, 5, 15):
            assert await pc_out(ctx, bus, 0, endpoint, DATA0, b"\x01") is None, endpoint
        assert await drain_out_stream(ctx, dut) == []

    sim.add_testbench(bench)
    sim.run()


@pytest.mark.parametrize(("present", "number"), [(False, 2), (False, 0), (True, 0)])
def test_an_absent_or_illegal_out_endpoint_acks_nothing(present: bool, number: int):
    """With nothing to relay the clone answers no OUT at all.

    The endpoint is parked on 16, outside the 4-bit token field, rather than on
    any real number: 0 would claim EP0 OUT, and every number 1..15 is one a
    device may declare.
    """
    top, sim = _make_top()
    bus, dut = top.bus, top.dut

    async def bench(ctx):
        assert not ctx.get(dut.out_present), "the input must default to absent"
        await _power_up_with_out_endpoint(ctx, bus, dut, number, present=present)
        for endpoint in (1, 2, 3, 4, 5, 15):
            assert await pc_out(ctx, bus, 0, endpoint, DATA0, b"\x01") is None, endpoint
        assert await drain_out_stream(ctx, dut) == []

    sim.add_testbench(bench)
    sim.run()


def test_a_toggle_mismatch_resend_is_acked_but_delivered_once():
    """A PC that missed our ACK resends with the same toggle [USB2.0 8.6.4]."""
    top, sim = _make_top()
    bus, dut = top.bus, top.dut

    async def bench(ctx):
        await _power_up_with_out_endpoint(ctx, bus, dut, 2)
        assert await pc_out(ctx, bus, 0, 2, DATA0, b"\xaa\xbb") == [ACK]
        assert await pc_out(ctx, bus, 0, 2, DATA0, b"\xaa\xbb") == [ACK]
        assert await pc_out(ctx, bus, 0, 2, DATA1, b"\xcc") == [ACK]
        assert await drain_out_stream(ctx, dut) == [b"\xaa\xbb", b"\xcc"]

    sim.add_testbench(bench)
    sim.run()


def test_a_full_out_fifo_naks_the_pc_until_the_writer_drains_it():
    top, sim = _make_top()
    bus, dut = top.bus, top.dut
    packets = [bytes([k] * 32) for k in range(8)]

    async def bench(ctx):
        await _power_up_with_out_endpoint(ctx, bus, dut, 2)
        # out_ready stays low: the writer is holding a packet.
        accepted = 0
        for _ in range(len(packets)):
            pid = DATA0 if accepted % 2 == 0 else DATA1
            handshake = await pc_out(ctx, bus, 0, 2, pid, packets[accepted])
            if handshake == [NAK]:
                break
            assert handshake == [ACK]
            accepted += 1
        else:
            raise AssertionError("the OUT FIFO never filled")
        assert accepted >= 1

        # Once drained, the NAKed packet is retried with the same toggle.
        assert await drain_out_stream(ctx, dut) == packets[:accepted]
        pid = DATA0 if accepted % 2 == 0 else DATA1
        assert await pc_out(ctx, bus, 0, 2, pid, packets[accepted]) == [ACK]
        assert await drain_out_stream(ctx, dut) == [packets[accepted]]

    sim.add_testbench(bench)
    sim.run()


def test_the_out_toggle_restarts_at_data0_after_set_configuration():
    """USB 2.0 9.1.1.5: SET_CONFIGURATION resets every endpoint's data toggle.

    LUNA's OUT endpoint resets it only on ClearFeature(ENDPOINT_HALT). Ending a
    session on the odd toggle then made the PC's first DATA0 after a
    re-configuration look like a resend: ACKed, and silently discarded.
    """
    top, sim = _make_top()
    bus, dut = top.bus, top.dut

    async def bench(ctx):
        await _power_up_with_out_endpoint(ctx, bus, dut, 2)
        assert await pc_out(ctx, bus, 0, 2, DATA0, b"\x01") == [ACK]
        assert await drain_out_stream(ctx, dut) == [b"\x01"]

        new_config = await control_write_no_data(
            ctx,
            bus,
            address=0,
            request_type=0x00,
            request=SET_CONFIGURATION,
            value=1,
            index=0,
            pulse_signal=dut.debug_config_changed,
            value_signal=dut.debug_new_config,
        )
        assert new_config == 1
        assert await pc_out(ctx, bus, 0, 2, DATA0, b"\x02") == [ACK]
        assert await drain_out_stream(ctx, dut) == [b"\x02"], "first DATA0 after SET_CONFIGURATION"

    sim.add_testbench(bench)
    sim.run()


def test_the_out_toggle_restarts_at_data0_after_a_bus_reset():
    top, sim = _make_top()
    bus, dut = top.bus, top.dut

    async def bench(ctx):
        await _power_up_with_out_endpoint(ctx, bus, dut, 2)
        assert await pc_out(ctx, bus, 0, 2, DATA0, b"\x01") == [ACK]
        assert await drain_out_stream(ctx, dut) == [b"\x01"]
        # Leave a packet in the FIFO too: a reset discards it with the toggle.
        assert await pc_out(ctx, bus, 0, 2, DATA1, b"\xee") == [ACK]

        # SE0 for well over LUNA's 5 us (300-cycle) reset threshold.
        saw_reset = False
        ctx.set(bus.line_state, 0)
        for _ in range(400):
            await ctx.tick("usb")
            saw_reset |= bool(ctx.get(dut.debug_reset_detected))
        ctx.set(bus.line_state, 1)
        await ctx.tick("usb")
        assert saw_reset
        assert await drain_out_stream(ctx, dut) == []

        assert await pc_out(ctx, bus, 0, 2, DATA0, b"\x02") == [ACK]
        assert await drain_out_stream(ctx, dut) == [b"\x02"], "first DATA0 after a bus reset"

    sim.add_testbench(bench)
    sim.run()


def test_other_control_traffic_leaves_the_out_toggle_alone():
    """Only a bus reset or SET_CONFIGURATION may reset the OUT endpoint."""
    top, sim = _make_top()
    bus, dut = top.bus, top.dut

    async def bench(ctx):
        await _power_up_with_out_endpoint(ctx, bus, dut, 2)
        assert await pc_out(ctx, bus, 0, 2, DATA0, b"\x01") == [ACK]
        assert (
            await control_write_no_data(
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
            == 5
        )
        # Still expecting DATA1, so a DATA0 now is a resend: ACKed, not delivered.
        assert await pc_out(ctx, bus, 5, 2, DATA0, b"\x01") == [ACK]
        assert await pc_out(ctx, bus, 5, 2, DATA1, b"\x02") == [ACK]
        assert await drain_out_stream(ctx, dut) == [b"\x01", b"\x02"]

    sim.add_testbench(bench)
    sim.run()


# --- Relay IN endpoints bound to runtime numbers -------------------------------


async def pc_in_or_none(ctx, bus, address: int, endpoint: int):
    """One IN token: the device's reply, or None if it stays silent."""
    await pc_send(ctx, bus, token_packet(IN_PID, address, endpoint))
    return await pc_handshake_or_none(ctx, bus)


def test_an_in_endpoint_numbered_fifteen_serves_its_slot():
    """Slot 0 bound to endpoint 15: the PC reads the report there, and slots
    left unbound answer nothing -- not even on the numbers they used to have."""
    top, sim = _make_top()
    bus, dut = top.bus, top.dut

    async def bench(ctx):
        await _power_up(ctx, bus, dut, in_endpoints=(15,))
        await push_report(ctx, dut, 15, MOUSE_REPORT)
        for _ in range(4):
            await ctx.tick("usb")
        reply = await pc_in_or_none(ctx, bus, 0, 15)
        assert reply is not None and reply[0] in (DATA0, DATA1), reply
        assert reply[1:-2] == MOUSE_REPORT
        await pc_send(ctx, bus, [ACK])
        for endpoint in (1, 2, 3, 4):
            assert await pc_in_or_none(ctx, bus, 0, endpoint) is None, endpoint

    sim.add_testbench(bench)
    sim.run()


def test_an_in_and_an_out_endpoint_may_share_a_number():
    """Direction tells them apart [USB2.0 9.6.6]: an OUT to 1 reaches the OUT
    stream, and an IN from 1 still reads the IN slot's report."""
    top, sim = _make_top()
    bus, dut = top.bus, top.dut

    async def bench(ctx):
        ctx.set(dut.out_present, 1)
        ctx.set(dut.out_endpoint_number, 1)
        await ctx.tick("usb")
        await _power_up(ctx, bus, dut, in_endpoints=(1,))
        assert await pc_out(ctx, bus, 0, 1, DATA0, b"\x05\x06") == [ACK]
        assert await drain_out_stream(ctx, dut) == [b"\x05\x06"]
        await push_report(ctx, dut, 1, MOUSE_REPORT)
        for _ in range(4):
            await ctx.tick("usb")
        reply = await pc_in_or_none(ctx, bus, 0, 1)
        assert reply is not None and reply[1:-2] == MOUSE_REPORT, reply

    sim.add_testbench(bench)
    sim.run()
