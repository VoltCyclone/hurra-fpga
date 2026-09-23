"""End-to-end: HID class control transfers forwarded verbatim, on the wire.

Correctness of the control relay is "bytes in equal bytes out". The gateware
never interprets the request, so a console's authentication handshake is just
a byte sequence like any other -- which is what lets this be validated with no
console on the bench.

Nothing here is stubbed on the forwarding path. A scripted PC drives the raw
UTMI bus into the real ``MouseCloneDevice`` -- so the requests pass through
LUNA's real control endpoint and request multiplexer -- whose
``HIDClassRequestHandler`` forwards through the real ``ControlRelay``. Only
the device on the far side of the relay is scripted, and it deliberately
takes its time, so every transfer exercises the NAK deferral rather than
completing before the host could notice.

Stubs are what let three handler bugs through earlier on this branch: a
dropped relay ack, a truncated read address, and an IN packet streamed only
while a one-cycle strobe was high. This file is the guard against that class.
"""

import pytest
from amaranth import Elaboratable, Module
from amaranth.sim import Simulator
from luna.gateware.interface.utmi import UTMIInterface
from test_device_clone_e2e import (
    ACK,
    DATA0,
    DATA1,
    NAK,
    OUT_PID,
    _power_up,
    data_packet,
    pc_in_transaction,
    pc_receive,
    pc_send,
    pc_setup_transaction,
    setup_packet,
    token_packet,
    usb_crc16,
)

from hurra_cynthion.control_relay import ControlRelay
from hurra_cynthion.descriptors import DescriptorStore
from hurra_cynthion.device import MouseCloneDevice

HID_GET_REPORT = 0x01
HID_SET_REPORT = 0x09

#: How long the scripted target sits on a request before answering. Long
#: enough that the host's IN tokens arrive while the relay is still working
#: and must be NAKed, short enough to stay inside the NAK retry budget
#: (MAX_NAK_ATTEMPTS = 50 in test_device_clone_e2e). Measured: one scripted IN
#: round trip is about 9 cycles, so 1500 cycles produced 170 NAKs and blew the
#: budget; 200 yields roughly 20, comfortably inside it and still well past
#: the three or so retries a host would give silence.
TARGET_DELAY_CYCLES = 200

#: Shaped like the DS4 auth traffic this path exists for, but the gateware
#: never sees them as anything but bytes.
AUTH_CHALLENGE = bytes([0xF0, 0x01, 0x00, 0x00] + list(range(60)))
AUTH_RESPONSE = bytes([0xF1, 0x01] + list(range(62)))


class _Top(Elaboratable):
    def __init__(self):
        self.bus = UTMIInterface()
        self.store = DescriptorStore()
        self.relay = ControlRelay()
        self.dut = MouseCloneDevice(bus=self.bus, store=self.store, control_relay=self.relay)

    def elaborate(self, platform):
        del platform
        m = Module()
        m.submodules.store = self.store
        m.submodules.relay = self.relay
        m.submodules.dut = self.dut
        return m


def _scripted_target(relay, *, in_payload: bytes, seen: dict):
    """The device behind the relay: records what reaches it, answers late.

    Plays the role of the host-side USBControlTransferEngine talking to the
    real controller on TARGET. It honours ctl_data_ready, as the engine's
    STREAM_READ state requires, and indexes the OUT payload with
    ctl_out_index, as its WAIT_WRITE state does.
    """

    async def target(ctx) -> None:
        while True:
            await ctx.tick("usb").until(relay.ctl_start)
            seen["request"] = (
                ctx.get(relay.ctl_request_type),
                ctx.get(relay.ctl_request),
                ctx.get(relay.ctl_value),
                ctx.get(relay.ctl_index),
                ctx.get(relay.ctl_length),
            )
            length = ctx.get(relay.ctl_length)
            is_in = bool(ctx.get(relay.ctl_request_type) & 0x80)

            if not is_in:
                received = []
                for offset in range(length):
                    ctx.set(relay.ctl_out_index, offset)
                    await ctx.delay(1e-9)
                    received.append(ctx.get(relay.ctl_out_payload))
                seen["out_payload"] = bytes(received)

            for _ in range(TARGET_DELAY_CYCLES):
                await ctx.tick("usb")

            transferred = 0
            if is_in:
                for byte in in_payload[:length]:
                    ctx.set(relay.ctl_data, byte)
                    ctx.set(relay.ctl_data_valid, 1)
                    await ctx.tick("usb").until(relay.ctl_data_ready)
                    transferred += 1
                ctx.set(relay.ctl_data_valid, 0)
            else:
                transferred = length

            ctx.set(relay.ctl_transferred, transferred)
            ctx.set(relay.ctl_status, 0)
            ctx.set(relay.ctl_done, 1)
            await ctx.tick("usb")
            ctx.set(relay.ctl_done, 0)

    return target


