from dataclasses import replace

import pytest
from amaranth.back import rtlil
from amaranth.sim import Simulator
from luna.gateware.usb.usb2.packet import (
    USBDataPacketGenerator,
    USBDataPacketReceiver,
    USBHandshakeDetector,
    USBHandshakeGenerator,
    USBInterpacketTimer,
)

from hurra_cynthion.timing import HostTiming
from hurra_cynthion.transaction import USBHostTransactionEngine
from hurra_cynthion.types import TransactionStatus

OUT_PID = 0x1
IN_PID = 0x9
SOF_PID = 0x5
SETUP_PID = 0xD

ACK = 0xD2
NAK = 0x5A
STALL = 0x1E
DATA0 = 0xC3
DATA1 = 0x4B


def usb_crc5(payload: int) -> int:
    remainder = 0x1F
    for bit_number in range(11):
        feedback = ((payload >> bit_number) & 1) ^ (remainder & 1)
        remainder >>= 1
        if feedback:
            remainder ^= 0x14
    return remainder ^ 0x1F


def token_packet(pid: int, address: int = 5, endpoint: int = 2) -> list[int]:
    payload = address | (endpoint << 7)
    return [
        pid | ((~pid & 0xF) << 4),
        payload & 0xFF,
        ((payload >> 8) & 0x07) | (usb_crc5(payload) << 3),
    ]


def usb_crc16(payload: list[int]) -> int:
    crc = 0xFFFF
    for byte in payload:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc ^ 0xFFFF


def data_packet(pid_byte: int, payload: list[int]) -> list[int]:
    crc = usb_crc16(payload)
    return [pid_byte, *payload, crc & 0xFF, crc >> 8]


def simulate(bench, timing: HostTiming | None = None, *, high_speed: int = 0) -> None:
    dut = USBHostTransactionEngine(timing=timing or HostTiming.simulation())
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def wrapped(ctx) -> None:
        ctx.set(dut.connected, 1)
        ctx.set(dut.utmi.tx_ready, 1)
        ctx.set(dut.high_speed, high_speed)
        await bench(ctx, dut)

    simulation.add_testbench(wrapped)
    simulation.run()


async def start_transaction(
    ctx,
    dut,
    pid: int,
    *,
    payload: list[int] | None = None,
    toggle: int = 0,
) -> None:
    payload = payload or []
    ctx.set(dut.token_pid, pid)
    ctx.set(dut.address, 5)
    ctx.set(dut.endpoint, 2)
    ctx.set(dut.data_toggle, toggle)
    ctx.set(dut.tx_length, len(payload))
    ctx.set(dut.tx_payload, payload[0] if payload else 0)
    ctx.set(dut.start, 1)
    await ctx.tick("usb")
    ctx.set(dut.start, 0)


async def receive_tx_packet(ctx, dut, payload: list[int] | None = None) -> list[int]:
    payload = payload or []
    for _ in range(200):
        index = ctx.get(dut.tx_index)
        ctx.set(dut.tx_payload, payload[index] if index < len(payload) else 0)
        if ctx.get(dut.utmi.tx_valid):
            break
        assert ctx.get(dut.busy)
        await ctx.tick("usb")
    else:
        raise AssertionError("timed out waiting for transmitted packet")

    packet = []
    while ctx.get(dut.utmi.tx_valid):
        index = ctx.get(dut.tx_index)
        ctx.set(dut.tx_payload, payload[index] if index < len(payload) else 0)
        packet.append(ctx.get(dut.utmi.tx_data))
        await ctx.tick("usb")
    return packet


async def send_rx_packet(ctx, dut, packet: list[int], *, byte_interval: int = 1) -> None:
    ctx.set(dut.utmi.rx_active, 1)
    ctx.set(dut.utmi.rx_valid, 0)
    await ctx.tick("usb")
    for byte in packet:
        ctx.set(dut.utmi.rx_data, byte)
        ctx.set(dut.utmi.rx_valid, 1)
        await ctx.tick("usb")
        ctx.set(dut.utmi.rx_valid, 0)
        for _ in range(byte_interval - 1):
            await ctx.tick("usb")
    ctx.set(dut.utmi.rx_valid, 0)
    ctx.set(dut.utmi.rx_active, 0)
    await ctx.tick("usb")


