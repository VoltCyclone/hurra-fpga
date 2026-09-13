from _descriptor_store_helpers import seed_descriptor
from amaranth import Elaboratable, Module
from amaranth.sim import Simulator
from usb_protocol.types import USBRequestType, USBStandardRequests

from hurra_cynthion.descriptors import DescriptorStore
from hurra_cynthion.device_control import (
    ClonedDescriptorStreamer,
    ClonedStandardRequestHandler,
    HIDClassRequestHandler,
)

HID_SET_IDLE = 0x0A


class _StreamerHarness(Elaboratable):
    def __init__(self, max_packet_size: int = 64):
        self.store = DescriptorStore()
        self.dut = ClonedDescriptorStreamer(self.store, max_packet_size=max_packet_size)
        self.m = Module()
        self.m.submodules.store = self.store
        self.m.submodules.dut = self.dut

    def elaborate(self, platform):
        return self.m


async def _collect_chunk(ctx, dut, value, w_index, length, start_position):
    ctx.set(dut.value, value)
    ctx.set(dut.w_index, w_index)
    ctx.set(dut.length, length)
    ctx.set(dut.start_position, start_position)
    ctx.set(dut.tx.ready, 1)
    ctx.set(dut.start, 1)
    await ctx.tick("usb")
    ctx.set(dut.start, 0)
    out = []
    for _ in range(80):
        if ctx.get(dut.stall):
            return None
        if ctx.get(dut.tx.valid):
            out.append(ctx.get(dut.tx.payload))
            if ctx.get(dut.tx.last):
                break
        await ctx.tick("usb")
    return out


def test_streamer_returns_full_short_descriptor():
    top = _StreamerHarness()
    sim = Simulator(top)
    sim.add_clock(1e-6, domain="usb")
    device_desc = bytes(range(18))

    async def bench(ctx):
        await seed_descriptor(ctx, top.store, dtype=1, index=0, w_index=0, data=device_desc)
        chunk = await _collect_chunk(
            ctx, top.dut, value=0x0100, w_index=0, length=18, start_position=0
        )
        assert chunk == list(device_desc)

    sim.add_testbench(bench)
    sim.run()


def test_streamer_requests_registered_serve_metadata_before_streaming():
    top = _StreamerHarness()
    assert hasattr(top.store, "serve_request"), "DescriptorStore lacks the pipelined serve port"
    sim = Simulator(top)
    sim.add_clock(1e-6, domain="usb")
    device_desc = bytes(range(18))

    async def bench(ctx):
        await seed_descriptor(ctx, top.store, dtype=1, index=0, w_index=0, data=device_desc)
        ctx.set(top.dut.value, 0x0100)
        ctx.set(top.dut.w_index, 0)
        ctx.set(top.dut.length, 18)
        ctx.set(top.dut.start_position, 0)
        ctx.set(top.dut.tx.ready, 1)
        ctx.set(top.dut.start, 1)
        assert ctx.get(top.store.serve_request), "streamer bypassed the registered serve port"
        await ctx.tick("usb")
        ctx.set(top.dut.start, 0)

        collected = []
        for _ in range(90):
            assert not ctx.get(top.dut.stall)
            if ctx.get(top.dut.tx.valid):
                collected.append(ctx.get(top.dut.tx.payload))
                if ctx.get(top.dut.tx.last):
                    break
            await ctx.tick("usb")
        assert collected == list(device_desc)

    sim.add_testbench(bench)
    sim.run()


def test_setup_prepare_handles_min_gap_in_and_streams_64_bytes_without_bubbles():
    top = _StreamerHarness()
    sim = Simulator(top)
    sim.add_clock(1e-6, domain="usb")
    descriptor = bytes(range(64))

    async def bench(ctx):
        await seed_descriptor(ctx, top.store, dtype=1, index=0, w_index=0, data=descriptor)
        ctx.set(top.dut.value, 0x0100)
        ctx.set(top.dut.w_index, 0)
        ctx.set(top.dut.length, 64)
        ctx.set(top.dut.start_position, 0)
        ctx.set(top.dut.tx.ready, 1)

        ctx.set(top.dut.prepare, 1)
        assert ctx.get(top.store.serve_request)
        await ctx.tick("usb")
        ctx.set(top.dut.prepare, 0)
        # Model the shortest useful SETUP-to-IN gap: the packet request
        # arrives on the very next clock while the directory scan is active.
        ctx.set(top.dut.start, 1)
        await ctx.tick("usb")
        ctx.set(top.dut.start, 0)

        collected = []
        streaming = False
        for _ in range(100):
            if streaming:
                assert ctx.get(top.dut.tx.valid), "tx.valid bubbled inside a descriptor packet"
            if ctx.get(top.dut.tx.valid):
                streaming = True
                collected.append(ctx.get(top.dut.tx.payload))
                if ctx.get(top.dut.tx.last):
                    break
            await ctx.tick("usb")
        else:
            raise AssertionError("prepared descriptor packet did not complete")

        assert collected == list(descriptor)

    sim.add_testbench(bench)
    sim.run()


