from _descriptor_store_helpers import seed_descriptor
from amaranth import Elaboratable, Module
from amaranth.sim import Simulator

from hurra_cynthion import device as device_module
from hurra_cynthion.descriptors import DescriptorStore


async def _read_device_port(ctx, store, dtype, index, w_index, length):
    ctx.set(store.device_lookup_type, dtype)
    ctx.set(store.device_lookup_index, index)
    ctx.set(store.device_lookup_w_index, w_index)
    ctx.set(store.device_lookup_request, 1)
    await ctx.tick("usb")
    ctx.set(store.device_lookup_request, 0)
    for _ in range(16):
        if ctx.get(store.device_lookup_response):
            break
        await ctx.tick("usb")
    else:
        raise AssertionError("device lookup did not respond")
    result = []
    for offset in range(length):
        ctx.set(store.device_lookup_offset, offset)
        for _ in range(4):
            await ctx.tick("usb")
            if ctx.get(store.device_lookup_data_valid):
                break
        else:
            raise AssertionError("device payload read was not granted")
        result.append(ctx.get(store.device_lookup_data))
    return result


async def _request_serve(ctx, store, dtype, index, w_index, *, replace=False, limit=14):
    ctx.set(store.serve_type, dtype)
    ctx.set(store.serve_index, index)
    ctx.set(store.serve_w_index, w_index)
    ctx.set(store.serve_cancel, replace)
    assert ctx.get(store.serve_ready)
    ctx.set(store.serve_request, 1)
    await ctx.tick("usb")
    ctx.set(store.serve_request, 0)
    ctx.set(store.serve_cancel, 0)
    for elapsed in range(1, limit + 1):
        await ctx.tick("usb")
        if ctx.get(store.serve_response):
            return elapsed
    raise AssertionError(f"serve lookup did not respond within {limit} clocks")


def test_copy_source_port_pipelines_slot_metadata_and_bytes():
    source = DescriptorStore()
    assert hasattr(source, "copy_read_enable"), "DescriptorStore lacks the copy-source port"

    sim = Simulator(source)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        payload = b"metadata-copy"
        await seed_descriptor(ctx, source, dtype=0x22, index=0, w_index=3, data=payload)

        ctx.set(source.copy_read_enable, 1)
        ctx.set(source.copy_slot, 0)
        ctx.set(source.copy_offset, 5)
        ctx.set(source.copy_request, 1)
        await ctx.tick("usb")
        ctx.set(source.copy_request, 0)
        while not ctx.get(source.copy_response):
            await ctx.tick("usb")
        await ctx.tick("usb")

        assert ctx.get(source.copy_valid) == 1
        assert ctx.get(source.copy_type) == 0x22
        assert ctx.get(source.copy_index) == 0
        assert ctx.get(source.copy_w_index) == 3
        assert ctx.get(source.copy_length) == len(payload)
        assert ctx.get(source.copy_data_valid)
        assert ctx.get(source.copy_data) == payload[5]

    sim.add_testbench(bench)
    sim.run()


def test_serve_port_registers_metadata_before_reading_payload():
    store = DescriptorStore()
    assert hasattr(store, "serve_request"), "DescriptorStore lacks the pipelined serve port"

    sim = Simulator(store)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        payload = b"local-serving"
        await seed_descriptor(ctx, store, dtype=3, index=2, w_index=0x0409, data=payload)

        ctx.set(store.serve_type, 3)
        ctx.set(store.serve_index, 2)
        ctx.set(store.serve_w_index, 0x0409)
        ctx.set(store.serve_request, 1)
        await ctx.tick("usb")
        ctx.set(store.serve_request, 0)

        for _ in range(4):
            if ctx.get(store.serve_response):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("serve metadata response did not arrive")

        assert ctx.get(store.serve_found)
        assert ctx.get(store.serve_length) == len(payload)
        ctx.set(store.serve_offset, 6)
        ctx.set(store.serve_read_enable, 1)
        await ctx.tick("usb")
        assert ctx.get(store.serve_data) == payload[6]

    sim.add_testbench(bench)
    sim.run()