async def wait_done(ctx, dut, limit: int = 200) -> None:
    for _ in range(limit):
        if ctx.get(dut.done):
            return
        await ctx.tick("usb")
    raise AssertionError("transaction did not finish")


async def read_rx(ctx, dut, index: int) -> int:
    ctx.set(dut.rx_read_index, index)
    await ctx.delay(1e-9)
    return ctx.get(dut.rx_read_data)


@pytest.mark.parametrize(
    ("pid", "toggle", "expected_data_pid"),
    [(OUT_PID, 1, DATA1), (SETUP_PID, 1, DATA0)],
)
def test_out_and_setup_send_luna_data_and_accept_ack(
    pid: int, toggle: int, expected_data_pid: int
) -> None:
    payload = [0x11, 0x22, 0x33]

    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, pid, payload=payload, toggle=toggle)
        assert await receive_tx_packet(ctx, dut, payload) == token_packet(pid)
        assert await receive_tx_packet(ctx, dut, payload) == data_packet(expected_data_pid, payload)
        await send_rx_packet(ctx, dut, [ACK])
        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS
        assert ctx.get(dut.rx_length) == 0

    simulate(bench)


@pytest.mark.parametrize(
    ("handshake", "status"),
    [(NAK, TransactionStatus.NAK), (STALL, TransactionStatus.STALL)],
)
def test_out_classifies_negative_handshakes(handshake: int, status: TransactionStatus) -> None:
    async def bench(ctx, dut) -> None:
        payload = [0xA5]
        await start_transaction(ctx, dut, OUT_PID, payload=payload)
        await receive_tx_packet(ctx, dut, payload)
        await receive_tx_packet(ctx, dut, payload)
        await send_rx_packet(ctx, dut, [handshake])
        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == status

    simulate(bench)


def test_out_times_out_without_a_handshake() -> None:
    async def bench(ctx, dut) -> None:
        payload = [0x55]
        await start_transaction(ctx, dut, OUT_PID, payload=payload)
        await receive_tx_packet(ctx, dut, payload)
        await receive_tx_packet(ctx, dut, payload)
        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == TransactionStatus.TIMEOUT

    simulate(bench)


def test_malformed_in_response_still_times_out() -> None:
    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, IN_PID)
        await receive_tx_packet(ctx, dut)
        await ctx.tick("usb")
        ctx.set(dut.utmi.rx_active, 1)
        await ctx.tick("usb")
        ctx.set(dut.utmi.rx_active, 0)
        await ctx.tick("usb")
        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == TransactionStatus.TIMEOUT

    simulate(bench)


def test_sustained_rx_active_cannot_extend_response_deadline() -> None:
    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, IN_PID)
        await receive_tx_packet(ctx, dut)
        await ctx.tick("usb")
        ctx.set(dut.utmi.rx_active, 1)
        await wait_done(ctx, dut, limit=5_000)
        assert ctx.get(dut.status) == TransactionStatus.TIMEOUT

    simulate(bench)


def test_chattering_rx_active_cannot_extend_response_deadline() -> None:
    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, IN_PID)
        await receive_tx_packet(ctx, dut)
        await ctx.tick("usb")

        for cycle in range(5_000):
            ctx.set(dut.utmi.rx_active, cycle & 1)
            await ctx.tick("usb")
            if ctx.get(dut.done):
                break
        else:
            raise AssertionError("chattering RXActive extended the response forever")

        assert ctx.get(dut.status) == TransactionStatus.TIMEOUT

    simulate(bench)


@pytest.mark.parametrize(
    ("handshake", "status"),
    [(NAK, TransactionStatus.NAK), (STALL, TransactionStatus.STALL)],
)
def test_in_classifies_negative_handshakes(handshake: int, status: TransactionStatus) -> None:
    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, IN_PID)
        assert await receive_tx_packet(ctx, dut) == token_packet(IN_PID)
        await send_rx_packet(ctx, dut, [handshake])
        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == status

    simulate(bench)