def test_prepared_selection_is_reused_for_retry_and_later_packet():
    top = _StreamerHarness(max_packet_size=4)
    sim = Simulator(top)
    sim.add_clock(1e-6, domain="usb")
    descriptor = bytes(range(8))

    async def bench(ctx):
        await seed_descriptor(ctx, top.store, dtype=1, index=0, w_index=0, data=descriptor)
        ctx.set(top.dut.value, 0x0100)
        ctx.set(top.dut.w_index, 0)
        ctx.set(top.dut.length, 8)
        ctx.set(top.dut.start_position, 0)
        ctx.set(top.dut.prepare, 1)
        await ctx.tick("usb")
        ctx.set(top.dut.prepare, 0)
        while not ctx.get(top.store.serve_response):
            await ctx.tick("usb")
        await ctx.tick("usb")

        first = await _collect_chunk(ctx, top.dut, 0x0100, 0, 8, 0)
        assert first == [0, 1, 2, 3]
        await ctx.tick("usb")

        ctx.set(top.dut.start, 1)
        assert not ctx.get(top.store.serve_request), "retry re-scanned prepared metadata"
        await ctx.tick("usb")
        ctx.set(top.dut.start, 0)
        retry = []
        for _ in range(8):
            if ctx.get(top.dut.tx.valid):
                retry.append(ctx.get(top.dut.tx.payload))
                if ctx.get(top.dut.tx.last):
                    break
            await ctx.tick("usb")
        assert retry == [0, 1, 2, 3]
        await ctx.tick("usb")

        later = await _collect_chunk(ctx, top.dut, 0x0100, 0, 8, 4)
        assert later == [4, 5, 6, 7]

    sim.add_testbench(bench)
    sim.run()


def test_new_prepare_preempts_an_in_flight_selection():
    top = _StreamerHarness()
    sim = Simulator(top)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        await seed_descriptor(ctx, top.store, dtype=1, index=0, w_index=0, data=b"old")
        await seed_descriptor(ctx, top.store, dtype=2, index=0, w_index=0, data=b"new")
        ctx.set(top.dut.w_index, 0)
        ctx.set(top.dut.length, 3)
        ctx.set(top.dut.start_position, 0)
        ctx.set(top.dut.tx.ready, 1)

        ctx.set(top.dut.value, 0x0100)
        ctx.set(top.dut.prepare, 1)
        await ctx.tick("usb")
        ctx.set(top.dut.prepare, 0)
        for _ in range(16):
            if ctx.get(top.store.serve_response):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("first prepare never produced a response")

        # Replace the request on the exact cycle the old response is visible.
        assert ctx.get(top.store.serve_found)
        ctx.set(top.dut.value, 0x0200)
        ctx.set(top.dut.prepare, 1)
        await ctx.tick("usb")
        ctx.set(top.dut.prepare, 0)
        ctx.set(top.dut.start, 1)
        await ctx.tick("usb")
        ctx.set(top.dut.start, 0)

        collected = []
        for _ in range(40):
            if ctx.get(top.dut.tx.valid):
                collected.append(ctx.get(top.dut.tx.payload))
                if ctx.get(top.dut.tx.last):
                    break
            await ctx.tick("usb")
        assert bytes(collected) == b"new"

    sim.add_testbench(bench)
    sim.run()


