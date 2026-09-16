import importlib
import re

import pytest
from amaranth.back import rtlil
from amaranth.sim import Simulator

from hurra_cynthion.injection_wire import (
    INJ_TYPE_DESCRIPTOR_FRAGMENT,
    INJ_TYPE_IDLE,
    INJ_TYPE_RELATIVE,
    MESSAGE_TYPES,
    SOF,
    DescriptorFragmentPayload,
    RelativePayload,
    crc16_ccitt_false,
    pack_slot,
    unpack_slot,
)


def test_slot_master_uses_exact_byte_memories_and_addressed_payload_seams() -> None:
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(clock_hz=60_000_000, slot_cycles=7_500, sck_div=4)
    assert not hasattr(dut, "tx_payload")
    assert not hasattr(dut, "rx_payload")
    assert len(dut.tx_payload_request) == 1
    assert len(dut.tx_payload_address) == 5
    assert len(dut.tx_payload_response) == 1
    assert len(dut.tx_payload_data) == 8
    assert len(dut.rx_payload_address) == 5
    assert len(dut.rx_payload_data) == 8

    netlist = rtlil.convert(
        dut,
        ports=[
            dut.sck,
            dut.mosi,
            dut.miso,
            dut.cs_n,
            dut.mcu_ready,
            dut.tx_valid,
            dut.tx_ready,
            dut.tx_type,
            dut.tx_fill_start,
            dut.tx_payload_request,
            dut.tx_payload_address,
            dut.tx_payload_response,
            dut.tx_payload_data,
            dut.rx_valid,
            dut.rx_ready,
            dut.rx_type,
            dut.rx_sequence,
            dut.rx_payload_address,
            dut.rx_payload_read_enable,
            dut.rx_payload_data,
        ],
    )
    assert len(re.findall(r"(?m)^\s*memory width 8 size 32", netlist)) == 1
    assert len(re.findall(r"(?m)^\s*memory width 8 size 64", netlist)) == 1
    assert "tx_queued_frame" not in netlist
    assert "tx_shift" not in netlist
    assert "rx_shift" not in netlist


def _spi_slot_master_type():
    try:
        module = importlib.import_module("hurra_cynthion.spi_link")
    except ModuleNotFoundError:
        return None
    return getattr(module, "SPISlotMaster", None)


def _wire_bits(slot: bytes) -> list[int]:
    return [(byte >> bit) & 1 for byte in slot for bit in range(7, -1, -1)]


def _wire_bytes(bits: list[int]) -> bytes:
    assert len(bits) == 256
    result = bytearray()
    for offset in range(0, len(bits), 8):
        byte = 0
        for bit in bits[offset : offset + 8]:
            byte = (byte << 1) | bit
        result.append(byte)
    return bytes(result)


def _with_valid_crc(slot: bytes) -> bytes:
    result = bytearray(slot)
    result[-2:] = crc16_ccitt_false(result[:-2]).to_bytes(2, "little")
    return bytes(result)


async def _queue_message(
    ctx,
    dut,
    type_: int,
    payload: bytes,
    *,
    response_latency: int = 1,
) -> None:
    assert response_latency >= 1
    ctx.set(dut.tx_type, type_)
    ctx.set(dut.tx_valid, 1)
    ctx.set(dut.tx_payload_response, 0)
    fill_started = False
    pending_address = None
    response_countdown = 0
    for _ in range(512):
        ctx.set(dut.tx_payload_response, 0)
        if pending_address is not None:
            if response_countdown == 0:
                ctx.set(dut.tx_payload_data, payload[pending_address])
                ctx.set(dut.tx_payload_response, 1)
                pending_address = None
            else:
                response_countdown -= 1

        if ctx.get(dut.tx_payload_request):
            assert pending_address is None
            pending_address = ctx.get(dut.tx_payload_address)
            response_countdown = response_latency - 1

        fill_started |= bool(ctx.get(dut.tx_fill_start))
        if fill_started and ctx.get(dut.tx_ready):
            await ctx.tick("usb")
            ctx.set(dut.tx_valid, 0)
            ctx.set(dut.tx_payload_response, 0)
            return
        await ctx.tick("usb")
    raise AssertionError("TX frame fill did not complete")


async def _read_rx_payload(ctx, dut) -> bytes:
    payload = bytearray()
    ctx.set(dut.rx_payload_read_enable, 1)
    for address in range(26):
        ctx.set(dut.rx_payload_address, address)
        await ctx.tick("usb")
        payload.append(ctx.get(dut.rx_payload_data))
    ctx.set(dut.rx_payload_read_enable, 0)
    return bytes(payload)


