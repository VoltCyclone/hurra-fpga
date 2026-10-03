"""End to end: one HID interrupt-OUT endpoint relayed PC -> clone -> real device.

Nothing on the OUT path is stubbed. A scripted PC drives the raw UTMI bus of
the real ``MouseCloneDevice``, whose LUNA OUT endpoint streams into the real
``BoundedMouseHost``'s ``InterruptOutWriter``; that shares the real arbiter and
the real ``USBHostTransactionEngine`` with the IN pollers, onto the TARGET wire
of a raw-UTMI model of the device. The two halves are joined by
``gateware.connect_clone_to_host`` -- the function the production top calls -- plus
the clone connect/copy/EP0 lines copied from the top.

Not covered, because none of it touches the OUT path: the ULPI PHYs and PLL the
production top instantiates, and the injection plane (IN reports are discarded
here; the device NAKs every poll anyway).
"""

from _ds4_fixture import DS4_CONFIG_DESCRIPTOR, DS4_REPORT_DESCRIPTOR
from _out_relay_target import (
    OutPacket,
    OutRelayTarget,
    hid_configuration,
    serve,
    wait_until,
)
from amaranth import Elaboratable, Module
from amaranth.sim import Simulator
from luna.gateware.interface.utmi import UTMIInterface
from test_device_clone_e2e import (
    ACK,
    DATA0,
    DATA1,
    IN_PID,
    NAK,
    SET_ADDRESS,
    SET_CONFIGURATION,
    STALL,
    control_write_no_data,
    pc_handshake_or_none,
    pc_out,
    pc_send,
    token_packet,
)
from test_end_to_end import assert_power_attach_and_reset

from hurra_cynthion.device import MouseCloneDevice
from hurra_cynthion.gateware import connect_clone_to_host
from hurra_cynthion.host import BoundedMouseHost
from hurra_cynthion.timing import HostTiming

#: The address the PC gives the clone; deliberately not the 1 the host gives
#: the real device, so a forwarded token proves the host re-addressed it.
CLONE_ADDRESS = 9
DS4_OUT_ENDPOINT = 3
DS4_IN_ENDPOINT = 4
DS4_INTERFACE = 3


def ds4_output_report(rumble: int, red: int, green: int, blue: int) -> bytes:
    """A 32-byte DS4 USB output report 0x05: flags, rumble, lightbar, padding."""
    report = bytes([0x05, 0x07, 0x00, 0x00, rumble, rumble ^ 0xFF, red, green, blue])
    return report + bytes(32 - len(report))


class OutRelayHarness(Elaboratable):
    """The production host and clone, wired as CynthionMouseHostTop wires them."""

    def __init__(self) -> None:
        self.timing = HostTiming.simulation()
        self.host = BoundedMouseHost(timing=self.timing)
        self.pc_bus = UTMIInterface()
        self.device = MouseCloneDevice(
            bus=self.pc_bus,
            store=self.host.descriptor_store,
            control_relay=self.host.control_relay,
        )

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        m.submodules.host = host = self.host
        m.submodules.device = device = self.device
        m.d.comb += [
            device.copy_enable.eq(host.enumerated),
            device.connect.eq(host.enumerated & device.copy_done),
            device.ep0_max_packet.eq(host.enumerator.ep0_max_packet),
            # IN reports are not under test here.
            host.report_ready.eq(1),
        ]
        connect_clone_to_host(m, host=host, device=device)
        return m


def _run(target: OutRelayTarget, bench, *, watch=None) -> None:
    """Run ``bench`` once the PC has addressed and configured the clone.

    ``watch``, if given, runs in the background from the first cycle, for
    one-cycle pulses a bench blocked inside a transfer helper would miss.
    """
    dut = OutRelayHarness()
    host, device, pc_bus = dut.host, dut.device, dut.pc_bus
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def real_device(ctx) -> None:
        await serve(ctx, host, target)

    async def pc(ctx) -> None:
        ctx.set(host.utmi.tx_ready, 1)
        ctx.set(pc_bus.tx_ready, 1)
        ctx.set(pc_bus.line_state, 1)
        await assert_power_attach_and_reset(ctx, host, dut.timing)
        await wait_until(
            ctx,
            lambda: bool(ctx.get(host.enumerated) and ctx.get(pc_bus.term_select)),
            limit=40_000,
            what="the host to enumerate the device and connect the clone",
        )
        assert (
            await control_write_no_data(
                ctx,
                pc_bus,
                address=0,
                request_type=0x00,
                request=SET_ADDRESS,
                value=CLONE_ADDRESS,
                index=0,
                pulse_signal=device.debug_address_changed,
                value_signal=device.debug_new_address,
            )
            == CLONE_ADDRESS
        )
        assert (
            await control_write_no_data(
                ctx,
                pc_bus,
                address=CLONE_ADDRESS,
                request_type=0x00,
                request=SET_CONFIGURATION,
                value=1,
                index=0,
                pulse_signal=device.debug_config_changed,
                value_signal=device.debug_new_config,
            )
            == 1
        )
        await bench(ctx, dut)

    simulation.add_testbench(real_device, background=True)
    if watch is not None:

        async def watcher(ctx) -> None:
            await watch(ctx, dut)

        simulation.add_testbench(watcher, background=True)
    simulation.add_testbench(pc)
    simulation.run()


