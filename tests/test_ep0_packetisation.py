"""Wire-level EP0 packetisation of the AUX clone's IN data stages.

The clone serves the real device's device descriptor verbatim, bMaxPacketSize0
(byte 7) included, so the PC splits every EP0 data stage at that size. The
clone has to packetise at it too. A device that declares 8 and answers
GET_DESCRIPTOR(device) with one 18-byte packet exceeds its own packet size
[USB2.0 5.5.3], and a Full Speed host throws the transfer away -- the clone
never enumerates.

Every test drives the raw UTMI bus as the PC would, so the packet boundaries
and DATA0/DATA1 PIDs asserted here are the ones on the wire, through LUNA's
real control endpoint and request multiplexer.
"""

import re
import warnings

import pytest
from _descriptor_store_helpers import seed_descriptor
from cynthion.gateware.platform import CynthionPlatformRev1D4
from test_device_clone_e2e import (
    ACK,
    DATA0,
    DATA1,
    DEVICE_DESCRIPTOR,
    GET_DESCRIPTOR,
    MOUSE_REPORT,
    SET_ADDRESS,
    _make_top,
    _power_up,
    assert_packets,
    control_in_packets,
    pc_in_data_packet,
    pc_in_transaction,
    pc_send,
    pc_send_ack_watching,
    pc_setup_transaction,
    pc_status_out,
    push_report,
    setup_packet,
)

from hurra_cynthion.gateware import CynthionMouseHostTop


def _device_descriptor(mps: int) -> bytes:
    return DEVICE_DESCRIPTOR[:7] + bytes([mps]) + DEVICE_DESCRIPTOR[8:]


#: A boot mouse's whole configuration: configuration, interface, HID and one
#: interrupt-IN endpoint descriptor, 34 bytes -- more than one packet at 8, 16
#: or 32 and a short packet at each.
CONFIGURATION_34 = bytes(
    [9, 2, 34, 0, 1, 1, 0, 0xA0, 50]
    + [9, 4, 0, 0, 1, 3, 1, 2, 0]
    + [9, 0x21, 0x11, 0x01, 0, 1, 0x22, 52, 0]
    + [7, 5, 0x81, 3, 4, 0, 10]
)
assert len(CONFIGURATION_34) == 34

#: An exact multiple of 8, 16 and 32, so only a ZLP can end its data stage.
STRING_32 = bytes([32, 3]) + bytes(range(0x40, 0x40 + 30))

#: Longer than 64, so the fallback packet size is visible on the wire.
CONFIGURATION_70 = bytes([9, 2, 70, 0]) + bytes(range(66))


def _get_descriptor(value: int, length: int, index: int = 0) -> list[int]:
    return setup_packet(0x80, GET_DESCRIPTOR, value, index, length)


def _run(bench, *, descriptors, mps: int | None) -> None:
    top, sim = _make_top()

    async def wrapped(ctx) -> None:
        for dtype, index, w_index, data in descriptors:
            await seed_descriptor(
                ctx, top.store, dtype=dtype, index=index, w_index=w_index, data=data
            )
        await _power_up(ctx, top.bus, top.dut, mps=mps)
        await bench(ctx, top.bus, top.dut)

    sim.add_testbench(wrapped)
    sim.run()


def test_unconnected_packet_size_keeps_single_64_byte_packets() -> None:
    """Left unconnected, ep0_max_packet reproduces the pre-change behaviour.

    The diagnostic top never drives it, so its default has to be 64.
    """

    async def bench(ctx, bus, dut) -> None:
        packets = await control_in_packets(
            ctx, bus, setup_bytes=_get_descriptor(0x0100, 18), mps=64
        )
        assert_packets(packets, sizes=[18], data=DEVICE_DESCRIPTOR)

    _run(bench, descriptors=[(1, 0, 0, DEVICE_DESCRIPTOR)], mps=None)


@pytest.mark.parametrize(("mps", "sizes"), [(8, [8, 8, 2]), (16, [16, 2]), (32, [18]), (64, [18])])
def test_device_descriptor_is_split_at_its_own_bmaxpacketsize0(mps: int, sizes) -> None:
    descriptor = _device_descriptor(mps)

    async def bench(ctx, bus, dut) -> None:
        packets = await control_in_packets(
            ctx, bus, setup_bytes=_get_descriptor(0x0100, 18), mps=mps
        )
        assert_packets(packets, sizes=sizes, data=descriptor)

    _run(bench, descriptors=[(1, 0, 0, descriptor)], mps=mps)


