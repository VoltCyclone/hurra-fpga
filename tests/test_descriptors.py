import re

import pytest
from amaranth.back import rtlil
from amaranth.sim import Simulator

from hurra_cynthion.descriptors import (
    DESCRIPTOR_ENTRY_COUNT,
    DESCRIPTOR_STORE_SIZE,
    MAX_CONFIGURATION_SIZE,
    MAX_ENDPOINTS,
    MAX_INTERFACES,
    MAX_PACKET_SIZE,
    MAX_REPORT_SIZE,
    MAX_STRING_SIZE,
    DescriptorStore,
    MalformedDescriptorError,
    OversizedDescriptorError,
    UnsupportedTopologyError,
    parse_device_descriptor,
    parse_mouse_configuration,
    validate_string_descriptor,
)

VALID_DEVICE_DESCRIPTOR = bytes(
    [
        18,
        1,
        0x00,
        0x02,
        0,
        0,
        0,
        64,
        0x09,
        0x12,
        0x01,
        0x00,
        0x00,
        0x01,
        1,
        2,
        3,
        1,
    ]
)


def mouse_configuration(*, extra_descriptors: bytes = b"") -> bytes:
    body = bytes(
        [
            9,
            4,
            2,
            0,
            1,
            3,
            1,
            2,
            0,
            9,
            0x21,
            0x11,
            0x01,
            0,
            1,
            0x22,
            52,
            0,
        ]
    )
    endpoint = bytes([7, 5, 0x83, 3, 8, 0, 10])
    total_length = 9 + len(body) + len(extra_descriptors) + len(endpoint)
    header = bytes([9, 2, total_length & 0xFF, total_length >> 8, 1, 7, 0, 0x80, 50])
    return header + body + extra_descriptors + endpoint


def _interface(number, iface_class, subclass, protocol, endpoint_addr, report_len) -> bytes:
    interface = bytes([9, 4, number, 0, 1, iface_class, subclass, protocol, 0])
    hid = bytes([9, 0x21, 0x11, 0x01, 0, 1, 0x22, report_len & 0xFF, report_len >> 8])
    endpoint = bytes([7, 5, endpoint_addr, 3, 8, 0, 10])
    return interface + hid + endpoint


def composite_configuration(interfaces) -> bytes:
    # interfaces: list of (class, subclass, protocol, endpoint_addr, report_len)
    body = b"".join(_interface(index, *spec) for index, spec in enumerate(interfaces))
    total = 9 + len(body)
    header = bytes([9, 2, total & 0xFF, total >> 8, len(interfaces), 7, 0, 0x80, 50])
    return header + body


# DeathAdder-shaped: boot mouse (interface 0) plus two keyboard interfaces.
DEATHADDER = [
    (3, 1, 2, 0x81, 52),
    (3, 1, 1, 0x82, 65),
    (3, 0, 0, 0x83, 65),
]


def replace_byte(data: bytes, offset: int, value: int) -> bytes:
    changed = bytearray(data)
    changed[offset] = value
    return bytes(changed)


def replace_u16(data: bytes, offset: int, value: int) -> bytes:
    changed = bytearray(data)
    changed[offset : offset + 2] = value.to_bytes(2, "little")
    return bytes(changed)


def simulate_store(bench) -> None:
    dut = DescriptorStore()
    simulation = Simulator(dut)
    simulation.add_clock(1e-6, domain="usb")

    async def wrapped(ctx) -> None:
        await bench(ctx, dut)

    simulation.add_testbench(wrapped)
    simulation.run()


async def pulse_capture_start(ctx, dut, key: tuple[int, int, int]) -> None:
    descriptor_type, descriptor_index, w_index = key
    ctx.set(dut.capture_type, descriptor_type)
    ctx.set(dut.capture_index, descriptor_index)
    ctx.set(dut.capture_w_index, w_index)
    ctx.set(dut.capture_start, 1)
    await ctx.tick("usb")
    ctx.set(dut.capture_start, 0)


