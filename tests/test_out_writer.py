import re

import pytest
from amaranth import Signal
from amaranth.back import rtlil
from amaranth.sim import Simulator
from test_poller import ScriptedTransaction
from test_transaction import ACK, DATA0, DATA1, NAK, data_packet, send_rx_packet, token_packet

from hurra_cynthion.out_writer import InterruptOutWriter
from hurra_cynthion.timing import HostTiming
from hurra_cynthion.transaction import OUT_PID, USBHostTransactionPort
from hurra_cynthion.types import TransactionStatus

PULSES = ("pulse_written", "pulse_nak", "pulse_stall", "pulse_timeout", "pulse_dropped")


class ScriptedOutTransaction(ScriptedTransaction):
    """The poller's scripted seam plus the arbiter's ``start_ready`` grant."""

    def __init__(self) -> None:
        super().__init__()
        self.start_ready = Signal(init=1)


def simulate(bench, *, timing: HostTiming | None = None, max_packet_size: int = 8) -> None:
    transaction = ScriptedOutTransaction()
    dut = InterruptOutWriter(transaction=transaction, timing=timing or HostTiming.simulation())
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def wrapped(ctx) -> None:
        ctx.set(dut.address, 5)
        ctx.set(dut.endpoint, 3)
        ctx.set(dut.max_packet_size, max_packet_size)
        ctx.set(dut.interval, 2)
        ctx.set(dut.connected, 1)
        ctx.set(dut.enable, 1)
        await ctx.tick("usb")
        await bench(ctx, dut, transaction)

    simulation.add_testbench(wrapped)
    simulation.run()


async def pulse_sofs(ctx, dut, count: int) -> None:
    for _ in range(count):
        ctx.set(dut.sof_tick, 1)
        await ctx.tick("usb")
        ctx.set(dut.sof_tick, 0)


async def send_packet(ctx, dut, payload: bytes, *, last: bool = True) -> None:
    """Offer ``payload`` one byte per cycle; every byte must be accepted at once."""
    for index, byte in enumerate(payload):
        ctx.set(dut.out_valid, 1)
        ctx.set(dut.out_data, byte)
        ctx.set(dut.out_last, last and index == len(payload) - 1)
        assert ctx.get(dut.out_ready), f"byte {index} refused"
        await ctx.tick("usb")
    ctx.set(dut.out_valid, 0)
    ctx.set(dut.out_last, 0)


def read_payload(ctx, transaction, length: int) -> bytes:
    """Step ``tx_index`` the way the engine's data generator does."""
    data = bytearray()
    for index in range(length):
        ctx.set(transaction.tx_index, index)
        data.append(ctx.get(transaction.tx_payload))
    return bytes(data)


async def accept_write(ctx, dut, transaction, *, toggle: int, payload: bytes) -> None:
    assert ctx.get(transaction.start)
    assert ctx.get(transaction.token_pid) == OUT_PID
    assert ctx.get(transaction.address) == 5
    assert ctx.get(transaction.endpoint) == 3
    assert ctx.get(transaction.data_toggle) == toggle
    assert ctx.get(transaction.tx_length) == len(payload)
    await ctx.tick("usb")
    assert ctx.get(dut.active)
    assert not ctx.get(transaction.start)
    ctx.set(transaction.busy, 1)
    assert read_payload(ctx, transaction, len(payload)) == payload


async def complete_write(ctx, transaction, status: TransactionStatus) -> None:
    ctx.set(transaction.status, status.value)
    ctx.set(transaction.busy, 0)
    ctx.set(transaction.done, 1)
    await ctx.tick("usb")
    ctx.set(transaction.done, 0)


async def issue_due_write(ctx, dut, transaction, *, toggle: int, payload: bytes) -> None:
    await pulse_sofs(ctx, dut, ctx.get(dut.interval))
    await accept_write(ctx, dut, transaction, toggle=toggle, payload=payload)


def pulses(ctx, dut) -> set[str]:
    return {name for name in PULSES if ctx.get(getattr(dut, name))}


