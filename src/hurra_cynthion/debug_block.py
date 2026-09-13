"""Shared power, LED, and JTAG register plumbing for diagnostic tops."""

from __future__ import annotations

from amaranth import Cat, Const, DomainRenamer, Elaboratable, Module, Signal, Value
from luna.gateware.interface.jtag import JTAGRegisterInterface

from .debug_regs import MAGIC, RegisterMap

LED_NAMES = ["heartbeat", "led1", "led2", "led3", "led4", "led5"]


class _RetainedState(Elaboratable):
    """Owns the registers that hold sampled values for asynchronous register reads.

    Most interesting debug sources are one-cycle strobes, while a JTAG read samples
    whatever the register happens to hold at an arbitrary moment. Wiring a strobe
    straight into ``status`` therefore has a capture probability near zero. Every
    retention register built by :meth:`DebugRegisterBlock.latch_on`, ``sticky``, and
    ``counter`` lands here as one ``(target, event, value)`` spec.

    The ``usb`` domain is hard-coded to match ``DebugRegisterBlock.elaborate``; a
    ``sync``-domain source wired through these helpers would be a silent CDC bug.
    Since the whole register file is renamed into ``usb`` (see
    ``DebugRegisterBlock.elaborate``), that is no longer an isolated assumption
    here but the module-wide invariant: a ``sync`` signal reaching any
    ``status`` register is a CDC in the opposite direction.
    """

    def __init__(self) -> None:
        self.specs: list[tuple[Signal, Value, Value]] = []

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        for target, event, value in self.specs:
            with m.If(event):
                m.d.usb += target.eq(value)
        return m


