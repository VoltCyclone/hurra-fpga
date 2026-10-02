"""Track which endpoints the real device sends boot-protocol reports on.

A BIOS sends SET_PROTOCOL(boot) while binding; the clone forwards it, and the
real device switches that interface's reports to the 8-byte boot layout. The
clone still serves the report-protocol descriptor, and the injection map is
compiled from it, so the injection plane must stand aside on those endpoints
until the device is back in report protocol.

Two halves:

- The snoop. Every forward the TARGET completed raises the relay's
  ``forward_ok``. A SET_PROTOCOL among them sets (wValue 0, boot) or clears
  (anything else) ``boot_mask`` for every IN endpoint of the interface in
  wIndex. It reflects the device, not the PC: a forward the handler had already
  given up on can still have changed the device.

- The resync. A real device on a real bus reverts to report protocol on a bus
  reset. Behind the clone it never sees the PC's reset of AUX (a BIOS-to-OS
  handoff), so on ``resync_trigger`` -- a PC bus reset or SET_CONFIGURATION on
  the clone -- this replays SET_PROTOCOL(report) to each interface still in
  boot, through the relay's second, lower-priority source. One interface at a
  time, lowest slot first, each once however many of its endpoints are marked.
  Bits clear only on the device's success; a failure is counted and waits for
  the next trigger, leaving injection stood down, which is the safe direction.
  A PC SET_PROTOCOL to an interface supersedes its pending replay: replayed
  afterwards, it would undo the PC's explicit choice.

Losing the device (``ready`` low) resets everything: a TARGET bus reset
returns every HID device to report protocol [HID 1.11 7.2.6].

The mask is indexed by endpoint number, bit 0 unused, which is what the
injection engine already carries per report. One boot-format report may still
be in flight as a bit clears and be mutated once; that is inherent.
"""

# Ruff's context-manager simplification obscures nested Amaranth control-flow DSL structure.
# ruff: noqa: SIM117

from functools import reduce
from operator import or_

from amaranth import Cat, Elaboratable, Module, Mux, ResetInserter, Signal, Value

from .control_relay import CLASS_INTERFACE_OUT, HID_SET_PROTOCOL
from .descriptors import MAX_ENDPOINT_NUMBER, MAX_ENDPOINTS


