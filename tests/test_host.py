import os
import re
import types
import warnings

from _out_relay_target import OutPacket, OutRelayTarget, hid_configuration, serve, wait_until
from amaranth import Const, Elaboratable, Module, Signal
from amaranth.back import rtlil
from amaranth.sim import Simulator
from cynthion.gateware.platform import CynthionPlatformRev1D4
from luna.gateware.interface.utmi import UTMIInterface
from test_end_to_end import (
    ACK,
    DATA0,
    DATA1,
    NAK,
    STALL,
    SetupRequest,
    assert_power_attach_and_reset,
)

from hurra_cynthion.control import USBControlTransferEngine
from hurra_cynthion.control_relay import ControlRelay
from hurra_cynthion.descriptors import MAX_ENDPOINTS
from hurra_cynthion.gateware import (
    _INJECTION_LINK_PMOD_A,
    CynthionMouseHostTop,
    injection_link_directions,
)
from hurra_cynthion.host import (
    BoundedMouseHost,
    ReportMergeMux,
    USBHostTransactionArbiter,
)
from hurra_cynthion.out_writer import InterruptOutWriter
from hurra_cynthion.poller import IN_PID, InterruptInPoller
from hurra_cynthion.timing import HostTiming
from hurra_cynthion.transaction import OUT_PID, USBHostTransactionEngine, USBHostTransactionPort
from hurra_cynthion.types import HostError, TransactionStatus

SOF_PID = 0x5


def usb_crc5(payload: int) -> int:
    remainder = 0x1F
    for bit_number in range(11):
        feedback = ((payload >> bit_number) & 1) ^ (remainder & 1)
        remainder >>= 1
        if feedback:
            remainder ^= 0x14
    return remainder ^ 0x1F


def sof_packet(frame: int) -> list[int]:
    return [
        0xA5,
        frame & 0xFF,
        ((frame >> 8) & 0x07) | (usb_crc5(frame) << 3),
    ]


def test_transaction_engine_emits_sof_and_finishes_without_response() -> None:
    utmi = UTMIInterface()
    dut = USBHostTransactionEngine(utmi=utmi, timing=HostTiming.simulation())
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        frame = 0x345
        ctx.set(utmi.tx_ready, 1)
        ctx.set(dut.connected, 1)
        ctx.set(dut.sof, 1)
        ctx.set(dut.frame, frame)
        ctx.set(dut.start, 1)
        await ctx.tick("usb")
        ctx.set(dut.start, 0)

        packet = []
        for _ in range(20):
            if ctx.get(utmi.tx_valid):
                packet.append(ctx.get(utmi.tx_data))
            if ctx.get(dut.done):
                break
            await ctx.tick("usb")

        assert packet == sof_packet(frame)
        assert ctx.get(dut.status) == TransactionStatus.SUCCESS.value
        assert ctx.get(dut.rx_length) == 0
        assert not ctx.get(dut.busy)

    simulation.add_testbench(bench)
    simulation.run()


class ScriptedEngine(USBHostTransactionPort, Elaboratable):
    def elaborate(self, platform) -> Module:
        del platform
        return Module()


class PollSofHarness(Elaboratable):
    def __init__(self) -> None:
        self.engine = ScriptedEngine()
        self.control_port = USBHostTransactionPort()
        self.poller_port = USBHostTransactionPort()
        self.poller = InterruptInPoller(
            transaction=self.poller_port,
            timing=HostTiming.simulation(),
            add_transaction_submodule=False,
        )
        self.arbiter = USBHostTransactionArbiter(
            engine=self.engine,
            control_port=self.control_port,
            poller_ports=[self.poller_port],
        )

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        m.submodules.engine = self.engine
        m.submodules.poller = self.poller
        m.submodules.arbiter = self.arbiter
        m.d.comb += [
            self.poller.enable.eq(1),
            self.poller.connected.eq(1),
            self.poller.address.eq(5),
            self.poller.endpoint.eq(3),
            self.poller.max_packet_size.eq(8),
            self.poller.interval.eq(1),
            self.arbiter.control_phase.eq(0),
            self.arbiter.connected.eq(1),
        ]
        return m


def test_sof_wins_a_collision_and_due_poll_is_issued_exactly_once_afterward() -> None:
    dut = PollSofHarness()
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        ctx.set(dut.poller.sof_tick, 1)
        ctx.set(dut.arbiter.sof_start, 1)
        ctx.set(dut.arbiter.sof_frame, 9)
        await ctx.tick("usb")
        ctx.set(dut.poller.sof_tick, 0)

        assert ctx.get(dut.engine.start)
        assert ctx.get(dut.engine.sof)
        assert ctx.get(dut.engine.frame) == 9
        assert not ctx.get(dut.poller_port.start_ready)
        assert not ctx.get(dut.poller.active)

        ctx.set(dut.engine.busy, 1)
        await ctx.tick("usb")
        ctx.set(dut.arbiter.sof_start, 0)
        ctx.set(dut.engine.busy, 0)
        await ctx.delay(1e-9)

        assert ctx.get(dut.poller_port.start_ready)
        assert ctx.get(dut.engine.start)
        assert not ctx.get(dut.engine.sof)
        await ctx.tick("usb")
        assert ctx.get(dut.poller.active)
        assert not ctx.get(dut.engine.start)

    simulation.add_testbench(bench)
    simulation.run()


def test_host_owns_one_shared_engine_and_wires_public_interfaces() -> None:
    from hurra_cynthion import BoundedMouseHost as PublicBoundedMouseHost

    assert PublicBoundedMouseHost is BoundedMouseHost
    host = BoundedMouseHost(timing=HostTiming.simulation())
    assert host.control.transaction is host.control_port
    assert len(host.pollers) == MAX_ENDPOINTS
    # One port per poller, plus the interrupt-OUT writer on the last one: the
    # arbiter is generic in its port count, so the writer is just one more.
    assert len(host.poller_ports) == MAX_ENDPOINTS + 1
    for poller, port in zip(host.pollers, host.poller_ports[:MAX_ENDPOINTS], strict=True):
        assert poller.transaction is port
    assert isinstance(host.out_writer, InterruptOutWriter)
    assert host.out_writer.transaction is host.poller_ports[-1]
    assert not host.out_writer.add_transaction_submodule
    assert host.arbiter.poller_ports == host.poller_ports
    assert host.merge.sources == host.pollers
    assert host.enumerator.control is host.control
    assert host.enumerator.descriptor_store is host.descriptor_store
    assert host.lookup_type is host.descriptor_store.lookup_type
    assert host.lookup_request is host.descriptor_store.lookup_request
    assert host.lookup_ready is host.descriptor_store.lookup_ready
    assert host.lookup_response is host.descriptor_store.lookup_response
    assert host.lookup_data is host.descriptor_store.lookup_data

    netlist = rtlil.convert(
        host,
        ports=[
            host.connected,
            host.enumerated,
            host.error_code,
            host.report_valid,
            host.report_ready,
            host.report_data,
            host.report_interface,
            host.report_endpoint,
            host.lookup_type,
            host.lookup_request,
            host.lookup_ready,
            host.lookup_response,
            host.lookup_data,
        ],
    )
    assert len(re.findall(r"(?m)^\s*memory width 8 size 4096", netlist)) == 1
    # One 64-byte memory per poller report buffer, one in the shared
    # transaction engine's receive path, one for the control relay's bounce
    # buffer, and one holding the interrupt-OUT writer's pending packet. This
    # count is deliberately exact: a new 64-byte memory is real BRAM/LUT-RAM
    # in a design whose worst placer seed has only +1.58% timing margin, so
    # it should have to be justified here.
    assert len(re.findall(r"(?m)^\s*memory width 8 size 64", netlist)) == MAX_ENDPOINTS + 3
    assert netlist.count("transaction_engine") >= 1
    assert len(netlist) < 3_500_000


