import pytest
from amaranth import Array, Elaboratable, Module, Signal
from amaranth.back import rtlil
from amaranth.sim import Simulator

from hurra_cynthion.control import USBControlTransferEngine
from hurra_cynthion.timing import HostTiming
from hurra_cynthion.types import TransactionStatus

OUT_PID = 0x1
IN_PID = 0x9
SETUP_PID = 0xD


class ScriptedTransaction(Elaboratable):
    """Transaction seam whose completion and receive buffer are testbench-driven."""

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
    dut = USBControlTransferEngine(
        transaction=transaction,
        timing=timing if timing is not None else HostTiming.simulation(),
    )
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def wrapped(ctx) -> None:
        ctx.set(dut.connected, 1)
        ctx.set(dut.max_packet_size, 4)
        ctx.set(dut.data_ready, 1)
        await bench(ctx, dut, transaction)

    simulation.add_testbench(wrapped)
    simulation.run()


async def start_control(
    ctx,
    dut,
    *,
    address: int = 5,
    request_type: int = 0,
    request: int = 1,
    value: int = 0x1234,
    index: int = 0x5678,
    length: int = 0,
    payload: list[int] | None = None,
) -> None:
    ctx.set(dut.address, address)
    ctx.set(dut.request_type, request_type)
    ctx.set(dut.request, request)
    ctx.set(dut.value, value)
    ctx.set(dut.index, index)
    ctx.set(dut.length, length)
    ctx.set(dut.out_payload, (payload or [0])[0])
    ctx.set(dut.start, 1)
    await ctx.tick("usb")
    ctx.set(dut.start, 0)


async def complete_transaction(
    ctx,
    dut,
    transaction,
    *,
    status: TransactionStatus = TransactionStatus.SUCCESS,
    rx_payload: list[int] | None = None,
    rx_toggle: int = 0,
    duplicate: bool = False,
    out_payload: list[int] | None = None,
) -> dict[str, int | list[int]]:
    for _ in range(30):
        if ctx.get(transaction.start):
            break
        assert ctx.get(dut.busy)
        await ctx.tick("usb")
    else:
        raise AssertionError("control engine did not issue a transaction")

    request = {
        "pid": ctx.get(transaction.token_pid),
        "address": ctx.get(transaction.address),
        "endpoint": ctx.get(transaction.endpoint),
        "toggle": ctx.get(transaction.data_toggle),
        "length": ctx.get(transaction.tx_length),
        "payload": [],
    }

    # The real engine accepts ``start`` here, then consumes indexed payload
    # while the control sequencer is waiting for transaction completion.
    await ctx.tick("usb")
    for index in range(request["length"]):
        ctx.set(transaction.tx_index, index)
        if out_payload is not None:
            absolute_index = ctx.get(dut.out_index)
            ctx.set(dut.out_payload, out_payload[absolute_index])
        await ctx.delay(1e-9)
        request["payload"].append(ctx.get(transaction.tx_payload))

    rx_payload = rx_payload or []
    for index, byte in enumerate(rx_payload):
        ctx.set(transaction.rx_bytes[index], byte)
    ctx.set(transaction.rx_length, len(rx_payload))
    ctx.set(transaction.rx_data_toggle, rx_toggle)
    ctx.set(transaction.duplicate, duplicate)
    ctx.set(transaction.status, status.value)
    ctx.set(transaction.done, 1)
    await ctx.tick("usb")
    ctx.set(transaction.done, 0)
    return request


async def collect_stream(ctx, dut, count: int) -> tuple[list[int], list[tuple[int, int]]]:
    payload = []
    boundaries = []
    for _ in range(100):
        if ctx.get(dut.data_valid) and ctx.get(dut.data_ready):
            payload.append(ctx.get(dut.data))
            boundaries.append((ctx.get(dut.data_first), ctx.get(dut.data_last)))
        if len(payload) == count:
            return payload, boundaries
        await ctx.tick("usb")
    raise AssertionError("read stream did not produce the expected bytes")


def assert_transaction(
    actual,
    pid: int,
    *,
    address: int = 5,
    toggle: int,
    payload: list[int],
) -> None:
    assert actual == {
        "pid": pid,
        "address": address,
        "endpoint": 0,
        "toggle": toggle,
        "length": len(payload),
        "payload": payload,
    }