def test_in_accepts_expected_data_acks_and_exposes_buffer() -> None:
    payload = [0x10, 0x20, 0x30, 0x40]

    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, IN_PID, toggle=1)
        assert await receive_tx_packet(ctx, dut) == token_packet(IN_PID)
        await send_rx_packet(ctx, dut, data_packet(DATA1, payload))
        assert await receive_tx_packet(ctx, dut) == [ACK]
        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS
        assert ctx.get(dut.rx_length) == len(payload)
        assert ctx.get(dut.rx_data_toggle) == 1
        assert not ctx.get(dut.duplicate)
        assert [await read_rx(ctx, dut, i) for i in range(len(payload))] == payload

    simulate(bench)


def test_in_waits_for_transmitted_ack_to_drain_before_done() -> None:
    timing = replace(HostTiming.simulation(), interpacket_delay_cycles=4)

    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, IN_PID, toggle=1)
        assert await receive_tx_packet(ctx, dut) == token_packet(IN_PID)
        await send_rx_packet(ctx, dut, data_packet(DATA1, [0x10]))
        assert await receive_tx_packet(ctx, dut) == [ACK]

        for _ in range(timing.interpacket_delay_cycles):
            assert ctx.get(dut.busy)
            assert not ctx.get(dut.done)
            await ctx.tick("usb")

        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS

    simulate(bench, timing)


def test_sof_waits_for_transmitted_token_to_drain_before_done() -> None:
    timing = replace(HostTiming.simulation(), interpacket_delay_cycles=4)

    async def bench(ctx, dut) -> None:
        frame = 0x345
        ctx.set(dut.sof, 1)
        ctx.set(dut.frame, frame)
        ctx.set(dut.start, 1)
        await ctx.tick("usb")
        ctx.set(dut.start, 0)
        assert await receive_tx_packet(ctx, dut) == token_packet(
            SOF_PID, address=frame & 0x7F, endpoint=frame >> 7
        )

        for _ in range(timing.interpacket_delay_cycles):
            assert ctx.get(dut.busy)
            assert not ctx.get(dut.done)
            await ctx.tick("usb")

        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS

    simulate(bench, timing)


def test_consecutive_outbound_transactions_do_not_queue_a_ghost_packet() -> None:
    setup = [0x80, 6, 0, 1, 0, 0, 8, 0]

    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, SETUP_PID, payload=setup)
        assert await receive_tx_packet(ctx, dut, setup) == token_packet(SETUP_PID)
        assert await receive_tx_packet(ctx, dut, setup) == data_packet(DATA0, setup)
        await send_rx_packet(ctx, dut, [ACK])
        await wait_done(ctx, dut)

        await start_transaction(ctx, dut, OUT_PID, toggle=1)
        assert await receive_tx_packet(ctx, dut) == token_packet(OUT_PID)
        assert await receive_tx_packet(ctx, dut) == data_packet(DATA1, [])
        await send_rx_packet(ctx, dut, [ACK])
        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS

    simulate(bench)


def test_in_crc_is_reset_after_setup_and_sof_transactions() -> None:
    setup = [0x80, 6, 0, 1, 0, 0, 8, 0]
    descriptor = [18, 1, 0, 2, 0, 0, 0, 64]

    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, SETUP_PID, payload=setup)
        assert await receive_tx_packet(ctx, dut, setup) == token_packet(SETUP_PID)
        assert await receive_tx_packet(ctx, dut, setup) == data_packet(DATA0, setup)
        await send_rx_packet(ctx, dut, [ACK])
        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS

        frame = 0x345
        ctx.set(dut.sof, 1)
        ctx.set(dut.frame, frame)
        ctx.set(dut.start, 1)
        await ctx.tick("usb")
        ctx.set(dut.start, 0)
        assert await receive_tx_packet(ctx, dut) == token_packet(
            SOF_PID, address=frame & 0x7F, endpoint=frame >> 7
        )
        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS
        ctx.set(dut.sof, 0)

        await start_transaction(ctx, dut, IN_PID, toggle=1)
        assert await receive_tx_packet(ctx, dut) == token_packet(IN_PID)
        await send_rx_packet(ctx, dut, data_packet(DATA1, descriptor))
        assert await receive_tx_packet(ctx, dut) == [ACK]
        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS

    simulate(bench)