def test_prepare_retries_after_same_cycle_clear_without_false_handshake():
    top = _StreamerHarness()
    sim = Simulator(top)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        await seed_descriptor(ctx, top.store, dtype=1, index=0, w_index=0, data=b"old")
        ctx.set(top.dut.value, 0x0100)
        ctx.set(top.dut.w_index, 0)
        ctx.set(top.dut.length, 3)
        ctx.set(top.dut.start_position, 0)
        ctx.set(top.dut.tx.ready, 1)

        ctx.set(top.dut.prepare, 1)
        ctx.set(top.store.clear, 1)
        assert ctx.get(top.store.serve_request)
        assert not ctx.get(top.store.serve_ready)
        await ctx.tick("usb")
        ctx.set(top.dut.prepare, 0)
        ctx.set(top.store.clear, 0)

        ctx.set(top.dut.start, 1)
        await ctx.tick("usb")
        ctx.set(top.dut.start, 0)

        responses = 0
        for _ in range(24):
            responses += int(ctx.get(top.store.serve_response))
            if ctx.get(top.dut.stall):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("prepare accepted during clear was lost")

        assert responses == 1

    sim.add_testbench(bench)
    sim.run()


def test_prepare_retries_after_same_cycle_successful_commit():
    top = _StreamerHarness()
    sim = Simulator(top)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        await seed_descriptor(ctx, top.store, dtype=1, index=0, w_index=0, data=b"old")

        ctx.set(top.store.capture_type, 1)
        ctx.set(top.store.capture_index, 0)
        ctx.set(top.store.capture_w_index, 0)
        ctx.set(top.store.capture_start, 1)
        await ctx.tick("usb")
        ctx.set(top.store.capture_start, 0)
        while not ctx.get(top.store.capture_ready):
            await ctx.tick("usb")
        ctx.set(top.store.capture_data, ord("N"))
        ctx.set(top.store.capture_valid, 1)
        await ctx.tick("usb")
        ctx.set(top.store.capture_valid, 0)

        ctx.set(top.dut.value, 0x0100)
        ctx.set(top.dut.w_index, 0)
        ctx.set(top.dut.length, 1)
        ctx.set(top.dut.start_position, 0)
        ctx.set(top.dut.tx.ready, 1)
        ctx.set(top.dut.prepare, 1)
        ctx.set(top.store.capture_commit, 1)
        assert ctx.get(top.store.serve_request)
        assert not ctx.get(top.store.serve_ready)
        await ctx.tick("usb")
        ctx.set(top.dut.prepare, 0)
        ctx.set(top.store.capture_commit, 0)

        chunk = await _collect_chunk(ctx, top.dut, 0x0100, 0, 1, 0)
        assert chunk == [ord("N")]

    sim.add_testbench(bench)
    sim.run()


def test_streamer_truncates_to_wlength():
    top = _StreamerHarness()
    sim = Simulator(top)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        await seed_descriptor(ctx, top.store, dtype=1, index=0, w_index=0, data=bytes(range(18)))
        chunk = await _collect_chunk(
            ctx, top.dut, value=0x0100, w_index=0, length=8, start_position=0
        )
        assert chunk == list(range(8))

    sim.add_testbench(bench)
    sim.run()


def test_streamer_stalls_on_missing_descriptor():
    top = _StreamerHarness()
    sim = Simulator(top)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        chunk = await _collect_chunk(
            ctx, top.dut, value=0x0600, w_index=0, length=10, start_position=0
        )
        assert chunk is None  # stalled

    sim.add_testbench(bench)
    sim.run()


def test_streamer_terminates_when_start_position_exceeds_length():
    """Regression: `start_position` past the end of a found descriptor must not hang.

    Before the fix, `chunk_end` collapsed to `effective_length <= start_position`,
    so `pos` (initialised to `start_position`) could never reach `chunk_end - 1`;
    `tx.valid` stayed asserted with zero-payload garbage forever and neither
    `tx.last` nor `stall` ever fired. The fix must reach a terminating cycle
    (a ZLP -- see the boundary test below) within a small, bounded number of
    cycles, deliver zero data bytes, and must not stall (the descriptor *was*
    found; only the position is out of range).
    """
    top = _StreamerHarness()
    sim = Simulator(top)
    sim.add_clock(1e-6, domain="usb")
    device_desc = bytes(range(18))

    async def bench(ctx):
        await seed_descriptor(ctx, top.store, dtype=1, index=0, w_index=0, data=device_desc)

        ctx.set(top.dut.value, 0x0100)
        ctx.set(top.dut.w_index, 0)
        ctx.set(top.dut.length, 18)
        ctx.set(top.dut.start_position, 20)  # well past the 18-byte descriptor
        ctx.set(top.dut.tx.ready, 1)
        ctx.set(top.dut.start, 1)
        await ctx.tick("usb")
        ctx.set(top.dut.start, 0)

        terminated = False
        saw_data_byte = False
        # A generous but firmly bounded window: a correct implementation
        # resolves this in 1-2 cycles. If this loop exhausts without
        # `terminated` becoming true, the streamer hung.
        for _ in range(20):
            assert ctx.get(top.dut.stall) == 0, "out-of-range start_position must not stall"
            if ctx.get(top.dut.tx.valid):
                if ctx.get(top.dut.tx.first):
                    saw_data_byte = True
                if ctx.get(top.dut.tx.last):
                    terminated = True
                    break
            await ctx.tick("usb")

        assert terminated, "streamer hung: tx.valid stayed asserted with no last/stall"
        assert not saw_data_byte, "streamer emitted a spurious data byte past the descriptor's end"

    sim.add_testbench(bench)
    sim.run()