@pytest.mark.parametrize(
    ("mps", "sizes"),
    [(8, [8, 8, 8, 8, 2]), (16, [16, 16, 2]), (32, [32, 2]), (64, [34])],
)
def test_configuration_descriptor_is_split_at_bmaxpacketsize0(mps: int, sizes) -> None:
    async def bench(ctx, bus, dut) -> None:
        packets = await control_in_packets(
            ctx, bus, setup_bytes=_get_descriptor(0x0200, 0xFF), mps=mps
        )
        assert_packets(packets, sizes=sizes, data=CONFIGURATION_34)

    _run(
        bench,
        descriptors=[(1, 0, 0, _device_descriptor(mps)), (2, 0, 0, CONFIGURATION_34)],
        mps=mps,
    )


@pytest.mark.parametrize(
    ("mps", "length", "sizes"),
    [
        (8, 9, [8, 1]),  # the host's customary first 9-byte configuration read
        (8, 16, [8, 8]),  # truncated exactly on a packet boundary
        (8, 12, [8, 4]),  # truncated mid-packet
        (16, 32, [16, 16]),
        (32, 20, [20]),
    ],
)
def test_wlength_shorter_than_descriptor_truncates(mps: int, length: int, sizes) -> None:
    async def bench(ctx, bus, dut) -> None:
        packets = await control_in_packets(
            ctx, bus, setup_bytes=_get_descriptor(0x0200, length), mps=mps
        )
        assert_packets(packets, sizes=sizes, data=CONFIGURATION_34[:length])

    _run(
        bench,
        descriptors=[(1, 0, 0, _device_descriptor(mps)), (2, 0, 0, CONFIGURATION_34)],
        mps=mps,
    )


@pytest.mark.parametrize("mps", [8, 16, 32, 64])
def test_exact_multiple_ends_with_a_zlp_when_the_host_asks_for_more(mps: int) -> None:
    """A data stage that ends exactly on a full packet has not ended.

    The host keeps asking until it sees a short packet [USB2.0 5.5.3], so a
    32-byte answer to wLength 255 needs a trailing ZLP -- carrying the next
    PID in the sequence, or the host takes it for a retry and drops it.
    """
    full = len(STRING_32) // mps
    sizes = [mps] * full + [0] if full else [len(STRING_32)]

    async def bench(ctx, bus, dut) -> None:
        packets = await control_in_packets(
            ctx, bus, setup_bytes=_get_descriptor(0x0301, 0xFF, 0x0409), mps=mps
        )
        assert_packets(packets, sizes=sizes, data=STRING_32)

    _run(
        bench,
        descriptors=[(1, 0, 0, _device_descriptor(mps)), (3, 1, 0x0409, STRING_32)],
        mps=mps,
    )


def test_descriptor_zlp_is_sent_once_not_repeated() -> None:
    """LUNA's transmitter never raises ``ready`` for a ZLP.

    It goes IDLE -> SEND_PID -> CRC with no payload state, so a ZLP is a
    one-cycle ``valid & last`` (LUNA's own send_zlp idiom). Holding it until
    ``ready`` kept it asserted, and the transmitter sent ZLP after ZLP into
    the host's status stage. At the default 64-byte EP0 that needed a
    descriptor of exactly 64 bytes; at 8 it is any whose length is a multiple
    of 8.
    """
    string_64 = bytes([64, 3]) + bytes(range(62))

    async def bench(ctx, bus, dut) -> None:
        packets = await control_in_packets(
            ctx, bus, setup_bytes=_get_descriptor(0x0301, 0xFF, 0x0409), mps=64
        )
        assert_packets(packets, sizes=[64, 0], data=string_64)

    _run(bench, descriptors=[(1, 0, 0, DEVICE_DESCRIPTOR), (3, 1, 0x0409, string_64)], mps=None)