async def wait_capture_ready(ctx, dut, *, limit: int = 14) -> int:
    for elapsed in range(limit + 1):
        if ctx.get(dut.capture_ready) or ctx.get(dut.capture_overflow):
            return elapsed
        await ctx.tick("usb")
    pytest.fail(f"capture allocation did not complete within {limit} clocks")


async def start_capture(ctx, dut, key: tuple[int, int, int]) -> None:
    await pulse_capture_start(ctx, dut, key)
    await wait_capture_ready(ctx, dut)


async def append_bytes(ctx, dut, payload: bytes) -> None:
    for byte in payload:
        assert ctx.get(dut.capture_ready)
        ctx.set(dut.capture_data, byte)
        ctx.set(dut.capture_valid, 1)
        await ctx.tick("usb")
    ctx.set(dut.capture_valid, 0)


async def finish_capture(ctx, dut, *, commit: bool) -> None:
    control = dut.capture_commit if commit else dut.capture_abort
    ctx.set(control, 1)
    await ctx.tick("usb")
    ctx.set(control, 0)


async def capture(ctx, dut, key: tuple[int, int, int], payload: bytes) -> None:
    await start_capture(ctx, dut, key)
    await append_bytes(ctx, dut, payload)
    await finish_capture(ctx, dut, commit=True)


async def lookup(ctx, dut, key: tuple[int, int, int], offset: int = 0) -> tuple[int, int, int]:
    descriptor_type, descriptor_index, w_index = key
    ctx.set(dut.lookup_type, descriptor_type)
    ctx.set(dut.lookup_index, descriptor_index)
    ctx.set(dut.lookup_w_index, w_index)
    ctx.set(dut.lookup_offset, offset)
    while not ctx.get(dut.lookup_ready):
        await ctx.tick("usb")
    ctx.set(dut.lookup_request, 1)
    await ctx.tick("usb")
    ctx.set(dut.lookup_request, 0)
    for _ in range(16):
        if ctx.get(dut.lookup_response):
            break
        await ctx.tick("usb")
    else:
        pytest.fail("descriptor lookup did not respond within 16 clocks")
    # Payload is a one-clock synchronous read after metadata selection.
    await ctx.tick("usb")
    return (
        ctx.get(dut.lookup_found),
        ctx.get(dut.lookup_length),
        ctx.get(dut.lookup_data),
    )


def test_descriptor_limits_are_exact_and_stable() -> None:
    assert DESCRIPTOR_STORE_SIZE == 4096
    assert DESCRIPTOR_ENTRY_COUNT == 12
    assert MAX_INTERFACES == 4
    assert MAX_ENDPOINTS == 4
    assert MAX_CONFIGURATION_SIZE == 1024
    assert MAX_REPORT_SIZE == 2048
    assert MAX_STRING_SIZE == 255
    assert MAX_PACKET_SIZE == 64


def test_store_capture_commit_and_requested_lookup() -> None:
    async def bench(ctx, dut) -> None:
        key = (1, 0, 0)
        await capture(ctx, dut, key, b"\x12\x01\xaa")

        assert await lookup(ctx, dut, key, 0) == (1, 3, 0x12)
        assert await lookup(ctx, dut, key, 1) == (1, 3, 0x01)
        assert await lookup(ctx, dut, key, 2) == (1, 3, 0xAA)
        assert await lookup(ctx, dut, (1, 1, 0), 0) == (0, 0, 0)

    simulate_store(bench)


def test_store_out_of_range_lookup_returns_zero() -> None:
    async def bench(ctx, dut) -> None:
        key = (3, 4, 0x0409)
        await capture(ctx, dut, key, b"\x04\x03")
        assert await lookup(ctx, dut, key, 2) == (1, 2, 0)
        assert await lookup(ctx, dut, key, 4095) == (1, 2, 0)

    simulate_store(bench)