async def _run_transfer(
    ctx, dut, return_slot: bytes, *, limit: int, tv_so_cycles: int = 0
) -> tuple[bytes, list]:
    """Drive one slot, modelling the slave's MISO valid delay.

    ``tv_so_cycles`` defers presenting each return bit by that many ``usb``
    cycles after the SCK falling edge that enables it. The CH32 specifies
    ``tV(SO)`` max 25 ns and ``th(SO)`` min 15 ns after that edge, so at
    60 MHz (16.67 ns/cycle) the legal window is roughly 1 to 2 cycles.
    The default 0 preserves the zero-delay slave every existing caller uses.
    """
    return_bits = _wire_bits(return_slot)
    return_index = 0
    transmitted_bits = []
    edges = []
    previous_cs = ctx.get(dut.cs_n)
    previous_sck = ctx.get(dut.sck)
    previous_mosi = ctx.get(dut.mosi)
    ctx.set(dut.miso, return_bits[0])
    # (cycle_due, bit) entries waiting out the slave's tV(SO).
    deferred: list[tuple[int, int]] = []

    for cycle in range(limit):
        await ctx.tick("usb")
        for due, bit in [entry for entry in deferred if entry[0] <= cycle]:
            ctx.set(dut.miso, bit)
            deferred.remove((due, bit))
        cs = ctx.get(dut.cs_n)
        sck = ctx.get(dut.sck)
        mosi = ctx.get(dut.mosi)

        if previous_cs and not cs:
            assert sck == 0
        if not cs and sck != previous_sck:
            direction = "rising" if sck else "falling"
            edges.append((cycle, direction))
            if direction == "rising":
                transmitted_bits.append(mosi)
            else:
                return_index += 1
                if return_index < len(return_bits):
                    if tv_so_cycles:
                        deferred.append((cycle + tv_so_cycles, return_bits[return_index]))
                    else:
                        ctx.set(dut.miso, return_bits[return_index])
        if not cs and mosi != previous_mosi:
            # MOSI is preloaded as CS falls, then changes only on falling SCK.
            assert previous_cs or (previous_sck == 1 and sck == 0)
        if not previous_cs and cs:
            assert sck == 0
            return _wire_bytes(transmitted_bits), edges

        previous_cs = cs
        previous_sck = sck
        previous_mosi = mosi

    raise AssertionError("SPI transfer did not complete within its bounded budget")


def test_slots_have_exact_cadence_clock_count_and_mode_zero_edges() -> None:
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(clock_hz=60_000_000, slot_cycles=7_500, sck_div=4)
    simulation = Simulator(dut)
    simulation.add_clock(1 / 60_000_000, domain="usb")

    payload = DescriptorFragmentPayload(
        descriptor_generation=3,
        interface_number=2,
        offset=0,
        total=3,
        data=b"abc" + bytes(15),
    ).to_bytes()

    async def bench(ctx):
        assert ctx.get(dut.cs_n)
        assert not ctx.get(dut.sck)
        ctx.set(dut.mcu_ready, 1)
        await _queue_message(ctx, dut, INJ_TYPE_DESCRIPTOR_FRAGMENT, payload)

        cycle = 0
        starts = []
        first_edges = []
        first_bits = []
        previous_sck = ctx.get(dut.sck)
        tracking_first = False
        while len(starts) < 2:
            await ctx.tick("usb")
            cycle += 1
            if ctx.get(dut.slot_start):
                starts.append(cycle)
                if len(starts) == 1:
                    tracking_first = True
            sck = ctx.get(dut.sck)
            if tracking_first and not ctx.get(dut.cs_n) and sck != previous_sck:
                first_edges.append((cycle, "rising" if sck else "falling"))
                if sck:
                    first_bits.append(ctx.get(dut.mosi))
            if tracking_first and ctx.get(dut.cs_n) and first_edges:
                tracking_first = False
                assert not sck
            previous_sck = sck

        assert starts[1] - starts[0] == 7_500
        assert len(first_edges) == 512
        assert [kind for _, kind in first_edges[::2]] == ["rising"] * 256
        assert [kind for _, kind in first_edges[1::2]] == ["falling"] * 256
        assert all(
            later[0] - earlier[0] == 2
            for earlier, later in zip(first_edges, first_edges[1:], strict=False)
        )
        assert _wire_bytes(first_bits) == pack_slot(INJ_TYPE_DESCRIPTOR_FRAGMENT, 0, payload)

    simulation.add_testbench(bench)
    simulation.run()


def test_transmit_bytes_match_pack_slot_for_idle_and_every_message_type() -> None:
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(clock_hz=60_000_000, slot_cycles=1_200, sck_div=4)
    simulation = Simulator(dut)
    simulation.add_clock(1 / 60_000_000, domain="usb")

    async def bench(ctx):
        ctx.set(dut.mcu_ready, 1)
        idle_tx, _ = await _run_transfer(ctx, dut, pack_slot(INJ_TYPE_IDLE, 0, b""), limit=3_000)
        assert idle_tx == pack_slot(INJ_TYPE_IDLE, 0, b"")

        sequence = 0
        for type_ in MESSAGE_TYPES.values():
            if type_ == INJ_TYPE_IDLE:
                continue
            payload = bytes((type_ + index * 17) & 0xFF for index in range(26))
            await _queue_message(ctx, dut, type_, payload)
            transmitted, _ = await _run_transfer(
                ctx, dut, pack_slot(INJ_TYPE_IDLE, 0, b""), limit=2_000
            )
            expected = _with_valid_crc(bytes((SOF, type_, sequence, 26)) + payload + bytes(2))
            assert transmitted == expected
            sequence = (sequence + 1) & 0xFF

    simulation.add_testbench(bench)
    simulation.run()