def _ds4_target() -> OutRelayTarget:
    return OutRelayTarget(
        configuration=DS4_CONFIG_DESCRIPTOR,
        report_descriptor=DS4_REPORT_DESCRIPTOR,
        interface=DS4_INTERFACE,
        in_endpoint=DS4_IN_ENDPOINT,
        out_endpoint=DS4_OUT_ENDPOINT,
    )


async def _pc_writes(ctx, dut, pid: int, report: bytes) -> None:
    handshake = await pc_out(ctx, dut.pc_bus, CLONE_ADDRESS, DS4_OUT_ENDPOINT, pid, report)
    assert handshake == [ACK], f"the clone must ACK the PC's OUT: {handshake!r}"


async def _await_target_packets(ctx, target: OutRelayTarget, count: int) -> None:
    await wait_until(
        ctx,
        lambda: len(target.out_packets) >= count,
        limit=20_000,
        what=f"{count} OUT packets at the real device",
    )
    assert len(target.out_packets) == count, target.out_packets


def test_a_ds4_output_report_reaches_the_real_device_verbatim() -> None:
    target = _ds4_target()
    reports = [ds4_output_report(0x10 * k, 0xFF, k, 0x80) for k in range(5)]

    async def bench(ctx, dut) -> None:
        host = dut.host
        assert ctx.get(host.out_present)
        assert ctx.get(host.out_number) == DS4_OUT_ENDPOINT

        # PC DATA0 -> device DATA0, then PC DATA1 -> device DATA1, bytes intact.
        # The host addresses the real device at 1, not at the clone's address.
        await _pc_writes(ctx, dut, DATA0, reports[0])
        await _await_target_packets(ctx, target, 1)
        assert target.out_packets[0] == OutPacket(1, DS4_OUT_ENDPOINT, DATA0, reports[0], ACK)
        await _pc_writes(ctx, dut, DATA1, reports[1])
        await _await_target_packets(ctx, target, 2)
        assert target.out_packets[1] == OutPacket(1, DS4_OUT_ENDPOINT, DATA1, reports[1], ACK)

        # One NAK from the device: the same packet is resent identically.
        target.out_script.append(NAK)
        await _pc_writes(ctx, dut, DATA0, reports[2])
        await _await_target_packets(ctx, target, 4)
        assert target.out_packets[2:] == [
            OutPacket(1, DS4_OUT_ENDPOINT, DATA0, reports[2], NAK),
            OutPacket(1, DS4_OUT_ENDPOINT, DATA0, reports[2], ACK),
        ]

        # A STALL drops that packet and the next still flows. The writer's
        # toggle does not advance on a STALL, and nothing clears the real
        # endpoint's halt (out of scope), so the next goes out on DATA1 again.
        target.out_script.append(STALL)
        await _pc_writes(ctx, dut, DATA1, reports[3])
        await _await_target_packets(ctx, target, 5)
        assert target.out_packets[4] == OutPacket(1, DS4_OUT_ENDPOINT, DATA1, reports[3], STALL)
        await _pc_writes(ctx, dut, DATA0, reports[4])
        await _await_target_packets(ctx, target, 6)
        assert target.out_packets[5] == OutPacket(1, DS4_OUT_ENDPOINT, DATA1, reports[4], ACK)

        # A full 64-byte packet carries no ``last`` out of LUNA's endpoint; the
        # writer frames it by wMaxPacketSize, and the short one after it still
        # starts a packet of its own.
        full = bytes(range(0x40, 0x80))
        await _pc_writes(ctx, dut, DATA1, full)
        await _pc_writes(ctx, dut, DATA0, reports[0])
        await _await_target_packets(ctx, target, 8)
        assert target.out_packets[6:] == [
            OutPacket(1, DS4_OUT_ENDPOINT, DATA0, full, ACK),
            OutPacket(1, DS4_OUT_ENDPOINT, DATA1, reports[0], ACK),
        ]

        # Nothing on the OUT path disturbed the IN side of the relay.
        assert ctx.get(host.enumerated)
        assert not ctx.get(host.polling_failed)
        assert target.in_polls > 0

    _run(target, bench)


