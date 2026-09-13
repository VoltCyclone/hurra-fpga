"""Shared JTAG register schemas for diagnostic gateware and host tools.

Address 0 belongs to LUNA's size negotiation. Diagnostic registers begin at 1
and pack their fields least-significant bit first into 32-bit words.
"""

from __future__ import annotations

from dataclasses import dataclass

MAGIC = 0xCAFE1234
REGISTER_SIZE = 32
ADDRESS_SIZE = 7


@dataclass(frozen=True)
class Field:
    """One packed bitfield within a 32-bit register (LSB-first order)."""

    name: str
    width: int


@dataclass
class Register:
    """A single JTAG debug register."""

    name: str
    address: int
    kind: str
    fields: tuple[Field, ...] = ()
    doc: str = ""
    mem_name: str | None = None
    depth: int | None = None

    @property
    def writable(self) -> bool:
        return self.kind in ("control", "mem_addr")

    def decode(self, word: int) -> dict[str, int]:
        """Split a raw 32-bit read into its named fields (LSB-first)."""
        word &= (1 << REGISTER_SIZE) - 1
        if not self.fields:
            return {"value": word}
        out: dict[str, int] = {}
        offset = 0
        for f in self.fields:
            out[f.name] = (word >> offset) & ((1 << f.width) - 1)
            offset += f.width
        return out

    def encode(self, **values: int) -> int:
        """Pack named field values into a raw 32-bit word (LSB-first)."""
        if not self.fields:
            return int(values.get("value", 0)) & ((1 << REGISTER_SIZE) - 1)
        word = 0
        offset = 0
        for f in self.fields:
            v = int(values.get(f.name, 0)) & ((1 << f.width) - 1)
            word |= v << offset
            offset += f.width
        return word


class RegisterMap:
    """An ordered, address-allocated collection of debug registers."""

    def __init__(self, name: str, *, magic_address: int = 1) -> None:
        if magic_address < 1:
            raise ValueError("magic_address must be >= 1 (address 0 is reserved)")
        self.name = name
        self.magic_address = magic_address
        self.register_size = REGISTER_SIZE
        self.address_size = ADDRESS_SIZE
        self._by_name: dict[str, Register] = {}
        self._by_addr: dict[int, Register] = {}
        self._next = 1
        self._add(Register("magic", magic_address, "magic", doc="sanity constant = MAGIC"))

    def _alloc(self) -> int:
        while self._next in self._by_addr or self._next == 0:
            self._next += 1
        addr = self._next
        self._next += 1
        return addr

    def _add(self, reg: Register) -> Register:
        if reg.address == 0:
            raise ValueError("address 0 is reserved for size auto-negotiation")
        if reg.address in self._by_addr:
            raise ValueError(f"register address {reg.address} already in use")
        if reg.name in self._by_name:
            raise ValueError(f"register name {reg.name!r} already in use")
        total = sum(f.width for f in reg.fields)
        if total > REGISTER_SIZE:
            raise ValueError(f"register {reg.name!r} fields total {total} bits > {REGISTER_SIZE}")
        self._by_name[reg.name] = reg
        self._by_addr[reg.address] = reg
        return reg

    def status(self, name: str, fields, doc: str = "") -> Register:
        """A read-only status register with packed named fields."""
        fs = tuple(Field(n, w) for n, w in fields)
        return self._add(Register(name, self._alloc(), "status", fs, doc))

    def control(self, name: str, fields, doc: str = "") -> Register:
        """A read/write register."""
        fs = tuple(Field(n, w) for n, w in fields)
        return self._add(Register(name, self._alloc(), "control", fs, doc))

    def memory(self, name: str, depth: int, doc: str = "") -> tuple[Register, Register]:
        """Declare a writable index and read-only data register pair."""
        addr = self._add(
            Register(f"{name}_addr", self._alloc(), "mem_addr", (), f"{doc} (index)", name, depth)
        )
        data = self._add(
            Register(f"{name}_data", self._alloc(), "mem_data", (), f"{doc} (data)", name, depth)
        )
        return addr, data

    def __getitem__(self, name: str) -> Register:
        return self._by_name[name]

    def get(self, name: str) -> Register | None:
        return self._by_name.get(name)

    def by_address(self, address: int) -> Register | None:
        return self._by_addr.get(address)

    def registers(self) -> list[Register]:
        return list(self._by_name.values())

    def status_registers(self) -> list[Register]:
        return [r for r in self._by_name.values() if r.kind in ("status", "magic")]

    def memories(self) -> list[str]:
        seen: list[str] = []
        for r in self._by_name.values():
            if r.mem_name and r.mem_name not in seen:
                seen.append(r.mem_name)
        return seen


