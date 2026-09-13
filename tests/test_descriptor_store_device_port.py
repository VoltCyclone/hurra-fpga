from _descriptor_store_helpers import seed_descriptor
from amaranth.sim import Simulator

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
    out = []
    for offset in range(length):
        ctx.set(store.device_lookup_offset, offset)
        for _ in range(4):
            await ctx.tick("usb")
            if ctx.get(store.device_lookup_data_valid):
                break
        else:
            raise AssertionError("device payload read was not granted")
        out.append(ctx.get(store.device_lookup_data))
    return out


def test_device_port_reads_same_bytes_as_capture():
    store = DescriptorStore()
    sim = Simulator(store)
    sim.add_clock(1e-6, domain="usb")

    device_desc = bytes(range(18))

    async def bench(ctx):
        await seed_descriptor(ctx, store, dtype=1, index=0, w_index=0, data=device_desc)
        # device-side port sees it
        ctx.set(store.device_lookup_type, 1)
        ctx.set(store.device_lookup_index, 0)
        ctx.set(store.device_lookup_w_index, 0)
        assert await _read_device_port(ctx, store, 1, 0, 0, 18) == list(device_desc)
        assert ctx.get(store.device_lookup_found) == 1
        assert ctx.get(store.device_lookup_length) == 18
        # a miss reports not-found
        ctx.set(store.device_lookup_type, 2)
        ctx.set(store.device_lookup_request, 1)
        await ctx.tick("usb")
        ctx.set(store.device_lookup_request, 0)
        while not ctx.get(store.device_lookup_response):
            await ctx.tick("usb")
        assert ctx.get(store.device_lookup_found) == 0

    sim.add_testbench(bench)
    sim.run()