def test_streamer_emits_zlp_at_exact_multiple_boundary():
    """An 8-byte descriptor with max_packet_size=4 needs a ZLP after two full chunks.

    Chunk 1 (start_position=0): 4 data bytes, tx.last on the 4th.
    Chunk 2 (start_position=4): 4 data bytes, tx.last on the 8th.
    Chunk 3 (start_position=8, == effective_length): a zero-length packet --
    tx.valid & tx.last without tx.first, and no data byte delivered. This is
    the LUNA wire convention for a ZLP (USBDataPacketGenerator, packet.py;
    GetDescriptorHandlerBlock.SEND_ZLP, descriptor.py).
    """
    top = _StreamerHarness(max_packet_size=4)
    sim = Simulator(top)
    sim.add_clock(1e-6, domain="usb")
    device_desc = bytes(range(8))

    async def bench(ctx):
        await seed_descriptor(ctx, top.store, dtype=1, index=0, w_index=0, data=device_desc)

        first_chunk = await _collect_chunk(
            ctx, top.dut, value=0x0100, w_index=0, length=8, start_position=0
        )
        assert first_chunk == list(range(4))
        # `_collect_chunk` returns as soon as it observes `tx.last`, one
        # cycle before the DUT's registered `active` flag actually clears.
        # Tick once more so the next `start` pulse lands while the DUT is
        # genuinely idle, instead of being silently absorbed by the tail
        # end of the previous (still-`active`) transfer.
        await ctx.tick("usb")

        second_chunk = await _collect_chunk(
            ctx, top.dut, value=0x0100, w_index=0, length=8, start_position=4
        )
        assert second_chunk == list(range(4, 8))
        await ctx.tick("usb")

        ctx.set(top.dut.value, 0x0100)
        ctx.set(top.dut.w_index, 0)
        ctx.set(top.dut.length, 8)
        ctx.set(top.dut.start_position, 8)
        ctx.set(top.dut.tx.ready, 1)
        ctx.set(top.dut.start, 1)
        await ctx.tick("usb")
        ctx.set(top.dut.start, 0)

        saw_zlp = False
        for _ in range(20):
            assert ctx.get(top.dut.stall) == 0, "the boundary ZLP must not stall"
            if ctx.get(top.dut.tx.valid):
                assert ctx.get(top.dut.tx.first) == 0, "a ZLP must not assert `first`"
                assert ctx.get(top.dut.tx.last) == 1, "a ZLP must assert `last` on its strobe cycle"
                saw_zlp = True
                break
            await ctx.tick("usb")

        assert saw_zlp, "streamer never emitted the terminating zero-length packet"

    sim.add_testbench(bench)
    sim.run()


def _make_handler_top():
    store = DescriptorStore()
    handler = ClonedStandardRequestHandler(store, max_packet_size=64)
    m = Module()
    m.submodules.store = store
    m.submodules.handler = handler

    class _Top(Elaboratable):
        def elaborate(self, platform):
            return m

    top = _Top()
    top.store = store
    top.handler = handler
    return top