def test_serve_scan_finds_slot_eleven_and_misses_within_fourteen_clocks():
    store = DescriptorStore()
    sim = Simulator(store)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        for slot in range(12):
            await seed_descriptor(
                ctx,
                store,
                dtype=3,
                index=slot,
                w_index=0x0409,
                data=bytes([slot]),
            )

        elapsed = await _request_serve(ctx, store, 3, 11, 0x0409)
        assert elapsed <= 14
        assert ctx.get(store.serve_found)
        assert ctx.get(store.serve_length) == 1

        elapsed = await _request_serve(ctx, store, 3, 12, 0x0409, replace=True)
        assert elapsed <= 14
        assert not ctx.get(store.serve_found)

    sim.add_testbench(bench)
    sim.run()


def test_cached_serve_selection_requires_explicit_cancel_to_replace():
    store = DescriptorStore()
    sim = Simulator(store)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        await seed_descriptor(ctx, store, dtype=1, index=0, w_index=0, data=b"old")
        await seed_descriptor(ctx, store, dtype=2, index=0, w_index=0, data=b"new")

        await _request_serve(ctx, store, 1, 0, 0)
        assert ctx.get(store.serve_found)
        assert not ctx.get(store.serve_ready)

        await _request_serve(ctx, store, 2, 0, 0, replace=True)
        assert ctx.get(store.serve_found)
        assert ctx.get(store.serve_length) == 3

    sim.add_testbench(bench)
    sim.run()


def test_shared_payload_port_prioritizes_serve_then_copy_then_device_lookup():
    store = DescriptorStore()
    assert hasattr(store, "copy_data_valid")
    assert hasattr(store, "device_lookup_data_valid")
    assert hasattr(store, "serve_read_enable")
    sim = Simulator(store)
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        payload = b"priority"
        await seed_descriptor(ctx, store, dtype=3, index=0, w_index=0, data=payload)

        ctx.set(store.copy_slot, 0)
        ctx.set(store.copy_request, 1)
        ctx.set(store.device_lookup_type, 3)
        ctx.set(store.device_lookup_index, 0)
        ctx.set(store.device_lookup_w_index, 0)
        ctx.set(store.device_lookup_request, 1)
        await ctx.tick("usb")
        ctx.set(store.copy_request, 0)
        ctx.set(store.device_lookup_request, 0)
        saw_copy = False
        saw_device = False
        for _ in range(24):
            saw_copy |= bool(ctx.get(store.copy_response))
            saw_device |= bool(ctx.get(store.device_lookup_response))
            if saw_copy and saw_device:
                break
            await ctx.tick("usb")
        assert saw_copy and saw_device

        await _request_serve(ctx, store, 3, 0, 0)
        assert ctx.get(store.serve_found)
        ctx.set(store.serve_offset, 0)
        ctx.set(store.serve_read_enable, 1)
        ctx.set(store.copy_read_enable, 1)
        ctx.set(store.copy_offset, 2)
        ctx.set(store.device_lookup_offset, 3)
        await ctx.tick("usb")
        assert ctx.get(store.serve_data) == payload[0]
        assert ctx.get(store.copy_data) == 0
        assert ctx.get(store.device_lookup_data) == 0

        ctx.set(store.serve_cancel, 1)
        await ctx.tick("usb")
        ctx.set(store.serve_cancel, 0)
        ctx.set(store.serve_read_enable, 0)
        await ctx.tick("usb")
        assert ctx.get(store.copy_data_valid)
        assert ctx.get(store.copy_data) == payload[2]
        assert ctx.get(store.device_lookup_data) == 0

        ctx.set(store.copy_read_enable, 0)
        await ctx.tick("usb")
        assert ctx.get(store.device_lookup_data_valid)
        assert ctx.get(store.device_lookup_data) == payload[3]

        # A successful descriptor mutation must dominate a serve-directory
        # match that would otherwise complete on the same edge.
        ctx.set(store.capture_type, 3)
        ctx.set(store.capture_index, 0)
        ctx.set(store.capture_w_index, 0)
        ctx.set(store.capture_start, 1)
        await ctx.tick("usb")
        ctx.set(store.capture_start, 0)
        while not ctx.get(store.capture_ready):
            await ctx.tick("usb")
        ctx.set(store.capture_data, ord("z"))
        ctx.set(store.capture_valid, 1)
        await ctx.tick("usb")
        ctx.set(store.capture_valid, 0)

        ctx.set(store.serve_type, 3)
        ctx.set(store.serve_index, 0)
        ctx.set(store.serve_w_index, 0)
        ctx.set(store.serve_request, 1)
        await ctx.tick("usb")
        ctx.set(store.serve_request, 0)
        await ctx.tick("usb")
        ctx.set(store.capture_commit, 1)
        await ctx.tick("usb")
        ctx.set(store.capture_commit, 0)
        assert not ctx.get(store.serve_found)
        assert not ctx.get(store.serve_response)

    sim.add_testbench(bench)
    sim.run()