def test_message_queued_during_active_transfer_is_ready_for_next_slot() -> None:
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(clock_hz=60_000_000, slot_cycles=1_200, sck_div=4)
    simulation = Simulator(dut)
    simulation.add_clock(1 / 60_000_000, domain="usb")

    payload = DescriptorFragmentPayload(
        descriptor_generation=0x1234,
        interface_number=2,
        offset=7,
        total=9,
        data=bytes(range(18)),
    ).to_bytes()
    idle_slot = pack_slot(INJ_TYPE_IDLE, 0, b"")

    async def producer(ctx):
        while not ctx.get(dut.busy):
            await ctx.tick("usb")
        await _queue_message(ctx, dut, INJ_TYPE_DESCRIPTOR_FRAGMENT, payload)

    async def bench(ctx):
        ctx.set(dut.mcu_ready, 1)
        first_tx, _ = await _run_transfer(ctx, dut, idle_slot, limit=3_000)
        assert first_tx == idle_slot
        second_tx, _ = await _run_transfer(ctx, dut, idle_slot, limit=2_000)
        assert second_tx == pack_slot(INJ_TYPE_DESCRIPTOR_FRAGMENT, 0, payload)

    simulation.add_testbench(producer)
    simulation.add_testbench(bench)
    simulation.run()


def test_aborted_fill_ignores_stale_response_and_never_queues_partial_frame() -> None:
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(clock_hz=60_000_000, slot_cycles=1_200, sck_div=4)
    simulation = Simulator(dut)
    simulation.add_clock(1 / 60_000_000, domain="usb")

    payload = bytes((index * 11) & 0xFF for index in range(26))
    idle_slot = pack_slot(INJ_TYPE_IDLE, 0, b"")

    async def bench(ctx):
        ctx.set(dut.mcu_ready, 1)
        ctx.set(dut.tx_type, INJ_TYPE_DESCRIPTOR_FRAGMENT)
        ctx.set(dut.tx_valid, 1)
        ctx.set(dut.tx_payload_response, 0)

        for _ in range(32):
            if ctx.get(dut.tx_payload_request):
                stale_address = ctx.get(dut.tx_payload_address)
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("payload request never started")

        ctx.set(dut.tx_valid, 0)
        await ctx.tick("usb")
        ctx.set(dut.tx_payload_data, payload[stale_address])
        ctx.set(dut.tx_payload_response, 1)
        await ctx.tick("usb")
        ctx.set(dut.tx_payload_response, 0)
        for _ in range(64):
            assert not ctx.get(dut.tx_ready)
            await ctx.tick("usb")

        transmitted, _ = await _run_transfer(ctx, dut, idle_slot, limit=3_000)
        assert transmitted == idle_slot

        await _queue_message(
            ctx,
            dut,
            INJ_TYPE_DESCRIPTOR_FRAGMENT,
            payload,
            response_latency=3,
        )
        transmitted, _ = await _run_transfer(ctx, dut, idle_slot, limit=2_000)
        assert transmitted == pack_slot(INJ_TYPE_DESCRIPTOR_FRAGMENT, 0, payload)

    simulation.add_testbench(bench)
    simulation.run()


@pytest.mark.parametrize("ready_offset", (-2, -1, 0))
def test_frame_fill_at_slot_boundary_never_uses_stale_first_ram_byte(
    ready_offset: int,
) -> None:
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(clock_hz=60_000_000, slot_cycles=7_500, sck_div=4)
    simulation = Simulator(dut)
    simulation.add_clock(1 / 60_000_000, domain="usb")

    payload = DescriptorFragmentPayload(
        descriptor_generation=0x1234,
        interface_number=2,
        offset=7,
        total=9,
        data=bytes(range(18)),
    ).to_bytes()
    idle_slot = pack_slot(INJ_TYPE_IDLE, 0, b"")
    message_slot = pack_slot(INJ_TYPE_DESCRIPTOR_FRAGMENT, 0, payload)

    async def producer(ctx):
        while not ctx.get(dut.slot_start):
            await ctx.tick("usb")
        # A fill-start handshake takes 59 clocks through one-cycle addressed
        # payload responses and the safe-queue ready
        # pulse. Place that pulse exactly at, one before, or two before the
        # following production slot boundary.
        for _ in range(dut.slot_cycles - 59 + ready_offset):
            await ctx.tick("usb")
        await _queue_message(ctx, dut, INJ_TYPE_DESCRIPTOR_FRAGMENT, payload)

    async def observer(ctx):
        ctx.set(dut.mcu_ready, 1)
        transmitted = [(await _run_transfer(ctx, dut, idle_slot, limit=9_000))[0] for _ in range(3)]
        if ready_offset == 0:
            assert transmitted == [idle_slot, idle_slot, message_slot]
        else:
            assert transmitted == [idle_slot, message_slot, idle_slot]

    simulation.add_testbench(producer)
    simulation.add_testbench(observer)
    simulation.run()


