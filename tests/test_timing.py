from hurra_cynthion.timing import HostTiming


def test_hardware_microframe_is_an_exact_eighth_of_a_frame() -> None:
    """High Speed runs eight 125 us microframes per 1 ms frame.

    The scheduler increments the 11-bit frame number once per frame and emits a
    SOF once per microframe, so the ratio has to be exactly eight with no
    remainder -- a rounded microframe would let the two counters drift apart
    and the frame number would slip against the bus.
    """
    timing = HostTiming.hardware()

    assert timing.microframe_cycles * 8 == timing.frame_cycles
    assert timing.microframe_cycles == timing.clock_hz // 8_000
    assert timing.microframe_cycles == 7_500


def test_simulation_microframe_is_an_exact_eighth_of_a_frame() -> None:
    """The bounded simulation profile must preserve the same 8:1 ratio."""
    timing = HostTiming.simulation()

    assert timing.microframe_cycles * 8 == timing.frame_cycles


def test_high_speed_interpacket_delay_outlasts_token_serialisation() -> None:
    """The gap must outlast the token's wire form, which is 8 UTMI clocks.

    A token on the wire is SYNC (32 bits) + PID (8) + address/endpoint/CRC5
    (16) + EOP (8) = 64 bits. UTMI carries 8 bits per clock at High Speed, so
    that is 8 clocks. Handing DATA to the PHY before then produces a single
    merged packet with no EOP, which the device never ACKs.

    This is the trap the Full Speed comment warns about: scaling the Full
    Speed constant down by the 40x rate ratio gives 5 cycles, which is SHORTER
    than the serialisation it exists to outlast.
    """
    timing = HostTiming.hardware()
    token_wire_bits = 32 + 8 + 16 + 8
    token_serialisation_cycles = token_wire_bits // 8

    assert token_serialisation_cycles == 8
    assert timing.hs_interpacket_delay_cycles > token_serialisation_cycles
    # Still inside the 8-192 HS bit-time interpacket window (USB 2.0 7.1.18.2).
    assert (timing.hs_interpacket_delay_cycles - token_serialisation_cycles) * 8 <= 192


def test_naive_rate_scaling_of_the_full_speed_delay_would_be_too_short() -> None:
    """Guard the derivation itself, not just the number.

    If someone later "simplifies" the High Speed delay to the Full Speed one
    divided by the 40x rate ratio, this fails and says why.
    """
    timing = HostTiming.hardware()
    naive = timing.interpacket_delay_cycles // 40

    assert naive < 8, "premise changed: naive scaling is no longer the short answer"
    assert timing.hs_interpacket_delay_cycles > naive


def test_full_speed_timings_are_unchanged_by_the_high_speed_additions() -> None:
    """Step 1 is additive. Every Full Speed constant keeps its value."""
    timing = HostTiming.hardware()

    assert timing.frame_cycles == 60_000
    assert timing.interpacket_delay_cycles == 200
    assert timing.reset_cycles == 3_000_000
    assert timing.transaction_timeout_cycles == 60_000
