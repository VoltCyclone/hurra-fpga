from amaranth.sim import Simulator

from hurra_cynthion.control_relay import ControlRelay


def simulate(bench, **kwargs) -> None:
    dut = ControlRelay(**kwargs)
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def wrapped(ctx) -> None:
        # In production the host drives this from enumerator.ready.
        ctx.set(dut.enable, 1)
        await ctx.tick("usb")
        await bench(ctx, dut)

    simulation.add_testbench(wrapped)
    simulation.run()


async def _issue(ctx, dut, *, request_type, request, value, index, length):
    ctx.set(dut.request_type, request_type)
    ctx.set(dut.request, request)
    ctx.set(dut.value, value)
    ctx.set(dut.index, index)
    ctx.set(dut.length, length)
    ctx.set(dut.request_valid, 1)
    await ctx.tick("usb").until(dut.request_ready)
    ctx.set(dut.request_valid, 0)


async def _feed_in_data(ctx, dut, payload):
    """Play the target's IN data stage back at the relay."""
    for offset, byte in enumerate(payload):
        ctx.set(dut.ctl_data, byte)
        ctx.set(dut.ctl_data_valid, 1)
        ctx.set(dut.ctl_data_first, offset == 0)
        ctx.set(dut.ctl_data_last, offset == len(payload) - 1)
        await ctx.tick("usb").until(dut.ctl_data_ready)
        await ctx.tick("usb")
    ctx.set(dut.ctl_data_valid, 0)
    ctx.set(dut.ctl_transferred, len(payload))
    ctx.set(dut.ctl_done, 1)
    ctx.set(dut.ctl_status, 0)
    await ctx.tick("usb")
    ctx.set(dut.ctl_done, 0)


async def _read_buffer(ctx, dut, length):
    out = []
    for addr in range(length):
        ctx.set(dut.read_addr, addr)
        await ctx.tick("usb")
        out.append(ctx.get(dut.read_data))
    return out


def test_in_transfer_is_forwarded_verbatim():
    payload = [0xF1, 0x00, 0xAA, 0x55] + list(range(60))

    async def bench(ctx, dut):
        await _issue(ctx, dut, request_type=0xA1, request=0x01, value=0x03F1, index=3, length=64)

        # The relay must reproduce the request at the target, unaltered.
        assert ctx.get(dut.ctl_request_type) == 0xA1
        assert ctx.get(dut.ctl_request) == 0x01
        assert ctx.get(dut.ctl_value) == 0x03F1
        assert ctx.get(dut.ctl_index) == 3
        assert ctx.get(dut.ctl_length) == 64
        assert ctx.get(dut.request_pending) == 1

        await _feed_in_data(ctx, dut, payload)
        await ctx.tick("usb").until(dut.response_valid)

        assert ctx.get(dut.response_error) == 0
        assert ctx.get(dut.response_length) == len(payload)
        assert await _read_buffer(ctx, dut, len(payload)) == payload

        ctx.set(dut.response_ack, 1)
        await ctx.tick("usb")
        ctx.set(dut.response_ack, 0)
        await ctx.tick("usb")
        assert ctx.get(dut.request_pending) == 0

    simulate(bench)


def test_out_transfer_payload_is_replayed_to_target():
    payload = [0xF0, 0x01] + list(range(62))

    async def bench(ctx, dut):
        ctx.set(dut.request_type, 0x21)  # host->device, class, interface
        ctx.set(dut.request, 0x09)  # SET_REPORT
        ctx.set(dut.value, 0x03F0)
        ctx.set(dut.index, 3)
        ctx.set(dut.length, len(payload))
        ctx.set(dut.request_valid, 1)
        await ctx.tick("usb").until(dut.request_ready)
        ctx.set(dut.request_valid, 0)

        for byte in payload:
            ctx.set(dut.out_data, byte)
            ctx.set(dut.out_valid, 1)
            await ctx.tick("usb")
        ctx.set(dut.out_valid, 0)
        await ctx.tick("usb").until(dut.ctl_start)

        # The relay must hand the engine each captured byte on demand.
        for offset, byte in enumerate(payload):
            ctx.set(dut.ctl_out_index, offset)
            await ctx.tick("usb")
            assert ctx.get(dut.ctl_out_payload) == byte, f"byte {offset}"

    simulate(bench)


def test_oversized_request_is_rejected_not_truncated():
    async def bench(ctx, dut):
        ctx.set(dut.request_type, 0xA1)
        ctx.set(dut.request, 0x01)
        ctx.set(dut.value, 0)
        ctx.set(dut.index, 0)
        ctx.set(dut.length, 65)  # one past the 64-byte buffer
        ctx.set(dut.request_valid, 1)
        await ctx.tick("usb").until(dut.request_ready)
        ctx.set(dut.request_valid, 0)
        await ctx.tick("usb").until(dut.response_valid)

        assert ctx.get(dut.overflow) == 1
        assert ctx.get(dut.response_error) == 1
        assert ctx.get(dut.ctl_start) == 0, "must not issue an oversized transfer"

    simulate(bench)