def test_a_device_without_a_relayable_out_endpoint_gets_no_out_traffic() -> None:
    """A 65-byte OUT endpoint is beyond the relay: the clone serves no OUT at all."""
    target = OutRelayTarget(
        configuration=hid_configuration(
            in_address=0x81,
            out_address=0x05,
            in_mps=8,
            out_mps=65,
            interval=1,
            report_length=5,
        ),
        report_descriptor=bytes(5),
        interface=0,
        in_endpoint=1,
        out_endpoint=5,
    )

    async def bench(ctx, dut) -> None:
        host = dut.host
        assert not ctx.get(host.out_present)
        assert ctx.get(host.enumerator.out_ignored)
        for endpoint in (5, 1, 15):
            handshake = await pc_out(ctx, dut.pc_bus, CLONE_ADDRESS, endpoint, DATA0, b"\x01")
            assert handshake is None, (endpoint, handshake)
        for _ in range(200):
            await ctx.tick("usb")
        assert target.out_packets == []
        assert ctx.get(host.enumerated)

    _run(target, bench)


def test_a_pc_reconfiguration_flushes_the_writer_through_the_production_wiring() -> None:
    """The clone's OUT FIFO reset must reach the writer, or a stale partial packet splices."""
    target = _ds4_target()
    flushes = [0]

    async def watch(ctx, dut) -> None:
        while True:
            flushes[0] += ctx.get(dut.host.out_writer.flush)
            await ctx.tick("usb")

    async def bench(ctx, dut) -> None:
        # The PC's bus reset and first SET_CONFIGURATION have flushed already.
        before = flushes[0]
        assert before > 0
        await control_write_no_data(
            ctx,
            dut.pc_bus,
            address=CLONE_ADDRESS,
            request_type=0x00,
            request=SET_CONFIGURATION,
            value=1,
            index=0,
            pulse_signal=dut.device.debug_config_changed,
            value_signal=dut.device.debug_new_config,
        )
        for _ in range(4):
            await ctx.tick("usb")
        assert flushes[0] == before + 1, "one SET_CONFIGURATION must flush exactly once"

    _run(target, bench, watch=watch)


def test_high_endpoint_numbers_are_served_through_the_production_wiring() -> None:
    """IN endpoint 15 and OUT endpoint 12, bound from the host by connect_clone_to_host.

    The model device NAKs every poll, so the clone has no report to give: a
    bound IN endpoint answers the PC's token with a NAK, where an unbound one
    stays silent.
    """
    target = OutRelayTarget(
        configuration=hid_configuration(
            in_address=0x8F,
            out_address=0x0C,
            in_mps=8,
            out_mps=8,
            interval=1,
            report_length=5,
        ),
        report_descriptor=bytes(5),
        interface=0,
        in_endpoint=15,
        out_endpoint=12,
    )

    async def bench(ctx, dut) -> None:
        host = dut.host
        assert ctx.get(host.ep_number[0]) == 15
        assert ctx.get(host.out_number) == 12

        await pc_send(ctx, dut.pc_bus, token_packet(IN_PID, CLONE_ADDRESS, 15))
        assert await pc_handshake_or_none(ctx, dut.pc_bus) == [NAK]
        await pc_send(ctx, dut.pc_bus, token_packet(IN_PID, CLONE_ADDRESS, 1))
        assert await pc_handshake_or_none(ctx, dut.pc_bus) is None

        report = bytes([0x01, 0x02, 0x03])
        handshake = await pc_out(ctx, dut.pc_bus, CLONE_ADDRESS, 12, DATA0, report)
        assert handshake == [ACK]
        await _await_target_packets(ctx, target, 1)
        assert target.out_packets[0] == OutPacket(1, 12, DATA0, report, ACK)

    _run(target, bench)
