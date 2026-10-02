"""bInterval decode and the SOF-paced due counter shared by interrupt endpoints."""

from amaranth import Const, Module, Mux, Signal, Value


def decode_interval(interval: Value, high_speed: Value) -> Value:
    """Return the period, in ``sof_tick``s, that a raw bInterval byte asks for.

    bInterval means different things at the two speeds.

    Full Speed: a direct count of 1 ms frames, 1-255.
    High Speed: an *exponent*. The period is 2**(bInterval-1) microframes,
      so bInterval=4 is 8 microframes = 1 ms, not 4 ms.

    Reading a High Speed value as a direct count services a device that asked
    for 1 ms at 500 us -- twice as fast as it asked for. The device still
    works, so nothing looks broken; it is just out of spec.

    High Speed bInterval is only legal in 1-16. Clamp upward to 16, which
    yields the slowest legal period rather than letting the shift overflow and
    flood the bus on a malformed descriptor. Zero is malformed at both speeds
    and falls back to every tick.
    """
    hs_clamped = Mux(interval == 0, 1, Mux(interval > 16, 16, interval))
    # Width 1 << 15 fits a 16-bit counter: exponent 0-15 gives 1-32768.
    hs_interval = Const(1, 1) << (hs_clamped - 1)[:4]
    return Mux(high_speed, hs_interval, Mux(interval == 0, 1, interval))


class IntervalPacer:
    """Raise ``due`` once per decoded bInterval period of ``sof_tick``s.

    Deliberately not an Elaboratable. Each owner runs the counter under its own
    reset and freeze conditions (the poller freezes it once failed), so the
    methods emit into the owner's module, inside whatever control context they
    are called from. Keeping it in the owner's module also left the poller's
    netlist byte-identical when this was extracted from it.
    """

    def __init__(
        self,
        *,
        interval: Value,
        high_speed: Value,
        sof_tick: Value,
        due_name: str = "due",
        domain: str = "usb",
    ) -> None:
        self.interval = interval
        self.high_speed = high_speed
        self.sof_tick = sof_tick
        self.domain = domain

        self.counter = Signal(16, name="interval_counter")
        #: Latched until consumed, so a period that ends while the bus is busy
        #: is serviced late rather than skipped.
        self.due = Signal(name=due_name)
        self.period = Signal(16, name="effective_interval")
        #: Combinational: this ``sof_tick`` ends the current period.
        self.elapsed = sof_tick & (self.counter == self.period - 1)

    def decode(self, m: Module) -> None:
        m.d.comb += self.period.eq(decode_interval(self.interval, self.high_speed))

    def clear(self, m: Module) -> None:
        m.d[self.domain] += [self.counter.eq(0), self.due.eq(0)]

    def advance(self, m: Module) -> None:
        with m.If(self.sof_tick):
            with m.If(self.elapsed):
                m.d[self.domain] += [self.counter.eq(0), self.due.eq(1)]
            with m.Else():
                m.d[self.domain] += self.counter.eq(self.counter + 1)

    def consume(self, m: Module) -> None:
        """Spend ``due`` on a transaction issued this cycle."""
        m.d[self.domain] += self.due.eq(0)
        # A period ending on the issue cycle itself must not be lost.
        with m.If(self.elapsed):
            m.d[self.domain] += self.due.eq(1)
