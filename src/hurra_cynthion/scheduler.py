from amaranth import Elaboratable, Module, Mux, Signal

from .timing import HostTiming


class FrameScheduler(Elaboratable):
    """Schedule USB start-of-frame tokens at Full or High Speed."""

    def __init__(self, timing: HostTiming) -> None:
        self.timing = timing

        self.enable = Signal()
        self.token_ready = Signal()
        #: Negotiated link speed, from the enumerator. Selects the SOF cadence
        #: and how the frame number advances; see ``microframe`` below.
        self.high_speed = Signal()

        self.frame_tick = Signal()
        self.sof_start = Signal()
        self.frame_number = Signal(11)

    def elaborate(self, platform) -> Module:
        m = Module()

        interval_counter = Signal(range(self.timing.frame_cycles))
        # Position within the 1 ms frame, 0-7. Used only at High Speed, where a
        # SOF goes out every 125 us microframe but the frame number still
        # advances only once per frame -- so eight consecutive SOFs carry the
        # same number (USB 2.0 section 8.4.3). The microframe index itself is
        # never transmitted; the SOF packet carries the 11-bit frame number and
        # nothing else, and position is implicit. Incrementing the frame number
        # per microframe would run it eight times too fast and leave it
        # permanently out of step with what the device counts.
        microframe = Signal(3)
        sof_pending = Signal()

        interval_limit = Mux(
            self.high_speed, self.timing.microframe_cycles, self.timing.frame_cycles
        )

        # Suppress a pending request combinationally as soon as traffic is
        # disabled. Waiting for the registered clear would leave one cycle in
        # which the arbiter could launch a stale SOF while the PHY is entering
        # bus reset and updating Function Control.
        m.d.comb += self.sof_start.eq(sof_pending & self.enable)
        m.d.sync += self.frame_tick.eq(0)

        with m.If(~self.enable):
            m.d.sync += [
                interval_counter.eq(0),
                microframe.eq(0),
                sof_pending.eq(0),
            ]
        with m.Else():
            # A pending SOF follows ordinary ready/valid semantics. The frame
            # number changes only after the token generator accepts it, so the
            # microframe position advances on the same event and the two stay
            # locked to SOFs actually emitted rather than to SOFs merely due.
            with m.If(sof_pending & self.token_ready):
                m.d.sync += sof_pending.eq(0)
                with m.If(self.high_speed):
                    # Signal(3) wraps 7 -> 0 on its own.
                    m.d.sync += microframe.eq(microframe + 1)
                    with m.If(microframe == 7):
                        m.d.sync += self.frame_number.eq(self.frame_number + 1)
                with m.Else():
                    m.d.sync += self.frame_number.eq(self.frame_number + 1)

            # ``>=`` rather than ``==`` so that a speed change is self-correcting.
            # Dropping from Full to High Speed shortens the limit, and a counter
            # already past the new one would otherwise miss the comparison and
            # stall SOF for a full wrap of the counter's range.
            with m.If(interval_counter >= interval_limit - 1):
                m.d.sync += [
                    interval_counter.eq(0),
                    self.frame_tick.eq(1),
                    sof_pending.eq(1),
                ]
            with m.Else():
                m.d.sync += interval_counter.eq(interval_counter + 1)

        return m