def test_store_abort_rolls_back_allocation_and_preserves_entries() -> None:
    async def bench(ctx, dut) -> None:
        committed_key = (1, 0, 0)
        aborted_key = (2, 0, 0)
        await capture(ctx, dut, committed_key, b"old")
        await start_capture(ctx, dut, aborted_key)
        await append_bytes(ctx, dut, b"discard")
        await finish_capture(ctx, dut, commit=False)

        assert await lookup(ctx, dut, committed_key, 2) == (1, 3, ord("d"))
        assert await lookup(ctx, dut, aborted_key) == (0, 0, 0)

        await capture(ctx, dut, aborted_key, b"new")
        assert await lookup(ctx, dut, aborted_key, 2) == (1, 3, ord("w"))

    simulate_store(bench)


def test_store_clear_invalidates_metadata_and_resets_allocator() -> None:
    async def bench(ctx, dut) -> None:
        key = (0x22, 0, 2)
        await capture(ctx, dut, key, b"report")
        ctx.set(dut.clear, 1)
        await ctx.tick("usb")
        ctx.set(dut.clear, 0)
        assert await lookup(ctx, dut, key) == (0, 0, 0)

        await capture(ctx, dut, key, bytes(DESCRIPTOR_STORE_SIZE))
        assert not ctx.get(dut.capture_overflow)
        assert await lookup(ctx, dut, key, DESCRIPTOR_STORE_SIZE - 1) == (
            1,
            DESCRIPTOR_STORE_SIZE,
            0,
        )

    simulate_store(bench)


def test_store_has_exactly_twelve_entries_and_rejects_a_thirteenth() -> None:
    async def bench(ctx, dut) -> None:
        for index in range(DESCRIPTOR_ENTRY_COUNT):
            await capture(ctx, dut, (3, index, 0x0409), bytes([index]))

        thirteenth = (3, DESCRIPTOR_ENTRY_COUNT, 0x0409)
        await start_capture(ctx, dut, thirteenth)
        assert ctx.get(dut.capture_busy)
        assert ctx.get(dut.capture_overflow)
        assert not ctx.get(dut.capture_ready)
        await finish_capture(ctx, dut, commit=True)
        assert await lookup(ctx, dut, thirteenth) == (0, 0, 0)

        for index in range(DESCRIPTOR_ENTRY_COUNT):
            assert await lookup(ctx, dut, (3, index, 0x0409)) == (1, 1, index)

    simulate_store(bench)


def test_store_rejects_4kib_overflow_without_corrupting_prior_entries() -> None:
    async def bench(ctx, dut) -> None:
        first = (1, 0, 0)
        overflowing = (2, 0, 0)
        await capture(ctx, dut, first, b"safe")
        await start_capture(ctx, dut, overflowing)
        await append_bytes(ctx, dut, bytes(DESCRIPTOR_STORE_SIZE - 4))
        assert not ctx.get(dut.capture_ready)
        ctx.set(dut.capture_valid, 1)
        ctx.set(dut.capture_data, 0xA5)
        await ctx.tick("usb")
        ctx.set(dut.capture_valid, 0)
        assert ctx.get(dut.capture_overflow)
        await finish_capture(ctx, dut, commit=True)

        assert await lookup(ctx, dut, first, 3) == (1, 4, ord("e"))
        assert await lookup(ctx, dut, overflowing) == (0, 0, 0)

    simulate_store(bench)


def test_store_same_key_recapture_atomically_replaces_metadata() -> None:
    async def bench(ctx, dut) -> None:
        key = (3, 1, 0x0409)
        await capture(ctx, dut, key, b"old")
        await start_capture(ctx, dut, key)
        await append_bytes(ctx, dut, b"replacement")

        assert await lookup(ctx, dut, key, 2) == (1, 3, ord("d"))
        await finish_capture(ctx, dut, commit=True)
        assert await lookup(ctx, dut, key, 10) == (1, 11, ord("t"))

    simulate_store(bench)