def capture_register_map() -> RegisterMap:
    """Register map exposed by ``capture_diag``."""
    m = RegisterMap("capture", magic_address=1)

    m.status(
        "rx_status",
        [
            ("cap_count", 6),
            ("cap_done", 1),
            ("connected", 1),
            ("rx_error_ever", 1),
            ("rx_valid_total", 16),
        ],
        "RX capture progress",
    )
    m.status(
        "pid_scan",
        [("first_pid_val", 8), ("first_pid_pos", 6), ("first_pid_found", 1)],
        "first recognizable RX PID found in the stream",
    )
    m.status(
        "power_enum",
        [
            ("aux_vbus_en", 1),
            ("target_discharge", 1),
            ("connected", 1),
            ("enumerating", 1),
            ("enumerated", 1),
            ("error_code", 4),
            ("line_state", 2),
            ("enum_attempt", 3),
        ],
        "power + enumeration state (line_state 0=SE0 1=J 2=K 3=SE1)",
    )
    m.status(
        "tx_status",
        [("tx_count", 5), ("tx_done", 1), ("tx_total", 16)],
        "TX capture progress",
    )
    m.status(
        "tx_pids",
        [("setup_pids", 8), ("in_pids", 8), ("data_pids", 8), ("other_pids", 8)],
        "transmitted PID type counts (SOF excluded)",
    )
    m.status(
        "sof_lastpid",
        [
            ("sof", 16),
            ("last_nonsof_pid", 8),
            ("last_start_sof", 1),
            ("last_start_pid", 4),
            ("last_start_control_done", 1),
            ("last_start_control_busy", 1),
        ],
        "SOF count, last non-SOF PID, and most recent engine-start source",
    )
    m.status(
        "ls_status",
        [("ls_idx", 8), ("ls_done", 1), ("ls_armed", 1)],
        "line_state time-series capture status",
    )
    m.status(
        "control_path",
        [
            ("control_req_seen", 1),
            ("control_txn_seen", 1),
            ("engine_start_seen", 1),
            ("control_busy_seen", 1),
            ("ready", 1),
            ("traffic_enable", 1),
            ("release_seen", 1),
            ("release_busy_seen", 1),
            ("register_done_seen", 1),
            ("traffic_seen", 1),
            ("first_in_seen", 1),
            ("first_in_dir_seen", 1),
            ("first_in_nxt_seen", 1),
            ("first_in_control_seen", 1),
            ("first_in_register_seen", 1),
            ("first_in_tx_req_seen", 1),
            ("first_in_reg_req_seen", 1),
            ("first_in_utmi_busy_seen", 1),
            ("first_in_sample_count", 5),
        ],
        "sticky control-transfer path milestones",
    )
    m.status(
        "control_counts",
        [
            ("request_count", 8),
            ("done_count", 8),
            ("failure_count", 8),
            ("first_failure_seen", 1),
            ("first_failure_status", 3),
        ],
        "control request/completion counts and first failure status",
    )
    m.status(
        "first_failure_req",
        [("request_type", 8), ("request", 8), ("address", 7)],
        "first failed control request header",
    )
    m.status(
        "first_failure_value",
        [("value", 16), ("index", 16)],
        "first failed control request value and index",
    )
    m.status(
        "first_failure_result",
        [("length", 16), ("transferred", 16)],
        "first failed control request length and transferred bytes",
    )
    m.status("dbg_max_rrc", [("value", 32)], "max reset_recovery_counter reached")
    m.status("dbg_recovery_wait_max", [("value", 32)], "max recovery_wait reached")
    m.status("dbg_nonj_events", [("value", 32)], "non-J cycles seen during recovery")
    m.status(
        "live_state",
        [
            ("txn_busy", 1),
            ("txn_done", 1),
            ("txn_status", 3),
            ("control_busy", 1),
            ("tx_valid", 1),
            ("rx_active", 1),
            ("line_state", 2),
            ("control_status", 3),
        ],
        "live transaction/control engine state",
    )
    m.status(
        "txn_fsm",
        [("dbg_state", 4), ("tx_valid", 1), ("tx_ready", 1), ("rx_active", 1), ("rx_valid", 1)],
        "transaction FSM state index + tx/rx handshake",
    )
    m.status(
        "ulpi_bus",
        [
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
        ],
        "raw ULPI bus state and LUNA internal ownership at the RX-to-TX boundary",
    )
    m.status(
        "setup_resp",
        [
            ("first_setup_seen", 1),
            ("saw_send_data", 1),
            ("resp_done", 1),
            ("rx_active_ever", 1),
            ("dir_ever", 1),
            ("rx_error_ever", 1),
            ("raw_bytes", 8),
            ("first_byte_found", 1),
            ("first_byte", 8),
            ("first_is_valid_pid", 1),
            ("sample_count", 8),
        ],
        "first SETUP response activity and captured PID",
    )
    m.status(
        "phy_vbus",
        [
            ("vbus_valid", 1),
            ("session_valid", 1),
            ("session_end", 1),
            ("host_disconnect", 1),
            ("rx_active", 1),
        ],
        "live target-PHY VBUS/session state",
    )
    m.memory("rx", 32, "RX byte capture")
    m.memory("tx", 16, "TX byte capture")
    m.memory("ls", 128, "line_state time-series")
    m.memory("respctl", 200, "SETUP-resp window: packed {dir,nxt,rx_active,rx_error,ls} per cycle")
    m.memory("respdat", 200, "SETUP-resp window: raw ULPI data byte per cycle")
    m.status(
        "diff_status",
        [("diff_done", 1), ("diff_bytes", 9)],
        "direct D+/D- capture status from the report descriptor's final data ACK",
    )
    m.status("report_tx_lo", [("value", 32)], "first four accepted status-stage TX bytes")
    m.status(
        "report_tx_hi",
        [("value", 28), ("count", 4)],
        "next four accepted status-stage TX bytes and total captured byte count",
    )
    m.memory(
        "usbdiff",
        256,
        "wire+FSM trace, 2 samples/byte (4b each: b0=D+ b1=D- b2=tx_valid "
        "b3=in_WAIT_DATA_GAP), @30MHz, armed at the report descriptor data ACK",
    )
    return m


