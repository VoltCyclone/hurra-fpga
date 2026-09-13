"""Clock-domain timing profiles for hardware and short simulations."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HostTiming:
    """Cycle counts used by the bounded full-speed host state machines."""

    clock_hz: int
    frame_cycles: int
    microframe_cycles: int
    attach_stable_cycles: int
    detach_stable_cycles: int
    reset_cycles: int
    chirp_detect_cycles: int
    chirp_drive_cycles: int
    chirp_timeout_cycles: int
    chirp_tail_cycles: int
    reset_recovery_cycles: int
    address_recovery_cycles: int
    vbus_discharge_cycles: int
    transaction_timeout_cycles: int
    interpacket_delay_cycles: int
    hs_interpacket_delay_cycles: int
    max_transport_errors: int
    control_nak_timeout_cycles: int

    @classmethod
    def hardware(cls, clock_hz: int = 60_000_000) -> "HostTiming":
        """Return USB timings for the Cynthion ``usb`` domain."""
        return cls(
            clock_hz=clock_hz,
            frame_cycles=clock_hz // 1_000,
            # High Speed schedules on 125 us microframes, eight to the 1 ms
            # frame. The 11-bit frame number still advances once per frame
            # (USB 2.0 section 8.4.3), so eight consecutive SOFs carry the same
            # number and the scheduler counts microframes separately. The ratio
            # must divide exactly or the two counters drift.
            microframe_cycles=clock_hz // 8_000,
            attach_stable_cycles=clock_hz // 10,
            detach_stable_cycles=max(1, clock_hz // 400_000),
            # Root-port reset must last at least 50 ms (USB 2.0 section 7.1.7.5,
            # TDRSTR); clock_hz // 20 is 50 ms. The shorter TDRST (10-20 ms) only
            # applies to hub-driven SetPortFeature(PORT_RESET), not a root port.
            reset_cycles=clock_hz // 20,
            # High-speed detection handshake, USB 2.0 section 7.1.7.5.
            #
            # How long a K must persist to count as the device's chirp rather
            # than a glitch. The device chirp lasts 1-7 ms, so 2.5 us is a
            # generous qualifier -- it matches the chirp-bit validity time
            # LUNA's device-side sequencer uses for the mirror of this.
            chirp_detect_cycles=max(1, clock_hz // 400_000),
            # Each host K and each host J lasts 40-60 us; 50 us sits in the
            # middle. Six of these back to back is the K-J-K-J-K-J the device
            # is waiting for, about 300 us inside a 50 ms reset.
            chirp_drive_cycles=max(1, clock_hz // 20_000),
            # How long to wait for the device chirp to appear before
            # concluding the device is Full Speed only. The device must start
            # chirping within 7.3 ms of the reset (T_UCH); 7.5 ms covers it
            # with margin and still leaves most of the 50 ms reset window.
            chirp_timeout_cycles=max(1, (clock_hz * 3) // 400),
            # SE0 tail at the end of the reset, after the host stops
            # chirping. USB 2.0 section 7.1.7.5 requires the host to keep
            # alternating K and J until 100-500 us before the reset ends and
            # only then stop driving; 200 us sits in the middle.
            #
            # This is not cosmetic. The device switches to High Speed within
            # 500 us of seeing three K-J pairs, and a High Speed device reads
            # 3 ms of squelch as a reset. Stopping the chirp early and
            # holding SE0 for the rest of a 50 ms reset therefore knocks the
            # device it just trained straight back down to Full Speed.
            chirp_tail_cycles=max(1, clock_hz // 5_000),
            # Stable-J debounce after reset before the first SETUP: the device's
            # pull-up must be steady for this long (SOF runs during the wait, and
            # RESET_RECOVERY tolerates the reboot SE0 up to a longer timeout).
            reset_recovery_cycles=max(1, clock_hz // 100),
            address_recovery_cycles=max(1, clock_hz // 500),
            vbus_discharge_cycles=clock_hz // 10,
            transaction_timeout_cycles=clock_hz // 1_000,
            # Gap between the SETUP/OUT token and its DATA packet. The ULPI PHY
            # has a TX FIFO: the whole 3-byte token is *accepted* in ~7 UTMI
            # cycles, but the PHY then serialises it onto the wire over the full
            # token length (SYNC+PID+addr/ep+CRC5 = 32 FS bits) plus its EOP.
            # tx_valid deasserting is therefore NOT "token done on the wire". If
            # DATA is handed to the FIFO while the token is still serialising, the
            # PHY appends it and emits a single merged, unterminated packet with no
            # EOP between token and data -- the device then never ACKs. So this
            # gap must outlast the token's whole wire serialisation + EOP, then add
            # a short interpacket gap: ~40 FS bit-times (one FS bit = clock_hz//12e6
            # cycles) covers the 32-bit token + EOP + a few bits, and lands DATA a
            # few bit-times after the token EOP -- inside the device's turnaround.
            interpacket_delay_cycles=40 * max(1, clock_hz // 12_000_000),
            # The same hazard at High Speed, but the arithmetic inverts and the
            # Full Speed constant cannot simply be scaled down. UTMI carries 8
            # bits per clock at HS, so the PHY serialises one byte per clock and
            # the token's whole wire form -- SYNC (32 bits) + PID (8) +
            # address/endpoint/CRC5 (16) + EOP (8) = 64 bits -- takes 8 clocks
            # rather than ~160. Scaling 200 down by the 40x rate ratio gives 5,
            # which is SHORTER than the 8 clocks of serialisation this delay
            # exists to outlast -- it would reintroduce exactly the merged,
            # unterminated packet the Full Speed value was written to prevent.
            # So: 8 clocks of serialisation plus 4 clocks of margin. The margin
            # is 32 HS bit times, inside the 8-192 bit-time interpacket window
            # of USB 2.0 section 7.1.18.2.
            hs_interpacket_delay_cycles=12,
            max_transport_errors=3,
            control_nak_timeout_cycles=max(1, clock_hz // 2),
        )

    @classmethod
    def simulation(cls) -> "HostTiming":
        """Return small nonzero counts suitable for bounded simulations."""
        return cls(
            clock_hz=60_000_000,
            frame_cycles=16,
            microframe_cycles=2,
            attach_stable_cycles=4,
            detach_stable_cycles=3,
            reset_cycles=4,
            chirp_detect_cycles=2,
            chirp_drive_cycles=3,
            chirp_timeout_cycles=8,
            chirp_tail_cycles=4,
            reset_recovery_cycles=3,
            address_recovery_cycles=2,
            vbus_discharge_cycles=4,
            transaction_timeout_cycles=8,
            interpacket_delay_cycles=1,
            hs_interpacket_delay_cycles=1,
            max_transport_errors=3,
            control_nak_timeout_cycles=32,
        )