def test_short_packet_is_written_as_out_and_success_flips_the_toggle() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await send_packet(ctx, dut, b"\x01\x02\x03")
        # Held, not dropped: refusing the next byte is what makes the clone NAK the PC.
        assert not ctx.get(dut.out_ready)
        await issue_due_write(ctx, dut, transaction, toggle=0, payload=b"\x01\x02\x03")
        await complete_write(ctx, transaction, TransactionStatus.SUCCESS)
        assert pulses(ctx, dut) == {"pulse_written"}
        assert ctx.get(dut.toggle) == 1
        assert ctx.get(dut.last_status) == TransactionStatus.SUCCESS
        assert not ctx.get(dut.active)
        assert ctx.get(dut.out_ready)
        await ctx.tick("usb")
        assert pulses(ctx, dut) == set()

        await send_packet(ctx, dut, b"\xaa\xbb")
        await issue_due_write(ctx, dut, transaction, toggle=1, payload=b"\xaa\xbb")

    simulate(bench)


def test_full_max_packet_without_last_is_framed_by_max_packet_size() -> None:
    """LUNA's OUT endpoint does not mark ``last`` on a full-size packet."""
    payload = bytes(range(64))

    async def bench(ctx, dut, transaction) -> None:
        await send_packet(ctx, dut, payload, last=False)
        ctx.set(dut.out_valid, 1)
        ctx.set(dut.out_data, 0xEE)
        assert not ctx.get(dut.out_ready)
        ctx.set(dut.out_valid, 0)
        await issue_due_write(ctx, dut, transaction, toggle=0, payload=payload)

    simulate(bench, max_packet_size=64)


def test_small_endpoint_frames_a_continuous_stream_at_its_max_packet_size() -> None:
    stream = bytes(range(0x10, 0x20))

    async def bench(ctx, dut, transaction) -> None:
        await send_packet(ctx, dut, stream[:8], last=False)
        assert not ctx.get(dut.out_ready)
        await issue_due_write(ctx, dut, transaction, toggle=0, payload=stream[:8])
        await complete_write(ctx, transaction, TransactionStatus.SUCCESS)

        await send_packet(ctx, dut, stream[8:], last=False)
        assert not ctx.get(dut.out_ready)
        await issue_due_write(ctx, dut, transaction, toggle=1, payload=stream[8:])

    simulate(bench, max_packet_size=8)


def test_nak_retries_the_same_packet_and_toggle_only_at_the_next_due() -> None:
    payload = b"\x5a\xa5\x01"

    async def bench(ctx, dut, transaction) -> None:
        await send_packet(ctx, dut, payload)
        await issue_due_write(ctx, dut, transaction, toggle=0, payload=payload)
        await complete_write(ctx, transaction, TransactionStatus.NAK)
        assert pulses(ctx, dut) == {"pulse_nak"}
        assert ctx.get(dut.last_status) == TransactionStatus.NAK
        assert ctx.get(dut.toggle) == 0
        assert not ctx.get(dut.active)
        assert not ctx.get(dut.out_ready)

        for _ in range(4):
            assert not ctx.get(transaction.start)
            await ctx.tick("usb")
        await pulse_sofs(ctx, dut, 1)
        assert not ctx.get(transaction.start)
        await pulse_sofs(ctx, dut, 1)
        await accept_write(ctx, dut, transaction, toggle=0, payload=payload)
        await complete_write(ctx, transaction, TransactionStatus.SUCCESS)
        assert pulses(ctx, dut) == {"pulse_written"}
        assert ctx.get(dut.toggle) == 1

    simulate(bench)


def test_stall_drops_and_counts_the_packet_without_disabling_the_writer() -> None:
    async def bench(ctx, dut, transaction) -> None:
        # A STALL on a rumble or LED endpoint must never be able to stop input.
        assert not hasattr(dut, "failed")
        await send_packet(ctx, dut, b"\x01")
        await issue_due_write(ctx, dut, transaction, toggle=0, payload=b"\x01")
        await complete_write(ctx, transaction, TransactionStatus.STALL)
        assert pulses(ctx, dut) == {"pulse_stall", "pulse_dropped"}
        assert ctx.get(dut.last_status) == TransactionStatus.STALL
        assert ctx.get(dut.toggle) == 0
        assert ctx.get(dut.out_ready)

        await send_packet(ctx, dut, b"\x02\x03")
        await issue_due_write(ctx, dut, transaction, toggle=0, payload=b"\x02\x03")
        await complete_write(ctx, transaction, TransactionStatus.SUCCESS)
        assert pulses(ctx, dut) == {"pulse_written"}
        assert ctx.get(dut.toggle) == 1

    simulate(bench)


