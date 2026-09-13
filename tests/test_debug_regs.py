"""Tests for the shared debug-register schema and its retention plumbing.

Mostly pure Python. The retention tests at the end simulate the real
``DebugRegisterBlock`` because retaining a pulse is a hardware property that a
schema check cannot express.
"""

import pytest
from amaranth import Elaboratable, Module, Signal
from amaranth.sim import Simulator

from hurra_cynthion import debug_block
from hurra_cynthion.debug_block import DebugRegisterBlock
from hurra_cynthion.debug_regs import (
    MAGIC,
    MAP_ERROR,
    REGISTER_SIZE,
    RegisterMap,
    aux_clone_register_map,
    capture_register_map,
    report_injection_register_map,
)
from hurra_cynthion.injection_map import MapError
from hurra_cynthion.regdebug import RegisterDebugLink, format_fields, register_map_for_name


def test_magic_register_at_reserved_free_address() -> None:
    m = RegisterMap("x")
    assert m.magic_address == 1
    assert m["magic"].address == 1
    assert m["magic"].kind == "magic"
    # Address 0 is reserved for LUNA size auto-negotiation; nothing may claim it.
    assert m.by_address(0) is None


def test_address_zero_is_rejected() -> None:
    with pytest.raises(ValueError, match="reserved"):
        RegisterMap("x", magic_address=0)


def test_addresses_are_unique_and_nonzero() -> None:
    m = capture_register_map()
    addrs = [r.address for r in m.registers()]
    assert 0 not in addrs
    assert len(addrs) == len(set(addrs))


@pytest.mark.parametrize(
    ("name", "factory"),
    [
        ("capture", capture_register_map),
        ("aux-clone", aux_clone_register_map),
        ("report-injection", report_injection_register_map),
    ],
)
def test_all_register_maps_fit_seven_bit_apollo_address_and_host_schema(name, factory) -> None:
    regmap = factory()
    addresses = [register.address for register in regmap.registers()]

    assert regmap.address_size == 7
    assert addresses
    assert min(addresses) > 0
    assert max(addresses) < 2**regmap.address_size
    assert len(addresses) == len(set(addresses))

    assert register_map_for_name(name).registers() == regmap.registers()
    link = object.__new__(RegisterDebugLink)
    link.regmap = regmap
    for register in regmap.registers():
        assert link.resolve(register.name) is register
        assert link.resolve(hex(register.address)) is register


def test_debug_block_derives_apollo_interface_width_from_register_map(monkeypatch) -> None:
    constructed = {}

    class FakeJTAGRegisterInterface:
        def __init__(self, *, address_size, register_size) -> None:
            constructed["address_size"] = address_size
            constructed["register_size"] = register_size

    monkeypatch.setattr(debug_block, "JTAGRegisterInterface", FakeJTAGRegisterInterface)
    regmap = report_injection_register_map()
    block = DebugRegisterBlock(regmap=regmap)
    block._MustUse__used = True

    assert constructed == {
        "address_size": regmap.address_size,
        "register_size": regmap.register_size,
    }


def test_field_widths_fit_in_register() -> None:
    m = capture_register_map()
    for reg in m.registers():
        total = sum(f.width for f in reg.fields)
        assert total <= REGISTER_SIZE, f"{reg.name} packs {total} > {REGISTER_SIZE} bits"


def test_oversized_field_set_raises() -> None:
    m = RegisterMap("x")
    with pytest.raises(ValueError, match="bits"):
        m.status("too_wide", [("a", 20), ("b", 20)])


def test_duplicate_name_raises() -> None:
    m = RegisterMap("x")
    m.status("dup", [("a", 1)])
    with pytest.raises(ValueError, match="name"):
        m.status("dup", [("b", 1)])


@pytest.mark.parametrize(
    "name",
    ["rx_status", "power_enum", "tx_pids", "txn_fsm", "live_state"],
)
def test_encode_decode_roundtrip(name: str) -> None:
    m = capture_register_map()
    reg = m[name]
    # Fill each field with a distinct value that fits its width.
    values = {f.name: (i + 1) & ((1 << f.width) - 1) for i, f in enumerate(reg.fields)}
    word = reg.encode(**values)
    assert reg.decode(word) == values