def test_store_latches_capture_key_at_start() -> None:
    async def bench(ctx, dut) -> None:
        started_key = (3, 1, 0x0409)
        changed_key = (3, 2, 0x0411)
        await start_capture(ctx, dut, started_key)
        await append_bytes(ctx, dut, b"key")
        ctx.set(dut.capture_type, changed_key[0])
        ctx.set(dut.capture_index, changed_key[1])
        ctx.set(dut.capture_w_index, changed_key[2])
        await finish_capture(ctx, dut, commit=True)

        assert await lookup(ctx, dut, started_key, 2) == (1, 3, ord("y"))
        assert await lookup(ctx, dut, changed_key) == (0, 0, 0)

    simulate_store(bench)


def test_store_capture_allocation_scans_sequentially_before_ready() -> None:
    async def bench(ctx, dut) -> None:
        await pulse_capture_start(ctx, dut, (1, 0, 0))
        assert ctx.get(dut.capture_busy)
        assert not ctx.get(dut.capture_ready)
        elapsed = await wait_capture_ready(ctx, dut)
        assert 11 <= elapsed <= 14
        await finish_capture(ctx, dut, commit=False)

    simulate_store(bench)


def test_store_abort_and_clear_cancel_an_allocation_scan_atomically() -> None:
    async def bench(ctx, dut) -> None:
        key = (3, 7, 0x0409)

        await pulse_capture_start(ctx, dut, key)
        ctx.set(dut.capture_abort, 1)
        await ctx.tick("usb")
        ctx.set(dut.capture_abort, 0)
        assert not ctx.get(dut.capture_busy)
        assert await lookup(ctx, dut, key) == (0, 0, 0)

        await pulse_capture_start(ctx, dut, key)
        ctx.set(dut.clear, 1)
        await ctx.tick("usb")
        ctx.set(dut.clear, 0)
        assert not ctx.get(dut.capture_busy)
        assert not ctx.get(dut.capture_ready)

        await capture(ctx, dut, key, b"fresh")
        assert await lookup(ctx, dut, key, 4) == (1, 5, ord("h"))

    simulate_store(bench)


def test_store_queues_simultaneous_admin_clients_without_request_loss() -> None:
    async def bench(ctx, dut) -> None:
        key = (0x22, 0, 3)
        await capture(ctx, dut, key, b"directory")

        for prefix in ("lookup", "device_lookup"):
            ctx.set(getattr(dut, f"{prefix}_type"), key[0])
            ctx.set(getattr(dut, f"{prefix}_index"), key[1])
            ctx.set(getattr(dut, f"{prefix}_w_index"), key[2])
        ctx.set(dut.export_slot, 0)
        ctx.set(dut.copy_slot, 0)
        for request in (
            dut.lookup_request,
            dut.device_lookup_request,
            dut.export_request,
            dut.copy_request,
        ):
            ctx.set(request, 1)
        await ctx.tick("usb")
        for request in (
            dut.lookup_request,
            dut.device_lookup_request,
            dut.export_request,
            dut.copy_request,
        ):
            ctx.set(request, 0)

        seen = set()
        responses = {
            "lookup": dut.lookup_response,
            "device": dut.device_lookup_response,
            "export": dut.export_response,
            "copy": dut.copy_response,
        }
        for _ in range(48):
            for name, response in responses.items():
                if ctx.get(response):
                    seen.add(name)
            if len(seen) == len(responses):
                break
            await ctx.tick("usb")

        assert seen == set(responses)
        assert ctx.get(dut.lookup_found)
        assert ctx.get(dut.device_lookup_found)
        assert ctx.get(dut.export_valid)
        assert ctx.get(dut.copy_valid)

    simulate_store(bench)