def test_set_address_pulses_address_changed():
    top = _make_handler_top()
    sim = Simulator(top)
    sim.add_clock(1e-6, domain="usb")
    iface = top.handler.interface

    async def bench(ctx):
        ctx.set(iface.setup.type, USBRequestType.STANDARD)
        ctx.set(iface.setup.request, USBStandardRequests.SET_ADDRESS)
        ctx.set(iface.setup.value, 7)
        ctx.set(iface.setup.received, 1)
        await ctx.tick("usb")
        ctx.set(iface.setup.received, 0)
        # Drive the status phase; the handler should request the address change.
        # `handshakes_in.ack` is normally driven by the control endpoint's own
        # handshake detector once the host ACKs our status-phase ZLP -- it's an
        # input to this handler (RequestHandlerInterface.handshakes_in, see
        # luna/gateware/usb/usb2/request.py), not something the handler
        # generates itself, so the isolated-handler test must supply it.
        ctx.set(iface.status_requested, 1)
        ctx.set(iface.handshakes_in.ack, 1)
        seen = False
        for _ in range(10):
            if ctx.get(iface.address_changed):
                assert ctx.get(iface.new_address) == 7
                seen = True
                break
            await ctx.tick("usb")
        assert seen

    sim.add_testbench(bench)
    sim.run()


def test_get_descriptor_routes_through_handler_to_streamer():
    """A GET_DESCRIPTOR request driven at the handler's `.interface` must return the
    exact bytes seeded into the store, proving the handler's FSM correctly routes
    GET_DESCRIPTOR through `ClonedDescriptorStreamer` and out the control endpoint's
    `tx` stream (not just that the streamer works in isolation, per the Task-2 tests).
    """
    top = _make_handler_top()
    sim = Simulator(top)
    sim.add_clock(1e-6, domain="usb")
    iface = top.handler.interface
    device_desc = bytes(range(18))

    async def bench(ctx):
        await seed_descriptor(ctx, top.store, dtype=1, index=0, w_index=0, data=device_desc)

        ctx.set(iface.setup.type, USBRequestType.STANDARD)
        ctx.set(iface.setup.is_in_request, 1)
        ctx.set(iface.setup.request, USBStandardRequests.GET_DESCRIPTOR)
        ctx.set(iface.setup.value, 0x0100)  # DEVICE descriptor, index 0
        ctx.set(iface.setup.index, 0)
        ctx.set(iface.setup.length, 18)
        ctx.set(iface.setup.received, 1)
        assert ctx.get(top.store.serve_request), "GET_DESCRIPTOR SETUP did not start prepare"
        await ctx.tick("usb")
        ctx.set(iface.setup.received, 0)

        # Drive the data phase: an IN token has arrived and we're ready to accept
        # the response.
        ctx.set(iface.tx.ready, 1)
        ctx.set(iface.data_requested, 1)
        await ctx.tick("usb")
        ctx.set(iface.data_requested, 0)

        collected = []
        for _ in range(80):
            if ctx.get(iface.tx.valid):
                collected.append(ctx.get(iface.tx.payload))
                if ctx.get(iface.tx.last):
                    break
            await ctx.tick("usb")

        assert collected == list(device_desc)

    sim.add_testbench(bench)
    sim.run()


def test_hid_set_idle_is_acked():
    handler = HIDClassRequestHandler()
    m = Module()
    m.submodules.handler = handler

    class _Top(Elaboratable):
        def elaborate(self, platform):
            return m

    sim = Simulator(_Top())
    sim.add_clock(1e-6, domain="usb")
    iface = handler.interface

    async def bench(ctx):
        ctx.set(iface.setup.type, 1)  # USBRequestType.CLASS == 1
        ctx.set(iface.setup.request, HID_SET_IDLE)
        ctx.set(iface.setup.received, 1)
        await ctx.tick("usb")
        ctx.set(iface.setup.received, 0)
        ctx.set(iface.status_requested, 1)
        acked = False
        for _ in range(10):
            if ctx.get(iface.handshakes_out.ack):
                acked = True
                break
            await ctx.tick("usb")
        assert acked

    sim.add_testbench(bench)
    sim.run()


async def _prepare_then_start_on_serve_response(
    ctx, top, *, value, w_index, length, start_position
):
    """Pulse ``start`` on the exact cycle the store answers the directory scan.

    Returns whether the collision was actually exercised, so the test fails loudly
    if store latency ever changes and this case stops being reachable.
    """
    dut = top.dut
    ctx.set(dut.value, value)
    ctx.set(dut.w_index, w_index)
    ctx.set(dut.length, length)
    ctx.set(dut.start_position, start_position)
    ctx.set(dut.tx.ready, 1)
    ctx.set(dut.prepare, 1)
    await ctx.tick("usb")
    ctx.set(dut.prepare, 0)

    coincident = False
    for _ in range(40):
        if ctx.get(top.store.serve_response):
            coincident = True
            break
        await ctx.tick("usb")
    ctx.set(dut.start, 1)
    await ctx.tick("usb")
    ctx.set(dut.start, 0)
    return coincident