@pytest.mark.parametrize(
    "status",
    [TransactionStatus.TIMEOUT, TransactionStatus.CRC_ERROR, TransactionStatus.OVERFLOW],
)
def test_transport_errors_retry_the_same_packet_then_drop_it(status: TransactionStatus) -> None:
    timing = HostTiming.simulation()
    payload = b"\x10\x20"

    async def bench(ctx, dut, transaction) -> None:
        await send_packet(ctx, dut, payload)
        for attempt in range(1, timing.max_transport_errors + 1):
            await issue_due_write(ctx, dut, transaction, toggle=0, payload=payload)
            await complete_write(ctx, transaction, status)
            assert ctx.get(dut.last_status) == status
            if attempt < timing.max_transport_errors:
                assert pulses(ctx, dut) == set()
                assert not ctx.get(dut.out_ready)
                assert not ctx.get(transaction.start)
            else:
                assert pulses(ctx, dut) == {"pulse_timeout", "pulse_dropped"}
                assert ctx.get(dut.out_ready)
        assert ctx.get(dut.toggle) == 0

        await pulse_sofs(ctx, dut, 4)
        assert not ctx.get(transaction.start)

        # A fresh packet starts a fresh error budget. Its due already latched above.
        await send_packet(ctx, dut, b"\x30")
        await accept_write(ctx, dut, transaction, toggle=0, payload=b"\x30")
        await complete_write(ctx, transaction, status)
        assert pulses(ctx, dut) == set()
        assert not ctx.get(dut.out_ready)

    simulate(bench, timing=timing)


def test_a_nak_resets_the_transport_error_budget() -> None:
    """Only CONSECUTIVE transport errors drop a packet, as in the IN poller.

    A NAK proves the device is present and answering, so errors either side of
    one are not evidence of a gone device and must not add up to a drop.
    """
    timing = HostTiming.simulation()
    payload = b"\x77\x88"
    errors = timing.max_transport_errors - 1

    async def bench(ctx, dut, transaction) -> None:
        await send_packet(ctx, dut, payload)
        for _ in range(3):
            for _ in range(errors):
                await issue_due_write(ctx, dut, transaction, toggle=0, payload=payload)
                await complete_write(ctx, transaction, TransactionStatus.TIMEOUT)
                assert pulses(ctx, dut) == set()
            await issue_due_write(ctx, dut, transaction, toggle=0, payload=payload)
            await complete_write(ctx, transaction, TransactionStatus.NAK)
            assert pulses(ctx, dut) == {"pulse_nak"}
            assert not ctx.get(dut.out_ready)

        # The budget is still whole: exactly max_transport_errors in a row drop it.
        for attempt in range(1, timing.max_transport_errors + 1):
            await issue_due_write(ctx, dut, transaction, toggle=0, payload=payload)
            await complete_write(ctx, transaction, TransactionStatus.TIMEOUT)
            if attempt < timing.max_transport_errors:
                assert pulses(ctx, dut) == set()
            else:
                assert pulses(ctx, dut) == {"pulse_timeout", "pulse_dropped"}
                assert ctx.get(dut.out_ready)

    simulate(bench, timing=timing)


def test_disconnected_status_drops_the_packet() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await send_packet(ctx, dut, b"\x01")
        await issue_due_write(ctx, dut, transaction, toggle=0, payload=b"\x01")
        await complete_write(ctx, transaction, TransactionStatus.DISCONNECTED)
        assert pulses(ctx, dut) == {"pulse_dropped"}
        assert ctx.get(dut.last_status) == TransactionStatus.DISCONNECTED
        assert ctx.get(dut.out_ready)
        assert ctx.get(dut.toggle) == 0

    simulate(bench)


