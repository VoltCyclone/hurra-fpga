"""BootProtocolTracker: which endpoints the real device is sending boot-layout reports on.

The relay is scripted here. The snoop side is driven with ``forward_ok`` pulses
carrying a forwarded request; the resync side by a background model of the
relay's second source that accepts an offer, holds ``resync_active``, then
pulses ``resync_done`` with a scripted outcome.
"""

from amaranth.sim import Simulator

from hurra_cynthion.boot_protocol import BootProtocolTracker

SET_IDLE = 0x0A
SET_PROTOCOL = 0x0B

#: slot -> (interface, endpoint number). Interface 2 owns endpoints 1 and 3;
#: interface 5 owns endpoint 2; interface 7 owns endpoint 4.
DEFAULT_TABLE = [(2, 1), (5, 2), (2, 3), (7, 4)]


def bits(*numbers: int) -> int:
    return sum(1 << n for n in numbers)


class ScriptedRelay:
    """The relay's resync source: accept, hold active, complete with a scripted error."""

    def __init__(self, dut: BootProtocolTracker, *, busy_cycles: int = 4) -> None:
        self.dut = dut
        self.busy_cycles = busy_cycles
        self.accepted: list[int] = []
        self.errors: list[int] = []
        #: Set to hold the relay busy with a PC request: no offer is taken.
        self.blocked = False
        self.in_flight = False

    async def run(self, ctx) -> None:
        dut = self.dut
        while True:
            if not self.blocked and ctx.get(dut.resync_valid):
                self.accepted.append(ctx.get(dut.resync_index))
                self.in_flight = True
                await ctx.tick("usb")
                ctx.set(dut.relay_resync_active, 1)
                for _ in range(self.busy_cycles):
                    await ctx.tick("usb")
                ctx.set(dut.relay_resync_active, 0)
                ctx.set(dut.relay_resync_done, 1)
                ctx.set(dut.relay_error, self.errors.pop(0) if self.errors else 0)
                await ctx.tick("usb")
                ctx.set(dut.relay_resync_done, 0)
                self.in_flight = False
            else:
                await ctx.tick("usb")


def simulate(bench, *, table=DEFAULT_TABLE, busy_cycles: int = 4) -> None:
    dut = BootProtocolTracker()
    relay = ScriptedRelay(dut, busy_cycles=busy_cycles)
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def wrapped(ctx) -> None:
        ctx.set(dut.ep_count, len(table))
        for k, (interface, number) in enumerate(table):
            ctx.set(dut.ep_interface[k], interface)
            ctx.set(dut.ep_number[k], number)
        ctx.set(dut.ready, 1)
        await ctx.tick("usb")
        await bench(ctx, dut, relay)

    async def relay_model(ctx) -> None:
        await relay.run(ctx)

    simulation.add_testbench(relay_model, background=True)
    simulation.add_testbench(wrapped)
    simulation.run()


async def forward(
    ctx, dut, *, request=SET_PROTOCOL, value=0, index=2, request_type=0x21, settle=True
) -> None:
    """One forward_ok pulse, as the relay raises it after a successful forward."""
    ctx.set(dut.forward_type, request_type)
    ctx.set(dut.forward_request, request)
    ctx.set(dut.forward_value, value)
    ctx.set(dut.forward_index, index)
    ctx.set(dut.forward_ok, 1)
    await ctx.tick("usb")
    ctx.set(dut.forward_ok, 0)
    if settle:
        for _ in range(3):
            await ctx.tick("usb")


async def trigger(ctx, dut) -> None:
    ctx.set(dut.resync_trigger, 1)
    await ctx.tick("usb")
    ctx.set(dut.resync_trigger, 0)


async def run_for(ctx, dut, cycles: int, *, watch=()) -> dict[str, int]:
    counts = {name: 0 for name in watch}
    for _ in range(cycles):
        for name in watch:
            counts[name] += ctx.get(getattr(dut, name))
        await ctx.tick("usb")
    return counts


# --- The snoop: boot_mask follows the device ---------------------------------------


def test_set_protocol_boot_marks_every_endpoint_of_that_interface_only() -> None:
    async def bench(ctx, dut, relay) -> None:
        assert ctx.get(dut.boot_mask) == 0
        await forward(ctx, dut, value=0, index=2)
        assert ctx.get(dut.boot_mask) == bits(1, 3)
        await forward(ctx, dut, value=0, index=7)
        assert ctx.get(dut.boot_mask) == bits(1, 3, 4)

    simulate(bench)