async def _collect_after_start(ctx, dut, *, limit: int = 200):
    """Drain one packet, returning (payload_bytes, stalled)."""
    out = []
    for _ in range(limit):
        if ctx.get(dut.stall):
            return out, True
        if ctx.get(dut.tx.valid):
            out.append(ctx.get(dut.tx.payload))
            if ctx.get(dut.tx.last):
                return out, False
        await ctx.tick("usb")
    raise AssertionError("packet did not complete")


async def _seed_decoys(ctx, store, count: int) -> None:
    for index in range(count):
        await seed_descriptor(
            ctx, store, dtype=3, index=index, w_index=0x0409, data=bytes([index] * 4)
        )


def test_start_coincident_with_serve_response_still_arms_transfer():
    # serve_response fires at prepare + 3 + slot, a one-cycle window at a fixed
    # directory slot. A start landing on it used to set `prepared` while latching
    # neither request_length nor request_start, so nothing was armed and the
    # device emitted nothing at all - a timeout, not even a STALL.
    top = _StreamerHarness()
    sim = Simulator(top)
    sim.add_clock(1e-6, domain="usb")
    descriptor = bytes(range(18))

    async def bench(ctx):
        await _seed_decoys(ctx, top.store, 5)
        await seed_descriptor(ctx, top.store, dtype=1, index=0, w_index=0, data=descriptor)
        coincident = await _prepare_then_start_on_serve_response(
            ctx, top, value=0x0100, w_index=0, length=18, start_position=0
        )
        assert coincident, "store latency changed; the collision is no longer exercised"

        payload, stalled = await _collect_after_start(ctx, top.dut)
        assert not stalled
        assert payload == list(descriptor)

    sim.add_testbench(bench)
    sim.run()


def test_start_coincident_with_missing_descriptor_stalls():
    # The not-found path answers at the end of the full directory scan, so it is
    # the most exposed. On collision it used to go silent instead of stalling,
    # trading a fast STALL for a timeout on every speculative OS probe.
    top = _StreamerHarness()
    sim = Simulator(top)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        await _seed_decoys(ctx, top.store, 5)
        await seed_descriptor(ctx, top.store, dtype=1, index=0, w_index=0, data=bytes(range(18)))
        coincident = await _prepare_then_start_on_serve_response(
            ctx, top, value=0x0300, w_index=0, length=18, start_position=0
        )
        assert coincident, "store latency changed; the collision is no longer exercised"

        payload, stalled = await _collect_after_start(ctx, top.dut)
        assert stalled, "missing descriptor went silent instead of stalling"
        assert payload == []

    sim.add_testbench(bench)
    sim.run()


def test_start_coincident_first_packet_of_multipacket_descriptor():
    # On collision the first 64-byte packet used to be dropped entirely, leaving
    # the host with [0, 64, 2] instead of [64, 64, 2].
    top = _StreamerHarness()
    sim = Simulator(top)
    sim.add_clock(1e-6, domain="usb")
    descriptor = bytes((index * 7) & 0xFF for index in range(130))

    async def bench(ctx):
        await _seed_decoys(ctx, top.store, 5)
        await seed_descriptor(ctx, top.store, dtype=2, index=0, w_index=0, data=descriptor)
        coincident = await _prepare_then_start_on_serve_response(
            ctx, top, value=0x0200, w_index=0, length=130, start_position=0
        )
        assert coincident, "store latency changed; the collision is no longer exercised"

        first, stalled = await _collect_after_start(ctx, top.dut)
        assert not stalled
        await ctx.tick("usb")
        second = await _collect_chunk(ctx, top.dut, 0x0200, 0, 130, 64)
        await ctx.tick("usb")
        third = await _collect_chunk(ctx, top.dut, 0x0200, 0, 130, 128)

        assert [len(first), len(second), len(third)] == [64, 64, 2]
        assert bytes(first + second + third) == descriptor

    sim.add_testbench(bench)
    sim.run()