def test_host_drives_reset_then_normal_full_speed_phy_values() -> None:
    timing = HostTiming.simulation()
    utmi = UTMIInterface()
    dut = BoundedMouseHost(utmi=utmi, timing=timing)
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        ctx.set(utmi.vbus_valid, 1)
        ctx.set(utmi.line_state, 1)

        for _ in range(timing.vbus_discharge_cycles + timing.attach_stable_cycles + 8):
            await ctx.tick("usb")
            if ctx.get(dut.connected):
                break
        assert ctx.get(dut.connected)
        assert ctx.get(utmi.xcvr_select) == 0
        assert not ctx.get(utmi.term_select)
        assert ctx.get(utmi.op_mode) == 2
        assert ctx.get(utmi.dp_pulldown)
        assert ctx.get(utmi.dm_pulldown)
        assert not ctx.get(utmi.id_pullup)
        assert not ctx.get(utmi.suspend)

        for _ in range(timing.reset_cycles + 2):
            await ctx.tick("usb")
        assert ctx.get(utmi.xcvr_select) == 1
        assert ctx.get(utmi.term_select)
        assert ctx.get(utmi.op_mode) == 0

    simulation.add_testbench(bench)
    simulation.run()


def test_host_attaches_without_target_phy_vbus_valid() -> None:
    # The target PHY senses TARGET-C VBUS, while CONTROL powers TARGET-A.
    # Attachment therefore follows commanded port power and line state, not
    # the target PHY's vbus_valid signal.
    timing = HostTiming.simulation()
    utmi = UTMIInterface()
    dut = BoundedMouseHost(utmi=utmi, timing=timing)
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        ctx.set(utmi.vbus_valid, 0)
        ctx.set(utmi.line_state, 1)  # stable full-speed J idle

        for _ in range(timing.vbus_discharge_cycles + timing.attach_stable_cycles + 8):
            await ctx.tick("usb")
            if ctx.get(dut.connected):
                break
        assert ctx.get(dut.connected)
        assert ctx.get(dut.enumerating)
        assert ctx.get(dut.error_code) == HostError.NONE.value

    simulation.add_testbench(bench)
    simulation.run()


def test_disconnect_clears_polling_and_allows_a_new_attach() -> None:
    timing = HostTiming.simulation()
    utmi = UTMIInterface()
    dut = BoundedMouseHost(utmi=utmi, timing=timing)
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def reach_attached(ctx) -> None:
        # vbus_valid intentionally stays low in this board topology.
        ctx.set(utmi.line_state, 1)
        for _ in range(timing.vbus_discharge_cycles + timing.attach_stable_cycles + 8):
            await ctx.tick("usb")
            if ctx.get(dut.connected):
                return
        raise AssertionError("host did not attach")

    async def bench(ctx) -> None:
        await reach_attached(ctx)
        assert ctx.get(dut.enumerating)

        # A device cannot be declared removed while the host is driving reset.
        for _ in range(timing.reset_cycles + timing.reset_recovery_cycles):
            await ctx.tick("usb")

        # Removing the device leaves a sustained SE0 after its pull-up is gone.
        ctx.set(utmi.line_state, 0)
        for _ in range(timing.detach_stable_cycles + 1):
            await ctx.tick("usb")
        assert not ctx.get(dut.connected)
        assert not ctx.get(dut.pollers[0].enable)
        assert ctx.get(dut.error_code) == HostError.DISCONNECTED.value

        # A fresh stable J starts enumeration again.
        await reach_attached(ctx)
        assert ctx.get(dut.connected)
        assert ctx.get(dut.enumerating)
        assert not ctx.get(dut.enumerated)

    simulation.add_testbench(bench)
    simulation.run()


def test_cynthion_r1_4_top_requests_target_and_aux_phy_and_control_power_routes() -> None:
    platform = CynthionPlatformRev1D4()
    with warnings.catch_warnings():
        # The pinned Cynthion/LUNA stack still uses Amaranth 0.5's legacy
        # platform-request and sibling-domain APIs.
        warnings.simplefilter("ignore", DeprecationWarning)
        build_plan = platform.prepare(CynthionMouseHostTop())
    netlist = build_plan.files["top.il"]

    requested = {name for name, _number in platform._requested}
    assert "target_phy" in requested
    assert "aux_phy" in requested
    assert "aux_vbus_in_en" in requested
    assert "aux_vbus_en" in requested
    assert "target_a_discharge" in requested
    assert "control_vbus_in_en" in requested
    assert "target_c_vbus_en" in requested
    assert "control_vbus_en" in requested
    assert ("injection_link", 0) in platform._requested
    assert all(("led", index) in platform._requested for index in range(6))
    # CONTROL powers TARGET-A; AUX carries the PC-facing clone.
    assert "connect \\aux_vbus_in_en_0__o 1'0" in netlist
    assert "connect \\aux_vbus_en_0__o 1'0" in netlist
    assert "connect \\control_vbus_in_en_0__o 1'1" in netlist
    assert "connect \\target_c_vbus_en_0__o 1'0" in netlist
    # The host owns the sole descriptor memory; the clone serves it directly.
    assert len(re.findall(r"(?m)^\s*memory width 8 size 4096", netlist)) == 1
    assert "top.device.descriptor_store" not in netlist
    assert "top.device.copy_engine" not in netlist
    # Host enumeration must enable the clone's compatibility readiness gate.
    assert "connect \\copy_enable 1'0" not in netlist
    # The clone must not depend on AUX vbus_valid, which stays low here.
    top_module = netlist.split("\nend\n", 1)[0]
    assert "connect \\B \\vbus_valid" not in top_module
    # The clone's endpoint buffers raise this above the host-only count.
    assert len(re.findall(r"(?m)^\s*memory width 8 size 64", netlist)) >= MAX_ENDPOINTS + 1
    # The generic map/engine and fixed-slot link are intentionally present in
    # the production netlist. Keep a broad ceiling to catch accidental
    # duplication; real ECP5 fit/timing is enforced by the production build.
    assert len(netlist) < 75_000_000


# The documented PMOD-A wiring, keyed by subsignal: physical connector pin, ECP5
# ball on r1.4, and which end drives it. Mirrors docs/hardware/ch32-cynthion-wiring.md.
PMOD_A_WIRING = {
    "sck": ("1", "C9", "o"),
    "mosi": ("2", "B9", "o"),
    "miso": ("3", "D11", "i"),
    "cs_n": ("4", "C12", "o"),
    "mcu_ready": ("7", "C8", "i"),
    "usb_sync": ("8", "D8", "o"),
    "spare0": ("9", "D9", "i"),
    "spare1": ("10", "C10", "i"),
}


