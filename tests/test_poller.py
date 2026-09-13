import re

from amaranth import Array, Elaboratable, Module, Signal
from amaranth.back import rtlil
from amaranth.sim import Simulator

from hurra_cynthion.poller import InterruptInPoller
from hurra_cynthion.timing import HostTiming
from hurra_cynthion.types import TransactionStatus

IN_PID = 0x9


class ScriptedTransaction(Elaboratable):
    """Transaction seam whose result and receive RAM are testbench-driven."""

    def __init__(self) -> None:
        self.start = Signal()
        self.token_pid = Signal(4)
        self.address = Signal(7)
        self.endpoint = Signal(4)
        self.data_toggle = Signal()
        self.tx_length = Signal(range(65))
        self.tx_payload = Signal(8)
        self.tx_index = Signal(6)
        self.connected = Signal()

        self.busy = Signal()
        self.done = Signal()
        self.status = Signal(3)
        self.rx_length = Signal(range(65))
        self.rx_read_index = Signal(6)
        self.rx_read_data = Signal(8)
        self.rx_data_toggle = Signal()
        self.duplicate = Signal()
        self.rx_bytes = [Signal(8, name=f"rx_byte_{index}") for index in range(64)]

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        m.d.comb += self.rx_read_data.eq(Array(self.rx_bytes)[self.rx_read_index])
        return m


def simulate(bench, *, timing: HostTiming | None = None) -> None:
    transaction = ScriptedTransaction()
    dut = InterruptInPoller(transaction=transaction, timing=timing or HostTiming.simulation())
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def wrapped(ctx) -> None:
        ctx.set(dut.address, 5)
        ctx.set(dut.endpoint, 3)
        ctx.set(dut.max_packet_size, 8)
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


async def accept_poll(ctx, dut, transaction, *, toggle: int) -> None:
    assert ctx.get(transaction.start)
    assert ctx.get(transaction.token_pid) == IN_PID
    assert ctx.get(transaction.address) == 5
    assert ctx.get(transaction.endpoint) == 3
    assert ctx.get(transaction.data_toggle) == toggle
    await ctx.tick("usb")
    assert ctx.get(dut.active)
    assert not ctx.get(transaction.start)
    ctx.set(transaction.busy, 1)


async def complete_poll(
    ctx,
    dut,
    transaction,
    status: TransactionStatus,
    *,
    payload: bytes = b"",
    duplicate: bool = False,
) -> None:
    for index, byte in enumerate(payload):
        ctx.set(transaction.rx_bytes[index], byte)
    ctx.set(transaction.rx_length, len(payload))
    ctx.set(transaction.duplicate, duplicate)
    ctx.set(transaction.status, status.value)
    ctx.set(transaction.busy, 0)
    ctx.set(transaction.done, 1)
    await ctx.tick("usb")
    ctx.set(transaction.done, 0)
    ctx.set(transaction.duplicate, 0)
    for _ in payload:
        await ctx.tick("usb")


async def issue_due_poll(ctx, dut, transaction, *, toggle: int) -> None:
    await pulse_sofs(ctx, dut, ctx.get(dut.interval))
    await accept_poll(ctx, dut, transaction, toggle=toggle)


async def consume_report(ctx, dut) -> bytes:
    payload = bytearray()
    ctx.set(dut.report_ready, 1)
    while ctx.get(dut.report_valid):
        payload.append(ctx.get(dut.report_data))
        if len(payload) == 1:
            assert ctx.get(dut.report_first)
        else:
            assert not ctx.get(dut.report_first)
        assert ctx.get(dut.report_last) == (len(payload) == ctx.get(dut.report_length))
        await ctx.tick("usb")
    ctx.set(dut.report_ready, 0)
    return bytes(payload)


def test_waits_for_first_interval_and_latches_due_while_transaction_busy() -> None:
    async def bench(ctx, dut, transaction) -> None:
        ctx.set(transaction.busy, 1)
        await pulse_sofs(ctx, dut, 1)
        assert not ctx.get(transaction.start)
        await pulse_sofs(ctx, dut, 1)
        assert not ctx.get(transaction.start)

        ctx.set(transaction.busy, 0)
        await ctx.delay(1e-9)
        await accept_poll(ctx, dut, transaction, toggle=0)

    simulate(bench)