def test_decode_known_bit_layout() -> None:
    # power_enum: aux_vbus_en(1) target_discharge(1) connected(1) enumerating(1)
    # enumerated(1) error_code(4) line_state(2) enum_attempt(3), LSB-first.
    reg = capture_register_map()["power_enum"]
    word = reg.encode(
        aux_vbus_en=1,
        target_discharge=0,
        connected=1,
        enumerating=0,
        enumerated=0,
        error_code=5,
        line_state=2,
        enum_attempt=5,
    )
    # bit0=1, error_code=5 at bits[5:9], line_state=2 at bits[9:11], attempt=5 at bits[11:14]
    assert word == (1 << 0) | (1 << 2) | (5 << 5) | (2 << 9) | (5 << 11)
    decoded = reg.decode(word)
    assert decoded["aux_vbus_en"] == 1
    assert decoded["error_code"] == 5
    assert decoded["line_state"] == 2
    assert decoded["enum_attempt"] == 5


def test_raw_value_register_roundtrips_full_width() -> None:
    reg = capture_register_map()["dbg_max_rrc"]
    assert reg.decode(599999) == {"value": 599999}
    assert reg.encode(value=599999) == 599999


def test_ulpi_bus_register_exposes_rx_to_tx_turnaround_signals() -> None:
    reg = capture_register_map()["ulpi_bus"]
    assert [(field.name, field.width) for field in reg.fields] == [
        ("dir", 1),
        ("nxt", 1),
        ("stp", 1),
        ("utmi_busy", 1),
        ("control_busy", 1),
        ("register_busy", 1),
        ("transmit_busy", 1),
        ("register_out_req", 1),
        ("transmit_out_req", 1),
        ("data_out", 8),
        ("register_address", 6),
        ("register_write_data", 8),
    ]


def test_memory_pairs_have_addr_and_data_with_depths() -> None:
    m = capture_register_map()
    assert m.memories() == ["rx", "tx", "ls", "respctl", "respdat", "usbdiff"]
    for name, depth in [
        ("rx", 32),
        ("tx", 16),
        ("ls", 128),
        ("respctl", 200),
        ("respdat", 200),
        ("usbdiff", 256),
    ]:
        addr = m[f"{name}_addr"]
        data = m[f"{name}_data"]
        assert addr.kind == "mem_addr" and addr.writable
        assert data.kind == "mem_data" and not data.writable
        assert addr.depth == depth == data.depth


def test_magic_constant_value() -> None:
    assert MAGIC == 0xCAFE1234


def test_aux_clone_register_map_has_exact_status_boundaries() -> None:
    m = aux_clone_register_map()
    assert [(reg.address, reg.name) for reg in m.status_registers()] == [
        (1, "magic"),
        (2, "aux_prereq"),
        (3, "aux_link_live"),
        (4, "aux_seen"),
        (5, "aux_counts"),
        (6, "last_setup_0"),
        (7, "last_setup_1"),
        (8, "device_state"),
    ]
    assert [(field.name, field.width) for field in m["aux_counts"].fields] == [
        ("reset_count", 8),
        ("setup_count", 8),
        ("address_count", 8),
        ("config_count", 8),
    ]
    assert [(field.name, field.width) for field in m["last_setup_0"].fields] == [
        ("request_type", 8),
        ("request", 8),
        ("value", 16),
    ]
    assert [(field.name, field.width) for field in m["last_setup_1"].fields] == [
        ("index", 16),
        ("length", 16),
    ]


def test_aux_clone_register_fields_fit_in_register() -> None:
    for reg in aux_clone_register_map().registers():
        assert sum(field.width for field in reg.fields) <= REGISTER_SIZE


