import importlib

from _descriptor_store_helpers import seed_descriptor
from amaranth import Elaboratable, Module
from amaranth.back import rtlil
from amaranth.sim import Simulator

from hurra_cynthion.descriptors import DescriptorStore
from hurra_cynthion.injection_wire import (
    INJ_TYPE_DESCRIPTOR_FRAGMENT,
    DescriptorFragmentPayload,
)


def _descriptor_export_engine_type():
    try:
        module = importlib.import_module("hurra_cynthion.descriptor_export")
    except ModuleNotFoundError:
        return None
    return getattr(module, "DescriptorExportEngine", None)


async def _read_export_payload(ctx, engine) -> bytes:
    payload = bytearray()
    for address in range(26):
        ctx.set(engine.message_payload_address, address)
        ctx.set(engine.message_payload_request, 1)
        assert not ctx.get(engine.message_payload_response)
        await ctx.tick("usb")
        ctx.set(engine.message_payload_request, 0)

        latency = 1
        while not ctx.get(engine.message_payload_response):
            assert latency < 8
            await ctx.tick("usb")
            latency += 1
        assert latency >= 2
        payload.append(ctx.get(engine.message_payload_data))
        await ctx.tick("usb")
        assert not ctx.get(engine.message_payload_response)
    return bytes(payload)


def test_exporter_has_addressed_responses_without_fragment_register_bank() -> None:
    engine_type = _descriptor_export_engine_type()
    assert engine_type is not None, "DescriptorExportEngine is not implemented"

    store = DescriptorStore()
    engine = engine_type(store)
    assert not hasattr(engine, "message_payload")
    assert len(engine.message_payload_request) == 1
    assert len(engine.message_payload_address) == 5
    assert len(engine.message_payload_response) == 1
    assert len(engine.message_payload_data) == 8
    assert len(engine.message_payload_cancel) == 1

    converted = rtlil.convert(
        engine,
        ports=[
            engine.start,
            engine.busy,
            engine.done,
            engine.message_valid,
            engine.message_ready,
            engine.message_type,
            engine.message_payload_request,
            engine.message_payload_address,
            engine.message_payload_response,
            engine.message_payload_data,
            engine.message_payload_cancel,
        ],
    )
    assert "fragment_data_0" not in converted
    assert "fragment_bytes" not in converted


def test_export_port_is_synchronous_and_independent_of_serve_and_copy_ports() -> None:
    store = DescriptorStore()
    assert hasattr(store, "export_read_enable"), "DescriptorStore lacks an export read port"

    simulation = Simulator(store)
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        first = b"copy-port"
        second = b"export-port"
        await seed_descriptor(ctx, store, dtype=0x22, index=0, w_index=0, data=first)
        await seed_descriptor(ctx, store, dtype=0x22, index=0, w_index=1, data=second)

        ctx.set(store.copy_read_enable, 1)
        ctx.set(store.copy_slot, 0)
        ctx.set(store.copy_offset, 2)
        ctx.set(store.serve_type, 0x22)
        ctx.set(store.serve_index, 0)
        ctx.set(store.serve_w_index, 0)
        ctx.set(store.serve_offset, 1)
        ctx.set(store.serve_read_enable, 1)
        ctx.set(store.export_read_enable, 1)
        ctx.set(store.export_slot, 1)
        ctx.set(store.export_offset, 3)
        ctx.set(store.copy_request, 1)
        ctx.set(store.export_request, 1)
        ctx.set(store.serve_request, 1)
        await ctx.tick("usb")
        ctx.set(store.copy_request, 0)
        ctx.set(store.export_request, 0)
        ctx.set(store.serve_request, 0)
        saw_copy = False
        saw_export = False
        saw_serve = False
        for _ in range(16):
            saw_copy |= bool(ctx.get(store.copy_response))
            saw_export |= bool(ctx.get(store.export_response))
            saw_serve |= bool(ctx.get(store.serve_response))
            if saw_copy and saw_export and saw_serve:
                break
            await ctx.tick("usb")
        assert saw_copy and saw_export and saw_serve
        await ctx.tick("usb")

        assert ctx.get(store.copy_valid)
        assert ctx.get(store.copy_w_index) == 0
        assert ctx.get(store.serve_data) == first[1]
        assert ctx.get(store.copy_data) == 0
        assert ctx.get(store.export_valid)
        assert ctx.get(store.export_type) == 0x22
        assert ctx.get(store.export_index) == 0
        assert ctx.get(store.export_w_index) == 1
        assert ctx.get(store.export_length) == len(second)
        assert ctx.get(store.export_data) == second[3]

        ctx.set(store.serve_cancel, 1)
        await ctx.tick("usb")
        ctx.set(store.serve_cancel, 0)
        ctx.set(store.serve_read_enable, 0)
        await ctx.tick("usb")
        assert ctx.get(store.copy_data_valid)
        assert ctx.get(store.copy_data) == first[2]
        assert ctx.get(store.export_data) == second[3]

    simulation.add_testbench(bench)
    simulation.run()