def test_not_ready_sends_idle_keeps_queued_message_and_discards_return() -> None:
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(clock_hz=60_000_000, slot_cycles=1_200, sck_div=4)
    simulation = Simulator(dut)
    simulation.add_clock(1 / 60_000_000, domain="usb")

    tx_payload = DescriptorFragmentPayload(
        descriptor_generation=1,
        interface_number=0,
        offset=0,
        total=1,
        data=b"x" + bytes(17),
    ).to_bytes()
    rx_payload = RelativePayload(
        lease_generation=1,
        map_generation=2,
        command_sequence=3,
        target_frame=4,
        interface_number=0,
        endpoint_number=1,
        report_id=0,
        flags=0,
        x=0,
        y=0,
        wheel=0,
        pan=0,
        hold_reports=0,
    ).to_bytes()
    return_slot = pack_slot(INJ_TYPE_RELATIVE, 9, rx_payload)

    async def bench(ctx):
        ctx.set(dut.rx_ready, 0)
        ctx.set(dut.mcu_ready, 0)
        await _queue_message(ctx, dut, INJ_TYPE_DESCRIPTOR_FRAGMENT, tx_payload)

        first_tx, first_edges = await _run_transfer(ctx, dut, return_slot, limit=3_000)
        assert len(first_edges) == 512
        assert unpack_slot(first_tx).type_ == 0
        for _ in range(4):
            await ctx.tick("usb")
        assert not ctx.get(dut.rx_valid)
        assert not ctx.get(dut.tx_ready), "queued message was consumed while MCU_READY was low"

        ctx.set(dut.mcu_ready, 1)
        second_tx, _ = await _run_transfer(ctx, dut, return_slot, limit=2_000)
        assert unpack_slot(second_tx).type_ == INJ_TYPE_DESCRIPTOR_FRAGMENT
        for _ in range(4):
            await ctx.tick("usb")
        assert ctx.get(dut.rx_valid)
        assert ctx.get(dut.rx_type) == INJ_TYPE_RELATIVE
        assert ctx.get(dut.rx_sequence) == 9
        assert await _read_rx_payload(ctx, dut) == rx_payload

    simulation.add_testbench(bench)
    simulation.run()


def test_message_is_retained_while_mcu_ready_is_low() -> None:
    # ``send_message = mcu_ready & tx_queued`` and the frame is dequeued at the end
    # of a transfer, so a wrongly-high MCU_READY clocks queued frames into the void
    # with no retry. The FPGA-side guarantee that makes the PMOD-A pull-down
    # sufficient is this one: while ready is low the frame is *held*, not dropped.
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(clock_hz=60_000_000, slot_cycles=1_200, sck_div=4)
    simulation = Simulator(dut)
    simulation.add_clock(1 / 60_000_000, domain="usb")

    tx_payload = DescriptorFragmentPayload(
        descriptor_generation=1,
        interface_number=0,
        offset=0,
        total=1,
        data=b"z" + bytes(17),
    ).to_bytes()
    return_slot = pack_slot(INJ_TYPE_IDLE, 0, b"")

    async def bench(ctx):
        ctx.set(dut.rx_ready, 0)
        ctx.set(dut.mcu_ready, 0)
        await _queue_message(ctx, dut, INJ_TYPE_DESCRIPTOR_FRAGMENT, tx_payload)

        # Several slots, not just one: the frame must survive every one of them.
        for slot in range(4):
            transmitted, edges = await _run_transfer(ctx, dut, return_slot, limit=3_000)
            assert len(edges) == 512, slot
            assert unpack_slot(transmitted).type_ == INJ_TYPE_IDLE, slot
            assert not ctx.get(dut.tx_ready), f"queued frame consumed in slot {slot}"

        # The queue is still occupied, so a second frame cannot be filled into it.
        with pytest.raises(AssertionError, match="TX frame fill did not complete"):
            await _queue_message(ctx, dut, INJ_TYPE_DESCRIPTOR_FRAGMENT, tx_payload)
        ctx.set(dut.tx_valid, 0)

        # That attempt ran out mid-slot; resynchronise to a slot boundary.
        while ctx.get(dut.cs_n) == 0:
            await ctx.tick("usb")

        # Once the MCU raises ready, the original frame goes out intact.
        ctx.set(dut.mcu_ready, 1)
        transmitted, _ = await _run_transfer(ctx, dut, return_slot, limit=3_000)
        slot_frame = unpack_slot(transmitted)
        assert slot_frame.type_ == INJ_TYPE_DESCRIPTOR_FRAGMENT
        assert slot_frame.payload == tx_payload

    simulation.add_testbench(bench)
    simulation.run()


def test_crc_invalid_return_slot_never_reaches_rx_queue() -> None:
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(clock_hz=60_000_000, slot_cycles=1_200, sck_div=4)
    simulation = Simulator(dut)
    simulation.add_clock(1 / 60_000_000, domain="usb")

    payload = RelativePayload(
        lease_generation=1,
        map_generation=2,
        command_sequence=3,
        target_frame=4,
        interface_number=0,
        endpoint_number=1,
        report_id=0,
        flags=0,
        x=0,
        y=0,
        wheel=0,
        pan=0,
        hold_reports=0,
    ).to_bytes()
    valid_slot = pack_slot(INJ_TYPE_RELATIVE, 0x55, payload)
    invalid_slot = bytearray(valid_slot)
    invalid_slot[12] ^= 0x80

    async def bench(ctx):
        ctx.set(dut.mcu_ready, 1)
        ctx.set(dut.rx_ready, 0)
        await _run_transfer(ctx, dut, bytes(invalid_slot), limit=3_000)
        for _ in range(4):
            await ctx.tick("usb")
        assert not ctx.get(dut.rx_valid)
        assert ctx.get(dut.bad_crc_count) == 1

        await _run_transfer(ctx, dut, valid_slot, limit=2_000)
        for _ in range(4):
            await ctx.tick("usb")
        assert ctx.get(dut.rx_valid)
        assert ctx.get(dut.rx_type) == INJ_TYPE_RELATIVE
        assert ctx.get(dut.rx_sequence) == 0x55
        assert await _read_rx_payload(ctx, dut) == payload

        await _run_transfer(ctx, dut, valid_slot, limit=2_000)
        for _ in range(4):
            await ctx.tick("usb")
        assert ctx.get(dut.rx_queue_full_count) == 1

    simulation.add_testbench(bench)
    simulation.run()