def test_set_address_uses_old_address_and_commits_after_status() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await start_control(ctx, dut, address=3, request=5, value=42, index=0)
        setup = await complete_transaction(ctx, dut, transaction)
        assert_transaction(
            setup,
            SETUP_PID,
            address=3,
            toggle=0,
            payload=[0x00, 0x05, 42, 0, 0, 0, 0, 0],
        )
        assert not ctx.get(dut.set_address_valid)

        status = await complete_transaction(ctx, dut, transaction, rx_toggle=1)
        assert_transaction(status, IN_PID, address=3, toggle=1, payload=[])
        assert ctx.get(dut.done)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS
        assert ctx.get(dut.set_address_valid)
        assert ctx.get(dut.set_address) == 42

        await ctx.tick("usb")
        assert not ctx.get(dut.done)
        assert not ctx.get(dut.set_address_valid)

    simulate(bench)


def test_control_read_retries_nak_and_duplicate_and_honors_backpressure() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await start_control(ctx, dut, request_type=0x80, length=6)
        setup = await complete_transaction(ctx, dut, transaction)
        assert_transaction(
            setup,
            SETUP_PID,
            toggle=0,
            payload=[0x80, 1, 0x34, 0x12, 0x78, 0x56, 6, 0],
        )

        first = await complete_transaction(ctx, dut, transaction, status=TransactionStatus.NAK)
        assert_transaction(first, IN_PID, toggle=1, payload=[])
        retry = await complete_transaction(
            ctx, dut, transaction, rx_payload=[10, 11, 12, 13], rx_toggle=1
        )
        assert_transaction(retry, IN_PID, toggle=1, payload=[])

        ctx.set(dut.data_ready, 0)
        for _ in range(3):
            assert ctx.get(dut.data_valid)
            assert ctx.get(dut.data) == 10
            assert not ctx.get(transaction.start)
            await ctx.tick("usb")
        ctx.set(dut.data_ready, 1)
        payload, boundaries = await collect_stream(ctx, dut, 4)
        assert payload == [10, 11, 12, 13]
        assert boundaries == [(1, 0), (0, 0), (0, 0), (0, 0)]

        duplicate = await complete_transaction(ctx, dut, transaction, rx_toggle=1, duplicate=True)
        assert_transaction(duplicate, IN_PID, toggle=0, payload=[])
        expected = await complete_transaction(
            ctx, dut, transaction, rx_payload=[14, 15], rx_toggle=0
        )
        assert_transaction(expected, IN_PID, toggle=0, payload=[])
        payload, boundaries = await collect_stream(ctx, dut, 2)
        assert payload == [14, 15]
        assert boundaries == [(0, 0), (0, 1)]

        status = await complete_transaction(ctx, dut, transaction)
        assert_transaction(status, OUT_PID, toggle=1, payload=[])
        assert ctx.get(dut.done)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS
        assert ctx.get(dut.transferred) == 6

    simulate(bench)


def test_control_read_stops_at_requested_length_without_extra_in() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await start_control(ctx, dut, request_type=0x80, length=4)
        await complete_transaction(ctx, dut, transaction)
        data = await complete_transaction(
            ctx, dut, transaction, rx_payload=[1, 2, 3, 4], rx_toggle=1
        )
        assert_transaction(data, IN_PID, toggle=1, payload=[])
        payload, boundaries = await collect_stream(ctx, dut, 4)
        assert payload == [1, 2, 3, 4]
        assert boundaries[-1] == (0, 1)
        status = await complete_transaction(ctx, dut, transaction)
        assert_transaction(status, OUT_PID, toggle=1, payload=[])

    simulate(bench)


@pytest.mark.parametrize("final_payload", [[], [0xA5]])
def test_control_read_stops_on_short_or_zlp(final_payload: list[int]) -> None:
    async def bench(ctx, dut, transaction) -> None:
        await start_control(ctx, dut, request_type=0x80, length=9)
        await complete_transaction(ctx, dut, transaction)
        await complete_transaction(ctx, dut, transaction, rx_payload=final_payload, rx_toggle=1)
        if final_payload:
            assert await collect_stream(ctx, dut, 1) == ([0xA5], [(1, 1)])
        status = await complete_transaction(ctx, dut, transaction)
        assert_transaction(status, OUT_PID, toggle=1, payload=[])
        assert ctx.get(dut.transferred) == len(final_payload)

    simulate(bench)