@pytest.mark.parametrize("finish", ["abort", "commit"])
def test_store_admin_requests_survive_capture_finish_while_scan_is_active(finish) -> None:
    async def bench(ctx, dut) -> None:
        await capture(ctx, dut, (1, 0, 0), b"visible")
        await start_capture(ctx, dut, (2, 0, 0))

        ctx.set(dut.lookup_type, 1)
        ctx.set(dut.lookup_index, 0)
        ctx.set(dut.lookup_w_index, 0)
        ctx.set(dut.device_lookup_type, 1)
        ctx.set(dut.device_lookup_index, 0)
        ctx.set(dut.device_lookup_w_index, 0)
        ctx.set(dut.export_slot, 0)
        ctx.set(dut.copy_slot, 0)
        requests = (
            dut.lookup_request,
            dut.device_lookup_request,
            dut.export_request,
            dut.copy_request,
        )
        for request in requests:
            ctx.set(request, 1)
        await ctx.tick("usb")
        for request in requests:
            ctx.set(request, 0)
        await ctx.tick("usb")
        assert not ctx.get(dut.lookup_ready), "active lookup advertised another queue slot"

        finish_signal = dut.capture_abort if finish == "abort" else dut.capture_commit
        ctx.set(finish_signal, 1)
        await ctx.tick("usb")
        ctx.set(finish_signal, 0)

        responses = {
            "lookup": dut.lookup_response,
            "device": dut.device_lookup_response,
            "export": dut.export_response,
            "copy": dut.copy_response,
        }
        counts = {name: 0 for name in responses}
        for _ in range(64):
            for name, response in responses.items():
                counts[name] += int(ctx.get(response))
            if all(count == 1 for count in counts.values()):
                break
            await ctx.tick("usb")

        assert counts == {name: 1 for name in responses}

    simulate_store(bench)


def test_store_admin_ready_is_suppressed_during_clear() -> None:
    async def bench(ctx, dut) -> None:
        ctx.set(dut.clear, 1)
        ctx.set(dut.lookup_request, 1)
        ctx.set(dut.device_lookup_request, 1)
        ctx.set(dut.export_request, 1)
        ctx.set(dut.copy_request, 1)
        assert not ctx.get(dut.lookup_ready)
        assert not ctx.get(dut.device_lookup_ready)
        assert not ctx.get(dut.export_ready)
        assert not ctx.get(dut.copy_ready)
        await ctx.tick("usb")
        assert not ctx.get(dut.lookup_response)
        assert not ctx.get(dut.device_lookup_response)
        assert not ctx.get(dut.export_response)
        assert not ctx.get(dut.copy_response)

    simulate_store(bench)


def test_store_elaborates_to_payload_plus_exact_mirrored_directory_memories() -> None:
    dut = DescriptorStore()
    netlist = rtlil.convert(
        dut,
        ports=[
            dut.clear,
            dut.capture_start,
            dut.capture_type,
            dut.capture_index,
            dut.capture_w_index,
            dut.capture_valid,
            dut.capture_data,
            dut.capture_ready,
            dut.capture_commit,
            dut.capture_abort,
            dut.capture_busy,
            dut.capture_overflow,
            dut.lookup_type,
            dut.lookup_index,
            dut.lookup_w_index,
            dut.lookup_offset,
            dut.lookup_found,
            dut.lookup_length,
            dut.lookup_data,
        ],
    )
    assert len(re.findall(r"(?m)^\s*memory width 8 size 4096", netlist)) == 1
    assert len(re.findall(r"(?m)^\s*memory width 64 size 12", netlist)) == 2
    assert "metadata_type_0" not in netlist
    assert "metadata_index_0" not in netlist
    assert "metadata_w_index_0" not in netlist
    assert "metadata_start_0" not in netlist
    assert "metadata_length_0" not in netlist


def test_store_exposes_explicit_directory_request_handshakes() -> None:
    dut = DescriptorStore()
    for name in (
        "lookup_request",
        "lookup_ready",
        "lookup_response",
        "device_lookup_request",
        "device_lookup_ready",
        "device_lookup_response",
        "export_request",
        "export_ready",
        "export_response",
        "copy_request",
        "copy_ready",
        "copy_response",
        "serve_ready",
        "serve_cancel",
    ):
        assert hasattr(dut, name), name


def test_parse_valid_device_descriptor() -> None:
    parsed = parse_device_descriptor(VALID_DEVICE_DESCRIPTOR)
    assert parsed.ep0_max_packet_size == 64
    assert parsed.manufacturer_index == 1
    assert parsed.product_index == 2
    assert parsed.serial_number_index == 3