@pytest.mark.parametrize("line", ["enable", "connected"])
def test_disabled_drains_and_discards_bytes_and_resets_the_toggle(line: str) -> None:
    async def bench(ctx, dut, transaction) -> None:
        await send_packet(ctx, dut, b"\x01")
        await issue_due_write(ctx, dut, transaction, toggle=0, payload=b"\x01")
        await complete_write(ctx, transaction, TransactionStatus.SUCCESS)
        assert ctx.get(dut.toggle) == 1

        # A packet still pending when the writer is disabled is discarded too.
        await send_packet(ctx, dut, b"\xde\xad")
        assert not ctx.get(dut.out_ready)
        ctx.set(getattr(dut, line), 0)
        await ctx.tick("usb")
        assert ctx.get(dut.toggle) == 0
        assert not ctx.get(dut.active)
        assert ctx.get(dut.out_ready)

        await send_packet(ctx, dut, bytes(range(20)), last=False)
        await send_packet(ctx, dut, b"\xbe\xef")
        for _ in range(3):
            await pulse_sofs(ctx, dut, 2)
            assert not ctx.get(transaction.start)

        ctx.set(getattr(dut, line), 1)
        await ctx.tick("usb")
        for _ in range(2):
            await pulse_sofs(ctx, dut, 2)
            assert not ctx.get(transaction.start)
        await send_packet(ctx, dut, b"\x42")
        await accept_write(ctx, dut, transaction, toggle=0, payload=b"\x42")

    simulate(bench)


def test_disable_during_a_write_releases_active_and_ignores_the_late_done() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await send_packet(ctx, dut, b"\x01")
        await issue_due_write(ctx, dut, transaction, toggle=0, payload=b"\x01")
        ctx.set(dut.enable, 0)
        await ctx.tick("usb")
        assert not ctx.get(dut.active)
        await complete_write(ctx, transaction, TransactionStatus.SUCCESS)
        assert pulses(ctx, dut) == set()
        assert ctx.get(dut.toggle) == 0

    simulate(bench)


async def pulse_flush(ctx, dut) -> None:
    ctx.set(dut.flush, 1)
    await ctx.tick("usb")
    ctx.set(dut.flush, 0)


def test_flush_discards_a_partly_filled_packet_and_keeps_the_toggle() -> None:
    """The clone resets its OUT FIFO on a PC bus reset or SET_CONFIGURATION.

    Bytes the writer had already taken out of that FIFO belong to the old
    session. Kept, they were spliced onto the front of the new session's first
    packet. The toggle is the real device's sequence, which the PC's reset of
    the clone never touched, so it survives.
    """

    async def bench(ctx, dut, transaction) -> None:
        await send_packet(ctx, dut, b"\x01")
        await issue_due_write(ctx, dut, transaction, toggle=0, payload=b"\x01")
        await complete_write(ctx, transaction, TransactionStatus.SUCCESS)
        assert ctx.get(dut.toggle) == 1

        await send_packet(ctx, dut, b"\xde\xad\xbe", last=False)
        await pulse_flush(ctx, dut)
        assert pulses(ctx, dut) == set()
        assert ctx.get(dut.toggle) == 1
        await send_packet(ctx, dut, b"\x42\x43")
        await issue_due_write(ctx, dut, transaction, toggle=1, payload=b"\x42\x43")

    simulate(bench)


def test_a_byte_arriving_with_the_flush_is_discarded_even_if_it_ends_a_packet() -> None:
    """The FIFO resets on the same edge, so a byte popped that cycle is stale too."""

    async def bench(ctx, dut, transaction) -> None:
        await send_packet(ctx, dut, b"\xde\xad", last=False)
        ctx.set(dut.out_valid, 1)
        ctx.set(dut.out_data, 0xEF)
        ctx.set(dut.out_last, 1)
        await pulse_flush(ctx, dut)
        ctx.set(dut.out_valid, 0)
        ctx.set(dut.out_last, 0)
        assert ctx.get(dut.out_ready), "a stale packet must not be held"

        await send_packet(ctx, dut, b"\x07")
        await issue_due_write(ctx, dut, transaction, toggle=0, payload=b"\x07")

    simulate(bench)


def test_flush_drops_and_counts_a_packet_waiting_for_its_interval() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await send_packet(ctx, dut, b"\x01\x02")
        assert not ctx.get(dut.out_ready)
        await pulse_flush(ctx, dut)
        assert pulses(ctx, dut) == {"pulse_dropped"}
        assert ctx.get(dut.out_ready)
        await pulse_sofs(ctx, dut, 4)
        assert not ctx.get(transaction.start), "the dropped packet was still sent"

        # The interval came due with nothing to send; the next packet goes at once.
        await send_packet(ctx, dut, b"\x03")
        await accept_write(ctx, dut, transaction, toggle=0, payload=b"\x03")

    simulate(bench)