def test_poll_start_is_not_duplicated_while_active() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await issue_due_poll(ctx, dut, transaction, toggle=0)
        await pulse_sofs(ctx, dut, 6)
        assert ctx.get(dut.active)
        assert not ctx.get(transaction.start)

    simulate(bench)


def test_nak_emits_nothing_resets_errors_and_reschedules_normally() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await issue_due_poll(ctx, dut, transaction, toggle=0)
        await complete_poll(ctx, dut, transaction, TransactionStatus.NAK)
        assert not ctx.get(dut.report_valid)
        assert not ctx.get(dut.failed)
        assert ctx.get(dut.last_status) == TransactionStatus.NAK.value
        assert ctx.get(dut.transport_error_count) == 0

        await pulse_sofs(ctx, dut, 1)
        assert not ctx.get(transaction.start)
        await pulse_sofs(ctx, dut, 1)
        await accept_poll(ctx, dut, transaction, toggle=0)

    simulate(bench)


def test_expected_report_is_copied_locally_and_flips_toggle() -> None:
    async def bench(ctx, dut, transaction) -> None:
        payload = b"\x01\x02\x03\x04"
        await issue_due_poll(ctx, dut, transaction, toggle=0)
        await complete_poll(ctx, dut, transaction, TransactionStatus.SUCCESS, payload=payload)
        assert ctx.get(dut.report_valid)
        assert ctx.get(dut.report_length) == len(payload)
        assert not ctx.get(dut.active)

        for index in range(len(payload)):
            ctx.set(transaction.rx_bytes[index], 0xEE)
        ctx.set(dut.report_ready, 0)
        await ctx.tick("usb")
        assert ctx.get(dut.report_data) == payload[0]
        assert await consume_report(ctx, dut) == payload

        await issue_due_poll(ctx, dut, transaction, toggle=1)

    simulate(bench)


def test_report_stream_holds_under_backpressure_and_latches_an_elapsed_interval() -> None:
    async def bench(ctx, dut, transaction) -> None:
        payload = b"mouse"
        await issue_due_poll(ctx, dut, transaction, toggle=0)
        await complete_poll(ctx, dut, transaction, TransactionStatus.SUCCESS, payload=payload)

        ctx.set(dut.report_ready, 0)
        await pulse_sofs(ctx, dut, 4)
        for _ in range(3):
            assert ctx.get(dut.report_valid)
            assert ctx.get(dut.report_first)
            assert not ctx.get(dut.report_last)
            assert ctx.get(dut.report_data) == ord("m")
            assert not ctx.get(transaction.start)
            await ctx.tick("usb")

        assert await consume_report(ctx, dut) == payload
        await ctx.delay(1e-9)
        await accept_poll(ctx, dut, transaction, toggle=1)

    simulate(bench)


def test_duplicate_is_not_emitted_and_does_not_flip_toggle() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await issue_due_poll(ctx, dut, transaction, toggle=0)
        await complete_poll(
            ctx,
            dut,
            transaction,
            TransactionStatus.SUCCESS,
            payload=b"old",
            duplicate=True,
        )
        assert not ctx.get(dut.report_valid)
        assert ctx.get(dut.transport_error_count) == 0
        await issue_due_poll(ctx, dut, transaction, toggle=0)

    simulate(bench)


def test_zero_length_packet_advances_toggle_without_emitting() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await issue_due_poll(ctx, dut, transaction, toggle=0)
        await complete_poll(ctx, dut, transaction, TransactionStatus.SUCCESS)
        assert not ctx.get(dut.report_valid)
        await issue_due_poll(ctx, dut, transaction, toggle=1)

    simulate(bench)


