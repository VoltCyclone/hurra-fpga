"""End to end: a PC's SET_PROTOCOL reaches the real device, and is undone on a PC reset.

The production host and clone, joined by ``gateware.connect_clone_to_host`` --
the function the production top calls -- against a raw-UTMI model of the real
device (``test_out_relay_e2e``'s harness). Nothing between the PC's wire and the
device's is stubbed: LUNA's control endpoint, the HID class handler, the
control relay, the boot-protocol tracker, the arbiter and the transaction
engine are all real.
"""

from _out_relay_target import OutRelayTarget, hid_configuration, wait_until
from test_device_clone_e2e import (
    SET_CONFIGURATION,
    control_write_no_data,
    pc_setup_transaction,
    pc_status_in,
    setup_packet,
)
from test_end_to_end import SetupRequest
from test_out_relay_e2e import CLONE_ADDRESS, _run

HID_SET_PROTOCOL = 0x0B
IN_ENDPOINT = 1

#: Generous: the clone NAKs the status stage until the real device has
#: answered, behind any in-flight poll, across a whole control transfer.
STATUS_NAK_BUDGET = 400
#: The snoop marks the endpoint a few cycles after the relay completes --
#: forward_ok, the tracker's registered decode, its mask write -- and the
#: PC's ACK of the status stage comes after that completion anyway.
MARK_SETTLE_CYCLES = 50


def _target() -> OutRelayTarget:
    return OutRelayTarget(
        configuration=hid_configuration(
            in_address=0x80 | IN_ENDPOINT,
            out_address=0x02,
            in_mps=8,
            out_mps=8,
            interval=1,
            report_length=5,
        ),
        report_descriptor=bytes(5),
        interface=0,
        in_endpoint=IN_ENDPOINT,
        out_endpoint=2,
    )


async def _pc_set_protocol(ctx, dut, value: int) -> None:
    bus = dut.pc_bus
    await pc_setup_transaction(
        ctx, bus, CLONE_ADDRESS, setup_packet(0x21, HID_SET_PROTOCOL, value, 0, 0)
    )
    await pc_status_in(ctx, bus, CLONE_ADDRESS, max_attempts=STATUS_NAK_BUDGET)


def test_a_pc_set_protocol_boot_reaches_the_device_and_marks_its_endpoint() -> None:
    target = _target()

    async def bench(ctx, dut) -> None:
        host = dut.host
        assert ctx.get(host.boot_protocol) == 0
        await _pc_set_protocol(ctx, dut, 0)
        assert target.control_requests == [SetupRequest(0x21, HID_SET_PROTOCOL, 0, 0, 0)]
        await wait_until(
            ctx,
            lambda: ctx.get(host.boot_protocol) == 1 << IN_ENDPOINT,
            limit=MARK_SETTLE_CYCLES,
            what="the boot mark",
        )
        assert ctx.get(host.enumerated)

    _run(target, bench)


def test_a_pc_reconfiguration_returns_the_device_to_report_protocol() -> None:
    """A BIOS-to-OS handoff: the OS reconfigures the clone and may never send SET_PROTOCOL(1)."""
    target = _target()

    async def bench(ctx, dut) -> None:
        host = dut.host
        await _pc_set_protocol(ctx, dut, 0)
        await wait_until(
            ctx,
            lambda: ctx.get(host.boot_protocol) != 0,
            limit=MARK_SETTLE_CYCLES,
            what="the boot mark",
        )
        assert (
            await control_write_no_data(
                ctx,
                dut.pc_bus,
                address=CLONE_ADDRESS,
                request_type=0x00,
                request=SET_CONFIGURATION,
                value=1,
                index=0,
                pulse_signal=dut.device.debug_config_changed,
                value_signal=dut.device.debug_new_config,
            )
            == 1
        )
        await wait_until(
            ctx, lambda: ctx.get(host.boot_protocol) == 0, limit=20_000, what="the replay"
        )
        assert target.control_requests == [
            SetupRequest(0x21, HID_SET_PROTOCOL, 0, 0, 0),
            SetupRequest(0x21, HID_SET_PROTOCOL, 1, 0, 0),
        ]
        assert ctx.get(host.enumerated)

    _run(target, bench)