def test_report_injection_register_map_appends_bounded_diagnostics() -> None:
    m = report_injection_register_map()
    assert [(reg.address, reg.name) for reg in m.status_registers()] == [
        (1, "magic"),
        (2, "injection_link"),
        (3, "injection_map"),
        (4, "map_validation"),
        (5, "spi_slots"),
        (6, "spi_bad_sof"),
        (7, "spi_bad_crc"),
        (8, "spi_bad_length"),
        (9, "spi_bad_type"),
        (10, "spi_queue_full"),
        (11, "link_losses"),
        (12, "native_reports"),
        (13, "mutated_reports"),
        (14, "synthesized_reports"),
        (15, "command_commits"),
        (16, "command_overflows"),
        (17, "monitoring_drops"),
        (18, "telemetry_drops"),
        (19, "rx_duplicates"),
        (20, "rx_stale"),
        (21, "rx_gaps"),
        (22, "accepted_reports"),
        (23, "rx_invalid"),
        (24, "map_commits"),
        (25, "map_rejects"),
        (26, "injection_seen"),
        (27, "relay_drops"),
        (28, "usb_speed"),
        (29, "polls_issued"),
        (30, "poll_naks"),
    ]
    # speed_policy is the first control register in this map; it shares the
    # address space with the status registers, so it must land after them.
    assert [(reg.address, reg.name) for reg in m.registers() if reg.kind == "control"] == [
        (31, "speed_policy"),
    ]
    assert [(field.name, field.width) for field in m["usb_speed"].fields] == [
        ("aux_speed", 2),
        ("aux_full_speed_only", 1),
        ("target_xcvr_select", 2),
        ("target_term_select", 1),
        ("target_op_mode", 2),
        # Appended, not inserted: a new field at the end of an existing status
        # register leaves every register address unchanged (asserted above) and
        # only extends this one's bit layout.
        ("target_high_speed", 1),
        ("target_host_disconnect", 1),
        ("target_host_disconnect_seen", 1),
        ("target_device_unresponsive", 1),
    ]
    assert [(field.name, field.width) for field in m["speed_policy"].fields] == [
        ("aux_force_full_speed", 1),
        ("target_force_full_speed", 1),
    ]
    assert [(field.name, field.width) for field in m["relay_drops"].fields] == [
        ("unmatched", 16),
        ("congested", 16),
    ]
    assert [(field.name, field.width) for field in m["injection_map"].fields] == [
        ("descriptor_generation", 16),
        ("active_map_generation", 16),
    ]
    assert [(field.name, field.width) for field in m["injection_seen"].fields] == [
        ("export_done_seen", 1),
        ("map_busy_seen", 1),
    ]
    for reg in m.registers():
        assert sum(field.width for field in reg.fields) <= REGISTER_SIZE


def test_register_map_selector_preserves_capture_default_and_selects_aux() -> None:
    assert register_map_for_name("capture").name == "capture"
    assert register_map_for_name("aux-clone").name == "aux-clone"
    assert register_map_for_name("report-injection").name == "report-injection"
    with pytest.raises(ValueError, match="unknown register map"):
        register_map_for_name("unknown")


# --- Retention plumbing -------------------------------------------------------
#
# A JTAG read is an asynchronous sample of whatever a register holds, while the
# most interesting sources are one-cycle pulses. These tests simulate the real
# block and read the exact packed word handed to ``add_read_only_register``.


class _RetentionHarness(Elaboratable):
    """Elaborates a block's retention state and exposes one register's packed word."""

    def __init__(self, block: DebugRegisterBlock, register: str) -> None:
        self.block = block
        self._read = block.regs.registers[block.regmap[register].address]["read"]
        self.word = Signal(REGISTER_SIZE)

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        m.submodules.retained = self.block._retained
        m.d.comb += self.word.eq(self._read)
        return m


def _injection_block() -> DebugRegisterBlock:
    """Build the real block, silencing the guards on the parts we do not elaborate."""
    block = DebugRegisterBlock(regmap=report_injection_register_map())
    block._MustUse__used = True
    block.regs._MustUse__used = True
    block.regs.interface._MustUse__used = True
    return block


class _MapValidationFixture:
    """The ``map_validation`` sources wired exactly as ``gateware.py`` wires them."""

    def __init__(self) -> None:
        self.block = _injection_block()
        self.commit_ack = Signal()
        self.commit_error = Signal(4)
        self.commit_error_entry_index = Signal(8, init=0xFF)
        self.active_valid = Signal()
        self.busy = Signal()
        self.active_entries = Signal(7)
        self.active_layouts = Signal(5)
        self.map_rejects = self.block.counter(
            self.commit_ack & (self.commit_error != MapError.NONE), name="map_rejects"
        )
        self.block.status(
            "map_validation",
            error=self.block.latch_on(self.commit_ack, self.commit_error, name="map_error"),
            error_entry=self.block.latch_on(
                self.commit_ack,
                self.commit_error_entry_index,
                name="map_error_entry",
                init=0xFF,
            ),
            active_valid=self.active_valid,
            busy=self.busy,
            active_entries=self.active_entries,
            active_layouts=self.active_layouts,
        )
        self.harness = _RetentionHarness(self.block, "map_validation")
        self.register = self.block.regmap["map_validation"]

    def simulator(self) -> Simulator:
        sim = Simulator(self.harness)
        sim.add_clock(1e-6, domain="usb")
        return sim

    async def commit(self, ctx, error: int, entry: int) -> None:
        """Pulse ``commit_ack`` for exactly one cycle, as the store does."""
        ctx.set(self.commit_error, error)
        ctx.set(self.commit_error_entry_index, entry)
        ctx.set(self.commit_ack, 1)
        await ctx.tick("usb")
        # The store unconditionally re-defaults both fields on the next cycle.
        ctx.set(self.commit_ack, 0)
        ctx.set(self.commit_error, MapError.NONE)
        ctx.set(self.commit_error_entry_index, 0xFF)

    def decode(self, ctx) -> dict[str, int]:
        return self.register.decode(ctx.get(self.harness.word))