def test_accepts_a_64_byte_report_when_endpoint_mps_is_64() -> None:
    async def bench(ctx, dut, transaction) -> None:
        payload = bytes(range(64))
        ctx.set(dut.max_packet_size, 64)
        await issue_due_poll(ctx, dut, transaction, toggle=0)
        await complete_poll(ctx, dut, transaction, TransactionStatus.SUCCESS, payload=payload)
        assert ctx.get(dut.report_length) == 64
        assert await consume_report(ctx, dut) == payload

    simulate(bench)


def test_packet_larger_than_endpoint_mps_counts_as_transport_overflow() -> None:
    async def bench(ctx, dut, transaction) -> None:
        ctx.set(dut.max_packet_size, 4)
        await issue_due_poll(ctx, dut, transaction, toggle=0)
        await complete_poll(ctx, dut, transaction, TransactionStatus.SUCCESS, payload=b"12345")
        assert not ctx.get(dut.report_valid)
        assert ctx.get(dut.last_status) == TransactionStatus.OVERFLOW.value
        assert ctx.get(dut.transport_error_count) == 1
        # The transaction engine already ACKed this expected packet, so the
        # protocol toggle must advance even though the report is rejected.
        await issue_due_poll(ctx, dut, transaction, toggle=1)

    simulate(bench)


def test_stall_fails_immediately() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await issue_due_poll(ctx, dut, transaction, toggle=0)
        await complete_poll(ctx, dut, transaction, TransactionStatus.STALL)
        assert ctx.get(dut.failed)
        assert ctx.get(dut.last_status) == TransactionStatus.STALL.value
        assert ctx.get(dut.transport_error_count) == 0

    simulate(bench)


def test_transport_errors_are_consecutive_and_success_resets_the_count() -> None:
    timing = HostTiming.simulation()

    async def bench(ctx, dut, transaction) -> None:
        await issue_due_poll(ctx, dut, transaction, toggle=0)
        await complete_poll(ctx, dut, transaction, TransactionStatus.TIMEOUT)
        assert ctx.get(dut.transport_error_count) == 1

        await issue_due_poll(ctx, dut, transaction, toggle=0)
        await complete_poll(ctx, dut, transaction, TransactionStatus.SUCCESS, duplicate=True)
        assert ctx.get(dut.transport_error_count) == 0

        for count, status in enumerate(
            [TransactionStatus.CRC_ERROR, TransactionStatus.OVERFLOW, TransactionStatus.TIMEOUT],
            start=1,
        ):
            await issue_due_poll(ctx, dut, transaction, toggle=0)
            await complete_poll(ctx, dut, transaction, status)
            assert ctx.get(dut.transport_error_count) == count
        assert ctx.get(dut.failed)
        assert ctx.get(dut.last_status) == TransactionStatus.TIMEOUT.value

    simulate(bench, timing=timing)


def test_disconnect_fails_and_disable_reenable_resets_all_state() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await issue_due_poll(ctx, dut, transaction, toggle=0)
        await complete_poll(ctx, dut, transaction, TransactionStatus.SUCCESS, payload=b"xy")
        assert ctx.get(dut.report_valid)

        ctx.set(dut.connected, 0)
        await ctx.tick("usb")
        assert ctx.get(dut.failed)
        assert ctx.get(dut.last_status) == TransactionStatus.DISCONNECTED.value
        assert not ctx.get(dut.report_valid)

        ctx.set(dut.enable, 0)
        await ctx.tick("usb")
        assert not ctx.get(dut.failed)
        assert not ctx.get(dut.active)
        assert ctx.get(dut.transport_error_count) == 0
        assert ctx.get(dut.report_length) == 0

        ctx.set(dut.connected, 1)
        ctx.set(dut.enable, 1)
        await ctx.tick("usb")
        await issue_due_poll(ctx, dut, transaction, toggle=0)

    simulate(bench)


def test_poller_has_exactly_one_local_64_by_8_memory() -> None:
    transaction = ScriptedTransaction()
    dut = InterruptInPoller(transaction=transaction, timing=HostTiming.simulation())
    netlist = rtlil.convert(dut, ports=[dut.enable, dut.report_valid, dut.report_data])
    assert len(re.findall(r"(?m)^\s*memory width 8 size 64", netlist)) == 1
    assert len(netlist) < 300_000