def test_receive_validation_attributes_each_header_or_crc_error() -> None:
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(clock_hz=60_000_000, slot_cycles=1_200, sck_div=4)
    simulation = Simulator(dut)
    simulation.add_clock(1 / 60_000_000, domain="usb")

    valid = pack_slot(INJ_TYPE_RELATIVE, 7, bytes(26))
    bad_sof = bytearray(valid)
    bad_sof[0] = SOF ^ 0xFF
    bad_type = bytearray(valid)
    bad_type[1] = 0x7F
    bad_length = bytearray(valid)
    bad_length[3] = 25
    bad_crc = bytearray(valid)
    bad_crc[12] ^= 0x80
    cases = (
        (_with_valid_crc(bytes(bad_sof)), "bad_sof_count"),
        (_with_valid_crc(bytes(bad_type)), "bad_type_count"),
        (_with_valid_crc(bytes(bad_length)), "bad_length_count"),
        (bytes(bad_crc), "bad_crc_count"),
    )

    async def bench(ctx):
        ctx.set(dut.mcu_ready, 1)
        ctx.set(dut.rx_ready, 0)
        expected = {
            "bad_sof_count": 0,
            "bad_type_count": 0,
            "bad_length_count": 0,
            "bad_crc_count": 0,
        }
        for return_slot, counter_name in cases:
            await _run_transfer(ctx, dut, return_slot, limit=3_000)
            for _ in range(4):
                await ctx.tick("usb")
            expected[counter_name] += 1
            for name, value in expected.items():
                assert ctx.get(getattr(dut, name)) == value
            assert not ctx.get(dut.rx_valid)

    simulation.add_testbench(bench)
    simulation.run()


def test_held_receive_queue_survives_invalid_idle_and_full_then_replaces_on_consume() -> None:
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(clock_hz=60_000_000, slot_cycles=1_200, sck_div=4)
    simulation = Simulator(dut)
    simulation.add_clock(1 / 60_000_000, domain="usb")

    old_payload = bytes(range(26))
    dropped_payload = bytes(reversed(range(26)))
    replacement_payload = bytes((index * 7) & 0xFF for index in range(26))
    old_slot = pack_slot(INJ_TYPE_RELATIVE, 0x20, old_payload)
    dropped_slot = pack_slot(INJ_TYPE_RELATIVE, 0x21, dropped_payload)
    replacement_slot = pack_slot(INJ_TYPE_DESCRIPTOR_FRAGMENT, 0x22, replacement_payload)
    invalid_slot = bytearray(dropped_slot)
    invalid_slot[10] ^= 0x01
    idle_slot = pack_slot(INJ_TYPE_IDLE, 0, b"")

    async def assert_old(ctx):
        assert ctx.get(dut.rx_valid)
        assert ctx.get(dut.rx_type) == INJ_TYPE_RELATIVE
        assert ctx.get(dut.rx_sequence) == 0x20
        assert await _read_rx_payload(ctx, dut) == old_payload

    async def bench(ctx):
        ctx.set(dut.mcu_ready, 1)
        ctx.set(dut.rx_ready, 0)

        await _run_transfer(ctx, dut, old_slot, limit=3_000)
        for _ in range(4):
            await ctx.tick("usb")
        await assert_old(ctx)

        await _run_transfer(ctx, dut, bytes(invalid_slot), limit=2_000)
        for _ in range(4):
            await ctx.tick("usb")
        await assert_old(ctx)

        await _run_transfer(ctx, dut, idle_slot, limit=2_000)
        for _ in range(4):
            await ctx.tick("usb")
        await assert_old(ctx)

        await _run_transfer(ctx, dut, dropped_slot, limit=2_000)
        for _ in range(4):
            await ctx.tick("usb")
        assert ctx.get(dut.rx_queue_full_count) == 1
        await assert_old(ctx)

        await _run_transfer(ctx, dut, replacement_slot, limit=2_000)
        ctx.set(dut.rx_ready, 1)
        await ctx.tick("usb")
        ctx.set(dut.rx_ready, 0)
        for _ in range(2):
            await ctx.tick("usb")
        assert ctx.get(dut.rx_valid)
        assert ctx.get(dut.rx_type) == INJ_TYPE_DESCRIPTOR_FRAGMENT
        assert ctx.get(dut.rx_sequence) == 0x22
        assert await _read_rx_payload(ctx, dut) == replacement_payload

    simulation.add_testbench(bench)
    simulation.run()


def test_each_sof_edge_produces_one_usb_sync_pulse() -> None:
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(clock_hz=60_000_000, slot_cycles=7_500, sck_div=4)
    simulation = Simulator(dut)
    simulation.add_clock(1 / 60_000_000, domain="usb")

    async def bench(ctx):
        pulses = 0
        ctx.set(dut.sof_tick, 1)
        for _ in range(3):
            if ctx.get(dut.usb_sync):
                pulses += 1
            await ctx.tick("usb")
        ctx.set(dut.sof_tick, 0)
        await ctx.tick("usb")
        ctx.set(dut.sof_tick, 1)
        for _ in range(2):
            if ctx.get(dut.usb_sync):
                pulses += 1
            await ctx.tick("usb")
        assert pulses == 2

    simulation.add_testbench(bench)
    simulation.run()


_RELATIVE_PAYLOAD = RelativePayload(
    lease_generation=1,
    map_generation=1,
    command_sequence=2,
    target_frame=0,
    interface_number=0,
    endpoint_number=1,
    report_id=0,
    flags=0,
    x=5,
    y=-3,
    wheel=0,
    pan=0,
    hold_reports=0,
)