class DebugRegisterBlock(Elaboratable):
    """Owns power/LED/heartbeat/JTAG-register boilerplate for a diagnostic top."""

    def __init__(
        self,
        *,
        regmap: RegisterMap,
        host=None,
        aux_vbus_en: Value | int | None = None,
        target_discharge: Value | int | None = None,
        target_c_vbus_en: Value | int = 0,
        power_from_control: bool = False,
        num_leds: int = 6,
        heartbeat_bits: int = 26,
    ) -> None:
        self.regmap = regmap
        self.host = host
        self.num_leds = num_leds
        self.heartbeat_bits = heartbeat_bits
        self.heartbeat = Signal(heartbeat_bits)
        # Some diagnostics also power TARGET-C so the target PHY sees a VBUS session.
        self._target_c_vbus_en = target_c_vbus_en
        # Select exactly one source for TARGET-A VBUS to avoid backfeeding the ports.
        self._power_from_control = power_from_control

        self._aux_vbus_en = (
            aux_vbus_en
            if aux_vbus_en is not None
            else (host.aux_vbus_en if host is not None else 0)
        )
        self._target_discharge = (
            target_discharge
            if target_discharge is not None
            else (host.target_discharge if host is not None else 0)
        )

        self.regs = JTAGRegisterInterface(
            address_size=regmap.address_size, register_size=regmap.register_size
        )
        self._led_map: dict[int, Value] = {}
        self._retained = _RetainedState()

    def latch_on(self, event: Value, value: Value, *, name: str, init: int = 0) -> Signal:
        """Sample ``value`` on ``event`` and hold it until the next ``event``.

        Use for pulsed result codes: the returned signal is continuously valid, so an
        asynchronous read reports the most recent result rather than the idle default.
        """
        signal = Signal(len(Value.cast(value)), name=f"latched_{name}", init=init)
        self._retained.specs.append((signal, event, value))
        return signal

    def sticky(self, event: Value, *, name: str) -> Signal:
        """Set on the first ``event`` and hold for the life of the bitstream.

        Use for "did this ever happen" milestones that a single ``dump`` must observe
        long after the fact.
        """
        signal = Signal(name=f"seen_{name}")
        self._retained.specs.append((signal, event, Const(1)))
        return signal

    def counter(self, event: Value, *, name: str, width: int = 32) -> Signal:
        """Count ``event`` pulses, saturating at the maximum ``width`` can hold.

        Saturating rather than wrapping keeps a total readable as an absolute tally.
        """
        signal = Signal(width, name=f"count_{name}")
        limit = (1 << width) - 1
        self._retained.specs.append((signal, event & (signal != limit), signal + 1))
        return signal

    def status(self, name: str, **signals: Value) -> None:
        """Expose a status register, packed in schema field order."""
        reg = self.regmap[name]
        parts = []
        for f in reg.fields:
            if f.name not in signals:
                raise ValueError(f"status {name!r} missing signal for field {f.name!r}")
            value = signals[f.name]
            if len(value) != f.width:
                raise ValueError(
                    f"status {name!r}.{f.name}: width {len(value)} != field width {f.width}"
                )
            parts.append(value)
        extra = set(signals) - {f.name for f in reg.fields}
        if extra:
            raise ValueError(f"status {name!r}: unknown fields {sorted(extra)}")
        self.regs.add_read_only_register(reg.address, read=Cat(*parts))

    def status_from(self, name: str, source, *, prefix: str = "") -> None:
        """Expose a status register using same-named attributes from ``source``."""
        reg = self.regmap[name]
        self.status(
            name,
            **{field.name: getattr(source, f"{prefix}{field.name}") for field in reg.fields},
        )

    def control(self, name: str) -> tuple[Signal, Signal]:
        """Expose a read/write register and its one-cycle write strobe."""
        reg = self.regmap[name]
        width = sum(f.width for f in reg.fields) or self.regmap.register_size
        value = Signal(width, name=f"ctrl_{name}")
        strobe = Signal(name=f"ctrl_{name}_wr")
        self.regs.add_register(reg.address, value_signal=value, size=width, write_strobe=strobe)
        return value, strobe

    def memory(self, name: str, *, read_data: Value) -> Signal:
        """Expose a memory pair and return its host-writable index."""
        addr_reg = self.regmap[f"{name}_addr"]
        data_reg = self.regmap[f"{name}_data"]
        addr_width = max(1, ((addr_reg.depth or 1) - 1).bit_length())
        addr_sig = Signal(addr_width, name=f"mem_{name}_addr")
        self.regs.add_register(addr_reg.address, value_signal=addr_sig, size=addr_width)
        self.regs.add_read_only_register(data_reg.address, read=read_data)
        return addr_sig

    def set_led(self, index_or_name, value: Value) -> None:
        """Drive an LED by index or name."""
        index = index_or_name if isinstance(index_or_name, int) else LED_NAMES.index(index_or_name)
        if not 0 <= index < self.num_leds:
            raise ValueError(f"LED index {index} out of range")
        self._led_map[index] = value

    def elaborate(self, platform) -> Module:
        m = Module()
        m.d.usb += self.heartbeat.eq(self.heartbeat + 1)

        aux_vbus_in_en = platform.request("aux_vbus_in_en")
        aux_vbus_en = platform.request("aux_vbus_en")
        target_a_discharge = platform.request("target_a_discharge")
        control_vbus_in_en = platform.request("control_vbus_in_en")
        target_c_vbus_en = platform.request("target_c_vbus_en")
        control_vbus_en = platform.request("control_vbus_en")
        # The input enables use active-low board pins, so ``o=1`` turns them on.
        if self._power_from_control:
            m.d.comb += [
                aux_vbus_in_en.o.eq(0),
                aux_vbus_en.o.eq(0),
                control_vbus_in_en.o.eq(1),
                control_vbus_en.o.eq(self._aux_vbus_en),
            ]
        else:
            m.d.comb += [
                aux_vbus_in_en.o.eq(1),
                aux_vbus_en.o.eq(self._aux_vbus_en),
                control_vbus_in_en.o.eq(0),
                control_vbus_en.o.eq(0),
            ]
        m.d.comb += [
            target_a_discharge.o.eq(self._target_discharge),
            target_c_vbus_en.o.eq(self._target_c_vbus_en),
        ]
        if self.host is not None:
            m.d.comb += self.host.report_ready.eq(1)

        self.regs.add_read_only_register(
            self.regmap.magic_address, read=Const(MAGIC, self.regmap.register_size)
        )
        # Every register source in every top is usb-domain, including everything
        # _RetainedState owns. LUNA captures word_to_send with `m.d.sync +=`
        # (jtag.py) through a plain comb fan-in (spi.py), so leaving the
        # register file in `sync` makes every multi-bit read an unsynchronized
        # CDC. Renaming removes the crossing rather than making it rarer.
        # JTAGCommandInterface's `jtag` domain is local, so the TCK-clocked
        # shift path is untouched.
        m.submodules.regs = DomainRenamer("usb")(self.regs)
        m.submodules.retained = self._retained

        leds = [platform.request("led", i) for i in range(self.num_leds)]
        for i, led in enumerate(leds):
            default = self.heartbeat[-1] if i == 0 else 0
            m.d.comb += led.o.eq(self._led_map.get(i, default))

        return m