@pytest.mark.parametrize(
    "descriptor",
    [
        VALID_DEVICE_DESCRIPTOR[:17],
        replace_byte(VALID_DEVICE_DESCRIPTOR, 0, 17),
        replace_byte(VALID_DEVICE_DESCRIPTOR, 1, 2),
        replace_byte(VALID_DEVICE_DESCRIPTOR, 7, 0),
        replace_byte(VALID_DEVICE_DESCRIPTOR, 7, 12),
    ],
)
def test_parse_device_rejects_truncated_or_malformed_descriptors(descriptor: bytes) -> None:
    with pytest.raises(MalformedDescriptorError):
        parse_device_descriptor(descriptor)


def test_parse_device_rejects_more_than_one_configuration() -> None:
    with pytest.raises(UnsupportedTopologyError):
        parse_device_descriptor(replace_byte(VALID_DEVICE_DESCRIPTOR, 17, 2))


def test_parse_device_rejects_trailing_bytes_as_oversized() -> None:
    with pytest.raises(OversizedDescriptorError):
        parse_device_descriptor(VALID_DEVICE_DESCRIPTOR + b"\x00")


def test_parse_valid_boot_mouse_configuration() -> None:
    parsed = parse_mouse_configuration(mouse_configuration())
    assert parsed.configuration_value == 7
    assert len(parsed.endpoints) == 1
    (mouse,) = parsed.endpoints
    assert mouse.interface_number == 2
    assert mouse.endpoint_number == 3
    assert mouse.max_packet_size == 8
    assert mouse.interval == 10
    assert mouse.report_length == 52


def test_parse_composite_deathadder_captures_all_interrupt_in_endpoints() -> None:
    parsed = parse_mouse_configuration(composite_configuration(DEATHADDER))
    assert parsed.configuration_value == 7
    assert [e.interface_number for e in parsed.endpoints] == [0, 1, 2]
    assert [e.endpoint_number for e in parsed.endpoints] == [1, 2, 3]
    assert [e.report_length for e in parsed.endpoints] == [52, 65, 65]
    assert all(e.max_packet_size == 8 and e.interval == 10 for e in parsed.endpoints)


def test_parse_rejects_multiple_report_descriptors_on_one_interface() -> None:
    interface = bytes([9, 4, 0, 0, 1, 3, 1, 2, 0])
    # HID descriptor whose subordinate table lists two report (0x22) descriptors.
    hid = bytes([12, 0x21, 0x11, 0x01, 0, 2, 0x22, 52, 0, 0x22, 10, 0])
    endpoint = bytes([7, 5, 0x81, 3, 8, 0, 10])
    body = interface + hid + endpoint
    total = 9 + len(body)
    descriptor = bytes([9, 2, total & 0xFF, total >> 8, 1, 7, 0, 0x80, 50]) + body
    with pytest.raises(MalformedDescriptorError):
        parse_mouse_configuration(descriptor)


def test_parse_rejects_config_with_no_mouse_protocol_interface() -> None:
    only_keyboards = [(3, 1, 1, 0x81, 65), (3, 0, 0, 0x82, 65)]
    with pytest.raises(UnsupportedTopologyError):
        parse_mouse_configuration(composite_configuration(only_keyboards))


def test_parse_skips_non_hid_interface_but_keeps_mouse() -> None:
    with_vendor = [(3, 1, 2, 0x81, 52), (0xFF, 0, 0, 0x82, 0)]
    parsed = parse_mouse_configuration(composite_configuration(with_vendor))
    assert [e.interface_number for e in parsed.endpoints] == [0]