def test_control_write_chunks_absolute_payload_and_retries_nak() -> None:
    payload = [20, 21, 22, 23, 24, 25]

    async def bench(ctx, dut, transaction) -> None:
        await start_control(ctx, dut, request_type=0x00, length=len(payload), payload=payload)
        await complete_transaction(ctx, dut, transaction)

        first = await complete_transaction(
            ctx,
            dut,
            transaction,
            status=TransactionStatus.NAK,
            out_payload=payload,
        )
        assert_transaction(first, OUT_PID, toggle=1, payload=payload[:4])
        retry = await complete_transaction(ctx, dut, transaction, out_payload=payload)
        assert_transaction(retry, OUT_PID, toggle=1, payload=payload[:4])
        second = await complete_transaction(ctx, dut, transaction, out_payload=payload)
        assert_transaction(second, OUT_PID, toggle=0, payload=payload[4:])

        status = await complete_transaction(ctx, dut, transaction, rx_toggle=1)
        assert_transaction(status, IN_PID, toggle=1, payload=[])
        assert ctx.get(dut.done)
        assert ctx.get(dut.transferred) == len(payload)

    simulate(bench)


def test_setup_and_status_naks_retry_the_same_transaction() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await start_control(ctx, dut, request=9, value=1)
        setup = await complete_transaction(ctx, dut, transaction, status=TransactionStatus.NAK)
        assert_transaction(
            setup,
            SETUP_PID,
            toggle=0,
            payload=[0, 9, 1, 0, 0x78, 0x56, 0, 0],
        )
        setup_retry = await complete_transaction(ctx, dut, transaction)
        assert setup_retry == setup

        status = await complete_transaction(ctx, dut, transaction, status=TransactionStatus.NAK)
        assert_transaction(status, IN_PID, toggle=1, payload=[])
        status_retry = await complete_transaction(ctx, dut, transaction)
        assert status_retry == status
        assert ctx.get(dut.done)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS

    simulate(bench)


@pytest.mark.parametrize("stage", ["setup", "read", "write", "status"])
def test_control_nak_retry_budget_times_out_each_stage(stage: str) -> None:
    timing = HostTiming.simulation()
    payload = [1, 2, 3, 4]

    async def bench(ctx, dut, transaction) -> None:
        read = stage == "read"
        write = stage == "write"
        await start_control(
            ctx,
            dut,
            request_type=0x80 if read else 0,
            length=4 if (read or write) else 0,
            payload=payload if write else None,
        )

        if stage != "setup":
            await complete_transaction(ctx, dut, transaction)

        requests = []
        for _ in range(timing.control_nak_timeout_cycles * 2):
            if ctx.get(dut.done):
                break
            if ctx.get(transaction.start):
                current = {
                    "pid": ctx.get(transaction.token_pid),
                    "address": ctx.get(transaction.address),
                    "endpoint": ctx.get(transaction.endpoint),
                    "toggle": ctx.get(transaction.data_toggle),
                    "length": ctx.get(transaction.tx_length),
                    "payload": [],
                }
                requests.append(current)
                await ctx.tick("usb")
                ctx.set(transaction.status, TransactionStatus.NAK.value)
                ctx.set(transaction.done, 1)
                await ctx.tick("usb")
                ctx.set(transaction.done, 0)
            else:
                await ctx.tick("usb")

        assert ctx.get(dut.done)
        assert not ctx.get(dut.busy)
        assert ctx.get(dut.status) == TransactionStatus.TIMEOUT
        assert len(requests) >= 2
        assert all(request == requests[0] for request in requests)

    simulate(bench, timing=timing)