_AUX_CLONE_STATUS_REGISTERS = (
    (
        "aux_prereq",
        (
            ("host_connected", 1),
            ("host_enumerated", 1),
            ("host_error", 4),
            ("copy_enable", 1),
            ("copy_done", 1),
            ("copy_failed", 1),
            ("aux_vbus_valid", 1),
            ("effective_connect", 1),
            ("configured", 1),
        ),
        "host/copy/VBUS prerequisites for AUX attachment",
    ),
    (
        "aux_link_live",
        (
            ("line_state", 2),
            ("rx_active", 1),
            ("rx_valid", 1),
            ("rx_error", 1),
            ("tx_valid", 1),
            ("tx_ready", 1),
            ("reset_detected", 1),
            ("setup_received", 1),
            ("control_tx_valid", 1),
            ("control_stall", 1),
        ),
        "live AUX UTMI and EP0 state; strobe bits are for watch/scope use only, "
        "aux_seen is authoritative for whether an event ever happened",
    ),
    (
        "aux_seen",
        (
            ("connect_seen", 1),
            ("rx_active_seen", 1),
            ("rx_valid_seen", 1),
            ("rx_error_seen", 1),
            ("tx_valid_seen", 1),
            ("reset_seen", 1),
            ("setup_seen", 1),
            ("address_seen", 1),
            ("config_seen", 1),
            ("control_tx_seen", 1),
            ("control_stall_seen", 1),
        ),
        "sticky AUX attachment and control-path events",
    ),
    (
        "aux_counts",
        (
            ("reset_count", 8),
            ("setup_count", 8),
            ("address_count", 8),
            ("config_count", 8),
        ),
        "saturating AUX reset and EP0 milestone counters",
    ),
    (
        "last_setup_0",
        (("request_type", 8), ("request", 8), ("value", 16)),
        "last received SETUP request header and value",
    ),
    (
        "last_setup_1",
        (("index", 16), ("length", 16)),
        "last received SETUP index and length",
    ),
    (
        "device_state",
        (("last_address", 7), ("last_config", 8), ("configured", 1)),
        "last committed USB address/configuration and current configured state",
    ),
)