def test_parse_skips_interrupt_out_endpoint_and_keeps_interrupt_in() -> None:
    # A keyboard interface with an interrupt-OUT (LED) endpoint ahead of its
    # interrupt-IN: only the IN is captured, alongside the mouse's IN.
    mouse = (
        bytes([9, 4, 0, 0, 1, 3, 1, 2, 0])
        + bytes([9, 0x21, 0x11, 0x01, 0, 1, 0x22, 52, 0])
        + bytes([7, 5, 0x81, 3, 8, 0, 10])
    )
    keyboard = (
        bytes([9, 4, 1, 0, 2, 3, 1, 1, 0])
        + bytes([9, 0x21, 0x11, 0x01, 0, 1, 0x22, 65, 0])
        + bytes([7, 5, 0x02, 3, 8, 0, 10])  # interrupt OUT -> skipped
        + bytes([7, 5, 0x82, 3, 8, 0, 10])  # interrupt IN  -> captured
    )
    body = mouse + keyboard
    total = 9 + len(body)
    config = bytes([9, 2, total & 0xFF, total >> 8, 2, 7, 0, 0x80, 50]) + body
    parsed = parse_mouse_configuration(config)
    assert [e.interface_number for e in parsed.endpoints] == [0, 1]
    assert [e.endpoint_number for e in parsed.endpoints] == [1, 2]


def test_parse_rejects_more_than_max_interfaces() -> None:
    too_many = [(3, 1, 2, 0x81 + index, 52) for index in range(MAX_INTERFACES + 1)]
    with pytest.raises((UnsupportedTopologyError, OversizedDescriptorError)):
        parse_mouse_configuration(composite_configuration(too_many))


@pytest.mark.parametrize(
    "descriptor",
    [
        b"",
        mouse_configuration()[:8],
        replace_byte(mouse_configuration(), 0, 8),
        replace_byte(mouse_configuration(), 1, 1),
        replace_u16(mouse_configuration(), 2, len(mouse_configuration()) - 1),
        mouse_configuration()[:-1],
        replace_byte(mouse_configuration(), 9, 0),
        replace_byte(mouse_configuration(), 9, 1),
        replace_byte(mouse_configuration(), 18, 8),
        replace_byte(mouse_configuration(), 19, 0x20),
        replace_byte(mouse_configuration(), 23, 0),
        replace_u16(mouse_configuration(), 25, 0),
        replace_byte(mouse_configuration(), 27, 6),
        replace_byte(mouse_configuration(), 28, 4),
        replace_byte(mouse_configuration(), 31, 0),
        replace_byte(mouse_configuration(), 33, 0),
        replace_byte(mouse_configuration(), 29, 0x03),
        replace_byte(mouse_configuration(), 30, 2),
    ],
)
def test_parse_configuration_rejects_truncated_zero_length_and_malformed(
    descriptor: bytes,
) -> None:
    with pytest.raises(MalformedDescriptorError):
        parse_mouse_configuration(descriptor)


def test_parse_configuration_rejects_oversized_configuration() -> None:
    descriptor = mouse_configuration(extra_descriptors=bytes([255, 0x30]) + bytes(1022))
    assert len(descriptor) > MAX_CONFIGURATION_SIZE
    with pytest.raises(OversizedDescriptorError):
        parse_mouse_configuration(descriptor)


def test_parse_configuration_rejects_oversized_report() -> None:
    descriptor = replace_u16(mouse_configuration(), 25, MAX_REPORT_SIZE + 1)
    with pytest.raises(OversizedDescriptorError):
        parse_mouse_configuration(descriptor)


@pytest.mark.parametrize(
    "descriptor",
    [
        replace_byte(mouse_configuration(), 4, 2),
        replace_byte(mouse_configuration(), 14, 0),
        replace_byte(mouse_configuration(), 14, 2),
        replace_byte(mouse_configuration(), 16, 0),
        replace_byte(mouse_configuration(), 16, 1),
        replace_byte(mouse_configuration(), 29, 0x93),
    ],
)
def test_parse_configuration_rejects_unsupported_topologies(descriptor: bytes) -> None:
    with pytest.raises(UnsupportedTopologyError):
        parse_mouse_configuration(descriptor)