def test_flush_cannot_recall_a_packet_already_on_the_wire() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await send_packet(ctx, dut, b"\x01")
        await issue_due_write(ctx, dut, transaction, toggle=0, payload=b"\x01")
        await pulse_flush(ctx, dut)
        assert ctx.get(dut.active)
        await complete_write(ctx, transaction, TransactionStatus.SUCCESS)
        assert pulses(ctx, dut) == {"pulse_written"}
        assert ctx.get(dut.toggle) == 1

    simulate(bench)


def test_active_spans_issue_to_done() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await send_packet(ctx, dut, b"\x01")
        assert not ctx.get(dut.active)
        await pulse_sofs(ctx, dut, 2)
        assert ctx.get(transaction.start)
        assert not ctx.get(dut.active)
        await ctx.tick("usb")
        ctx.set(transaction.busy, 1)
        for _ in range(5):
            assert ctx.get(dut.active)
            await ctx.tick("usb")
        ctx.set(transaction.status, TransactionStatus.SUCCESS.value)
        ctx.set(transaction.busy, 0)
        ctx.set(transaction.done, 1)
        assert ctx.get(dut.active)
        await ctx.tick("usb")
        ctx.set(transaction.done, 0)
        assert not ctx.get(dut.active)

    simulate(bench)


def test_issue_waits_for_an_idle_engine_and_the_arbiter_grant() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await send_packet(ctx, dut, b"\x01")
        ctx.set(transaction.busy, 1)
        await pulse_sofs(ctx, dut, 2)
        assert not ctx.get(transaction.start)
        ctx.set(transaction.busy, 0)
        ctx.set(transaction.start_ready, 0)
        assert not ctx.get(transaction.start)
        # Due stays latched across further periods rather than piling up.
        await pulse_sofs(ctx, dut, 3)
        assert not ctx.get(transaction.start)
        ctx.set(transaction.start_ready, 1)
        await accept_write(ctx, dut, transaction, toggle=0, payload=b"\x01")
        await complete_write(ctx, transaction, TransactionStatus.SUCCESS)
        await send_packet(ctx, dut, b"\x02")
        assert not ctx.get(transaction.start)

    simulate(bench)


def test_back_to_back_packets_are_paced_one_per_interval() -> None:
    async def bench(ctx, dut, transaction) -> None:
        ctx.set(dut.interval, 3)
        for k in range(3):
            await send_packet(ctx, dut, bytes([k]))
            ticks = 0
            while not ctx.get(transaction.start):
                assert ticks < 3
                await pulse_sofs(ctx, dut, 1)
                ticks += 1
            assert ticks == 3
            await accept_write(ctx, dut, transaction, toggle=k & 1, payload=bytes([k]))
            await complete_write(ctx, transaction, TransactionStatus.SUCCESS)

    simulate(bench)


def _ticks_to_write(high_speed: int, interval: int, *, bound: int) -> int | None:
    """Return how many sof_ticks elapse before a buffered packet is first issued."""
    seen: dict[str, int | None] = {"n": None}

    async def bench(ctx, dut, transaction) -> None:
        ctx.set(dut.high_speed, high_speed)
        ctx.set(dut.interval, interval)
        await send_packet(ctx, dut, b"\x01")
        for n in range(1, bound + 1):
            await pulse_sofs(ctx, dut, 1)
            if ctx.get(transaction.start):
                seen["n"] = n
                return

    simulate(bench)
    return seen["n"]


def test_full_speed_interval_is_a_direct_frame_count() -> None:
    assert _ticks_to_write(0, 4, bound=64) == 4
    assert _ticks_to_write(0, 10, bound=64) == 10


def test_high_speed_interval_is_an_exponent_not_a_count() -> None:
    assert _ticks_to_write(1, 1, bound=64) == 1
    assert _ticks_to_write(1, 2, bound=64) == 2
    assert _ticks_to_write(1, 3, bound=64) == 4
    # bInterval=4 is 8 microframes = 1 ms, NOT 4 microframes.
    assert _ticks_to_write(1, 4, bound=64) == 8
    assert _ticks_to_write(1, 5, bound=64) == 16


def test_zero_interval_writes_every_tick_at_both_speeds() -> None:
    assert _ticks_to_write(0, 0, bound=64) == 1
    assert _ticks_to_write(1, 0, bound=64) == 1