def test_set_protocol_report_clears_only_that_interface() -> None:
    async def bench(ctx, dut, relay) -> None:
        await forward(ctx, dut, value=0, index=2)
        await forward(ctx, dut, value=0, index=5)
        await forward(ctx, dut, value=1, index=2)
        assert ctx.get(dut.boot_mask) == bits(2)

    simulate(bench)


def test_other_forwards_are_ignored() -> None:
    async def bench(ctx, dut, relay) -> None:
        await forward(ctx, dut, request=SET_IDLE, value=0, index=2)
        await forward(ctx, dut, request_type=0xA1, value=0, index=2)  # device-to-host
        await forward(ctx, dut, request_type=0x22, value=0, index=2)  # endpoint recipient
        await forward(ctx, dut, value=0, index=0x0102)  # wIndex high byte is not ours
        assert ctx.get(dut.boot_mask) == 0

    simulate(bench)


def test_slots_past_ep_count_are_never_marked() -> None:
    async def bench(ctx, dut, relay) -> None:
        ctx.set(dut.ep_count, 2)
        await forward(ctx, dut, value=0, index=2)
        assert ctx.get(dut.boot_mask) == bits(1)

    simulate(bench)


def test_losing_the_device_clears_the_mask() -> None:
    """A TARGET reset returns every HID device to report protocol [HID 1.11 7.2.6]."""

    async def bench(ctx, dut, relay) -> None:
        await forward(ctx, dut, value=0, index=2)
        ctx.set(dut.ready, 0)
        await ctx.tick("usb")
        assert ctx.get(dut.boot_mask) == 0
        ctx.set(dut.ready, 1)
        await ctx.tick("usb")
        assert ctx.get(dut.boot_mask) == 0

    simulate(bench)


# --- The resync: SET_PROTOCOL(report) replayed after the PC resets the clone --------


def test_a_trigger_with_nothing_in_boot_offers_nothing() -> None:
    async def bench(ctx, dut, relay) -> None:
        await trigger(ctx, dut)
        counts = await run_for(ctx, dut, 30, watch=("resync_valid",))
        assert counts["resync_valid"] == 0
        assert relay.accepted == []

    simulate(bench)


def test_one_interface_is_replayed_once_and_both_its_endpoints_clear() -> None:
    async def bench(ctx, dut, relay) -> None:
        await forward(ctx, dut, value=0, index=2)
        await trigger(ctx, dut)
        counts = await run_for(ctx, dut, 40, watch=("resync_ok", "resync_fail"))
        assert relay.accepted == [2]
        assert counts == {"resync_ok": 1, "resync_fail": 0}
        assert ctx.get(dut.boot_mask) == 0

    simulate(bench)


def test_interfaces_are_replayed_one_at_a_time_lowest_slot_first() -> None:
    async def bench(ctx, dut, relay) -> None:
        await forward(ctx, dut, value=0, index=7)
        await forward(ctx, dut, value=0, index=5)
        await forward(ctx, dut, value=0, index=2)
        await trigger(ctx, dut)
        early = []
        for _ in range(80):
            # The next interface is never offered while one is in flight.
            index = ctx.get(dut.resync_index)
            if relay.in_flight and ctx.get(dut.resync_valid) and index != relay.accepted[-1]:
                early.append(index)
            await ctx.tick("usb")
        # Slot order 0 (iface 2), 1 (iface 5), 3 (iface 7); slot 2 is iface 2 again.
        assert relay.accepted == [2, 5, 7]
        assert early == []
        assert ctx.get(dut.boot_mask) == 0

    simulate(bench)


def test_a_failed_replay_keeps_the_bits_and_waits_for_the_next_trigger() -> None:
    async def bench(ctx, dut, relay) -> None:
        await forward(ctx, dut, value=0, index=5)
        relay.errors.append(1)
        await trigger(ctx, dut)
        counts = await run_for(ctx, dut, 40, watch=("resync_ok", "resync_fail"))
        assert counts == {"resync_ok": 0, "resync_fail": 1}
        assert relay.accepted == [5]
        assert ctx.get(dut.boot_mask) == bits(2), "injection must stay stood down"

        await trigger(ctx, dut)
        counts = await run_for(ctx, dut, 40, watch=("resync_ok", "resync_fail"))
        assert counts == {"resync_ok": 1, "resync_fail": 0}
        assert relay.accepted == [5, 5]
        assert ctx.get(dut.boot_mask) == 0

    simulate(bench)