@pytest.mark.parametrize("mps", [8, 16, 32])
def test_exact_multiple_needs_no_zlp_when_wlength_is_met(mps: int) -> None:
    """With wLength met the host goes straight to status; no extra IN comes."""

    async def bench(ctx, bus, dut) -> None:
        packets = await control_in_packets(
            ctx, bus, setup_bytes=_get_descriptor(0x0301, len(STRING_32), 0x0409), mps=mps
        )
        assert_packets(packets, sizes=[mps] * (len(STRING_32) // mps), data=STRING_32)

    _run(
        bench,
        descriptors=[(1, 0, 0, _device_descriptor(mps)), (3, 1, 0x0409, STRING_32)],
        mps=mps,
    )


def test_lost_ack_resends_the_same_packet_with_the_same_pid() -> None:
    """Only an ACK advances the data stage [USB2.0 8.6.3, 8.6.4].

    A host that never saw the packet, or whose ACK was lost, sends IN again and
    must get the identical bytes and PID. Advancing on the IN token instead
    would silently skip a packet's worth of the descriptor.
    """
    descriptor = _device_descriptor(8)

    async def bench(ctx, bus, dut) -> None:
        await pc_setup_transaction(ctx, bus, 0, _get_descriptor(0x0100, 18))
        for pid, chunk in ((DATA1, descriptor[0:8]), (DATA0, descriptor[8:16])):
            first = await pc_in_data_packet(ctx, bus, 0)
            assert first == (pid, chunk), f"expected {pid:#x} {chunk.hex()}, got {first!r}"
            # No ACK: the host retries the same IN.
            retry = await pc_in_data_packet(ctx, bus, 0)
            assert retry == first, f"retry changed the packet: {first!r} -> {retry!r}"
            await pc_send(ctx, bus, [ACK])
        last = await pc_in_data_packet(ctx, bus, 0)
        assert last == (DATA1, descriptor[16:18]), f"final packet: {last!r}"
        await pc_send(ctx, bus, [ACK])
        await pc_status_out(ctx, bus, 0)

    _run(bench, descriptors=[(1, 0, 0, descriptor)], mps=8)


def test_ack_for_another_endpoint_does_not_advance_a_descriptor() -> None:
    """LUNA hands every endpoint every ACK the host sends.

    A host that missed EP0 packet k may poll an interrupt endpoint before it
    retries, and ACK that report. Taking that ACK as EP0's advanced the
    descriptor and toggled its PID, so the retry carried packet k+1: a silent
    gap in the descriptor, on the PID the host expected.
    """
    descriptor = _device_descriptor(8)

    async def bench(ctx, bus, dut) -> None:
        await pc_setup_transaction(ctx, bus, 0, _get_descriptor(0x0100, 18))
        first = await pc_in_data_packet(ctx, bus, 0)
        assert first == (DATA1, descriptor[0:8]), first
        # No ACK for it. The host polls EP1 instead, and ACKs that report.
        await push_report(ctx, dut, 1, MOUSE_REPORT)
        report = await pc_in_transaction(ctx, bus, 0, 1)
        assert report[1:-2] == MOUSE_REPORT, f"EP1 did not return the report: {report!r}"
        await pc_send(ctx, bus, [ACK])

        retry = await pc_in_data_packet(ctx, bus, 0)
        assert retry == first, f"an EP1 ACK advanced EP0: {first!r} -> {retry!r}"
        await pc_send(ctx, bus, [ACK])
        assert await pc_in_data_packet(ctx, bus, 0) == (DATA0, descriptor[8:16])
        await pc_send(ctx, bus, [ACK])
        assert await pc_in_data_packet(ctx, bus, 0) == (DATA1, descriptor[16:18])
        await pc_send(ctx, bus, [ACK])
        await pc_status_out(ctx, bus, 0)

    _run(bench, descriptors=[(1, 0, 0, descriptor)], mps=8)


async def _abandon_after_one_packet(ctx, bus) -> None:
    """A GET_DESCRIPTOR left after its first packet is ACKed, with no status stage."""
    await pc_setup_transaction(ctx, bus, 0, _get_descriptor(0x0200, 0xFF))
    assert await pc_in_data_packet(ctx, bus, 0) == (DATA1, CONFIGURATION_34[:8])
    await pc_send(ctx, bus, [ACK])


def test_abandoned_get_descriptor_does_not_leak_into_the_next() -> None:
    """A new SETUP ends whatever the last transfer was doing [USB2.0 8.5.3].

    LUNA's standard handler resets its descriptor offset and data PID only in
    IDLE, and its GET_DESCRIPTOR state never looks at a new SETUP. Abandoned
    after one ACKed packet, both stayed advanced, and the next GET_DESCRIPTOR
    resumed at byte 8 on DATA0 -- a first packet the host drops as a duplicate.
    """
    descriptor = _device_descriptor(8)

    async def bench(ctx, bus, dut) -> None:
        await _abandon_after_one_packet(ctx, bus)
        packets = await control_in_packets(ctx, bus, setup_bytes=_get_descriptor(0x0100, 18), mps=8)
        assert_packets(packets, sizes=[8, 8, 2], data=descriptor)

    _run(bench, descriptors=[(1, 0, 0, descriptor), (2, 0, 0, CONFIGURATION_34)], mps=8)


def test_abandoned_get_descriptor_does_not_swallow_set_address() -> None:
    """Stuck in GET_DESCRIPTOR, the standard handler never dispatched SET_ADDRESS.

    Its status IN got GET_DESCRIPTOR's status reply -- a bare ACK handshake,
    not a valid answer to an IN token -- and the address never committed.
    """

    async def bench(ctx, bus, dut) -> None:
        await _abandon_after_one_packet(ctx, bus)
        await pc_setup_transaction(ctx, bus, 0, setup_packet(0x00, SET_ADDRESS, 5, 0, 0))
        status = await pc_in_transaction(ctx, bus, 0, 0)
        assert status[0] == DATA1, f"SET_ADDRESS status was not a DATA1 packet: {status!r}"
        assert status[1:-2] == [], "SET_ADDRESS status was not zero-length"
        committed = await pc_send_ack_watching(
            ctx, bus, dut.debug_address_changed, dut.debug_new_address
        )
        assert committed == 5, "SET_ADDRESS never committed"

    _run(
        bench,
        descriptors=[(1, 0, 0, _device_descriptor(8)), (2, 0, 0, CONFIGURATION_34)],
        mps=8,
    )


@pytest.mark.parametrize("illegal", [0, 12, 127])
def test_illegal_packet_size_falls_back_to_64(illegal: int) -> None:
    """Only 8, 16, 32 and 64 are legal bMaxPacketSize0 values [USB2.0 9.6.1].

    The enumerator refuses anything else, so this is defence in depth: a value
    that reached the clone anyway must not become the packet size.
    """

    async def bench(ctx, bus, dut) -> None:
        packets = await control_in_packets(
            ctx, bus, setup_bytes=_get_descriptor(0x0200, 0xFF), mps=64
        )
        assert_packets(packets, sizes=[64, 6], data=CONFIGURATION_70)

    _run(
        bench,
        descriptors=[(1, 0, 0, DEVICE_DESCRIPTOR), (2, 0, 0, CONFIGURATION_70)],
        mps=illegal,
    )


def _module_body(netlist: str, name: str) -> str:
    match = re.search(rf"(?ms)^module \\{re.escape(name)}\n(.*?)^end\n", netlist)
    assert match, f"module {name} not in the netlist"
    return match.group(1)


def _net(text: str) -> str:
    """A net reference with any whole-signal ``[msb:0]`` slice dropped."""
    return re.sub(r" \[\d+:0\]$", "", text.strip())


def _cell_port(body: str, cell: str, port: str) -> str:
    match = re.search(rf"(?ms)^  cell \S+ {re.escape(cell)}\n(.*?)^  end\n", body)
    assert match, f"cell {cell} not in the top module"
    ports = dict(re.findall(r"(?m)^    connect \\(\S+) (.+)$", match.group(1)))
    assert port in ports, f"cell {cell} has no port {port}"
    return _net(ports[port])


def test_top_level_feeds_the_clone_the_enumerated_packet_size() -> None:
    """The production top must drive the clone from the real device's EP0 size.

    Left unconnected the clone would packetise at 64 whatever byte 7 of the
    cloned device descriptor says -- the babble this whole path exists to stop.

    Checked on the netlist the bitstream build consumes, not in simulation: the
    top instantiates the ECP5 clock generator and the ULPI and PMOD I/O
    buffers, which Amaranth's simulator cannot run. The walk back from the
    clone's port tolerates aliases and registers, so retiming the wire across
    the die does not break it.
    """
    platform = CynthionPlatformRev1D4()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        netlist = platform.prepare(CynthionMouseHostTop()).files["top.il"]
    top = _module_body(netlist, "top")

    source = _cell_port(top, r"\host", "ep0_max_packet")
    drivers = {_net(lhs): _net(rhs) for lhs, rhs in re.findall(r"(?m)^connect (\S+) (.+)$", top)}
    for cell in re.finditer(r"(?ms)^  cell \$dff \S+\n(.*?)^  end\n", top):
        ports = dict(re.findall(r"(?m)^    connect \\(\S+) (.+)$", cell.group(1)))
        drivers[_net(ports["Q"])] = _net(ports["D"])
    # A register's D comes from a process: its first, unconditional assign is
    # the data path; the reset value sits in a switch below it.
    ref = r"\S+(?: \[[\d:]+\])?"
    for process in re.finditer(r"(?ms)^  process \S+\n(.*?)^  end\n", top):
        first = re.search(rf"(?m)^    assign ({ref}) ({ref})$", process.group(1))
        if first:
            drivers.setdefault(_net(first.group(1)), _net(first.group(2)))

    net = _cell_port(top, r"\device", "ep0_max_packet")
    path = [net]
    while net != source and net in drivers and len(path) < 8:
        net = drivers[net]
        path.append(net)
    assert net == source, f"clone ep0_max_packet traces to {path}, not the host's {source}"