def test_control_nak_retry_budget_resets_after_forward_progress() -> None:
    timing = HostTiming.simulation()

    async def bench(ctx, dut, transaction) -> None:
        await start_control(ctx, dut, request_type=0x80, length=1)
        await complete_transaction(ctx, dut, transaction)

        await complete_transaction(ctx, dut, transaction, status=TransactionStatus.NAK)
        ctx.set(transaction.busy, 1)
        for _ in range(timing.control_nak_timeout_cycles // 3):
            await ctx.tick("usb")
        ctx.set(transaction.busy, 0)
        ctx.set(dut.data_ready, 0)
        await complete_transaction(ctx, dut, transaction, rx_payload=[0xA5], rx_toggle=1)
        for _ in range(timing.control_nak_timeout_cycles + 2):
            assert ctx.get(dut.busy)
            assert ctx.get(dut.data_valid)
            assert ctx.get(dut.data) == 0xA5
            await ctx.tick("usb")
        ctx.set(dut.data_ready, 1)
        assert await collect_stream(ctx, dut, 1) == ([0xA5], [(1, 1)])

        await complete_transaction(ctx, dut, transaction, status=TransactionStatus.NAK)
        ctx.set(transaction.busy, 1)
        for _ in range(timing.control_nak_timeout_cycles // 3):
            await ctx.tick("usb")
        ctx.set(transaction.busy, 0)
        await complete_transaction(ctx, dut, transaction)
        assert ctx.get(dut.done)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS

    simulate(bench, timing=timing)


def test_later_data_error_preserves_completed_byte_count() -> None:
    payload = [1, 2, 3, 4, 5, 6]

    async def bench(ctx, dut, transaction) -> None:
        await start_control(ctx, dut, length=len(payload), payload=payload)
        await complete_transaction(ctx, dut, transaction)
        await complete_transaction(ctx, dut, transaction, out_payload=payload)
        assert ctx.get(dut.transferred) == 4
        await complete_transaction(
            ctx,
            dut,
            transaction,
            status=TransactionStatus.STALL,
            out_payload=payload,
        )
        assert ctx.get(dut.done)
        assert ctx.get(dut.status) == TransactionStatus.STALL
        assert ctx.get(dut.transferred) == 4
        await ctx.tick("usb")
        assert not ctx.get(dut.done)
        assert ctx.get(dut.status) == TransactionStatus.STALL
        assert ctx.get(dut.transferred) == 4

    simulate(bench)


@pytest.mark.parametrize(
    "status",
    [
        TransactionStatus.STALL,
        TransactionStatus.TIMEOUT,
        TransactionStatus.CRC_ERROR,
        TransactionStatus.OVERFLOW,
        TransactionStatus.DISCONNECTED,
    ],
)
def test_terminal_transaction_errors_are_propagated(status: TransactionStatus) -> None:
    async def bench(ctx, dut, transaction) -> None:
        await start_control(ctx, dut)
        await complete_transaction(ctx, dut, transaction, status=status)
        assert ctx.get(dut.done)
        assert not ctx.get(dut.busy)
        assert ctx.get(dut.status) == status
        assert ctx.get(dut.transferred) == 0
        await ctx.tick("usb")
        assert not ctx.get(dut.done)
        assert ctx.get(dut.status) == status

    simulate(bench)


def test_read_rejects_packet_larger_than_remaining() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await start_control(ctx, dut, request_type=0x80, length=2)
        await complete_transaction(ctx, dut, transaction)
        await complete_transaction(ctx, dut, transaction, rx_payload=[1, 2, 3], rx_toggle=1)
        assert ctx.get(dut.done)
        assert ctx.get(dut.status) == TransactionStatus.OVERFLOW
        assert ctx.get(dut.transferred) == 0

    simulate(bench)


def test_read_rejects_packet_larger_than_max_packet_size() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await start_control(ctx, dut, request_type=0x80, length=9)
        await complete_transaction(ctx, dut, transaction)
        await complete_transaction(ctx, dut, transaction, rx_payload=[1, 2, 3, 4, 5], rx_toggle=1)
        assert ctx.get(dut.done)
        assert ctx.get(dut.status) == TransactionStatus.OVERFLOW
        assert ctx.get(dut.transferred) == 0
        assert not ctx.get(dut.data_valid)

    simulate(bench)


def test_non_zlp_in_status_is_overflow() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await start_control(ctx, dut)
        await complete_transaction(ctx, dut, transaction)
        await complete_transaction(ctx, dut, transaction, rx_payload=[0xFF], rx_toggle=1)
        assert ctx.get(dut.done)
        assert ctx.get(dut.status) == TransactionStatus.OVERFLOW

    simulate(bench)


def test_start_while_busy_does_not_replace_latched_request() -> None:
    async def bench(ctx, dut, transaction) -> None:
        await start_control(ctx, dut, request=1, value=2, index=3)
        ctx.set(dut.request, 99)
        ctx.set(dut.value, 100)
        ctx.set(dut.index, 101)
        ctx.set(dut.start, 1)

        setup = await complete_transaction(ctx, dut, transaction)
        ctx.set(dut.start, 0)
        assert_transaction(
            setup,
            SETUP_PID,
            toggle=0,
            payload=[0, 1, 2, 0, 3, 0, 0, 0],
        )
        await complete_transaction(ctx, dut, transaction)
        assert ctx.get(dut.done)
        await ctx.tick("usb")
        for _ in range(3):
            assert not ctx.get(transaction.start)
            await ctx.tick("usb")

    simulate(bench)


def test_control_engine_elaborates_with_real_transaction_engine() -> None:
    dut = USBControlTransferEngine()
    netlist = rtlil.convert(
        dut,
        ports=[
            dut.start,
            dut.connected,
            dut.address,
            dut.request_type,
            dut.request,
            dut.value,
            dut.index,
            dut.length,
            dut.max_packet_size,
            dut.out_payload,
            dut.out_index,
            dut.busy,
            dut.done,
            dut.status,
            dut.transferred,
            dut.data_valid,
            dut.data_ready,
            dut.data,
            dut.data_first,
            dut.data_last,
            dut.set_address_valid,
            dut.set_address,
        ],
    )
    assert len(netlist) < 500_000