def test_exports_report_descriptors_for_four_interfaces_in_18_byte_fragments() -> None:
    engine_type = _descriptor_export_engine_type()
    assert engine_type is not None, "DescriptorExportEngine is not implemented"

    store = DescriptorStore()
    engine = engine_type(store)

    class ExportTop(Elaboratable):
        def elaborate(self, platform):
            del platform
            m = Module()
            m.submodules.store = store
            m.submodules.engine = engine
            return m

    simulation = Simulator(ExportTop())
    simulation.add_clock(1e-6, domain="usb")

    descriptors = {
        interface: bytes((interface * 0x20 + offset) & 0xFF for offset in range(length))
        for interface, length in enumerate((1, 18, 19, 53))
    }

    async def bench(ctx):
        # A non-report descriptor must not be exported.
        await seed_descriptor(ctx, store, dtype=1, index=0, w_index=0, data=bytes(range(18)))
        for interface, descriptor in descriptors.items():
            await seed_descriptor(
                ctx,
                store,
                dtype=0x22,
                index=0,
                w_index=interface,
                data=descriptor,
            )

        ctx.set(engine.message_ready, 0)
        ctx.set(engine.start, 1)
        await ctx.tick("usb")
        ctx.set(engine.start, 0)

        fragments = []
        for _ in range(3000):
            if ctx.get(engine.message_valid):
                assert ctx.get(engine.message_type) == INJ_TYPE_DESCRIPTOR_FRAGMENT
                payload = await _read_export_payload(ctx, engine)
                fragments.append(DescriptorFragmentPayload.from_bytes(payload))
                ctx.set(engine.message_ready, 1)
                await ctx.tick("usb")
                ctx.set(engine.message_ready, 0)
            else:
                await ctx.tick("usb")
            if not ctx.get(engine.busy) and not ctx.get(engine.message_valid):
                break
        else:
            raise AssertionError("descriptor export did not finish within its bounded budget")

        rebuilt = {interface: bytearray() for interface in descriptors}
        expected_offsets = {interface: 0 for interface in descriptors}
        for fragment in fragments:
            interface = fragment.interface_number
            assert fragment.descriptor_generation == 0
            assert fragment.offset == expected_offsets[interface]
            assert fragment.total == len(descriptors[interface])
            useful = min(18, fragment.total - fragment.offset)
            rebuilt[interface].extend(fragment.data[:useful])
            assert fragment.data[useful:] == bytes(18 - useful)
            expected_offsets[interface] += useful

        assert {interface: bytes(data) for interface, data in rebuilt.items()} == descriptors

    simulation.add_testbench(bench)
    simulation.run()


def test_clear_increments_generation_and_aborts_an_in_flight_export() -> None:
    engine_type = _descriptor_export_engine_type()
    assert engine_type is not None, "DescriptorExportEngine is not implemented"

    store = DescriptorStore()
    engine = engine_type(store)

    class ExportTop(Elaboratable):
        def elaborate(self, platform):
            del platform
            m = Module()
            m.submodules.store = store
            m.submodules.engine = engine
            return m

    simulation = Simulator(ExportTop())
    simulation.add_clock(1e-6, domain="usb")

    async def bench(ctx):
        await seed_descriptor(
            ctx,
            store,
            dtype=0x22,
            index=0,
            w_index=2,
            data=bytes(range(64)),
        )
        assert ctx.get(store.descriptor_generation) == 0

        ctx.set(engine.start, 1)
        await ctx.tick("usb")
        ctx.set(engine.start, 0)
        for _ in range(500):
            if ctx.get(engine.message_valid):
                break
            await ctx.tick("usb")
        else:
            raise AssertionError("descriptor fragment never became valid")

        # Stall the fragment, then clear the source store underneath it.
        assert ctx.get(engine.busy)
        ctx.set(engine.message_ready, 0)
        ctx.set(engine.message_payload_address, 8)
        ctx.set(engine.message_payload_request, 1)
        await ctx.tick("usb")
        ctx.set(engine.message_payload_request, 0)
        assert not ctx.get(engine.message_payload_response)
        ctx.set(store.clear, 1)
        await ctx.tick("usb")
        assert not ctx.get(engine.message_payload_response)
        ctx.set(store.clear, 0)
        await ctx.tick("usb")

        assert ctx.get(store.descriptor_generation) == 1
        assert not ctx.get(engine.busy)
        assert not ctx.get(engine.message_valid)

        ctx.set(store.clear, 1)
        await ctx.tick("usb")
        ctx.set(store.clear, 0)
        await ctx.tick("usb")
        assert ctx.get(store.descriptor_generation) == 2

    simulation.add_testbench(bench)
    simulation.run()
