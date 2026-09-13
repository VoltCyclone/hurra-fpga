import os
import re
import types
import warnings

from amaranth import Const, Elaboratable, Module, Signal
from amaranth.back import rtlil
from amaranth.sim import Simulator
from cynthion.gateware.platform import CynthionPlatformRev1D4
from luna.gateware.interface.utmi import UTMIInterface

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
from hurra_cynthion.poller import InterruptInPoller
from hurra_cynthion.timing import HostTiming
from hurra_cynthion.transaction import USBHostTransactionEngine, USBHostTransactionPort
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
    assert len(host.poller_ports) == MAX_ENDPOINTS
    for poller, port in zip(host.pollers, host.poller_ports, strict=True):
        assert poller.transaction is port
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
    assert len(re.findall(r"(?m)^\s*memory width 8 size 64", netlist)) == MAX_ENDPOINTS + 1
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
    assert len({id(port) for port in host.poller_ports}) == MAX_ENDPOINTS
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