def _run(bench, *, in_payload: bytes = b"") -> dict:
    top = _Top()
    simulation = Simulator(top)
    simulation.add_clock(1e-6, domain="usb")
    seen: dict = {}
    simulation.add_testbench(
        _scripted_target(top.relay, in_payload=in_payload, seen=seen), background=True
    )

    async def wrapped(ctx) -> None:
        await _power_up(ctx, top.bus, top.dut)
        await bench(ctx, top.bus, top.relay)

    simulation.add_testbench(wrapped)
    simulation.run()
    return seen


async def _assert_relay_released(ctx, relay) -> None:
    """request_pending gates the arbiter's control phase.

    If a finished transfer ever left it high, every interrupt poller would be
    starved for good and the controller would stop reporting.
    """
    for _ in range(20):
        if not ctx.get(relay.request_pending):
            return
        await ctx.tick("usb")
    raise AssertionError("relay still pending after the transfer: pollers would starve")


@pytest.mark.parametrize("length", [1, 16, 63, 64])
def test_get_report_round_trips_verbatim(length: int) -> None:
    """GET_REPORT: the PC receives exactly what the device returned."""
    expected = AUTH_RESPONSE[:length]

    async def bench(ctx, bus, relay) -> None:
        await pc_setup_transaction(
            ctx, bus, 0, setup_packet(0xA1, HID_GET_REPORT, 0x03F1, 3, length)
        )
        # NAKed while the target works -- pc_receive would raise on silence.
        response = await pc_in_transaction(ctx, bus, 0, 0)
        assert response[0] == DATA1, f"first data-stage packet must be DATA1: {response!r}"
        payload = bytes(response[1:-2])
        assert payload == expected, f"payload altered in transit: {payload.hex()}"
        assert (response[-2] | (response[-1] << 8)) == usb_crc16(list(payload))

        await pc_send(ctx, bus, [ACK])
        # Status stage after IN data is OUT; the device ACKs it.
        await pc_send(ctx, bus, token_packet(OUT_PID, 0, 0))
        await pc_send(ctx, bus, data_packet(DATA1, []))
        assert await pc_receive(ctx, bus) == [ACK], "status OUT stage was not ACKed"

        await _assert_relay_released(ctx, relay)

    seen = _run(bench, in_payload=AUTH_RESPONSE)
    assert seen["request"] == (
        0xA1,
        HID_GET_REPORT,
        0x03F1,
        3,
        length,
    ), f"request altered in transit: {seen['request']}"


@pytest.mark.parametrize("length", [0, 1, 16, 63, 64])
def test_set_report_round_trips_verbatim(length: int) -> None:
    """SET_REPORT: the device receives exactly what the PC sent."""
    challenge = AUTH_CHALLENGE[:length]

    async def bench(ctx, bus, relay) -> None:
        await pc_setup_transaction(
            ctx, bus, 0, setup_packet(0x21, HID_SET_REPORT, 0x03F0, 3, length)
        )
        if length:
            await pc_send(ctx, bus, token_packet(OUT_PID, 0, 0))
            await pc_send(ctx, bus, data_packet(DATA1, list(challenge)))
            assert await pc_receive(ctx, bus) == [ACK], "OUT data stage was not ACKed"

        # Status stage after OUT data (or none) is IN: NAKed while the target
        # works, then a zero-length DATA packet -- never a bare handshake.
        status = await pc_in_transaction(ctx, bus, 0, 0)
        assert status and status[0] in (DATA0, DATA1), f"status IN was not DATA: {status!r}"
        assert status[1:-2] == [], "status IN stage was not a zero-length packet"
        await pc_send(ctx, bus, [ACK])

        await _assert_relay_released(ctx, relay)

    seen = _run(bench)
    assert seen["request"] == (0x21, HID_SET_REPORT, 0x03F0, 3, length)
    if length:
        assert (
            seen["out_payload"] == challenge
        ), f"payload altered in transit: {seen['out_payload'].hex()}"


def test_deferral_is_nak_not_silence_on_the_wire() -> None:
    """While the target works, every IN token gets a NAK.

    LUNA emits nothing unless a handler drives a handshake, and silence is a
    transaction error the host abandons after about three retries. This
    counts the NAKs directly, so a regression to silence fails here with a
    clear message rather than as a timeout somewhere else.
    """

    async def bench(ctx, bus, relay) -> None:
        await pc_setup_transaction(ctx, bus, 0, setup_packet(0xA1, HID_GET_REPORT, 0x03F1, 3, 16))
        naks = 0
        while True:
            await pc_send(ctx, bus, token_packet(0x9, 0, 0))
            response = await pc_receive(ctx, bus)
            if response != [NAK]:
                break
            naks += 1
        assert naks >= 2, f"expected the target delay to force NAKs, saw {naks}"
        assert response[0] == DATA1

    _run(bench, in_payload=AUTH_RESPONSE)