def test_a_trigger_mid_replay_does_not_repeat_a_finished_interface() -> None:
    async def bench(ctx, dut, relay) -> None:
        await forward(ctx, dut, value=0, index=2)
        await forward(ctx, dut, value=0, index=5)
        await trigger(ctx, dut)
        while not relay.in_flight:
            await ctx.tick("usb")
        # A second PC reset lands while interface 2's replay is in flight.
        await trigger(ctx, dut)
        await run_for(ctx, dut, 80)
        assert relay.accepted == [2, 5]
        assert ctx.get(dut.boot_mask) == 0

    simulate(bench, busy_cycles=10)


def test_losing_the_device_mid_replay_resets_everything_and_ignores_the_late_done() -> None:
    async def bench(ctx, dut, relay) -> None:
        await forward(ctx, dut, value=0, index=2)
        await forward(ctx, dut, value=0, index=5)
        await trigger(ctx, dut)
        while not relay.in_flight:
            await ctx.tick("usb")
        ctx.set(dut.ready, 0)
        await ctx.tick("usb")
        ctx.set(dut.ready, 1)
        counts = await run_for(ctx, dut, 60, watch=("resync_ok", "resync_fail", "resync_valid"))
        assert counts == {"resync_ok": 0, "resync_fail": 0, "resync_valid": 0}
        assert relay.accepted == [2]
        assert ctx.get(dut.boot_mask) == 0

    simulate(bench, busy_cycles=10)


def test_a_pc_set_protocol_supersedes_an_offered_replay_of_its_interface() -> None:
    """A replay landing after the PC's own SET_PROTOCOL(boot) would undo it.

    forward_ok is high on the relay's first COMPLETE cycle, T. A handler
    draining an abandoned forward acks it on T itself, so the relay is back in
    IDLE -- taking offers -- from T+1: the offer must already be withdrawn
    then, before the snoop's registered decode has cleared ``pending``. The
    bench samples ``resync_valid`` on each cycle itself, since a background
    model's view of "this cycle" depends on which testbench resumes first.
    """

    async def bench(ctx, dut, relay) -> None:
        await forward(ctx, dut, value=0, index=5)
        relay.blocked = True  # the relay is busy forwarding the PC's request
        await trigger(ctx, dut)
        await run_for(ctx, dut, 10)
        assert ctx.get(dut.resync_valid)
        assert ctx.get(dut.resync_index) == 5

        await forward(ctx, dut, value=0, index=5, settle=False)
        for cycle in range(1, 40):
            assert not ctx.get(dut.resync_valid), f"replay still offered at T+{cycle}"
            await ctx.tick("usb")
        relay.blocked = False
        await run_for(ctx, dut, 20)
        assert relay.accepted == []
        assert ctx.get(dut.boot_mask) == bits(2)

    simulate(bench)


def test_a_pc_forward_of_another_request_does_not_cancel_the_replay() -> None:
    async def bench(ctx, dut, relay) -> None:
        await forward(ctx, dut, value=0, index=5)
        relay.blocked = True
        await trigger(ctx, dut)
        await run_for(ctx, dut, 10)
        await forward(ctx, dut, request=SET_IDLE, value=0, index=5)
        await forward(ctx, dut, value=0, index=2)  # another interface's SET_PROTOCOL
        relay.blocked = False
        await run_for(ctx, dut, 40)
        assert relay.accepted == [5]
        assert ctx.get(dut.boot_mask) == bits(1, 3)

    simulate(bench)


def test_a_held_trigger_replays_once_even_when_the_replay_fails() -> None:
    """LUNA holds its bus reset as a level while AUX VBUS is absent.

    Acting on the level would replay a failing SET_PROTOCOL back to back for as
    long as the PC stays unplugged, pre-empting the input pollers each time.
    """

    async def bench(ctx, dut, relay) -> None:
        await forward(ctx, dut, value=0, index=5)
        relay.errors.extend([1] * 10)
        ctx.set(dut.resync_trigger, 1)
        counts = await run_for(ctx, dut, 120, watch=("resync_fail",))
        ctx.set(dut.resync_trigger, 0)
        assert counts["resync_fail"] == 1
        assert relay.accepted == [5]

    simulate(bench)