def test_in_duplicate_is_reacked_without_overwriting_buffer() -> None:
    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, IN_PID, toggle=0)
        await receive_tx_packet(ctx, dut)
        await send_rx_packet(ctx, dut, data_packet(DATA0, [0xAA]))
        assert await receive_tx_packet(ctx, dut) == [ACK]
        await wait_done(ctx, dut)
        await ctx.tick("usb")

        await start_transaction(ctx, dut, IN_PID, toggle=1)
        await receive_tx_packet(ctx, dut)
        await send_rx_packet(ctx, dut, data_packet(DATA0, [0xBB]))
        assert await receive_tx_packet(ctx, dut) == [ACK]
        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS
        assert ctx.get(dut.duplicate)
        assert ctx.get(dut.rx_length) == 0
        assert await read_rx(ctx, dut, 0) == 0xAA

    simulate(bench)


def test_in_reports_crc_error_without_ack() -> None:
    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, IN_PID)
        await receive_tx_packet(ctx, dut)
        packet = data_packet(DATA0, [1, 2, 3])
        packet[-1] ^= 0x80

        ctx.set(dut.utmi.rx_active, 1)
        ctx.set(dut.utmi.rx_valid, 0)
        await ctx.tick("usb")
        assert not ctx.get(dut.utmi.tx_valid)
        for byte in packet:
            ctx.set(dut.utmi.rx_data, byte)
            ctx.set(dut.utmi.rx_valid, 1)
            await ctx.tick("usb")
            assert not ctx.get(dut.utmi.tx_valid)
        ctx.set(dut.utmi.rx_valid, 0)
        ctx.set(dut.utmi.rx_active, 0)
        await ctx.tick("usb")
        assert not ctx.get(dut.utmi.tx_valid)

        for _ in range(200):
            if ctx.get(dut.done):
                break
            assert not ctx.get(dut.utmi.tx_valid)
            await ctx.tick("usb")
        else:
            raise AssertionError("CRC-error transaction did not finish")

        assert ctx.get(dut.status) == TransactionStatus.CRC_ERROR
        assert not ctx.get(dut.utmi.tx_valid)

    simulate(bench)


@pytest.mark.parametrize("payload", [[], list(range(64))])
def test_in_accepts_zlp_and_maximum_packet(payload: list[int]) -> None:
    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, IN_PID)
        await receive_tx_packet(ctx, dut)
        await send_rx_packet(ctx, dut, data_packet(DATA0, payload))
        assert await receive_tx_packet(ctx, dut) == [ACK]
        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS
        assert ctx.get(dut.rx_length) == len(payload)
        if payload:
            assert await read_rx(ctx, dut, 63) == 63

    simulate(bench)


def test_in_accepts_maximum_packet_at_full_speed_utmi_cadence() -> None:
    payload = list(range(64))

    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, IN_PID)
        await receive_tx_packet(ctx, dut)
        await send_rx_packet(
            ctx,
            dut,
            data_packet(DATA0, payload),
            byte_interval=40,
        )
        assert await receive_tx_packet(ctx, dut) == [ACK]
        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS
        assert ctx.get(dut.rx_length) == 64
        assert await read_rx(ctx, dut, 63) == 63

    simulate(bench)


def test_in_reports_overflow_for_more_than_64_bytes() -> None:
    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, IN_PID)
        await receive_tx_packet(ctx, dut)
        await send_rx_packet(ctx, dut, data_packet(DATA0, list(range(65))))
        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == TransactionStatus.OVERFLOW
        assert ctx.get(dut.rx_length) == 0

    simulate(bench)


def test_disconnect_aborts_an_active_transaction() -> None:
    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, OUT_PID, payload=[1])
        assert ctx.get(dut.busy)
        ctx.set(dut.connected, 0)
        await ctx.tick("usb")
        assert ctx.get(dut.done)
        assert ctx.get(dut.status) == TransactionStatus.DISCONNECTED

    simulate(bench)


def test_start_while_busy_is_ignored_and_done_is_a_pulse() -> None:
    async def bench(ctx, dut) -> None:
        payload = [0x77]
        await start_transaction(ctx, dut, OUT_PID, payload=payload)
        ctx.set(dut.token_pid, SETUP_PID)
        ctx.set(dut.start, 1)
        await ctx.tick("usb")
        ctx.set(dut.start, 0)

        assert await receive_tx_packet(ctx, dut, payload) == token_packet(OUT_PID)
        await receive_tx_packet(ctx, dut, payload)
        await send_rx_packet(ctx, dut, [ACK])
        await wait_done(ctx, dut)
        status = ctx.get(dut.status)
        length = ctx.get(dut.rx_length)
        await ctx.tick("usb")
        assert not ctx.get(dut.done)
        assert ctx.get(dut.status) == status == TransactionStatus.SUCCESS
        assert ctx.get(dut.rx_length) == length == 0

    simulate(bench)