def test_relay_waits_for_the_engine_rather_than_timing_out() -> None:
    """Review I1: the relay must not give up while the engine is still working.

    It used to time out after 20 ms and return to IDLE, but the engine's own
    NAK timeout is 500 ms, so the engine carried on -- and its late done and
    data were taken as the NEXT request's answer. The engine is guaranteed to
    finish (every wait state ends in TIMEOUT or DISCONNECTED), so the relay
    waits for it. Bounding the AUX host's wait is the handler's job.
    """

    async def bench(ctx, dut):
        await _issue(ctx, dut, request_type=0xA1, request=0x01, value=0, index=0, length=8)
        # Let the relay start an idle engine, then hold the engine busy.
        await ctx.tick("usb").until(dut.ctl_start)
        await ctx.tick("usb")
        ctx.set(dut.ctl_busy, 1)
        for _ in range(500):
            await ctx.tick("usb")
            assert not ctx.get(dut.response_valid), "relay gave up while the engine was busy"
            assert ctx.get(dut.request_pending)
            assert ctx.get(dut.engine_owned), "pollers must stay pre-empted while engine works"

        # The engine finishes, late, with an error: now the relay completes.
        ctx.set(dut.ctl_status, 2)
        ctx.set(dut.ctl_done, 1)
        await ctx.tick("usb")
        ctx.set(dut.ctl_done, 0)
        ctx.set(dut.ctl_busy, 0)
        await ctx.tick("usb").until(dut.response_valid)
        assert ctx.get(dut.response_error)
        assert not ctx.get(dut.engine_owned)

    simulate(bench)


def test_relay_refuses_requests_until_the_engine_is_handed_over() -> None:
    """Review I5: before TARGET is enumerated the engine is the enumerator's.

    Accepting then would drop our start pulse and let the enumerator's own
    traffic be taken as our answer.
    """

    async def bench(ctx, dut):
        ctx.set(dut.enable, 0)
        ctx.set(dut.request_valid, 1)
        ctx.set(dut.length, 8)
        await ctx.tick("usb")
        assert not ctx.get(dut.request_ready)
        assert not ctx.get(dut.request_pending)
        assert not ctx.get(dut.ctl_start)

    simulate(bench)


def test_abort_releases_an_abandoned_out_capture() -> None:
    """Review C1: a host abandoning a SET_REPORT mid-capture.

    CAPTURE_OUT had no exit but a full payload, so the relay sat there
    forever holding request_pending.
    """

    async def bench(ctx, dut):
        await _issue(ctx, dut, request_type=0x21, request=0x09, value=0, index=0, length=16)
        ctx.set(dut.out_data, 0xAA)
        ctx.set(dut.out_valid, 1)
        await ctx.tick("usb")
        ctx.set(dut.out_valid, 0)
        ctx.set(dut.abort, 1)
        await ctx.tick("usb")
        ctx.set(dut.abort, 0)
        await ctx.tick("usb").until(dut.response_valid)
        assert ctx.get(dut.response_error)
        assert not ctx.get(dut.ctl_start), "an aborted capture must never reach the engine"
        ctx.set(dut.response_ack, 1)
        await ctx.tick("usb")
        ctx.set(dut.response_ack, 0)
        await ctx.tick("usb")
        assert not ctx.get(dut.request_pending)

    simulate(bench)


# --- The boot-protocol snoop (forward_ok) and the resync source ------------------

SET_PROTOCOL = 0x0B


async def _finish(ctx, dut, *, status: int = 0) -> None:
    """Play the engine accepting the start and completing with ``status``."""
    await ctx.tick("usb").until(dut.ctl_start)
    ctx.set(dut.ctl_busy, 1)
    await ctx.tick("usb")
    ctx.set(dut.ctl_busy, 0)
    ctx.set(dut.ctl_status, status)
    ctx.set(dut.ctl_done, 1)
    await ctx.tick("usb")
    ctx.set(dut.ctl_done, 0)


async def _count_cycles(ctx, signal, cycles: int) -> int:
    seen = 0
    for _ in range(cycles):
        seen += ctx.get(signal)
        await ctx.tick("usb")
    return seen


def test_forward_ok_pulses_once_for_a_forward_the_target_accepted() -> None:
    async def bench(ctx, dut):
        await _issue(ctx, dut, request_type=0x21, request=SET_PROTOCOL, value=0, index=2, length=0)
        await _finish(ctx, dut, status=0)
        # Registered: one cycle after done, with the forwarded request still on ctl_*.
        assert ctx.get(dut.forward_ok)
        assert ctx.get(dut.ctl_request_type) == 0x21
        assert ctx.get(dut.ctl_request) == SET_PROTOCOL
        assert ctx.get(dut.ctl_value) == 0
        assert ctx.get(dut.ctl_index) == 2
        await ctx.tick("usb")
        assert await _count_cycles(ctx, dut.forward_ok, 20) == 0

    simulate(bench)