def aux_clone_register_map() -> RegisterMap:
    """Register map for the PC-facing AUX clone diagnostic."""
    m = RegisterMap("aux-clone", magic_address=1)
    for name, fields, doc in _AUX_CLONE_STATUS_REGISTERS:
        m.status(name, fields, doc)
    return m


def report_injection_register_map() -> RegisterMap:
    """Register map for the production hardware-injection data plane."""
    m = RegisterMap("report-injection", magic_address=1)
    m.status(
        "injection_link",
        [
            ("link_ready", 1),
            ("session_active", 1),
            ("spi_busy", 1),
            ("rx_pending", 1),
            ("map_active", 1),
            ("map_busy", 1),
            ("descriptor_busy", 1),
            ("relay_ready", 1),
        ],
        "live fixed-slot, map, descriptor, and relay readiness",
    )
    m.status(
        "injection_map",
        [("descriptor_generation", 16), ("active_map_generation", 16)],
        "authoritative descriptor generation and active map generation",
    )
    m.status(
        "map_validation",
        [
            ("error", 4),
            ("error_entry", 8),
            ("active_valid", 1),
            ("busy", 1),
            ("active_entries", 7),
            ("active_layouts", 5),
        ],
        "last commit result (retained until the next commit) and active-bank bounds",
    )
    for name, doc in (
        ("spi_slots", "free-running 125 us slot index; wraps mod 2^32 (~6.2 days), not saturating"),
        ("spi_bad_sof", "SPI return frames rejected for bad SOF"),
        ("spi_bad_crc", "SPI return frames rejected for bad CRC"),
        ("spi_bad_length", "SPI return frames rejected for bad length"),
        ("spi_bad_type", "SPI return frames rejected for unsupported type"),
        ("spi_queue_full", "valid SPI return frames dropped while RX queue full"),
        ("link_losses", "MCU_READY falling edges"),
        ("native_reports", "accepted physical-source reports"),
        ("mutated_reports", "accepted reports that transactionally committed a command"),
        ("synthesized_reports", "accepted stationary synthesized reports"),
        ("command_commits", "transactionally committed injection commands"),
        ("command_overflows", "rejected residual-overflow commands"),
        ("monitoring_drops", "best-effort native-report telemetry drops"),
        ("telemetry_drops", "bounded status-result queue drops"),
        ("rx_duplicates", "duplicate direction-local RX sequence rejections"),
        ("rx_stale", "stale or out-of-window direction-local RX sequence rejections"),
        ("rx_gaps", "in-window forward direction-local RX sequence gaps"),
        ("accepted_reports", "reports accepted by the injection engine"),
        ("rx_invalid", "in-window RX frames drained without action"),
        ("map_commits", "map commits acknowledged, accepted or rejected"),
        ("map_rejects", "map commits rejected with a non-NONE error"),
    ):
        m.status(name, [("value", 32)], doc)
    m.status(
        "injection_seen",
        [("export_done_seen", 1), ("map_busy_seen", 1)],
        "sticky descriptor-export completion and map-upload milestones",
    )
    # Both fields are expected to read 0 forever, so one address that is
    # non-zero at all is the useful signal -- and it keeps the append to a
    # single address. RegisterMap._alloc is declaration-ordered: append only.
    m.status(
        "relay_drops",
        [("unmatched", 16), ("congested", 16)],
        "reports dropped by the clone relay: no such endpoint / endpoint FIFO congested",
    )
    # Appended for the High Speed work. _alloc is declaration-ordered, so new
    # registers go here at the end -- inserting above shifts every later
    # address and silently invalidates readers built against the old map.
    m.status(
        "usb_speed",
        [
            ("aux_speed", 2),
            ("aux_full_speed_only", 1),
            ("target_xcvr_select", 2),
            ("target_term_select", 1),
            ("target_op_mode", 2),
            # The chirp result directly. It is derivable from the three PHY
            # fields above, but this is the bit you actually want during
            # bring-up: 1 means the host-side handshake completed.
            ("target_high_speed", 1),
            # Appended for the replug investigation. host_disconnect is the
            # PHY's only High Speed detach report and the design's only
            # recovery trigger there; the sticky bit says whether it has ever
            # asserted, which a live read cannot.
            ("target_host_disconnect", 1),
            ("target_host_disconnect_seen", 1),
            ("target_device_unresponsive", 1),
        ],
        "negotiated AUX device speed (0=HS 1=FS 2=LS), TARGET PHY mode and chirp result",
    )
    # Poll-cadence counters, appended for the High Speed rate investigation.
    # All wrap mod 2**32; read them as deltas over a window.
    #
    # native_reports alone cannot say why a rate is low: a poll that was never
    # issued and a poll the device NAKed produce the identical count. Taken
    # together these separate the host from the device --
    #   polls_issued ~= spi_slots -> the host is polling every microframe
    #   polls_issued << spi_slots -> the host is the bottleneck
    #   poll_naks makes up the gap -> the device had nothing to send
    # spi_slots is the reference: it free-runs on a 125 us divider off the
    # same usb clock, independent of the scheduler.
    for name, doc in (
        ("polls_issued", "interrupt-IN transactions started (excludes SOF and control)"),
        ("poll_naks", "interrupt-IN polls the device answered with NAK"),
    ):
        m.status(name, [("value", 32)], doc)
    # Runtime speed policy. Both fields default to 0 = "attempt High Speed",
    # because USB negotiates speed by chirp anyway: a device that offers HS and
    # gets no answer falls back to FS on its own. So the normal path needs no
    # configuration at all, and these exist only as an override for a device or
    # host that misbehaves at HS -- reachable with `regdebug write`, so nobody
    # has to build and flash a second bitstream to try the other speed.
    m.control(
        "speed_policy",
        [("aux_force_full_speed", 1), ("target_force_full_speed", 1)],
        "force Full Speed on AUX / TARGET; 0 = negotiate (default)",
    )
    return m


