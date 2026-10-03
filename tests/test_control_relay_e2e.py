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
    MOUSE_REPORT,
    NAK,
    OUT_PID,
    STALL,
    _power_up,
    assert_packets,
    control_in_packets,
    data_packet,
    pc_in_data_packet,
    pc_in_transaction,
    pc_receive,
    pc_send,
    pc_setup_transaction,
    pc_status_in,
    pc_status_out,
    push_report,
    setup_packet,
    token_packet,
    usb_crc16,
)

from hurra_cynthion.control_relay import ControlRelay
from hurra_cynthion.descriptors import DescriptorStore
from hurra_cynthion.device import MouseCloneDevice

HID_GET_REPORT = 0x01
HID_SET_REPORT = 0x09
HID_SET_IDLE = 0x0A
HID_SET_PROTOCOL = 0x0B

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


def _scripted_target(relay, *, in_payload: bytes, seen: dict, claim: int | None = None):
    """The device behind the relay: records what reaches it, answers late.

    Plays the role of the host-side USBControlTransferEngine talking to the
    real controller on TARGET. It honours ctl_data_ready, as the engine's
    STREAM_READ state requires, and indexes the OUT payload with
    ctl_out_index, as its WAIT_WRITE state does. ``claim`` makes it report a
    transferred length other than what it actually sent.
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
                answer = in_payload(seen["request"][2]) if callable(in_payload) else in_payload
                for byte in answer[:length]:
                    ctx.set(relay.ctl_data, byte)
                    ctx.set(relay.ctl_data_valid, 1)
                    await ctx.tick("usb").until(relay.ctl_data_ready)
                    transferred += 1
                ctx.set(relay.ctl_data_valid, 0)
            else:
                transferred = length

            ctx.set(relay.ctl_transferred, transferred if claim is None else claim)
            ctx.set(relay.ctl_status, 0)
            ctx.set(relay.ctl_done, 1)
            await ctx.tick("usb")
            ctx.set(relay.ctl_done, 0)

    return target


def _run(
    bench,
    *,
    in_payload: bytes = b"",
    mps: int | None = None,
    claim: int | None = None,
    pass_dut: bool = False,
) -> dict:
    top = _Top()
    simulation = Simulator(top)
    simulation.add_clock(1e-6, domain="usb")
    seen: dict = {}
    simulation.add_testbench(
        _scripted_target(top.relay, in_payload=in_payload, seen=seen, claim=claim),
        background=True,
    )

    async def wrapped(ctx) -> None:
        # In production the host drives this from enumerator.ready.
        ctx.set(top.relay.enable, 1)
        await _power_up(ctx, top.bus, top.dut, mps=mps)
        if pass_dut:
            await bench(ctx, top.bus, top.relay, top.dut)
        else:
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


@pytest.mark.parametrize("mps", [None, 8])
def test_zero_length_in_request_ends_with_a_status_zlp(mps: int | None) -> None:
    """PR review: an IN request with wLength 0 has no data stage.

    With no data stage the status stage is always IN [USB2.0 8.5.3], so the
    device must answer the host's IN token with a zero-length DATA1 packet --
    whatever direction bmRequestType names. Choosing the status reply from
    ``is_in_request`` alone answered it with a bare ACK handshake, which is
    not a valid reply to an IN token. At an 8-byte EP0 the handler owns the
    data PID, so this also checks nothing left it at DATA0.
    """

    async def bench(ctx, bus, relay) -> None:
        await pc_setup_transaction(ctx, bus, 0, setup_packet(0xA1, HID_GET_REPORT, 0x03F1, 3, 0))
        status = await pc_in_transaction(ctx, bus, 0, 0)
        assert status and status[0] == DATA1, f"status IN was not DATA1: {status!r}"
        assert status[1:-2] == [], "status IN stage was not a zero-length packet"
        await pc_send(ctx, bus, [ACK])

        await _assert_relay_released(ctx, relay)

    seen = _run(bench, in_payload=AUTH_RESPONSE, mps=mps)
    assert seen["request"] == (0xA1, HID_GET_REPORT, 0x03F1, 3, 0)


@pytest.mark.parametrize(("request_code", "value"), [(HID_SET_IDLE, 0x0100), (HID_SET_PROTOCOL, 0)])
def test_set_idle_and_set_protocol_reach_the_target_verbatim(request_code: int, value: int) -> None:
    """Answered locally, a BIOS's SET_PROTOCOL(boot) never reached the real device.

    The PC then parsed report-protocol data as the boot layout. Both requests
    take the ordinary forward now: NAKed until the target answers, then the
    status stage's zero-length DATA1.
    """

    async def bench(ctx, bus, relay) -> None:
        await pc_setup_transaction(ctx, bus, 0, setup_packet(0x21, request_code, value, 3, 0))
        await pc_status_in(ctx, bus, 0)
        await _assert_relay_released(ctx, relay)

    seen = _run(bench)
    assert seen["request"] == (0x21, request_code, value, 3, 0)


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


def _answer_by_value(value: int) -> bytes:
    """Byte 0 of the answer echoes the low byte of wValue (the report ID).

    Lets a test tell one request's answer from another's, so a stale reply
    served to the wrong request is visible rather than coincidentally equal.
    """
    return bytes([value & 0xFF]) + AUTH_RESPONSE[1:]


async def _get_report(ctx, bus, value: int, length: int = 16) -> list[int]:
    """Run a GET_REPORT to completion, retrying once if it is STALLed."""
    for _ in range(2):
        await pc_setup_transaction(
            ctx, bus, 0, setup_packet(0xA1, HID_GET_REPORT, value, 3, length)
        )
        response = await pc_in_transaction(ctx, bus, 0, 0)
        if response == [0x1E]:  # STALL -- the host's move is to retry
            continue
        await pc_send(ctx, bus, [ACK])
        await pc_send(ctx, bus, token_packet(OUT_PID, 0, 0))
        await pc_send(ctx, bus, data_packet(DATA1, []))
        assert await pc_receive(ctx, bus) == [ACK]
        return response
    raise AssertionError("GET_REPORT was STALLed twice in a row")


def test_abandoned_set_report_does_not_wedge_the_relay() -> None:
    """Review C1: a SET_REPORT abandoned before its data stage.

    The host sends the SETUP, then a new SETUP instead of the OUT data. The
    relay was left in CAPTURE_OUT forever, holding request_pending -- which
    gates the arbiter, so every interrupt poller would starve until power
    cycle. The next control transfer must still work.
    """

    async def bench(ctx, bus, relay) -> None:
        await pc_setup_transaction(ctx, bus, 0, setup_packet(0x21, HID_SET_REPORT, 0x03F0, 3, 16))
        # ...and abandon it: no OUT data, straight to a new request.
        response = await _get_report(ctx, bus, 0x03F2)
        assert response[1] == 0xF2, f"expected the 0xF2 report, got {response[1]:#x}"
        await _assert_relay_released(ctx, relay)

    _run(bench, in_payload=_answer_by_value)


def test_new_setup_mid_transfer_never_gets_the_old_answer() -> None:
    """Review I2: GET_REPORT A abandoned while deferred, then GET_REPORT B.

    The handler only consulted setup.received in IDLE, so B's IN token was
    answered from the buffer A's transfer filled -- the console would get the
    wrong report, silently. B may be STALLed (the host retries); it must
    never receive A's bytes.
    """

    async def bench(ctx, bus, relay) -> None:
        await pc_setup_transaction(ctx, bus, 0, setup_packet(0xA1, HID_GET_REPORT, 0x03F1, 3, 16))
        # One NAKed IN token, so A is genuinely in flight, then abandon it.
        await pc_send(ctx, bus, token_packet(0x9, 0, 0))
        assert await pc_receive(ctx, bus) == [NAK]
        response = await _get_report(ctx, bus, 0x03F2)
        assert (
            response[1] == 0xF2
        ), f"GET_REPORT(0xF2) was answered with report {response[1]:#x}: a stale reply"
        await _assert_relay_released(ctx, relay)

    _run(bench, in_payload=_answer_by_value)


async def _out_data_retrying_naks(ctx, bus, payload: list[int]) -> None:
    """Send one OUT data packet, resending it on NAK as a host does."""
    for _ in range(60):
        await pc_send(ctx, bus, token_packet(OUT_PID, 0, 0))
        await pc_send(ctx, bus, data_packet(DATA1, payload))
        # pc_receive raises on silence, which is the failure this guards.
        handshake = await pc_receive(ctx, bus)
        if handshake == [ACK]:
            return
        assert handshake == [NAK], f"OUT data got {handshake!r}, not ACK or NAK"
    raise AssertionError("OUT data NAKed past the retry budget")


def test_set_report_arriving_during_a_drain_is_held_off_not_lost() -> None:
    """Re-review N2: OUT data for a request queued behind a drain.

    I2's fix NAKs a new request while the abandoned one drains, then runs it.
    But a SET_REPORT's OUT data arrives as rx_ready_for_response, not as the
    data/status strobes DRAIN answered, so it got SILENCE -- the host sees
    transaction errors and fails exactly the request I2 meant to rescue.
    """

    async def bench(ctx, bus, relay) -> None:
        # GET_REPORT A in flight, then abandoned for SET_REPORT B.
        await pc_setup_transaction(ctx, bus, 0, setup_packet(0xA1, HID_GET_REPORT, 0x03F1, 3, 16))
        await pc_send(ctx, bus, token_packet(0x9, 0, 0))
        assert await pc_receive(ctx, bus) == [NAK]
        challenge = list(AUTH_CHALLENGE[:16])
        await pc_setup_transaction(ctx, bus, 0, setup_packet(0x21, HID_SET_REPORT, 0x03F0, 3, 16))
        await _out_data_retrying_naks(ctx, bus, challenge)
        status = await pc_in_transaction(ctx, bus, 0, 0)
        assert status and status[0] in (DATA0, DATA1) and status[1:-2] == [], status
        await pc_send(ctx, bus, [ACK])
        await _assert_relay_released(ctx, relay)

    seen = _run(bench, in_payload=_answer_by_value)
    assert seen["request"][1] == HID_SET_REPORT, f"B never reached the target: {seen['request']}"
    assert seen["out_payload"] == AUTH_CHALLENGE[:16]


# Forwarded responses at the cloned device's own bMaxPacketSize0.
#
# The clone advertises the real device's EP0 size, so a Full Speed host splits
# a forwarded GET_REPORT's data stage at 8 bytes just as it would with the real
# controller. A response streamed as one packet regardless was babble there.


def test_forwarded_get_report_is_split_at_bmaxpacketsize0() -> None:
    async def bench(ctx, bus, relay) -> None:
        packets = await control_in_packets(
            ctx, bus, setup_bytes=setup_packet(0xA1, HID_GET_REPORT, 0x03F1, 3, 20), mps=8
        )
        assert_packets(packets, sizes=[8, 8, 4], data=AUTH_RESPONSE[:20])
        await _assert_relay_released(ctx, relay)

    _run(bench, in_payload=AUTH_RESPONSE, mps=8)


def test_forwarded_response_on_a_packet_boundary_ends_with_a_zlp() -> None:
    """The target answered 16 of the 64 bytes asked for: two full packets.

    The host has seen no short packet and wLength is not met, so it asks
    again; the answer is a ZLP carrying the third PID in the sequence.
    """

    async def bench(ctx, bus, relay) -> None:
        packets = await control_in_packets(
            ctx, bus, setup_bytes=setup_packet(0xA1, HID_GET_REPORT, 0x03F1, 3, 64), mps=8
        )
        assert_packets(packets, sizes=[8, 8, 0], data=AUTH_RESPONSE[:16])
        await _assert_relay_released(ctx, relay)

    _run(bench, in_payload=AUTH_RESPONSE[:16], mps=8)


def test_forwarded_packet_is_resent_verbatim_after_a_lost_ack() -> None:
    """Only an ACK advances the data stage [USB2.0 8.6.3, 8.6.4].

    Restarting at byte 0 on every IN token made a retry correct only while a
    response fitted one packet. Across packets the retry must resend packet k
    -- same bytes, same PID -- not the start of the response, and not k+1.
    """

    async def bench(ctx, bus, relay) -> None:
        await pc_setup_transaction(ctx, bus, 0, setup_packet(0xA1, HID_GET_REPORT, 0x03F1, 3, 20))
        assert await pc_in_data_packet(ctx, bus, 0) == (DATA1, AUTH_RESPONSE[0:8])
        await pc_send(ctx, bus, [ACK])
        second = await pc_in_data_packet(ctx, bus, 0)
        assert second == (DATA0, AUTH_RESPONSE[8:16]), second
        # The host never ACKs it, and asks again.
        retry = await pc_in_data_packet(ctx, bus, 0)
        assert retry == second, f"retry changed the packet: {second!r} -> {retry!r}"
        await pc_send(ctx, bus, [ACK])
        assert await pc_in_data_packet(ctx, bus, 0) == (DATA1, AUTH_RESPONSE[16:20])
        await pc_send(ctx, bus, [ACK])
        await pc_status_out(ctx, bus, 0)
        await _assert_relay_released(ctx, relay)

    _run(bench, in_payload=AUTH_RESPONSE, mps=8)


async def _out_data_stage(ctx, bus, payload: bytes, mps: int) -> None:
    """The host side of a split OUT data stage: DATA1, DATA0, ... per packet."""
    for number, offset in enumerate(range(0, len(payload), mps)):
        await pc_send(ctx, bus, token_packet(OUT_PID, 0, 0))
        pid = DATA1 if number % 2 == 0 else DATA0
        await pc_send(ctx, bus, data_packet(pid, list(payload[offset : offset + mps])))
        handshake = await pc_receive(ctx, bus)
        assert handshake == [ACK], f"OUT packet {number} got {handshake!r}"


def test_set_report_status_zlp_is_data1_at_an_8_byte_ep0() -> None:
    """The status stage always carries DATA1 [USB2.0 8.5.3].

    The handler drives its own data PID now, so nothing about a split OUT data
    stage may leave it toggled when the status IN arrives.
    """
    challenge = AUTH_CHALLENGE[:20]

    async def bench(ctx, bus, relay) -> None:
        await pc_setup_transaction(
            ctx, bus, 0, setup_packet(0x21, HID_SET_REPORT, 0x03F0, 3, len(challenge))
        )
        await _out_data_stage(ctx, bus, challenge, 8)
        status = await pc_in_transaction(ctx, bus, 0, 0)
        assert status[0] == DATA1, f"status IN was not DATA1: {status!r}"
        assert status[1:-2] == [], "status IN stage was not a zero-length packet"
        await pc_send(ctx, bus, [ACK])
        await _assert_relay_released(ctx, relay)

    seen = _run(bench, mps=8)
    assert seen["out_payload"] == challenge


def test_new_setup_after_one_packet_restarts_at_data1_and_byte_0() -> None:
    """A transfer abandoned mid data stage must not leak into the next.

    GET_REPORT A is left after its first packet has been ACKed -- the data PID
    toggled and the packet base advanced. GET_REPORT B must start from its own
    byte 0 on DATA1, or the host drops its first packet as a duplicate.
    """

    async def bench(ctx, bus, relay) -> None:
        await pc_setup_transaction(ctx, bus, 0, setup_packet(0xA1, HID_GET_REPORT, 0x03F1, 3, 20))
        pid, payload = await pc_in_data_packet(ctx, bus, 0)
        assert (pid, len(payload), payload[0]) == (DATA1, 8, 0xF1), (pid, payload)
        await pc_send(ctx, bus, [ACK])

        # ...and abandon A. B may be STALLed while A drains; the host retries.
        for _ in range(2):
            await pc_setup_transaction(
                ctx, bus, 0, setup_packet(0xA1, HID_GET_REPORT, 0x03F2, 3, 20)
            )
            response = await pc_in_transaction(ctx, bus, 0, 0)
            if response != [STALL]:
                break
        else:
            raise AssertionError("GET_REPORT B was STALLed twice in a row")
        packets = [(response[0], bytes(response[1:-2]))]
        await pc_send(ctx, bus, [ACK])
        for _ in range(2):
            packets.append(await pc_in_data_packet(ctx, bus, 0))
            await pc_send(ctx, bus, [ACK])
        await pc_status_out(ctx, bus, 0)

        assert_packets(packets, sizes=[8, 8, 4], data=_answer_by_value(0xF2)[:20])
        await _assert_relay_released(ctx, relay)

    _run(bench, in_payload=_answer_by_value, mps=8)


def test_forwarded_exact_multiple_meeting_wlength_needs_no_zlp() -> None:
    """wLength met on a packet boundary: the host goes straight to status."""

    async def bench(ctx, bus, relay) -> None:
        packets = await control_in_packets(
            ctx, bus, setup_bytes=setup_packet(0xA1, HID_GET_REPORT, 0x03F1, 3, 16), mps=8
        )
        assert_packets(packets, sizes=[8, 8], data=AUTH_RESPONSE[:16])
        await _assert_relay_released(ctx, relay)

    _run(bench, in_payload=AUTH_RESPONSE, mps=8)


def test_forwarded_empty_response_is_a_single_data1_zlp() -> None:
    """The target returned nothing for a non-zero wLength.

    The first IN finds the packet already empty and gets a ZLP on DATA1, which
    ends the data stage; the host then runs the status stage as usual.
    """

    async def bench(ctx, bus, relay) -> None:
        packets = await control_in_packets(
            ctx, bus, setup_bytes=setup_packet(0xA1, HID_GET_REPORT, 0x03F1, 3, 16), mps=8
        )
        assert_packets(packets, sizes=[0], data=b"")
        await _assert_relay_released(ctx, relay)

    _run(bench, in_payload=b"", mps=8)


@pytest.mark.parametrize("length", [16, 64])
def test_overlong_claimed_response_is_clamped_to_the_relay_buffer(length: int) -> None:
    """A target claiming far more than the 64-byte buffer holds.

    The engine refuses a data stage longer than wLength, so this cannot happen
    with the real one; the clamp is defence in depth. With it, the clone serves
    only what the buffer holds, in bMaxPacketSize0 packets, and the host --
    which stops at wLength -- completes normally.
    """

    async def bench(ctx, bus, relay) -> None:
        packets = await control_in_packets(
            ctx,
            bus,
            setup_bytes=setup_packet(0xA1, HID_GET_REPORT, 0x03F1, 3, length),
            mps=8,
        )
        assert_packets(packets, sizes=[8] * (length // 8), data=AUTH_RESPONSE[:length])
        await _assert_relay_released(ctx, relay)

    _run(bench, in_payload=AUTH_RESPONSE, mps=8, claim=4096)


def test_ack_for_another_endpoint_does_not_advance_a_forwarded_response() -> None:
    """LUNA hands every endpoint every ACK the host sends.

    Between EP0 packet k and its retry, the host polls EP1 and ACKs that
    report. Taking the ACK as EP0's skipped packet k: the console would get a
    response with a hole in it, on the PID it expected.
    """

    async def bench(ctx, bus, relay, dut) -> None:
        await pc_setup_transaction(ctx, bus, 0, setup_packet(0xA1, HID_GET_REPORT, 0x03F1, 3, 20))
        first = await pc_in_data_packet(ctx, bus, 0)
        assert first == (DATA1, AUTH_RESPONSE[0:8]), first
        # No ACK for it. The host polls EP1 instead, and ACKs that report.
        await push_report(ctx, dut, 1, MOUSE_REPORT)
        report = await pc_in_transaction(ctx, bus, 0, 1)
        assert report[1:-2] == MOUSE_REPORT, f"EP1 did not return the report: {report!r}"
        await pc_send(ctx, bus, [ACK])

        retry = await pc_in_data_packet(ctx, bus, 0)
        assert retry == first, f"an EP1 ACK advanced EP0: {first!r} -> {retry!r}"
        await pc_send(ctx, bus, [ACK])
        assert await pc_in_data_packet(ctx, bus, 0) == (DATA0, AUTH_RESPONSE[8:16])
        await pc_send(ctx, bus, [ACK])
        assert await pc_in_data_packet(ctx, bus, 0) == (DATA1, AUTH_RESPONSE[16:20])
        await pc_send(ctx, bus, [ACK])
        await pc_status_out(ctx, bus, 0)
        await _assert_relay_released(ctx, relay)

    _run(bench, in_payload=AUTH_RESPONSE, mps=8, pass_dut=True)