def _prepared_top() -> tuple[CynthionMouseHostTop, dict[str, str]]:
    top = CynthionMouseHostTop()
    platform = CynthionPlatformRev1D4()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        build_plan = platform.prepare(top)
    return top, build_plan.files


def test_injection_link_buffers_drive_only_fpga_outputs() -> None:
    # A shared ``oe`` cannot express per-pin direction: ``io.Buffer.Signature``
    # declares ``oe: Out(1)`` whatever the port width, so the previous flat
    # ``dir="io"`` request output-enabled all eight pads and shorted the MCU's
    # drivers on pins 3 and 7.
    top, files = _prepared_top()
    netlist = files["top.il"]

    assert set(top.pmod_a_buffers) == set(PMOD_A_WIRING)
    for name, (_pin, _ball, direction) in PMOD_A_WIRING.items():
        assert top.pmod_a_buffers[name].direction.value == direction, name
        # An input buffer has no ``o`` member at all, so a wrong-direction buffer
        # cannot be silently driven.
        assert ("o" in top.pmod_a_buffers[name].signature.members) == (direction == "o"), name

    # Catches any reintroduction of a shared direction control, including a "fix"
    # that only changes the mask value but keeps a single ``dir="io"`` request.
    assert "injection_link_0__oe" not in netlist
    assert "user_pmod_0__oe" not in netlist
    assert "user_pmod" not in netlist

    for name, (_pin, _ball, direction) in PMOD_A_WIRING.items():
        port = f"injection_link_0__{name}__io"
        kind = "output" if direction == "o" else "input"
        terminal = "O" if direction == "o" else "I"
        # The pad is declared in the direction the resource asked for...
        assert re.search(rf"(?m)^\s*wire width 1 {kind}\s+\d+\s+\\{port}$", netlist), name
        # ...and buffered exactly once, by a buffer of that same direction.
        assert netlist.count(f"connect \\{terminal} \\{port} [0]") == 1, name
        # Only FPGA-driven pins get an output connection; MCU-driven pins and the
        # two unconnected spares must not be driven at all.
        assert (f"connect \\O \\{port} [0]" in netlist) == (direction == "o"), name
        assert (f"connect \\I \\{port} [0]" in netlist) == (direction == "i"), name


def test_injection_link_inputs_all_carry_a_pull_down() -> None:
    # The toolchain emits ``PULLMODE="NONE"`` by default, so an unpulled input
    # floats. A floating MCU_READY that reads high silently discards every
    # transmitted frame and the descriptor export never retriggers.
    _top, files = _prepared_top()
    lpf = files["top.lpf"]

    def iobuf_line(name: str) -> str:
        marker = f'"injection_link_0__{name}__io"'
        return next(line for line in lpf.splitlines() if marker in line and "IOBUF" in line)

    # Derived from the resource, so adding an input without a pull fails here.
    directions = injection_link_directions()
    inputs = [name for name, direction in directions.items() if direction == "i"]
    assert inputs
    for name, direction in directions.items():
        if direction == "i":
            assert "PULLMODE=DOWN" in iobuf_line(name), name
        else:
            assert "PULLMODE" not in iobuf_line(name), name

    assert lpf.count("PULLMODE") == len(inputs)
    assert lpf.count("PULLMODE=DOWN") == len(inputs)


def test_injection_link_pins_match_documented_physical_pins() -> None:
    # Guards a re-pin: pins are named by physical connector number, and the balls
    # they resolve to are revision-dependent.
    platform = CynthionPlatformRev1D4()
    mapping = platform.connectors[("pmod", 0)].mapping

    declared = {sub.name: sub for sub in _INJECTION_LINK_PMOD_A.ios}
    assert set(declared) == set(PMOD_A_WIRING)
    for name, (pin, ball, direction) in PMOD_A_WIRING.items():
        pins = declared[name].ios[0]
        assert pins.dir == direction, name
        assert pins.names == [f"pmod_0:{pin}"], name
        assert mapping[pin] == ball, name


def test_top_level_instantiates_aux_device_and_powers_from_control() -> None:
    top = CynthionMouseHostTop()
    platform = CynthionPlatformRev1D4()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        fragment = top.elaborate(platform)
    assert hasattr(top, "device")
    assert top.device.store is top.host.descriptor_store
    assert not hasattr(top.device, "copy_engine")
    assert hasattr(top, "injection_plane")
    assert hasattr(top, "spi_link")
    assert ("injection_link", 0) in platform._requested
    assert fragment is not None


class ArbiterHarness(Elaboratable):
    def __init__(self, poller_count: int = 2) -> None:
        self.engine = ScriptedEngine()
        self.control_port = USBHostTransactionPort()
        self.poller_ports = [USBHostTransactionPort() for _ in range(poller_count)]
        self.arbiter = USBHostTransactionArbiter(
            engine=self.engine,
            control_port=self.control_port,
            poller_ports=self.poller_ports,
        )

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        m.submodules.engine = self.engine
        m.submodules.arbiter = self.arbiter
        return m


def test_arbiter_holds_pending_sof_through_transaction_completion() -> None:
    dut = ArbiterHarness(poller_count=1)
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        ctx.set(dut.arbiter.connected, 1)
        ctx.set(dut.arbiter.control_phase, 1)
        ctx.set(dut.arbiter.sof_start, 1)
        ctx.set(dut.engine.done, 1)
        await ctx.delay(1e-9)

        # The current owner must consume completion before a pending SOF can
        # acquire the engine.
        assert not ctx.get(dut.arbiter.sof_ready)
        assert not ctx.get(dut.engine.start)

        await ctx.tick("usb")
        ctx.set(dut.engine.done, 0)
        await ctx.delay(1e-9)

        # Keep one additional cycle clear for the control engine to publish
        # its own completion and for the enumerator to enter BUS_RESET.
        assert not ctx.get(dut.arbiter.sof_ready)
        assert not ctx.get(dut.engine.start)

        await ctx.tick("usb")
        await ctx.delay(1e-9)
        assert ctx.get(dut.arbiter.sof_ready)
        assert ctx.get(dut.engine.start)
        assert ctx.get(dut.engine.sof)

    simulation.add_testbench(bench)
    simulation.run()


