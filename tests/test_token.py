from collections.abc import Iterable

import pytest
from amaranth.back import rtlil
from amaranth.sim import Simulator

from hurra_cynthion.timing import HostTiming
from hurra_cynthion.token import USBTokenGenerator
from hurra_cynthion.types import HostError, TransactionStatus

TOKEN_PIDS = (0x1, 0x9, 0x5, 0xD)  # OUT, IN, SOF, SETUP
SOF_PID = 0x5


def test_public_status_values_are_stable() -> None:
    assert {status.name: status.value for status in TransactionStatus} == {
        "SUCCESS": 0,
        "NAK": 1,
        "STALL": 2,
        "TIMEOUT": 3,
        "CRC_ERROR": 4,
        "OVERFLOW": 5,
        "DISCONNECTED": 6,
    }
    assert {error.name: error.value for error in HostError} == {
        "NONE": 0,
        "UNSUPPORTED_SPEED": 1,
        "UNSUPPORTED_TOPOLOGY": 2,
        "MALFORMED_DESCRIPTOR": 3,
        "OVERSIZED_DESCRIPTOR": 4,
        "CONTROL_FAILURE": 5,
        "POLLING_FAILURE": 6,
        "DISCONNECTED": 7,
    }


def test_hardware_timing_profile_uses_usb_millisecond_intervals() -> None:
    timing = HostTiming.hardware()
    assert timing.clock_hz == 60_000_000
    assert timing.frame_cycles == 60_000
    assert timing.attach_stable_cycles == 6_000_000
    # Root-port reset: USB 2.0 section 7.1.7.5 requires an overall reset of at
    # least 50 ms (TDRSTR) for a reset driven from a root port. 3,000,000 cycles
    # at 60 MHz is exactly 50 ms.
    assert timing.reset_cycles == 3_000_000
    assert timing.reset_recovery_cycles == 600_000
    assert timing.address_recovery_cycles == 120_000
    assert timing.vbus_discharge_cycles == 6_000_000
    assert timing.transaction_timeout_cycles > 0
    assert timing.interpacket_delay_cycles > 0
    assert timing.max_transport_errors > 0
    assert timing.control_nak_timeout_cycles == 30_000_000

    simulation = HostTiming.simulation()
    assert simulation.reset_recovery_cycles == 3
    assert simulation.address_recovery_cycles == 2
    assert simulation.control_nak_timeout_cycles == 32


def test_simulation_timing_profile_is_small_and_nonzero() -> None:
    timing = HostTiming.simulation()
    cycle_counts = (
        timing.frame_cycles,
        timing.attach_stable_cycles,
        timing.reset_cycles,
        timing.vbus_discharge_cycles,
        timing.transaction_timeout_cycles,
        timing.interpacket_delay_cycles,
        timing.max_transport_errors,
    )
    assert all(0 < count <= 16 for count in cycle_counts)


def test_token_generator_has_a_bounded_combinational_crc_network() -> None:
    dut = USBTokenGenerator()
    netlist = rtlil.convert(
        dut,
        ports=[
            dut.start,
            dut.pid,
            dut.address,
            dut.endpoint,
            dut.frame,
            dut.tx_valid,
            dut.tx_ready,
            dut.tx_data,
            dut.busy,
            dut.done,
        ],
    )
    assert len(netlist) < 100_000


def reference_usb_crc5(payload: int) -> int:
    remainder = 0x1F
    for bit_number in range(11):
        feedback = ((payload >> bit_number) & 1) ^ (remainder & 1)
        remainder >>= 1
        if feedback:
            remainder ^= 0x14
    return remainder ^ 0x1F


def expected_token(pid: int, payload: int) -> list[int]:
    pid_byte = pid | ((~pid & 0xF) << 4)
    return [
        pid_byte,
        payload & 0xFF,
        ((payload >> 8) & 0x07) | (reference_usb_crc5(payload) << 3),
    ]


async def start_token(ctx, dut, pid: int, payload: int) -> None:
    ctx.set(dut.pid, pid)
    if pid == SOF_PID:
        ctx.set(dut.frame, payload)
        ctx.set(dut.address, (~payload) & 0x7F)
        ctx.set(dut.endpoint, ((~payload) >> 7) & 0xF)
    else:
        ctx.set(dut.address, payload & 0x7F)
        ctx.set(dut.endpoint, (payload >> 7) & 0xF)
        ctx.set(dut.frame, (~payload) & 0x7FF)
    ctx.set(dut.start, 1)
    await ctx.tick()
    ctx.set(dut.start, 0)