REGISTER_MAP_FACTORIES = {
    "capture": capture_register_map,
    "aux-clone": aux_clone_register_map,
    "report-injection": report_injection_register_map,
}
REGISTER_MAP_NAMES = tuple(REGISTER_MAP_FACTORIES)


def register_map_for_name(name: str) -> RegisterMap:
    """Build a shared register map by its CLI name."""
    try:
        factory = REGISTER_MAP_FACTORIES[name]
    except KeyError as exc:
        raise ValueError(f"unknown register map {name!r}") from exc
    return factory()


# Labels used by the host-side formatter.
LINE_STATE = {0: "SE0", 1: "J", 2: "K", 3: "SE1"}
TXN_STATE = {
    0: "IDLE",
    1: "TOKEN_START",
    2: "TOKEN_SEND",
    3: "WAIT_DATA_GAP",
    4: "SEND_DATA",
    5: "WAIT_RESPONSE",
    6: "WAIT_ACK_GAP",
    7: "SEND_ACK",
    8: "WAIT_ACK_DRAIN",
    9: "WAIT_SOF_DRAIN",
}
TXN_STATUS = {
    0: "SUCCESS",
    1: "NAK",
    2: "STALL",
    3: "TIMEOUT",
    4: "CRC_ERROR",
    5: "OVERFLOW",
    6: "DISCONNECTED",
}
# Mirrors ``injection_map.MapError``, kept as a plain dict so the host tool needs no
# ``amaranth`` import. ``test_map_error_labels_match_the_gateware_enum`` pins it.
MAP_ERROR = {
    0: "NONE",
    1: "GENERATION",
    2: "MAP_GENERATION",
    3: "FIELD_COUNT",
    4: "LAYOUT_COUNT",
    5: "CRC",
    6: "INTERFACE",
    7: "ENDPOINT",
    8: "REPORT_LENGTH",
    9: "FIELD_WIDTH",
    10: "BIT_OFFSET",
    11: "REPORT_ID_PREFIX",
    12: "OVERLAP",
    13: "UNSUPPORTED_FIELD",
    14: "DUPLICATE_ENTRY",
}