def _valid_return_slot() -> bytes:
    payload = _RELATIVE_PAYLOAD.to_bytes()
    return pack_slot(INJ_TYPE_RELATIVE, 9, payload)


def _decode_with_slave_delay(tv_so_cycles: int) -> tuple[bool, int, bytes]:
    """Run one full return slot against a slave with the given tV(SO) delay."""
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(clock_hz=60_000_000, slot_cycles=1_200, sck_div=4)
    simulation = Simulator(dut)
    simulation.add_clock(1 / 60_000_000, domain="usb")
    result = {}

    async def bench(ctx):
        ctx.set(dut.mcu_ready, 1)
        ctx.set(dut.rx_ready, 0)
        await _run_transfer(ctx, dut, _valid_return_slot(), limit=3_000, tv_so_cycles=tv_so_cycles)
        for _ in range(4):
            await ctx.tick("usb")
        result["rx_valid"] = bool(ctx.get(dut.rx_valid))
        result["bad_sof_count"] = ctx.get(dut.bad_sof_count)
        result["payload"] = await _read_rx_payload(ctx, dut) if result["rx_valid"] else b""

    simulation.add_testbench(bench)
    simulation.run()
    return result["rx_valid"], result["bad_sof_count"], result["payload"]


def test_return_frame_decodes_with_a_spec_maximum_tv_so_slave() -> None:
    # The CH32 guarantees MISO old only until th(SO) = 15 ns and new only from
    # tV(SO) = 25 ns after the enabling (falling) edge. Sampling at 16.67 ns --
    # one usb cycle -- is squarely inside that uncertainty band. Two cycles
    # (33.3 ns) covers the spec maximum.
    rx_valid, bad_sof_count, payload = _decode_with_slave_delay(2)
    assert (
        rx_valid
    ), f"a spec-conforming slave's frame did not decode: bad_sof_count={bad_sof_count}"
    expected = _RELATIVE_PAYLOAD.to_bytes()
    assert payload == expected
    assert bad_sof_count == 0


def test_zero_delay_slave_still_decodes() -> None:
    # The model every other test in this file uses; proves the phase move did
    # not merely trade one violation for another at the early edge.
    rx_valid, bad_sof_count, payload = _decode_with_slave_delay(0)
    assert rx_valid, f"zero-delay slave frame did not decode: bad_sof_count={bad_sof_count}"
    assert payload == _RELATIVE_PAYLOAD.to_bytes()


def test_slave_presenting_at_the_latching_edge_does_not_decode() -> None:
    # The upper bound of the tV(SO) budget, which the three passing cases above
    # do not pin. The latching edge is at t = 50.0 ns (three usb cycles), so a
    # slave that only becomes valid *at* that edge has zero setup and must
    # fail. Measured: 0/1/2 cycles decode, 3 cycles does not.
    #
    # This matters for MCU selection, because the quotable budget is the last
    # PASSING value -- 33.3 ns -- not the 50.0 ns edge position. An MCU whose
    # data-valid-after-clock spec sits between the two has no setup margin at
    # all. Pinning the boundary here means a gateware change that moves the
    # sample phase is caught in CI rather than on a bench.
    rx_valid, _bad_sof_count, _payload = _decode_with_slave_delay(3)
    assert not rx_valid, (
        "a slave presenting MISO at the latching edge decoded; the sample phase "
        "moved and the tV(SO) budget is no longer 33.3 ns"
    )


def test_th_so_minimum_slave_still_decodes() -> None:
    # A slave changing MISO as early as th(SO) allows: 1 cycle = 16.67 ns,
    # just past the 15 ns minimum. With the previous two tests this brackets
    # the whole legal tV(SO)/th(SO) window.
    rx_valid, bad_sof_count, payload = _decode_with_slave_delay(1)
    assert rx_valid, f"th(SO)-minimum slave frame did not decode: bad_sof_count={bad_sof_count}"
    assert payload == _RELATIVE_PAYLOAD.to_bytes()