def test_writer_has_exactly_one_local_64_by_8_memory() -> None:
    # The poller's seam has no start_ready, which exercises the ~busy fallback.
    transaction = ScriptedTransaction()
    dut = InterruptOutWriter(transaction=transaction, timing=HostTiming.simulation())
    netlist = rtlil.convert(dut, ports=[dut.enable, dut.out_valid, dut.out_data, dut.out_ready])
    assert len(re.findall(r"(?m)^\s*memory width 8 size 64", netlist)) == 1
    assert len(netlist) < 300_000


def test_writer_drives_a_shared_port_without_owning_an_engine() -> None:
    port = USBHostTransactionPort()
    dut = InterruptOutWriter(
        transaction=port, timing=HostTiming.simulation(), add_transaction_submodule=False
    )
    netlist = rtlil.convert(
        dut, ports=[dut.enable, dut.out_valid, dut.out_data, dut.out_ready, port.start]
    )
    assert len(re.findall(r"(?m)^\s*memory width 8 size 64", netlist)) == 1


def test_writer_elaborates_with_real_transaction_engine() -> None:
    dut = InterruptOutWriter(timing=HostTiming.simulation())
    netlist = rtlil.convert(
        dut,
        ports=[
            dut.enable,
            dut.connected,
            dut.sof_tick,
            dut.address,
            dut.endpoint,
            dut.max_packet_size,
            dut.interval,
            dut.high_speed,
            dut.out_valid,
            dut.out_data,
            dut.out_last,
            dut.out_ready,
            dut.active,
            dut.last_status,
            dut.toggle,
            *(getattr(dut, name) for name in PULSES),
        ],
    )
    assert len(re.findall(r"(?m)^\s*memory width 8 size 64", netlist)) == 2
    assert len(netlist) < 1_000_000


async def collect_tx_packet(ctx, utmi, *, limit: int = 200) -> list[int]:
    for _ in range(limit):
        if ctx.get(utmi.tx_valid):
            break
        await ctx.tick("usb")
    else:
        raise AssertionError("timed out waiting for a transmitted packet")
    packet = []
    while ctx.get(utmi.tx_valid):
        packet.append(ctx.get(utmi.tx_data))
        await ctx.tick("usb")
    return packet


async def wait_for_pulse(ctx, dut, name: str, *, limit: int = 50) -> None:
    for _ in range(limit):
        if ctx.get(getattr(dut, name)):
            return
        await ctx.tick("usb")
    raise AssertionError(f"{name} never pulsed")


def test_real_engine_transmits_the_buffered_packet_from_tx_index() -> None:
    """The engine reads ``tx_payload`` combinationally as it steps ``tx_index``."""
    payload = [0x11, 0x22, 0x33, 0x44, 0x55]
    dut = InterruptOutWriter(timing=HostTiming.simulation())
    engine = dut.transaction
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def write_once(ctx, data_pid: int, handshake: int) -> None:
        assert ctx.get(engine.start)
        assert await collect_tx_packet(ctx, engine.utmi) == token_packet(OUT_PID, 5, 3)
        assert await collect_tx_packet(ctx, engine.utmi) == data_packet(data_pid, payload)
        await send_rx_packet(ctx, engine, [handshake])

    async def bench(ctx) -> None:
        ctx.set(engine.utmi.tx_ready, 1)
        ctx.set(dut.address, 5)
        ctx.set(dut.endpoint, 3)
        ctx.set(dut.max_packet_size, 8)
        ctx.set(dut.interval, 1)
        ctx.set(dut.connected, 1)
        ctx.set(dut.enable, 1)
        await ctx.tick("usb")

        await send_packet(ctx, dut, bytes(payload))
        await pulse_sofs(ctx, dut, 1)
        await write_once(ctx, DATA0, NAK)
        await wait_for_pulse(ctx, dut, "pulse_nak")
        await pulse_sofs(ctx, dut, 1)
        await write_once(ctx, DATA0, ACK)
        await wait_for_pulse(ctx, dut, "pulse_written")
        assert ctx.get(dut.toggle) == 1

        await send_packet(ctx, dut, bytes(payload))
        await pulse_sofs(ctx, dut, 1)
        await write_once(ctx, DATA1, ACK)
        await wait_for_pulse(ctx, dut, "pulse_written")
        assert ctx.get(dut.toggle) == 0

    simulation.add_testbench(bench)
    simulation.run()