def test_forward_ok_never_pulses_for_a_failed_forward() -> None:
    async def bench(ctx, dut):
        await _issue(ctx, dut, request_type=0x21, request=SET_PROTOCOL, value=0, index=2, length=0)
        ctx.set(dut.ctl_status, 3)  # STALL
        await ctx.tick("usb").until(dut.ctl_start)
        ctx.set(dut.ctl_done, 1)
        await ctx.tick("usb")
        ctx.set(dut.ctl_done, 0)
        assert await _count_cycles(ctx, dut.forward_ok, 20) == 0
        assert ctx.get(dut.response_valid) and ctx.get(dut.response_error)

    simulate(bench)


def test_forward_ok_never_pulses_for_an_overflow_or_an_unenabled_issue() -> None:
    async def bench(ctx, dut):
        await _issue(ctx, dut, request_type=0xA1, request=0x01, value=0, index=0, length=65)
        assert await _count_cycles(ctx, dut.forward_ok, 10) == 0
        ctx.set(dut.response_ack, 1)
        await ctx.tick("usb")
        ctx.set(dut.response_ack, 0)

        ctx.set(dut.ctl_busy, 1)  # keep the request parked in ISSUE
        await _issue(ctx, dut, request_type=0x21, request=SET_PROTOCOL, value=0, index=0, length=0)
        ctx.set(dut.enable, 0)
        assert await _count_cycles(ctx, dut.forward_ok, 10) == 0
        assert ctx.get(dut.response_valid) and ctx.get(dut.response_error)

    simulate(bench)


def test_a_resync_issues_set_protocol_report_and_completes_on_its_own() -> None:
    """The replay's answer is the relay's alone: the handler never sees it."""

    async def bench(ctx, dut):
        ctx.set(dut.resync_index, 3)
        ctx.set(dut.resync_valid, 1)
        await ctx.tick("usb")
        ctx.set(dut.resync_valid, 0)
        assert ctx.get(dut.resync_active)
        assert ctx.get(dut.request_pending)
        assert not ctx.get(dut.request_ready), "a resync in flight must hold the handler off"
        assert ctx.get(dut.ctl_request_type) == 0x21
        assert ctx.get(dut.ctl_request) == SET_PROTOCOL
        assert ctx.get(dut.ctl_value) == 1
        assert ctx.get(dut.ctl_index) == 3
        assert ctx.get(dut.ctl_length) == 0

        await _finish(ctx, dut, status=0)
        done = 0
        for _ in range(10):
            assert not ctx.get(dut.response_valid)
            assert not ctx.get(dut.forward_ok), "a replay is not the PC's forward"
            if ctx.get(dut.resync_done):
                done += 1
                assert not ctx.get(dut.response_error)
                assert not ctx.get(dut.resync_active)
            await ctx.tick("usb")
        assert done == 1
        assert not ctx.get(dut.request_pending)
        assert ctx.get(dut.request_ready)

    simulate(bench)


def test_a_failed_resync_reports_its_error_on_the_done_cycle() -> None:
    async def bench(ctx, dut):
        ctx.set(dut.resync_valid, 1)
        await ctx.tick("usb")
        ctx.set(dut.resync_valid, 0)
        await _finish(ctx, dut, status=3)
        await ctx.tick("usb").until(dut.resync_done)
        assert ctx.get(dut.response_error)

    simulate(bench)


def test_the_handler_wins_a_coincident_request_and_the_resync_waits() -> None:
    async def bench(ctx, dut):
        ctx.set(dut.resync_index, 1)
        ctx.set(dut.resync_valid, 1)
        await _issue(ctx, dut, request_type=0xA1, request=0x03, value=0, index=0, length=1)
        assert not ctx.get(dut.resync_active)
        assert ctx.get(dut.ctl_request) == 0x03
        await _feed_in_data(ctx, dut, [0x01])
        await ctx.tick("usb").until(dut.response_valid)
        ctx.set(dut.response_ack, 1)
        await ctx.tick("usb")
        ctx.set(dut.response_ack, 0)
        # Back in IDLE, the still-offered resync is taken.
        await ctx.tick("usb")
        assert ctx.get(dut.resync_active)
        assert ctx.get(dut.ctl_request) == SET_PROTOCOL

    simulate(bench)


def test_no_resync_is_accepted_before_the_engine_is_handed_over() -> None:
    async def bench(ctx, dut):
        ctx.set(dut.enable, 0)
        ctx.set(dut.resync_valid, 1)
        for _ in range(5):
            await ctx.tick("usb")
            assert not ctx.get(dut.resync_active)
            assert not ctx.get(dut.ctl_start)

    simulate(bench)