@pytest.mark.parametrize("offset", [0, 1, 2, 3])
def test_miso_is_sampled_while_sck_is_high(offset: int) -> None:
    # Present each return bit during exactly one usb-cycle offset of its
    # 4-cycle bit period and hold the complement at every other offset. The
    # frame decodes only at the offset the master actually samples. SCK is
    # high during offsets 2 and 3; the sample must land on 2, the first cycle
    # of the SCK-high window, farthest from both of the slave's data changes.
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(clock_hz=60_000_000, slot_cycles=1_200, sck_div=4)
    simulation = Simulator(dut)
    simulation.add_clock(1 / 60_000_000, domain="usb")
    return_bits = _wire_bits(_valid_return_slot())
    observed = {}

    async def bench(ctx):
        ctx.set(dut.mcu_ready, 1)
        ctx.set(dut.rx_ready, 0)
        phase = None
        bit_index = 0
        sck_high = []
        previous_cs = ctx.get(dut.cs_n)
        previous_sck = ctx.get(dut.sck)
        for _ in range(3_000):
            await ctx.tick("usb")
            cs = ctx.get(dut.cs_n)
            sck = ctx.get(dut.sck)
            if not previous_cs and cs:
                break
            if not cs and phase is None:
                # Anchor on the first SCK rise, not on CS falling: SCK edges
                # define the bit period, and the CS-to-first-edge distance is
                # the configurable tSU(NSS) guard band. Bit 0 is presented
                # unconditionally until then, so discrimination comes from
                # bits 1..255 rather than from the lead-in.
                ctx.set(dut.miso, return_bits[0])
                if sck and not previous_sck:
                    phase, bit_index = 2, 0
            elif not cs:
                phase = (phase + 1) % 4
                if phase == 0:
                    bit_index += 1
            previous_cs, previous_sck = cs, sck
            if phase is None or bit_index >= len(return_bits):
                continue
            bit = return_bits[bit_index]
            ctx.set(dut.miso, bit if phase == offset else bit ^ 1)
            if phase in (2, 3):
                sck_high.append(bool(ctx.get(dut.sck)))
        for _ in range(4):
            await ctx.tick("usb")
        observed["rx_valid"] = bool(ctx.get(dut.rx_valid))
        observed["bad_sof_count"] = ctx.get(dut.bad_sof_count)
        observed["sck_high"] = all(sck_high) and bool(sck_high)

    simulation.add_testbench(bench)
    simulation.run()

    assert observed["sck_high"], "offsets 2 and 3 are not the SCK-high half of the bit period"
    if offset == 2:
        assert observed["rx_valid"], (
            "MISO is not sampled at offset 2 (first SCK-high cycle); "
            f"bad_sof_count={observed['bad_sof_count']}"
        )
    else:
        assert not observed["rx_valid"], f"MISO was sampled at offset {offset}, expected 2"


def test_miso_pad_has_a_single_registered_consumer() -> None:
    # The eight downstream capture points must read one registered net, not
    # the pad. Eight flip-flops each sampling an unsynchronized pad can
    # resolve a metastable bit differently, letting the CRC validate a byte
    # that was stored differently in RX RAM.
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(clock_hz=60_000_000, slot_cycles=1_200, sck_div=4)
    netlist = rtlil.convert(dut, ports=[dut.miso, dut.mosi, dut.sck, dut.cs_n])
    pad_references = len(re.findall(r"\\miso\b", netlist))
    registered_references = len(re.findall(r"\\miso_q\b", netlist))
    # The pad appears exactly twice: its port declaration and the single
    # assignment feeding the register. Every capture point reads miso_q.
    assert pad_references == 2, (
        f"the miso pad has {pad_references} netlist references; it must appear only as a port "
        "and as the source of one register, or a future change has reintroduced a raw-pad reader"
    )
    assert registered_references > pad_references, "miso_q is not the net the capture points read"


def _slot_edge_geometry(cs_hold_cycles: int, *, cs_setup_cycles: int = 3) -> dict:
    """Instrument one full slot and return its CS/SCK edge geometry, in cycles."""
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(
        clock_hz=60_000_000,
        slot_cycles=1_200,
        sck_div=4,
        cs_hold_cycles=cs_hold_cycles,
        cs_setup_cycles=cs_setup_cycles,
    )
    simulation = Simulator(dut)
    simulation.add_clock(1 / 60_000_000, domain="usb")
    observed = {}

    async def bench(ctx):
        ctx.set(dut.mcu_ready, 1)
        cs_fall = cs_rise = first_rise = last_fall = None
        sck_during_hold = []
        previous_cs = ctx.get(dut.cs_n)
        previous_sck = ctx.get(dut.sck)
        for cycle in range(3_000):
            await ctx.tick("usb")
            cs = ctx.get(dut.cs_n)
            sck = ctx.get(dut.sck)
            if previous_cs and not cs:
                cs_fall = cycle
            if not cs and sck and not previous_sck and first_rise is None:
                first_rise = cycle
            if not cs and not sck and previous_sck:
                # Restart the collection on every falling edge, so what
                # survives is only what followed the *last* one.
                last_fall = cycle
                sck_during_hold = []
            elif last_fall is not None and cs_rise is None and not cs:
                sck_during_hold.append(sck)
            if not previous_cs and cs and cs_fall is not None:
                cs_rise = cycle
                break
            previous_cs, previous_sck = cs, sck
        observed.update(
            cs_fall=cs_fall,
            cs_rise=cs_rise,
            first_rise=first_rise,
            last_fall=last_fall,
            sck_during_hold=sck_during_hold,
        )

    simulation.add_testbench(bench)
    simulation.run()
    assert observed["cs_rise"] is not None, "the slot never completed"
    return observed


@pytest.mark.parametrize("cs_hold_cycles", [1, 2, 3, 5])
def test_cs_n_is_held_for_the_configured_hold_after_the_last_sck_edge(
    cs_hold_cycles: int,
) -> None:
    # The CH32 needs th(NSS) >= 2*tHCLK measured from the last SCK edge
    # (Table 3-26; Figures 3-10/3-11 place the arrow after the SCK burst).
    # For CPOL=0 that is the final falling edge -- the pessimistic reading,
    # which the default 3 cycles (50.00 ns) satisfies for any HCLK >= 40 MHz.
    geometry = _slot_edge_geometry(cs_hold_cycles)
    held = geometry["cs_rise"] - geometry["last_fall"]
    assert held == cs_hold_cycles, (
        f"CS_N held {held} usb cycle(s) ({held * 1000 / 60:.2f} ns) after the last SCK fall, "
        f"need {cs_hold_cycles} ({cs_hold_cycles * 1000 / 60:.2f} ns)"
    )