def test_map_validation_error_survives_an_asynchronous_jtag_read_long_after_commit() -> None:
    fixture = _MapValidationFixture()
    sim = fixture.simulator()

    async def bench(ctx) -> None:
        await fixture.commit(ctx, MapError.OVERLAP, 1)
        elapsed = 0
        for checkpoint in (1, 5, 50, 1000):
            while elapsed < checkpoint:
                await ctx.tick("usb")
                elapsed += 1
            decoded = fixture.decode(ctx)
            assert decoded["error"] == MapError.OVERLAP, f"lost at +{checkpoint}"
            assert decoded["error_entry"] == 1, f"lost at +{checkpoint}"

    sim.add_testbench(bench)
    sim.run()


def test_map_validation_error_defaults_to_none_before_any_commit() -> None:
    fixture = _MapValidationFixture()
    sim = fixture.simulator()

    async def bench(ctx) -> None:
        for _ in range(20):
            await ctx.tick("usb")
        decoded = fixture.decode(ctx)
        assert decoded["error"] == MapError.NONE
        # 0xFF is the store's "no entry implicated" idle value; the latch must keep it.
        assert decoded["error_entry"] == 0xFF

    sim.add_testbench(bench)
    sim.run()


def test_map_validation_error_is_replaced_by_the_next_commit_result() -> None:
    fixture = _MapValidationFixture()
    sim = fixture.simulator()

    async def bench(ctx) -> None:
        await fixture.commit(ctx, MapError.OVERLAP, 1)
        for _ in range(10):
            await ctx.tick("usb")
        assert fixture.decode(ctx)["error"] == MapError.OVERLAP

        await fixture.commit(ctx, MapError.NONE, 0xFF)
        for _ in range(10):
            await ctx.tick("usb")
        decoded = fixture.decode(ctx)
        # Sticky until the next commit, not sticky forever.
        assert decoded["error"] == MapError.NONE
        assert decoded["error_entry"] == 0xFF

    sim.add_testbench(bench)
    sim.run()


def test_map_rejects_counter_separates_rejection_from_a_never_uploaded_map() -> None:
    fixture = _MapValidationFixture()
    sim = fixture.simulator()

    async def bench(ctx) -> None:
        for _ in range(10):
            await ctx.tick("usb")
        never_uploaded = ctx.get(fixture.harness.word)
        assert ctx.get(fixture.map_rejects) == 0

        await fixture.commit(ctx, MapError.CRC, 3)
        for _ in range(10):
            await ctx.tick("usb")
        # Without the counter both states pack to the same idle word, which is the
        # ambiguity an operator most needs resolved.
        assert never_uploaded == 0x0000_0FF0
        assert ctx.get(fixture.map_rejects) == 1

        # An accepted commit counts as a commit but not as a rejection.
        await fixture.commit(ctx, MapError.NONE, 0xFF)
        for _ in range(10):
            await ctx.tick("usb")
        assert ctx.get(fixture.map_rejects) == 1

    sim.add_testbench(bench)
    sim.run()


def test_debug_counter_saturates_instead_of_wrapping() -> None:
    block = _injection_block()
    event = Signal()
    count = block.counter(event, name="narrow", width=4)

    class Harness(Elaboratable):
        def elaborate(self, platform) -> Module:
            del platform
            m = Module()
            m.submodules.retained = block._retained
            return m

    sim = Simulator(Harness())
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        ctx.set(event, 1)
        for _ in range(40):
            await ctx.tick("usb")
        assert ctx.get(count) == 0xF

    sim.add_testbench(bench)
    sim.run()


def test_debug_sticky_holds_a_one_cycle_event() -> None:
    block = _injection_block()
    event = Signal()
    seen = block.sticky(event, name="thing")

    class Harness(Elaboratable):
        def elaborate(self, platform) -> Module:
            del platform
            m = Module()
            m.submodules.retained = block._retained
            return m

    sim = Simulator(Harness())
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        assert ctx.get(seen) == 0
        ctx.set(event, 1)
        await ctx.tick("usb")
        ctx.set(event, 0)
        elapsed = 0
        for checkpoint in (1, 50, 1000):
            while elapsed < checkpoint:
                await ctx.tick("usb")
                elapsed += 1
            assert ctx.get(seen) == 1, f"lost at +{checkpoint}"

    sim.add_testbench(bench)
    sim.run()