def test_arbiter_round_robins_pollers_and_prioritizes_control() -> None:
    dut = ArbiterHarness(poller_count=2)
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")
    port0, port1 = dut.poller_ports

    async def bench(ctx) -> None:
        ctx.set(dut.arbiter.connected, 1)
        ctx.set(port0.start, 1)
        ctx.set(port1.start, 1)
        await ctx.delay(1e-9)

        # rotate starts at poller 0: it alone is offered the bus; poller 1 waits.
        assert ctx.get(dut.engine.start)
        assert ctx.get(port0.start_ready)
        assert not ctx.get(port1.start_ready)
        assert not ctx.get(port0.busy)
        assert ctx.get(port1.busy)

        # Latch poller 0 as owner (grant cycle has engine idle), then run its
        # transaction: engine goes busy, no second grant may start meanwhile.
        await ctx.tick("usb")
        ctx.set(dut.engine.busy, 1)
        await ctx.delay(1e-9)
        assert not ctx.get(dut.engine.start)
        assert ctx.get(port0.busy)
        assert ctx.get(port1.busy)

        # Completion routes done only to the owner and advances the pointer.
        ctx.set(dut.engine.done, 1)
        await ctx.delay(1e-9)
        assert ctx.get(port0.done)
        assert not ctx.get(port1.done)
        await ctx.tick("usb")
        ctx.set(dut.engine.done, 0)
        ctx.set(dut.engine.busy, 0)
        await ctx.delay(1e-9)

        # Now poller 1 has priority.
        assert ctx.get(port1.start_ready)
        assert not ctx.get(port0.start_ready)
        assert ctx.get(dut.engine.start)

        # Control preempts polling entirely.
        ctx.set(dut.arbiter.control_phase, 1)
        ctx.set(dut.control_port.start, 1)
        await ctx.delay(1e-9)
        assert not ctx.get(port0.start_ready)
        assert not ctx.get(port1.start_ready)
        assert ctx.get(dut.engine.start)

    simulation.add_testbench(bench)
    simulation.run()


def test_arbiter_blocks_new_grants_while_a_poller_owns_the_receive_buffer() -> None:
    dut = ArbiterHarness(poller_count=2)
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")
    port0, port1 = dut.poller_ports

    async def bench(ctx) -> None:
        ctx.set(dut.arbiter.connected, 1)
        # A poller is still draining its report (active) but the engine is idle:
        # no other poller may start and clobber the shared receive buffer.
        ctx.set(dut.arbiter.poller_busy, 1)
        ctx.set(port0.start, 1)
        ctx.set(port1.start, 1)
        await ctx.delay(1e-9)
        assert not ctx.get(dut.engine.start)
        assert not ctx.get(port0.start_ready)
        assert not ctx.get(port1.start_ready)

        ctx.set(dut.arbiter.poller_busy, 0)
        await ctx.delay(1e-9)
        assert ctx.get(dut.engine.start)

    simulation.add_testbench(bench)
    simulation.run()


def test_host_instantiates_one_poller_per_endpoint_and_or_reduces_status() -> None:
    host = BoundedMouseHost(timing=HostTiming.simulation())
    assert len(host.pollers) == MAX_ENDPOINTS
    assert len({id(poller) for poller in host.pollers}) == MAX_ENDPOINTS
    assert len({id(port) for port in host.poller_ports}) == MAX_ENDPOINTS + 1
    # Aggregated status is a fresh OR reduction, not aliased to a single poller.
    assert host.polling_failed is not host.pollers[0].failed
    assert host.polling_active is not host.pollers[0].active


class _StubReportSource:
    def __init__(self, name: str) -> None:
        self.report_valid = Signal(name=f"{name}_valid")
        self.report_data = Signal(8, name=f"{name}_data")
        self.report_first = Signal(name=f"{name}_first")
        self.report_last = Signal(name=f"{name}_last")
        self.report_ready = Signal(name=f"{name}_ready")


def test_report_merge_streams_one_tagged_report_at_a_time() -> None:
    sources = [_StubReportSource("s0"), _StubReportSource("s1")]
    dut = ReportMergeMux(
        sources,
        interfaces=[Const(0, 8), Const(1, 8)],
        endpoints=[Const(1, 4), Const(2, 4)],
    )
    src0, src1 = sources
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        ctx.set(dut.report_ready, 1)
        # Both sources have a report ready; source 0 is two bytes, source 1 one.
        ctx.set(src1.report_valid, 1)
        ctx.set(src1.report_first, 1)
        ctx.set(src1.report_last, 1)
        ctx.set(src1.report_data, 0x55)
        ctx.set(src0.report_valid, 1)
        ctx.set(src0.report_first, 1)
        ctx.set(src0.report_last, 0)
        ctx.set(src0.report_data, 0xAA)
        await ctx.delay(1e-9)
        assert not ctx.get(dut.report_valid)  # not locked yet

        await ctx.tick("usb")  # lock onto source 0
        await ctx.delay(1e-9)
        assert ctx.get(dut.report_valid)
        assert ctx.get(dut.report_first)
        assert not ctx.get(dut.report_last)
        assert ctx.get(dut.report_data) == 0xAA
        assert ctx.get(dut.report_interface) == 0
        assert ctx.get(dut.report_endpoint) == 1
        assert ctx.get(src0.report_ready)
        assert not ctx.get(src1.report_ready)

        # The source advances on the clock edge after report_ready; its final
        # byte only becomes visible at the next cycle (still locked here).
        await ctx.tick("usb")
        ctx.set(src0.report_first, 0)
        ctx.set(src0.report_last, 1)
        ctx.set(src0.report_data, 0xBB)
        await ctx.delay(1e-9)
        assert ctx.get(dut.report_valid)
        assert ctx.get(dut.report_last)
        assert ctx.get(dut.report_data) == 0xBB

        # Streaming the last byte unlocks; source 0 then drops valid.
        await ctx.tick("usb")  # unlock, advance selector to source 1
        ctx.set(src0.report_valid, 0)
        ctx.set(src0.report_last, 0)
        await ctx.delay(1e-9)
        assert not ctx.get(dut.report_valid)

        await ctx.tick("usb")  # lock onto source 1
        await ctx.delay(1e-9)
        assert ctx.get(dut.report_valid)
        assert ctx.get(dut.report_first)
        assert ctx.get(dut.report_last)
        assert ctx.get(dut.report_data) == 0x55
        assert ctx.get(dut.report_interface) == 1
        assert ctx.get(dut.report_endpoint) == 2
        assert ctx.get(src1.report_ready)
        assert not ctx.get(src0.report_ready)

    simulation.add_testbench(bench)
    simulation.run()


def test_report_merge_releases_lock_when_source_drops_mid_report() -> None:
    # A disconnect/failure clears a poller's buffer without a last byte; the
    # merge must not stay locked on the dead source and wedge the pipeline.
    sources = [_StubReportSource("s0"), _StubReportSource("s1")]
    dut = ReportMergeMux(
        sources,
        interfaces=[Const(0, 8), Const(1, 8)],
        endpoints=[Const(1, 4), Const(2, 4)],
    )
    src0, src1 = sources
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        ctx.set(dut.report_ready, 1)
        # Lock onto source 0 partway through a multi-byte report.
        ctx.set(src0.report_valid, 1)
        ctx.set(src0.report_first, 1)
        ctx.set(src0.report_last, 0)
        ctx.set(src0.report_data, 0xAA)
        await ctx.tick("usb")  # lock onto source 0
        await ctx.delay(1e-9)
        assert ctx.get(dut.report_valid)
        assert ctx.get(dut.report_interface) == 0

        # Source 0 disconnects: buffer cleared, valid drops, no last byte seen.
        ctx.set(src0.report_valid, 0)
        ctx.set(src0.report_first, 0)
        await ctx.delay(1e-9)
        assert not ctx.get(dut.report_valid)

        await ctx.tick("usb")  # ~valid[sel] releases the lock
        await ctx.delay(1e-9)

        # The merge must be free to serve a different source, not wedged on 0.
        ctx.set(src1.report_valid, 1)
        ctx.set(src1.report_first, 1)
        ctx.set(src1.report_last, 1)
        ctx.set(src1.report_data, 0x55)
        await ctx.delay(1e-9)
        assert not ctx.get(dut.report_valid)

        await ctx.tick("usb")  # lock onto source 1
        await ctx.delay(1e-9)
        assert ctx.get(dut.report_valid)
        assert ctx.get(dut.report_data) == 0x55
        assert ctx.get(dut.report_interface) == 1
        assert ctx.get(src1.report_ready)

    simulation.add_testbench(bench)
    simulation.run()