def test_poller_elaborates_with_real_transaction_engine() -> None:
    dut = InterruptInPoller(timing=HostTiming.simulation())
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
            dut.report_valid,
            dut.report_ready,
            dut.report_data,
            dut.report_first,
            dut.report_last,
            dut.active,
            dut.failed,
            dut.last_status,
            dut.transport_error_count,
            dut.report_length,
        ],
    )
    assert len(re.findall(r"(?m)^\s*memory width 8 size 64", netlist)) == 2
    assert len(netlist) < 1_000_000


def _ticks_to_poll(high_speed: int, interval: int, *, bound: int) -> int | None:
    """Return how many sof_ticks elapse before the poller issues its first poll.

    ``interval_counter`` resets to zero on each poll, so the ticks to the first
    poll are the period. Measuring the first one keeps this deterministic and
    independent of NAK/retry and data-toggle behaviour, which other tests cover.
    """
    seen: dict[str, int | None] = {"n": None}

    async def bench(ctx, dut, transaction) -> None:
        ctx.set(dut.high_speed, high_speed)
        ctx.set(dut.interval, interval)
        await ctx.tick("usb")
        for n in range(1, bound + 1):
            await pulse_sofs(ctx, dut, 1)
            if ctx.get(transaction.start):
                seen["n"] = n
                return

    simulate(bench)
    return seen["n"]


def test_full_speed_interval_is_a_direct_frame_count() -> None:
    """At Full Speed bInterval counts 1 ms frames, so N means every N ticks."""
    assert _ticks_to_poll(0, 4, bound=64) == 4
    assert _ticks_to_poll(0, 10, bound=64) == 10
    assert _ticks_to_poll(0, 16, bound=64) == 16


def test_high_speed_interval_is_an_exponent_not_a_count() -> None:
    """At High Speed the period is 2**(bInterval-1) microframes.

    This is the failure the scope doc flags as easy to get silently wrong:
    reading the High Speed value as a direct count polls a device that asked for
    8 microframes (bInterval=4, i.e. 1 ms) at 4 microframes = 500 us, twice as
    fast as it asked for. The device still works, so nothing looks broken -- it
    is simply being polled out of spec.
    """
    assert _ticks_to_poll(1, 1, bound=64) == 1
    assert _ticks_to_poll(1, 2, bound=64) == 2
    assert _ticks_to_poll(1, 3, bound=64) == 4
    # bInterval=4 is 8 microframes = 1 ms, NOT 4 microframes.
    assert _ticks_to_poll(1, 4, bound=64) == 8
    assert _ticks_to_poll(1, 5, bound=64) == 16
    assert _ticks_to_poll(1, 6, bound=64) == 32


def test_high_speed_and_full_speed_disagree_on_the_same_descriptor_byte() -> None:
    """The same bInterval must be read differently at each speed.

    A guard against anyone collapsing the two branches back into one.
    """
    assert _ticks_to_poll(0, 4, bound=64) == 4
    assert _ticks_to_poll(1, 4, bound=64) == 8


def test_high_speed_out_of_range_interval_clamps_to_the_slowest_legal_period() -> None:
    """bInterval above 16 is not legal at High Speed; clamp rather than overflow.

    Clamping upward to the spec maximum gives the slowest legal poll. Letting
    the shift overflow instead would wrap and flood the bus on a malformed
    descriptor.
    """
    assert _ticks_to_poll(1, 16, bound=1 << 16) == 1 << 15
    assert _ticks_to_poll(1, 17, bound=1 << 16) == 1 << 15
    assert _ticks_to_poll(1, 255, bound=1 << 16) == 1 << 15


def test_zero_interval_polls_every_tick_at_both_speeds() -> None:
    """bInterval=0 is malformed; both speeds fall back to polling every tick."""
    assert _ticks_to_poll(0, 0, bound=64) == 1
    assert _ticks_to_poll(1, 0, bound=64) == 1