def test_transaction_engine_reuses_pinned_luna_packet_blocks() -> None:
    dut = USBHostTransactionEngine(timing=HostTiming.hardware())
    assert isinstance(dut._data_generator, USBDataPacketGenerator)
    assert isinstance(dut._data_receiver, USBDataPacketReceiver)
    assert isinstance(dut._handshake_generator, USBHandshakeGenerator)
    assert isinstance(dut._handshake_detector, USBHandshakeDetector)
    assert isinstance(dut._interpacket_timer, USBInterpacketTimer)

    netlist = rtlil.convert(
        dut,
        ports=[
            dut.start,
            dut.token_pid,
            dut.address,
            dut.endpoint,
            dut.data_toggle,
            dut.tx_length,
            dut.tx_payload,
            dut.tx_index,
            dut.connected,
            dut.busy,
            dut.done,
            dut.status,
            dut.rx_length,
            dut.rx_read_index,
            dut.rx_read_data,
            dut.rx_data_toggle,
            dut.duplicate,
        ],
    )
    assert len(netlist) < 1_000_000


def test_high_speed_in_transaction_completes() -> None:
    """A whole IN transaction must complete with high_speed asserted.

    This is the guard on ``fs_only``. USBInterpacketTimer takes fs_only as a
    *construction* parameter, and when it is set LUNA omits the High Speed
    branch from the elaborated design entirely -- so driving speed=HIGH would
    leave rx_to_tx_at_min, tx_to_rx_timeout and friends permanently deasserted
    and the engine would wait forever rather than fail visibly.
    """

    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, IN_PID, toggle=1)
        assert await receive_tx_packet(ctx, dut) == token_packet(IN_PID)
        await send_rx_packet(ctx, dut, data_packet(DATA1, [0x10, 0x20]))
        assert await receive_tx_packet(ctx, dut) == [ACK]
        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS
        assert ctx.get(dut.rx_length) == 2
        assert await read_rx(ctx, dut, 0) == 0x10
        assert await read_rx(ctx, dut, 1) == 0x20

    simulate(bench, high_speed=1)


def test_high_speed_drain_uses_the_high_speed_interpacket_delay() -> None:
    """The ACK drain is bounded by the High Speed constant when high_speed is set."""
    timing = replace(
        HostTiming.simulation(),
        interpacket_delay_cycles=4,
        hs_interpacket_delay_cycles=9,
    )

    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, IN_PID, toggle=1)
        assert await receive_tx_packet(ctx, dut) == token_packet(IN_PID)
        await send_rx_packet(ctx, dut, data_packet(DATA1, [0x10]))
        assert await receive_tx_packet(ctx, dut) == [ACK]

        # Must still be draining after the *Full Speed* delay would have ended.
        for _ in range(timing.hs_interpacket_delay_cycles):
            assert ctx.get(dut.busy)
            assert not ctx.get(dut.done)
            await ctx.tick("usb")

        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS

    simulate(bench, timing, high_speed=1)


def test_full_speed_drain_is_unaffected_by_the_high_speed_constant() -> None:
    """With high_speed deasserted the Full Speed delay still governs."""
    timing = replace(
        HostTiming.simulation(),
        interpacket_delay_cycles=9,
        hs_interpacket_delay_cycles=1,
    )

    async def bench(ctx, dut) -> None:
        await start_transaction(ctx, dut, IN_PID, toggle=1)
        assert await receive_tx_packet(ctx, dut) == token_packet(IN_PID)
        await send_rx_packet(ctx, dut, data_packet(DATA1, [0x10]))
        assert await receive_tx_packet(ctx, dut) == [ACK]

        for _ in range(timing.interpacket_delay_cycles):
            assert ctx.get(dut.busy)
            assert not ctx.get(dut.done)
            await ctx.tick("usb")

        await wait_done(ctx, dut)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS

    simulate(bench, timing, high_speed=0)