def test_main_applies_the_build_environment(monkeypatch):
    """`main()` must install the placer timing weight before LUNA builds.

    Regression guard for the timing-closure plan: the option must come from
    the single choke point every bitstream-producing invocation passes
    through, not from a human remembering an environment variable.
    """
    import sys

    from hurra_cynthion import gateware
    from hurra_cynthion.build_env import NEXTPNR_OPTS_VAR

    seen: dict[str, str | None] = {}

    def recorder(_top):
        seen["opts"] = os.environ.get(NEXTPNR_OPTS_VAR)

    luna_module = types.ModuleType("luna")
    luna_module.top_level_cli = recorder
    monkeypatch.setitem(sys.modules, "luna", luna_module)
    monkeypatch.setattr(sys, "argv", ["hurra_cynthion.gateware"])
    monkeypatch.delenv(NEXTPNR_OPTS_VAR, raising=False)

    gateware.main()

    assert seen["opts"] is not None, "main() left AMARANTH_nextpnr_opts unset"
    assert "--placer-heap-timingweight" in seen["opts"]


def test_relay_engine_ownership_gates_the_arbiter_control_phase() -> None:
    """A relay forward must win the bus; pollers resume when it clears.

    ``control_phase`` gates ``poll_bus_free`` in the arbiter, so raising it is
    what preempts the pollers -- and dropping it is what lets the report
    stream resume. A relay that never lowered ``request_pending`` would
    silently stall every endpoint forever.

    ``request_pending`` is driven by the relay's own FSM, so this asserts the
    wiring structurally; the dynamic behaviour is covered end to end in
    tests/test_control_relay_e2e.py.
    """
    host = BoundedMouseHost(timing=HostTiming.simulation())
    assert host.control_relay is not None

    netlist = rtlil.convert(host, ports=[host.connected, host.enumerated])
    # The arbiter's control phase must be a function of engine_owned, not of
    # enumerator.ready alone -- and not of request_pending, which also covers
    # time spent waiting on the AUX host (see ControlRelay.engine_owned).
    assert "engine_owned" in netlist


class _ArbiterHarness(Elaboratable):
    """A bare arbiter over a scripted engine, one control and one poller port."""

    def __init__(self) -> None:
        self.engine = ScriptedEngine()
        self.control_port = USBHostTransactionPort()
        self.poller_port = USBHostTransactionPort()
        self.arbiter = USBHostTransactionArbiter(
            engine=self.engine,
            control_port=self.control_port,
            poller_ports=[self.poller_port],
        )

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        m.submodules.engine = self.engine
        m.submodules.arbiter = self.arbiter
        return m


def test_a_forwarded_control_transfer_waits_for_the_poller_to_finish_copying() -> None:
    """Review: control transfers now happen AFTER enumeration.

    The arbiter was written for control only ever preceding polling. Once
    the relay forwards a control request, control_phase can rise while a
    poller has finished its IN transaction but is still copying the report
    out of the shared receive buffer (poller_busy). Two things went wrong:

    - control_grant ignored poller_busy, so the control transaction started
      at once and its received data overwrote the buffer mid-copy;
    - engine.rx_read_index followed control_phase, so the copying poller
      read the buffer with the CONTROL port's index.

    Either way the poller copied garbage into a report sent to the PC.
    """
    dut = _ArbiterHarness()
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        arbiter = dut.arbiter
        ctx.set(arbiter.connected, 1)
        ctx.set(dut.poller_port.rx_read_index, 7)
        ctx.set(dut.control_port.rx_read_index, 33)

        # A poller is mid-copy; the relay now wants the engine.
        ctx.set(arbiter.poller_busy, 1)
        ctx.set(arbiter.control_phase, 1)
        ctx.set(dut.control_port.start, 1)
        for _ in range(5):
            await ctx.delay(1e-9)
            assert not ctx.get(dut.engine.start), "control granted while a poller owns the buffer"
            assert not ctx.get(dut.control_port.start_ready)
            assert (
                ctx.get(dut.engine.rx_read_index) == 7
            ), "the copying poller must keep reading with its own index"
            await ctx.tick("usb")

        # Copy done: now, and only now, control takes the engine.
        ctx.set(arbiter.poller_busy, 0)
        await ctx.delay(1e-9)
        assert ctx.get(dut.control_port.start_ready)
        assert ctx.get(dut.engine.start)
        assert ctx.get(dut.engine.rx_read_index) == 33

    simulation.add_testbench(bench)
    simulation.run()


class _RelayOverArbiterHarness(Elaboratable):
    """ControlRelay -> real USBControlTransferEngine -> real arbiter -> scripted engine.

    Wired as host.py wires them, minus the enumerator's passthrough (which
    only forwards the same signals once ``ready``).
    """

    def __init__(self) -> None:
        self.engine = ScriptedEngine()
        self.control_port = USBHostTransactionPort()
        self.poller_port = USBHostTransactionPort()
        self.arbiter = USBHostTransactionArbiter(
            engine=self.engine,
            control_port=self.control_port,
            poller_ports=[self.poller_port],
        )
        self.control = USBControlTransferEngine(
            transaction=self.control_port,
            timing=HostTiming.simulation(),
            add_transaction_submodule=False,
        )
        self.relay = ControlRelay()

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        m.submodules.engine = self.engine
        m.submodules.arbiter = self.arbiter
        m.submodules.control = self.control
        m.submodules.relay = relay = self.relay
        control = self.control
        m.d.comb += [
            self.arbiter.connected.eq(1),
            self.arbiter.control_phase.eq(relay.engine_owned),
            relay.enable.eq(1),
            control.connected.eq(1),
            control.address.eq(5),
            control.max_packet_size.eq(8),
            control.start.eq(relay.ctl_start),
            control.request_type.eq(relay.ctl_request_type),
            control.request.eq(relay.ctl_request),
            control.value.eq(relay.ctl_value),
            control.index.eq(relay.ctl_index),
            control.length.eq(relay.ctl_length),
            control.out_payload.eq(relay.ctl_out_payload),
            control.data_ready.eq(relay.ctl_data_ready),
            relay.ctl_busy.eq(control.busy),
            relay.ctl_done.eq(control.done),
            relay.ctl_status.eq(control.status),
            relay.ctl_transferred.eq(control.transferred),
            relay.ctl_data.eq(control.data),
            relay.ctl_data_valid.eq(control.data_valid),
            relay.ctl_data_first.eq(control.data_first),
            relay.ctl_data_last.eq(control.data_last),
            relay.ctl_out_index.eq(control.out_index),
        ]
        return m