def test_parse_configuration_accepts_two_mouse_interfaces() -> None:
    two_mice = [(3, 1, 2, 0x81, 52), (3, 1, 2, 0x82, 52)]
    parsed = parse_mouse_configuration(composite_configuration(two_mice))
    assert [e.interface_number for e in parsed.endpoints] == [0, 1]
    assert [e.endpoint_number for e in parsed.endpoints] == [1, 2]


def test_parse_configuration_rejects_more_than_max_endpoints() -> None:
    # Four interfaces are within MAX_INTERFACES but declare five interrupt-IN
    # endpoints by giving the first interface a second (extra) endpoint.
    extra_endpoint = bytes([7, 5, 0x88, 3, 8, 0, 10])
    interfaces = b"".join(_interface(i, 3, 1, 2, 0x81 + i, 52) for i in range(4))
    body = interfaces + extra_endpoint
    total = 9 + len(body)
    descriptor = bytes([9, 2, total & 0xFF, total >> 8, 4, 7, 0, 0x80, 50]) + body
    with pytest.raises(OversizedDescriptorError):
        parse_mouse_configuration(descriptor)


def test_validate_language_and_utf16le_string_descriptors() -> None:
    assert validate_string_descriptor(b"\x06\x03\x09\x04\x11\x04", index=0) is None
    mouse_string = b"\x0c\x03" + "Mouse".encode("utf-16-le")
    assert validate_string_descriptor(mouse_string, index=2) is None


@pytest.mark.parametrize(
    ("descriptor", "index"),
    [
        (b"\x02\x03", 0),
        (b"\x04\x02\x09\x04", 0),
        (b"\x05\x03\x09\x04\x11", 0),
        (b"\x06\x03\x09\x04", 0),
        (b"\x04\x03\x00\xd8", 1),
        (bytes([255, 3]) + bytes(253), 1),
    ],
)
def test_validate_string_rejects_bad_type_length_language_table_and_utf16(
    descriptor: bytes, index: int
) -> None:
    with pytest.raises(MalformedDescriptorError):
        validate_string_descriptor(descriptor, index=index)


def test_validate_string_rejects_descriptors_over_255_bytes() -> None:
    with pytest.raises(OversizedDescriptorError):
        validate_string_descriptor(bytes([0, 3]) + bytes(MAX_STRING_SIZE - 1), index=1)


def test_generation_changed_strobe_matches_a_delayed_comparison_cycle_for_cycle():
    """The strobe must be indistinguishable from the comparison it replaced.

    Consumers previously derived "the descriptor generation changed" by keeping
    their own delayed copy and comparing. That comparison is now published by
    the store instead, which takes a 16-bit comparator fed by a long cross-die
    bus off the head of `invalidate`, `tx_valid` and `tx_abort`. The two must
    agree on every cycle, including while `clear` is held and the generation
    advances repeatedly.
    """
    store = DescriptorStore()
    simulation = Simulator(store)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        previous = ctx.get(store.descriptor_generation)
        mismatches = []
        strobes = 0

        async def step(clear: int) -> None:
            nonlocal previous, strobes
            ctx.set(store.clear, clear)
            await ctx.tick("usb")
            generation = ctx.get(store.descriptor_generation)
            strobe = ctx.get(store.descriptor_generation_changed)
            # The reference: what a consumer holding a delayed copy would see.
            reference = int(generation != previous)
            if strobe != reference:
                mismatches.append((generation, previous, strobe, reference))
            strobes += strobe
            previous = generation

        for _ in range(3):
            await step(0)
        # A single-cycle clear.
        await step(1)
        for _ in range(3):
            await step(0)
        # Held high across several cycles: the generation advances on each one,
        # so the strobe must stay high for exactly as long.
        for _ in range(4):
            await step(1)
        for _ in range(4):
            await step(0)

        assert mismatches == []
        # Guard against a strobe stuck low making the comparison vacuous.
        assert strobes == 5
        assert ctx.get(store.descriptor_generation) == 5

    simulation.add_testbench(bench)
    simulation.run()