def test_copy_engine_clones_all_valid_entries_and_clears_when_disabled():
    engine_type = getattr(device_module, "DescriptorStoreCopyEngine", None)
    assert engine_type is not None, "DescriptorStoreCopyEngine is not implemented"

    source = DescriptorStore()
    destination = DescriptorStore()
    engine = engine_type(source=source, destination=destination)

    class CopyTop(Elaboratable):
        def elaborate(self, platform):
            del platform
            m = Module()
            m.submodules.source = source
            m.submodules.destination = destination
            m.submodules.engine = engine
            return m

    sim = Simulator(CopyTop())
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        device_descriptor = bytes(range(18))
        report_descriptor = bytes((index * 7) & 0xFF for index in range(70))
        await seed_descriptor(ctx, source, dtype=1, index=0, w_index=0, data=device_descriptor)
        await seed_descriptor(ctx, source, dtype=0x22, index=0, w_index=3, data=report_descriptor)

        ctx.set(engine.enable, 1)
        for _ in range(1000):
            if ctx.get(engine.done):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("descriptor copy did not complete within its bounded budget")

        assert not ctx.get(engine.failed)
        assert await _read_device_port(ctx, destination, 1, 0, 0, 18) == list(device_descriptor)
        assert await _read_device_port(ctx, destination, 0x22, 0, 3, 70) == list(report_descriptor)

        ctx.set(engine.enable, 0)
        await ctx.tick("usb")
        assert not ctx.get(engine.done)
        assert not ctx.get(destination.device_lookup_found)

    sim.add_testbench(bench)
    sim.run()


def test_copy_engine_clones_all_twelve_directory_slots():
    source = DescriptorStore()
    destination = DescriptorStore()
    engine = device_module.DescriptorStoreCopyEngine(source=source, destination=destination)

    class CopyTop(Elaboratable):
        def elaborate(self, platform):
            del platform
            m = Module()
            m.submodules.source = source
            m.submodules.destination = destination
            m.submodules.engine = engine
            return m

    sim = Simulator(CopyTop())
    sim.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        for slot in range(12):
            await seed_descriptor(
                ctx,
                source,
                dtype=3,
                index=slot,
                w_index=0x0409,
                data=bytes([0x80 | slot]),
            )

        ctx.set(engine.enable, 1)
        await _request_serve(ctx, source, 3, 0, 0x0409)
        ctx.set(source.serve_offset, 0)
        for cycle in range(2000):
            ctx.set(source.serve_read_enable, (cycle % 7) < 3)
            if ctx.get(engine.done):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("twelve-slot descriptor copy exceeded its bounded budget")
        ctx.set(source.serve_read_enable, 0)
        assert not ctx.get(engine.failed)

        for slot in range(12):
            assert await _read_device_port(ctx, destination, 3, slot, 0x0409, 1) == [0x80 | slot]

    sim.add_testbench(bench)
    sim.run()