def test_a_relay_start_while_a_poller_is_busy_completes_once_the_poller_releases() -> None:
    """PR review: does the relay hang if the arbiter withholds its start?

    The concern was that ControlRelay leaves ISSUE on ``~ctl_busy`` without
    the arbiter's grant, so a start suppressed by ``poller_busy`` would leave
    it in AWAIT forever with ``engine_owned`` -- and so ``control_phase`` --
    held high.

    It does not, because the two handshakes are at different levels. The
    relay's ``ctl_start`` goes to USBControlTransferEngine, which accepts it
    in IDLE unconditionally. The arbiter's ``start_ready`` gates the
    *transactions* that engine then issues, and it holds each one in its
    ISSUE_* state until granted. This drives a poller-busy window across the
    relay's start and checks the transfer still completes.
    """
    dut = _RelayOverArbiterHarness()
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def responder(ctx) -> None:
        # Answer every transaction the arbiter lets through with SUCCESS.
        async for _, _, start in ctx.tick("usb").sample(dut.engine.start):
            if start:
                ctx.set(dut.engine.done, 1)
                ctx.set(dut.engine.status, TransactionStatus.SUCCESS.value)
            else:
                ctx.set(dut.engine.done, 0)

    async def bench(ctx) -> None:
        relay = dut.relay
        ctx.set(dut.arbiter.poller_busy, 1)
        # SET_IDLE-shaped: class OUT to the interface, no data stage.
        ctx.set(relay.request_type, 0x21)
        ctx.set(relay.request, 0x0A)
        ctx.set(relay.length, 0)
        ctx.set(relay.request_valid, 1)
        await ctx.tick("usb")
        ctx.set(relay.request_valid, 0)

        saw_control_start = False
        for _ in range(40):
            await ctx.tick("usb")
            saw_control_start |= bool(ctx.get(dut.control.busy))
            assert not ctx.get(dut.engine.start), "granted while a poller owns the buffer"
            assert not ctx.get(relay.response_valid)
        assert saw_control_start, "the control engine must have taken the relay's start"
        assert ctx.get(relay.engine_owned)

        ctx.set(dut.arbiter.poller_busy, 0)
        grants = []
        for _ in range(40):
            await ctx.delay(1e-9)
            if ctx.get(dut.engine.start):
                grants.append(ctx.get(dut.engine.token_pid))
            if ctx.get(relay.response_valid):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("relay never completed after the poller released")
        assert not ctx.get(relay.response_error)
        assert grants == [0b1101, 0b1001], f"expected SETUP then status IN, got {grants}"
        ctx.set(relay.response_ack, 1)
        await ctx.tick("usb")
        ctx.set(relay.response_ack, 0)
        await ctx.tick("usb")
        assert not ctx.get(relay.engine_owned), "control_phase must be released"

    simulation.add_process(responder)
    simulation.add_testbench(bench)
    simulation.run()


# --- Interrupt-OUT writer on the shared engine --------------------------------


class _WriterArbiterHarness(Elaboratable):
    """A real poller and the real OUT writer sharing the real arbiter.

    ``poller_busy`` is wired as host.py wires it: the pollers' ``active``
    OR the writer's. The engine is scripted by the test.
    """

    def __init__(self) -> None:
        timing = HostTiming.simulation()
        self.engine = ScriptedEngine()
        self.control_port = USBHostTransactionPort()
        self.poller_port = USBHostTransactionPort()
        self.writer_port = USBHostTransactionPort()
        self.poller = InterruptInPoller(
            transaction=self.poller_port, timing=timing, add_transaction_submodule=False
        )
        self.writer = InterruptOutWriter(
            transaction=self.writer_port, timing=timing, add_transaction_submodule=False
        )
        self.arbiter = USBHostTransactionArbiter(
            engine=self.engine,
            control_port=self.control_port,
            poller_ports=[self.poller_port, self.writer_port],
        )
        self.sof_tick = Signal()

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        m.submodules.engine = self.engine
        m.submodules.poller = self.poller
        m.submodules.writer = self.writer
        m.submodules.arbiter = self.arbiter
        for endpoint, owner in ((1, self.poller), (2, self.writer)):
            m.d.comb += [
                owner.enable.eq(1),
                owner.connected.eq(1),
                owner.sof_tick.eq(self.sof_tick),
                owner.address.eq(5),
                owner.endpoint.eq(endpoint),
                owner.max_packet_size.eq(8),
                owner.interval.eq(1),
            ]
        m.d.comb += [
            self.arbiter.control_phase.eq(0),
            self.arbiter.connected.eq(1),
            self.arbiter.poller_busy.eq(self.poller.active | self.writer.active),
        ]
        return m


def test_writer_and_poller_alternate_on_the_shared_engine_and_neither_starves() -> None:
    dut = _WriterArbiterHarness()
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def ticker(ctx) -> None:
        while True:
            ctx.set(dut.sof_tick, 1)
            await ctx.tick("usb")
            ctx.set(dut.sof_tick, 0)
            await ctx.tick("usb")

    async def feeder(ctx) -> None:
        # The clone side always has another two-byte packet waiting.
        sent = 0
        while True:
            ctx.set(dut.writer.out_valid, 1)
            ctx.set(dut.writer.out_data, sent & 0xFF)
            ctx.set(dut.writer.out_last, sent & 1)
            ready = ctx.get(dut.writer.out_ready)
            await ctx.tick("usb")
            sent += ready

    async def bench(ctx) -> None:
        grants = []
        payloads = []
        for _ in range(2000):
            if len(grants) == 16:
                break
            if not ctx.get(dut.engine.start):
                await ctx.tick("usb")
                continue
            pid = ctx.get(dut.engine.token_pid)
            grants.append(pid)
            if pid == OUT_PID:
                assert ctx.get(dut.engine.endpoint) == 2
                length = ctx.get(dut.engine.tx_length)
                data = []
                for index in range(length):
                    ctx.set(dut.engine.tx_index, index)
                    data.append(ctx.get(dut.engine.tx_payload))
                payloads.append(bytes(data))
            else:
                assert ctx.get(dut.engine.endpoint) == 1
            await ctx.tick("usb")
            ctx.set(dut.engine.busy, 1)
            for _ in range(3):
                await ctx.tick("usb")
                assert not ctx.get(dut.engine.start)
            ctx.set(dut.engine.busy, 0)
            ctx.set(dut.engine.done, 1)
            # NAK the IN polls so the poller never holds the receive buffer.
            status = TransactionStatus.SUCCESS if pid == OUT_PID else TransactionStatus.NAK
            ctx.set(dut.engine.status, status.value)
            await ctx.tick("usb")
            ctx.set(dut.engine.done, 0)
        else:
            raise AssertionError(f"only {len(grants)} grants: {grants}")

        # Both always due: the round robin alternates strictly.
        assert grants == [IN_PID, OUT_PID] * 8, grants
        assert payloads == [bytes([2 * k, 2 * k + 1]) for k in range(8)]

    simulation.add_testbench(ticker, background=True)
    simulation.add_testbench(feeder, background=True)
    simulation.add_testbench(bench)
    simulation.run()