async def accept_packet(ctx, dut) -> list[int]:
    packet = []
    while len(packet) < 3:
        assert ctx.get(dut.busy)
        assert ctx.get(dut.tx_valid)
        packet.append(ctx.get(dut.tx_data))
        await ctx.tick()
    return packet


def simulate(bench) -> None:
    dut = USBTokenGenerator()
    simulation = Simulator(dut)
    simulation.add_clock(1e-6)

    async def wrapped(ctx) -> None:
        await bench(ctx, dut)

    simulation.add_testbench(wrapped)
    simulation.run()


@pytest.mark.parametrize("pid", TOKEN_PIDS)
def test_all_token_pids_have_correct_complements(pid: int) -> None:
    async def bench(ctx, dut) -> None:
        ctx.set(dut.tx_ready, 1)
        await start_token(ctx, dut, pid, 0x5A5)
        packet = await accept_packet(ctx, dut)
        assert packet == expected_token(pid, 0x5A5)
        assert ctx.get(dut.done)
        assert not ctx.get(dut.busy)
        await ctx.tick()
        assert not ctx.get(dut.done)

    simulate(bench)


def test_token_payload_encoding_is_exhaustive() -> None:
    async def bench(ctx, dut) -> None:
        ctx.set(dut.tx_ready, 1)
        for pid in TOKEN_PIDS:
            for payload in range(1 << 11):
                await start_token(ctx, dut, pid, payload)
                assert await accept_packet(ctx, dut) == expected_token(pid, payload)
                assert ctx.get(dut.done)
                await ctx.tick()

    simulate(bench)


def test_sof_uses_frame_instead_of_address_and_endpoint() -> None:
    async def bench(ctx, dut) -> None:
        ctx.set(dut.tx_ready, 1)
        await start_token(ctx, dut, SOF_PID, 0x6D3)
        assert await accept_packet(ctx, dut) == expected_token(SOF_PID, 0x6D3)

    simulate(bench)


def test_utmi_backpressure_holds_each_byte_and_packet_state() -> None:
    stalls: Iterable[int] = (3, 1, 4)

    async def bench(ctx, dut) -> None:
        await start_token(ctx, dut, 0xD, 0x42A)
        expected = expected_token(0xD, 0x42A)
        for expected_byte, stall_cycles in zip(expected, stalls, strict=True):
            assert ctx.get(dut.tx_valid)
            assert ctx.get(dut.tx_data) == expected_byte
            for _ in range(stall_cycles):
                assert ctx.get(dut.busy)
                assert ctx.get(dut.tx_valid)
                assert ctx.get(dut.tx_data) == expected_byte
                assert not ctx.get(dut.done)
                await ctx.tick()
            ctx.set(dut.tx_ready, 1)
            await ctx.tick()
            ctx.set(dut.tx_ready, 0)
        assert not ctx.get(dut.busy)
        assert ctx.get(dut.done)
        assert not ctx.get(dut.tx_valid)

    simulate(bench)


def test_abort_discards_partial_token_before_next_start() -> None:
    async def bench(ctx, dut) -> None:
        ctx.set(dut.tx_ready, 1)
        await start_token(ctx, dut, SOF_PID, 0x345)
        assert ctx.get(dut.tx_data) == expected_token(SOF_PID, 0x345)[0]
        await ctx.tick()

        ctx.set(dut.abort, 1)
        await ctx.tick()
        ctx.set(dut.abort, 0)
        assert not ctx.get(dut.busy)
        assert not ctx.get(dut.tx_valid)
        assert not ctx.get(dut.done)

        await start_token(ctx, dut, 0x9, 0x001)
        assert await accept_packet(ctx, dut) == expected_token(0x9, 0x001)

    simulate(bench)


def test_start_while_busy_is_ignored() -> None:
    async def bench(ctx, dut) -> None:
        await start_token(ctx, dut, 0x1, 0x123)
        assert ctx.get(dut.tx_data) == expected_token(0x1, 0x123)[0]

        ctx.set(dut.pid, 0x9)
        ctx.set(dut.address, 0x55)
        ctx.set(dut.endpoint, 0xA)
        ctx.set(dut.start, 1)
        await ctx.tick()
        ctx.set(dut.start, 0)

        ctx.set(dut.tx_ready, 1)
        packet = await accept_packet(ctx, dut)
        assert packet == expected_token(0x1, 0x123)
        assert ctx.get(dut.done)

        await ctx.tick()
        assert not ctx.get(dut.busy)
        assert not ctx.get(dut.done)
        assert not ctx.get(dut.tx_valid)

    simulate(bench)
