"""Test helpers for populating a DescriptorStore in simulation."""


async def seed_descriptor(ctx, store, dtype: int, index: int, w_index: int, data: bytes) -> None:
    """Capture one descriptor into `store` via its capture handshake (usb domain)."""
    ctx.set(store.capture_type, dtype)
    ctx.set(store.capture_index, index)
    ctx.set(store.capture_w_index, w_index)
    ctx.set(store.capture_start, 1)
    await ctx.tick("usb")
    ctx.set(store.capture_start, 0)
    for byte in data:
        ctx.set(store.capture_data, byte)
        ctx.set(store.capture_valid, 1)
        # Advance while capture_ready is asserted.
        while not ctx.get(store.capture_ready):
            await ctx.tick("usb")
        await ctx.tick("usb")
    ctx.set(store.capture_valid, 0)
    ctx.set(store.capture_commit, 1)
    await ctx.tick("usb")
    ctx.set(store.capture_commit, 0)
    await ctx.tick("usb")