class _RelayAndWriterOverArbiterHarness(_RelayOverArbiterHarness):
    """The relay harness above with the real OUT writer on its poller port."""

    def __init__(self) -> None:
        super().__init__()
        self.writer = InterruptOutWriter(
            transaction=self.poller_port,
            timing=HostTiming.simulation(),
            add_transaction_submodule=False,
        )

    def elaborate(self, platform) -> Module:
        m = super().elaborate(platform)
        m.submodules.writer = writer = self.writer
        m.d.comb += [
            writer.enable.eq(1),
            writer.connected.eq(1),
            writer.address.eq(5),
            writer.endpoint.eq(2),
            writer.max_packet_size.eq(8),
            writer.interval.eq(1),
            # As host.py wires it: the writer holds the engine like a poller.
            self.arbiter.poller_busy.eq(writer.active),
        ]
        return m


def test_a_relay_start_while_the_writer_is_mid_out_completes_after_it_intact() -> None:
    """The engine reads ``tx_payload`` live through SEND_DATA.

    ``control_owns`` switches the engine's tx_payload/tx_length mux
    combinationally, so a relay transfer allowed to take ownership while an OUT
    is on the wire would splice the SETUP's bytes into the OUT packet.
    """
    dut = _RelayAndWriterOverArbiterHarness()
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")
    payload = bytes([0x05, 0xFF, 0x04, 0x00, 0x11, 0x22, 0x33, 0x44])
    respond = {"on": False}

    async def responder(ctx) -> None:
        async for _, _, start in ctx.tick("usb").sample(dut.engine.start):
            if not respond["on"]:
                continue
            ctx.set(dut.engine.done, start)
            ctx.set(dut.engine.status, TransactionStatus.SUCCESS.value)

    async def bench(ctx) -> None:
        writer = dut.writer
        relay = dut.relay
        for index, byte in enumerate(payload):
            ctx.set(writer.out_valid, 1)
            ctx.set(writer.out_data, byte)
            ctx.set(writer.out_last, index == len(payload) - 1)
            assert ctx.get(writer.out_ready)
            await ctx.tick("usb")
        ctx.set(writer.out_valid, 0)
        ctx.set(writer.sof_tick, 1)
        await ctx.tick("usb")
        ctx.set(writer.sof_tick, 0)
        assert ctx.get(dut.engine.start)
        assert ctx.get(dut.engine.token_pid) == OUT_PID
        await ctx.tick("usb")
        ctx.set(dut.engine.busy, 1)
        assert ctx.get(writer.active)

        # The relay asks for the engine while the OUT is on the wire.
        ctx.set(relay.request_type, 0x21)
        ctx.set(relay.request, 0x0A)
        ctx.set(relay.length, 0)
        ctx.set(relay.request_valid, 1)
        await ctx.tick("usb")
        ctx.set(relay.request_valid, 0)
        for _ in range(4):
            await ctx.tick("usb")
        assert ctx.get(relay.engine_owned)

        sent = []
        for index in range(len(payload)):
            ctx.set(dut.engine.tx_index, index)
            await ctx.delay(1e-9)
            assert not ctx.get(dut.engine.start), "control granted mid-OUT"
            assert ctx.get(dut.engine.tx_length) == len(payload)
            assert ctx.get(dut.engine.token_pid) == OUT_PID
            sent.append(ctx.get(dut.engine.tx_payload))
            await ctx.tick("usb")
        assert bytes(sent) == payload, "the OUT packet was corrupted mid-flight"
        assert not ctx.get(relay.response_valid)

        respond["on"] = True
        ctx.set(dut.engine.busy, 0)
        ctx.set(dut.engine.done, 1)
        ctx.set(dut.engine.status, TransactionStatus.SUCCESS.value)
        await ctx.tick("usb")
        ctx.set(dut.engine.done, 0)

        grants = []
        for _ in range(60):
            await ctx.delay(1e-9)
            if ctx.get(dut.engine.start):
                grants.append(ctx.get(dut.engine.token_pid))
            if ctx.get(relay.response_valid):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("relay never completed after the OUT")
        assert not ctx.get(relay.response_error)
        assert grants == [0b1101, 0b1001], f"expected SETUP then status IN, got {grants}"

    simulation.add_process(responder)
    simulation.add_testbench(bench)
    simulation.run()


# Full host against a raw-UTMI device that declares an interrupt-OUT endpoint.

_OUT_TEST_REPORT_DESCRIPTOR = bytes([0x06, 0x00, 0xFF, 0x09, 0x01])


def _out_target() -> OutRelayTarget:
    return OutRelayTarget(
        configuration=hid_configuration(
            in_address=0x81,
            out_address=0x02,
            in_mps=8,
            out_mps=8,
            interval=1,
            report_length=len(_OUT_TEST_REPORT_DESCRIPTOR),
        ),
        report_descriptor=_OUT_TEST_REPORT_DESCRIPTOR,
        interface=0,
        in_endpoint=1,
        out_endpoint=2,
    )


def _simulate_host_against(target: OutRelayTarget, bench) -> None:
    timing = HostTiming.simulation()
    host = BoundedMouseHost(timing=timing)
    simulation = Simulator(host)
    simulation.add_clock(1e-6, domain="usb")

    async def device(ctx) -> None:
        await serve(ctx, host, target)

    async def main(ctx) -> None:
        ctx.set(host.utmi.tx_ready, 1)
        ctx.set(host.report_ready, 1)
        await assert_power_attach_and_reset(ctx, host, timing)
        await wait_until(
            ctx,
            lambda: bool(ctx.get(host.enumerated) and ctx.get(host.out_writer.enable)),
            limit=20_000,
            what="enumeration with the OUT writer enabled",
        )
        assert ctx.get(host.out_present)
        assert ctx.get(host.out_number) == 2
        await bench(ctx, host)

    simulation.add_testbench(device, background=True)
    simulation.add_testbench(main)
    simulation.run()


async def _host_send_out(ctx, host, payload: bytes) -> None:
    """Offer one packet on the host's OUT sink, as the clone's endpoint stream would."""
    for index, byte in enumerate(payload):
        ctx.set(host.out_valid, 1)
        ctx.set(host.out_data, byte)
        ctx.set(host.out_last, index == len(payload) - 1)
        await wait_until(ctx, lambda: bool(ctx.get(host.out_ready)), limit=5_000, what="out_ready")
        await ctx.tick("usb")
    ctx.set(host.out_valid, 0)
    ctx.set(host.out_last, 0)


async def _quiesce(ctx, host) -> None:
    """Wait for a cycle with no transaction in flight, so counters and model agree."""
    engine = host.transaction
    await wait_until(
        ctx,
        lambda: not (
            ctx.get(engine.busy)
            or ctx.get(engine.start)
            or ctx.get(engine.done)
            or ctx.get(host.utmi.tx_valid)
            or ctx.get(host.utmi.rx_active)
        ),
        limit=500,
        what="an idle engine",
    )