@pytest.mark.parametrize("cs_setup_cycles", [2, 3, 5])
def test_cs_n_setup_before_the_first_sck_edge_is_the_configured_width(
    cs_setup_cycles: int,
) -> None:
    # tSU(NSS) >= 2*tHCLK, same requirement as the hold side (Table 3-26).
    # Two cycles (33.33 ns) satisfies it at 100 MHz (1.67x) and at 70 MHz
    # (1.17x), but 1.17x leaves only 4.7 ns for FF-to-pad skew between the
    # SCK and CS_N pads -- unconstrained today, since top.lpf carries no
    # output timing constraint on any PMOD pin. Three cycles (50.00 ns) is
    # the CH32 plan's recommended 3+3 guard band.
    geometry = _slot_edge_geometry(3, cs_setup_cycles=cs_setup_cycles)
    setup = geometry["first_rise"] - geometry["cs_fall"]
    assert setup == cs_setup_cycles, (
        f"tSU(NSS) is {setup} usb cycles ({setup * 1000 / 60:.2f} ns), "
        f"expected {cs_setup_cycles} ({cs_setup_cycles * 1000 / 60:.2f} ns)"
    )


def test_cs_setup_cycles_below_two_is_rejected() -> None:
    # Two cycles is the floor: the edge generator needs one cycle to leave the
    # slot boundary and one half-period to reach the first rising edge.
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"
    with pytest.raises(ValueError):
        master_type(clock_hz=60_000_000, slot_cycles=1_200, sck_div=4, cs_setup_cycles=1)


@pytest.mark.parametrize("cs_hold_cycles", [2, 3, 5])
def test_sck_stays_idle_low_through_the_entire_cs_hold(cs_hold_cycles: int) -> None:
    # The hold must not accidentally emit a 257th edge. Parametrised from 2:
    # a 1-cycle hold has no cycle strictly between the last fall and the CS
    # rise, so it could not observe an extra edge and would pass vacuously.
    geometry = _slot_edge_geometry(cs_hold_cycles)
    observed = geometry["sck_during_hold"]
    assert (
        len(observed) == cs_hold_cycles - 1
    ), f"expected {cs_hold_cycles - 1} cycle(s) inside the hold, saw {len(observed)}"
    assert not any(observed), f"SCK did not stay idle-low during the CS hold: {observed}"


@pytest.mark.parametrize("cs_hold_cycles", [1, 3, 5])
def test_slot_cadence_is_unchanged_by_the_cs_hold(cs_hold_cycles: int) -> None:
    # The hold lives inside the slot and cannot push the cadence.
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(
        clock_hz=60_000_000, slot_cycles=1_200, sck_div=4, cs_hold_cycles=cs_hold_cycles
    )
    simulation = Simulator(dut)
    simulation.add_clock(1 / 60_000_000, domain="usb")
    starts = []

    async def bench(ctx):
        ctx.set(dut.mcu_ready, 1)
        for cycle in range(5_000):
            await ctx.tick("usb")
            if ctx.get(dut.slot_start):
                starts.append(cycle)
                if len(starts) == 4:
                    return

    simulation.add_testbench(bench)
    simulation.run()
    assert len(starts) == 4
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:], strict=False)]
    assert gaps == [
        1_200,
        1_200,
        1_200,
    ], f"cs_hold_cycles={cs_hold_cycles} moved the cadence: {gaps}"


def test_receive_validation_still_attributes_each_error_with_the_cs_hold() -> None:
    # validate_pending now fires cs_hold_cycles - 1 cycles later. It must still
    # land in the same slot with identical counter attribution.
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"

    dut = master_type(clock_hz=60_000_000, slot_cycles=1_200, sck_div=4)
    simulation = Simulator(dut)
    simulation.add_clock(1 / 60_000_000, domain="usb")

    valid = pack_slot(INJ_TYPE_RELATIVE, 7, bytes(26))
    bad_sof = bytearray(valid)
    bad_sof[0] = SOF ^ 0xFF
    bad_type = bytearray(valid)
    bad_type[1] = 0x7F
    bad_length = bytearray(valid)
    bad_length[3] = 25
    bad_crc = bytearray(valid)
    bad_crc[12] ^= 0x80
    cases = (
        (_with_valid_crc(bytes(bad_sof)), "bad_sof_count"),
        (_with_valid_crc(bytes(bad_type)), "bad_type_count"),
        (_with_valid_crc(bytes(bad_length)), "bad_length_count"),
        (bytes(bad_crc), "bad_crc_count"),
    )

    async def bench(ctx):
        ctx.set(dut.mcu_ready, 1)
        ctx.set(dut.rx_ready, 0)
        expected = {
            "bad_sof_count": 0,
            "bad_type_count": 0,
            "bad_length_count": 0,
            "bad_crc_count": 0,
        }
        for return_slot, counter_name in cases:
            await _run_transfer(ctx, dut, return_slot, limit=3_000)
            for _ in range(4):
                await ctx.tick("usb")
            expected[counter_name] += 1
            for name, value in expected.items():
                assert ctx.get(getattr(dut, name)) == value
            assert not ctx.get(dut.rx_valid)

    simulation.add_testbench(bench)
    simulation.run()


def test_cs_hold_cycles_below_one_is_rejected() -> None:
    master_type = _spi_slot_master_type()
    assert master_type is not None, "SPISlotMaster is not implemented"
    with pytest.raises(ValueError):
        master_type(clock_hz=60_000_000, slot_cycles=1_200, sck_div=4, cs_hold_cycles=0)