class BootProtocolTracker(Elaboratable):
    def __init__(self) -> None:
        #: The enumerator's ``ready``: low resets every bit of state here.
        self.ready = Signal()
        # The host's captured IN endpoint table.
        self.ep_count = Signal(range(MAX_ENDPOINTS + 1))
        self.ep_interface = [Signal(8, name=f"ep_interface_{k}") for k in range(MAX_ENDPOINTS)]
        self.ep_number = [Signal(4, name=f"ep_number_{k}") for k in range(MAX_ENDPOINTS)]

        # The relay's snoop of the PC's own forwards.
        self.forward_ok = Signal()
        self.forward_type = Signal(8)
        self.forward_request = Signal(8)
        self.forward_value = Signal(16)
        self.forward_index = Signal(16)

        #: Registered at the top: a PC bus reset or SET_CONFIGURATION on the
        #: clone. Acted on at its rising edge.
        self.resync_trigger = Signal()
        self.resync_valid = Signal()
        self.resync_index = Signal(8)
        self.relay_resync_active = Signal()
        self.relay_resync_done = Signal()
        #: The relay's ``response_error``, read on the ``relay_resync_done`` cycle.
        self.relay_error = Signal()

        #: Bit n: endpoint number n's interface is in boot protocol. Registered.
        self.boot_mask = Signal(MAX_ENDPOINT_NUMBER + 1)
        self.resync_ok = Signal()
        self.resync_fail = Signal()

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        slots = range(MAX_ENDPOINTS)

        def endpoint_bits(selected) -> Value:
            """The boot_mask bits of the endpoint numbers of ``selected`` slots."""
            return reduce(or_, [Mux(selected[k], 1 << self.ep_number[k], 0) for k in slots])

        # Snoop, stage 1: register the decode, so nothing from the relay's
        # side of the die reaches the mask or the FSM in the same cycle.
        apply = Signal()
        boot_value = Signal()
        forward_match = Signal(MAX_ENDPOINTS)
        m.d.usb += [
            apply.eq(
                self.forward_ok
                & (self.forward_type == CLASS_INTERFACE_OUT)
                & (self.forward_request == HID_SET_PROTOCOL)
                & (self.forward_index[8:] == 0)
            ),
            boot_value.eq(self.forward_value == 0),
            forward_match.eq(
                Cat(
                    *[
                        (k < self.ep_count) & (self.ep_interface[k] == self.forward_index[:8])
                        for k in slots
                    ]
                )
            ),
        ]
        snooped = Signal(MAX_ENDPOINTS)
        m.d.comb += snooped.eq(Mux(apply, forward_match, 0))

        trigger_seen = Signal()
        trigger_prev = Signal()
        #: Slots whose interface still needs its replay.
        pending = Signal(MAX_ENDPOINTS)
        #: One-hot: the slot whose interface is being replayed.
        current = Signal(MAX_ENDPOINTS)
        current_interface = Signal(8)
        #: Slots sharing ``current_interface``: one replay settles them all.
        same_interface = Signal(MAX_ENDPOINTS)
        #: The replay just completed and the device accepted it.
        replayed = Signal()

        m.d.usb += [self.resync_ok.eq(0), self.resync_fail.eq(0)]
        m.d.comb += self.resync_index.eq(current_interface)

        with m.FSM(domain="usb"):
            with m.State("IDLE"):
                with m.If(trigger_seen):
                    m.d.usb += [
                        trigger_seen.eq(0),
                        pending.eq(
                            Cat(
                                *[
                                    (k < self.ep_count) & (self.boot_mask >> self.ep_number[k])[0]
                                    for k in slots
                                ]
                            )
                        ),
                    ]
                    m.next = "SELECT"

            with m.State("SELECT"):
                # Also the gap between replays in which the relay is free, so
                # a PC request held off by one gets in before the next.
                lowest = pending & (~pending + 1)
                with m.If(pending == 0):
                    m.next = "IDLE"
                with m.Else():
                    m.d.usb += [
                        current.eq(lowest),
                        current_interface.eq(
                            reduce(or_, [Mux(lowest[k], self.ep_interface[k], 0) for k in slots])
                        ),
                    ]
                    m.next = "OFFER"

            with m.State("OFFER"):
                m.d.usb += same_interface.eq(
                    Cat(
                        *[
                            (k < self.ep_count) & (self.ep_interface[k] == current_interface)
                            for k in slots
                        ]
                    )
                )
                offered = (pending & current).any()
                # Withdrawn while a PC SET_PROTOCOL's decode is in flight: the
                # snoop clears a superseded replay's pending bits the cycle
                # after ``apply``, but a handler draining an abandoned forward
                # acks the relay on ``forward_ok``'s own cycle, so the relay can
                # be back in IDLE, taking offers, while ``apply`` is high.
                m.d.comb += self.resync_valid.eq(offered & ~apply)
                with m.If(self.relay_resync_active):
                    m.next = "WAIT"
                with m.Elif(~offered):
                    m.next = "SELECT"

            with m.State("WAIT"):
                with m.If(self.relay_resync_done):
                    m.d.usb += pending.eq(pending & ~same_interface)
                    with m.If(self.relay_error):
                        m.d.usb += self.resync_fail.eq(1)
                    with m.Else():
                        m.d.comb += replayed.eq(1)
                        m.d.usb += self.resync_ok.eq(1)
                    m.next = "SELECT"

        # Snoop, stage 2: the PC's choice supersedes a replay of the same
        # interface (after the FSM, so it wins a same-cycle load), and wins a
        # same-cycle replay result for the mask.
        for k in slots:
            with m.If(snooped[k]):
                m.d.usb += pending[k].eq(0)
        set_bits = endpoint_bits(Mux(boot_value, snooped, 0))
        clear_bits = endpoint_bits(Mux(boot_value, 0, snooped) | Mux(replayed, same_interface, 0))
        m.d.usb += self.boot_mask.eq((self.boot_mask & ~clear_bits) | set_bits)

        # On the rising edge: LUNA holds its bus reset as a level while AUX
        # VBUS is absent, and a failing replay must not repeat back to back
        # for as long as that lasts. After the FSM, so an edge on IDLE's
        # consuming cycle is kept.
        m.d.usb += trigger_prev.eq(self.resync_trigger)
        with m.If(self.resync_trigger & ~trigger_prev):
            m.d.usb += trigger_seen.eq(1)

        return ResetInserter({"usb": ~self.ready})(m)