def test_host_writes_out_packets_between_in_polls_and_counts_only_in_polls() -> None:
    target = _out_target()
    # The device NAKs the first write: a NAKed OUT must not read as a NAKed poll.
    target.out_script.append(NAK)
    payloads = [bytes([0x10 + k, 0x20 + k, 0x30 + k]) for k in range(3)]

    async def bench(ctx, host) -> None:
        await _quiesce(ctx, host)
        polls, naks, in_polls = (
            ctx.get(host.polls_issued),
            ctx.get(host.poll_naks),
            target.in_polls,
        )
        for payload in payloads:
            await _host_send_out(ctx, host, payload)
        await wait_until(
            ctx,
            lambda: sum(p.handshake == ACK for p in target.out_packets) == len(payloads),
            limit=20_000,
            what="every OUT packet to be ACKed",
        )
        await _quiesce(ctx, host)

        polled = target.in_polls - in_polls
        # Interval 1 on both: the round robin gives the IN endpoint a poll
        # for every write, so the OUTs cannot have starved it.
        assert polled >= len(payloads), "the IN endpoint must keep being polled between OUTs"
        assert ctx.get(host.polls_issued) - polls == polled
        assert ctx.get(host.poll_naks) - naks == polled

        assert target.out_packets[0] == OutPacket(1, 2, DATA0, payloads[0], NAK)
        acked = [p for p in target.out_packets if p.handshake == ACK]
        assert [p.payload for p in acked] == payloads
        assert [p.pid for p in acked] == [DATA0, DATA1, DATA0]
        assert {(p.address, p.endpoint) for p in target.out_packets} == {(1, 2)}
        assert ctx.get(host.enumerated)
        assert ctx.get(host.error_code) == HostError.NONE

    _simulate_host_against(target, bench)


def test_a_stalled_out_endpoint_never_touches_enumerated_or_error_code() -> None:
    """A STALL on a rumble or LED endpoint must never be able to stop input."""
    target = _out_target()
    target.out_script.extend([STALL, STALL, STALL])

    async def bench(ctx, host) -> None:
        def healthy() -> bool:
            assert ctx.get(host.enumerated)
            assert ctx.get(host.error_code) == HostError.NONE
            assert not ctx.get(host.polling_failed)
            assert not ctx.get(host.device_unresponsive)
            return True

        in_polls = target.in_polls
        for k in range(3):
            await _host_send_out(ctx, host, bytes([k]))
            await wait_until(
                ctx,
                lambda k=k: healthy() and len(target.out_packets) == k + 1,
                limit=20_000,
                what=f"stalled write {k}",
            )
        for _ in range(200):
            healthy()
            await ctx.tick("usb")
        assert [p.handshake for p in target.out_packets] == [STALL] * 3

        # The writer still works, and so does the IN path.
        await _host_send_out(ctx, host, b"\x42")
        await wait_until(
            ctx,
            lambda: healthy() and len(target.out_packets) == 4,
            limit=20_000,
            what="the write after the stalls",
        )
        assert target.out_packets[-1].payload == b"\x42"
        assert target.out_packets[-1].handshake == ACK
        assert target.in_polls > in_polls

    _simulate_host_against(target, bench)


def test_a_forwarded_control_transfer_cannot_take_the_host_engine_mid_out() -> None:
    """host.py must hold the arbiter's poller_busy for the writer's whole OUT.

    Proved on the wire of the real host rather than through a harness that
    wires ``poller_busy`` itself: a relay start timed to land while the OUT is
    being transmitted must neither corrupt it nor overtake it.
    """
    target = _out_target()
    payload = bytes([0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7])

    async def bench(ctx, host) -> None:
        relay = host.control_relay
        await _host_send_out(ctx, host, payload)
        await wait_until(
            ctx, lambda: bool(ctx.get(host.out_writer.active)), limit=5_000, what="the OUT grant"
        )
        assert ctx.get(host.arbiter.poller_busy)
        assert not ctx.get(host.polling_active), "polling_active must stay pollers-only"
        ctx.set(relay.request_type, 0x21)
        ctx.set(relay.request, 0x0A)
        ctx.set(relay.value, 0x0100)
        ctx.set(relay.index, 0)
        ctx.set(relay.length, 0)
        ctx.set(relay.request_valid, 1)
        await ctx.tick("usb")
        ctx.set(relay.request_valid, 0)

        await wait_until(
            ctx, lambda: bool(ctx.get(relay.response_valid)), limit=20_000, what="relay response"
        )
        assert not ctx.get(relay.response_error)
        ctx.set(relay.response_ack, 1)
        await ctx.tick("usb")
        ctx.set(relay.response_ack, 0)

        assert target.out_packets == [OutPacket(1, 2, DATA0, payload, ACK)]
        assert target.control_requests == [SetupRequest(0x21, 0x0A, 0x0100, 0, 0)]
        assert ctx.get(host.enumerated)

    _simulate_host_against(target, bench)


# --- Boot protocol: snooped from the relay, replayed on a clone reset ----------------


async def _relay_class_no_data(ctx, host, *, request: int, value: int, index: int = 0) -> bool:
    """Forward one no-data class OUT through the host's relay; True if the target took it."""
    relay = host.control_relay
    ctx.set(relay.request_type, 0x21)
    ctx.set(relay.request, request)
    ctx.set(relay.value, value)
    ctx.set(relay.index, index)
    ctx.set(relay.length, 0)
    ctx.set(relay.request_valid, 1)
    await wait_until(ctx, lambda: bool(ctx.get(relay.request_ready)), limit=5_000, what="relay")
    await ctx.tick("usb")
    ctx.set(relay.request_valid, 0)
    await wait_until(
        ctx, lambda: bool(ctx.get(relay.response_valid)), limit=20_000, what="relay response"
    )
    accepted = not ctx.get(relay.response_error)
    ctx.set(relay.response_ack, 1)
    await ctx.tick("usb")
    ctx.set(relay.response_ack, 0)
    for _ in range(4):
        await ctx.tick("usb")
    return accepted


async def _pulse_boot_resync_trigger(ctx, host) -> None:
    ctx.set(host.boot_resync_trigger, 1)
    await ctx.tick("usb")
    ctx.set(host.boot_resync_trigger, 0)


def test_a_clone_reset_replays_set_protocol_report_to_the_real_device() -> None:
    target = _out_target()

    async def bench(ctx, host) -> None:
        assert await _relay_class_no_data(ctx, host, request=0x0B, value=0)
        await _pulse_boot_resync_trigger(ctx, host)
        await wait_until(
            ctx, lambda: ctx.get(host.boot_protocol) == 0, limit=20_000, what="the replay"
        )
        assert target.control_requests == [
            SetupRequest(0x21, 0x0B, 0, 0, 0),
            SetupRequest(0x21, 0x0B, 1, 0, 0),
        ]
        assert ctx.get(host.enumerated)

    _simulate_host_against(target, bench)


def test_a_refused_replay_leaves_the_endpoint_in_boot_protocol() -> None:
    target = _out_target()

    async def bench(ctx, host) -> None:
        assert await _relay_class_no_data(ctx, host, request=0x0B, value=0)
        target.stall_requests.add((0x21, 0x0B))
        await _pulse_boot_resync_trigger(ctx, host)
        tracker = host.boot_protocol_tracker
        await wait_until(
            ctx, lambda: bool(ctx.get(tracker.resync_fail)), limit=20_000, what="the refusal"
        )
        assert target.stalled_requests == [SetupRequest(0x21, 0x0B, 1, 0, 0)]
        for _ in range(4):
            await ctx.tick("usb")
        assert ctx.get(host.boot_protocol) == 1 << 1
        assert ctx.get(host.enumerated), "a refused replay must not touch the input path"

    _simulate_host_against(target, bench)