def test_injection_seen_retains_a_one_cycle_descriptor_export_done() -> None:
    # ``DescriptorExportEngine.done`` is exactly one usb cycle wide and appears in no
    # other register, so a dump-based bring-up can never otherwise observe it.
    block = _injection_block()
    export_done = Signal()
    map_busy = Signal()
    block.status(
        "injection_seen",
        export_done_seen=block.sticky(export_done, name="export_done_seen"),
        map_busy_seen=block.sticky(map_busy, name="map_busy_seen"),
    )
    harness = _RetentionHarness(block, "injection_seen")
    register = block.regmap["injection_seen"]

    sim = Simulator(harness)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx) -> None:
        assert register.decode(ctx.get(harness.word)) == {
            "export_done_seen": 0,
            "map_busy_seen": 0,
        }
        ctx.set(export_done, 1)
        await ctx.tick("usb")
        ctx.set(export_done, 0)
        elapsed = 0
        for checkpoint in (1, 50, 1000):
            while elapsed < checkpoint:
                await ctx.tick("usb")
                elapsed += 1
            decoded = register.decode(ctx.get(harness.word))
            assert decoded["export_done_seen"] == 1, f"lost at +{checkpoint}"
            # The two milestones stay independent.
            assert decoded["map_busy_seen"] == 0, f"cross-set at +{checkpoint}"

    sim.add_testbench(bench)
    sim.run()


def test_map_error_labels_match_the_gateware_enum() -> None:
    assert {member.value: member.name for member in MapError} == MAP_ERROR
    reg = report_injection_register_map()["map_validation"]
    decoded = reg.decode(reg.encode(error=MapError.OVERLAP, error_entry=1))
    assert "error=12(OVERLAP)" in format_fields(reg, decoded)


def _elaborated_debug_top():
    """Elaborate a real top that instantiates ``DebugRegisterBlock``.

    A CDC cannot be demonstrated in simulation -- Amaranth's simulator has no
    metastability and no clock skew -- so these assertions are structural:
    they name the clock domain the register file's logic is actually on.
    """
    import warnings

    from amaranth.hdl._ir import Fragment
    from cynthion.gateware.platform import CynthionPlatformRev1D4

    from hurra_cynthion.aux_clone_diag import AuxCloneDiagnosticTop

    platform = CynthionPlatformRev1D4()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return Fragment.get(AuxCloneDiagnosticTop(), platform)


def _walk(fragment, path=""):
    yield path, fragment
    for sub, name, _src in fragment.subfragments:
        yield from _walk(sub, f"{path}/{name}" if path else (name or "?"))


def test_debug_register_file_is_clocked_in_the_usb_domain() -> None:
    # LUNA captures word_to_send with `m.d.sync +=` (jtag.py) through a plain
    # comb fan-in (spi.py), while every register source is usb-domain. Leaving
    # the file in `sync` makes every multi-bit read an unsynchronized CDC that
    # can return a value the hardware never held -- e.g. a free-running 32-bit
    # counter rolling 0x0000FFFF -> 0x00010000 changes 17 bits on one usb edge
    # and can read back as 0x0001FFFF.
    domains: dict[str, list[str]] = {}
    for path, fragment in _walk(_elaborated_debug_top()):
        if "regs" not in path:
            continue
        for domain in fragment.statements:
            domains.setdefault(domain, []).append(path)

    assert "sync" not in domains, (
        "the debug register file still has sync-domain logic at "
        f"{domains.get('sync')}; its reads are an unsynchronized multi-bit CDC"
    )
    assert (
        "usb" in domains
    ), f"the debug register file has no usb-domain logic at all; found {sorted(domains)}"


def test_no_status_register_source_is_sync_domain() -> None:
    # Guards the next engineer who wires a sync signal into debug.status():
    # after the rename that is a CDC in the opposite direction, and nothing
    # else in the suite would catch it.
    from amaranth.hdl._ast import SignalSet

    fragment = _elaborated_debug_top()
    # Signals are unhashable (``__eq__`` builds an expression), so use
    # Amaranth's own SignalSet rather than a builtin set.
    sync_driven = SignalSet()
    register_file_reads = SignalSet()
    for path, sub in _walk(fragment):
        for domain, statements in sub.statements.items():
            if domain == "sync":
                for statement in statements:
                    sync_driven.update(statement._lhs_signals())
        if "regs" in path:
            for statements in sub.statements.values():
                for statement in statements:
                    register_file_reads.update(statement._rhs_signals())

    offenders = sorted(str(signal) for signal in register_file_reads if signal in sync_driven)
    assert not offenders, (
        f"{len(offenders)} signal(s) read by the debug register file are driven in the "
        f"sync domain: {offenders[:8]}"
    )
