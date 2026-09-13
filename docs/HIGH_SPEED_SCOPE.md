# High Speed: scope

What it takes to move this relay from Full Speed (12 Mb/s) to High Speed
(480 Mb/s), assessed against the code as of `61e568b`. Written before any HS
code, so the estimates are structural, not measured.

## The shape of the problem

The design is two independent USB stacks joined by a relay:

```
mouse --[TARGET-A]--> host stack (ours)  -> relay -> device stack (LUNA) --[AUX]--> PC
```

These negotiate speed **separately**, and the two halves are not equally hard.

**The device half is nearly free.** LUNA's `USBDevice` already does High Speed;
`device.py:248` currently opts out with `device.full_speed_only.eq(1)`.
`luna/gateware/usb/usb2/reset.py` (`USBResetSequencer`) implements the full
device-side chirp handshake -- `DEVICE_CHIRP` -> `AWAIT_HOST_K` ->
`AWAIT_HOST_J`, sequenced against USB 2.0 section 7.1.7.5. Flipping that flag
is most of the AUX-side work.

**The host half does not exist anywhere.** No open-source USB 2.0 HS *host*
controller exists in any HDL (established earlier in this project). The host
chirp sequence has to be written. The good news is that it is the mirror image
of LUNA's device sequencer, in the same idiom, against the same
`UTMITranslator` -- so `reset.py` is a working reference for the protocol
timing, not just a rough guide.

## 1. Host-side chirp negotiation (the novel work)

Today `enumerator.py` `BUS_RESET` already parks the PHY in the right mode:

```python
self.phy_xcvr_select.eq(0),   # HS transceiver
self.phy_term_select.eq(0),   # HS termination, no FS pull-up
self.phy_op_mode.eq(2),       # bit-stuffing and NRZI disabled -- chirp mode
```

That is the chirp drive configuration, so the PHY setup is already correct.
What is missing is the sequence itself, between `BUS_RESET` and
`RESET_RECOVERY`:

1. Drive SE0 for the reset period (exists).
2. **Watch for device chirp K** -- the device drives K for 1-7 ms starting
   about 2.5 us into the reset. Detect via `line_state == 2` while in HS drive
   mode. Absence means the device is FS-only: fall through to today's path.
3. **Drive host chirp K-J-K-J-K-J** -- alternating pairs, each 40-60 us, at
   least three pairs, continuing to the end of the reset window.
4. **Enter HS**: `xcvr_select=0`, `term_select=0`, `op_mode=0`.

Fallback to FS when no chirp is seen is not optional -- most HID devices are
FS-only, so the *common* path stays exactly as it is today. HS must be the
exception branch, not the new default.

## 2. Timing model (mechanical, but touches everything)

| Site | Today | High Speed |
|---|---|---|
| `timing.py` `frame_cycles` | `clock_hz // 1_000` (1 ms frame, 60,000 cy) | `clock_hz // 8_000` (125 us microframe, 7,500 cy) |
| `transaction.py:25` | `_FULL_SPEED_BIT_RATE = 12_000_000` | `480_000_000` |
| `transaction.py:26` | `_MAX_RX_PACKET_BYTES = 1 + 64 + 2` | `1 + 1024 + 2` (HS interrupt max) |
| `timing.py` `interpacket_delay_cycles` | `40 * (clock_hz // 12e6)` | Whole model changes -- see below |
| `descriptors.py:11` | `MAX_PACKET_SIZE = 64` | 512 (bulk) / up to 1024 (interrupt) |
| `scheduler.py:7` | "full-speed start-of-frame" | microframe, frame number + 3-bit uframe |

The `interpacket_delay_cycles` comment in `timing.py` is the one to read
carefully before changing: it exists because the ULPI PHY accepts a token into
its TX FIFO in ~7 UTMI cycles but then serialises it onto the wire over 32 FS
bit-times, and handing DATA over too early produces a single merged packet with
no EOP. At HS, UTMI is 8 bits at 60 MHz = 480 Mb/s, so one byte per clock and
serialisation is ~4 clocks rather than ~160. The hazard does not disappear --
it shrinks by 40x, and the constant must be re-derived, not merely scaled.

## 3. bInterval, which is a semantic change and not a scaling one

`poller.py:70-72`:

```python
effective_interval = Mux(self.interval == 0, 1, self.interval)
interval_elapsed = self.sof_tick & (interval_counter == effective_interval - 1)
```

At FS, `bInterval` for an interrupt endpoint is a **count of milliseconds**
(1-255). At HS it is an **exponent**: the period is `2^(bInterval-1)`
microframes, so `bInterval=4` means 8 microframes = 1 ms, not 4 ms. Treating
the HS value as a direct count polls a 1 ms mouse at 4 microframes = 500 us,
i.e. twice as fast as the device asked for.

This needs a decode at the point `ep_interval` is captured
(`enumerator.py:789`) or in the poller, gated on the negotiated speed. It is a
small change that is easy to get silently wrong, and the failure mode is a
device that works but is polled out of spec.

## 4. Buffering, and a caveat that inverts a recent decision

Max packet goes 64 -> 512 bytes, so the relay queues must grow. This partly
**reverses** the LUTRAM conversion in `61e568b` (see `BRAM_BUDGET.md`):

| relay queue | LUTRAM | EBR | better |
|---|---|---|---|
| 9 x 128 (FS, today) | 24 each | 1 each | LUTRAM |
| 9 x 512 (HS) | 96 each | 1 each | **block RAM** |

Post-LUTRAM headroom is 29 free EBRs and 4,345 free `TRELLIS_COMB` -- so the
EBRs are there to spend, which is the point of having reclaimed them. Moving
the relay queues back to block RAM when they grow is expected, not a regression.

## 5. Everything downstream of the speed gate

- `host.py:306` `normal_full_speed` hard-codes the FS PHY configuration
  (`xcvr_select == 1 & term_select & op_mode == 0`) and gates the scheduler on
  it. Needs to become a speed-aware "operating normally" predicate.
- `enumerator.py:339` rejects anything but `line_state == 1` at attach with
  `UNSUPPORTED_SPEED`. This stays correct: **HS devices attach as FS**, with a
  D+ pull-up, and only negotiate HS during reset. No change needed here, which
  is worth knowing before someone "fixes" it.
- `device.py:248` `full_speed_only.eq(1)` -> driven from negotiated speed.
- Descriptor rewriting: the relay presents the device's descriptors to the PC.
  If TARGET negotiates FS and AUX negotiates HS (or vice versa), `bInterval`
  and `wMaxPacketSize` must be **translated between encodings**, not copied.
  This is the subtlest part of the whole change and has no analogue in the
  current code, which copies them verbatim.

## 6. Constraints that shape the implementation

- **No retiming.** yosys has no retiming pass, so every pipeline stage is
  manual. HS logic must be written already-pipelined; it cannot be fixed after
  the fact by a synthesis flag.
- **Timing margin is 4.5%** at 62.68 MHz against a 60 MHz constraint, on a
  22-level control cone that new HS logic will feed into. Shortening that cone
  (see `BRAM_BUDGET.md`) should come *before* HS logic lands, not after.
- **60 MHz `usb` domain is fixed** by ULPI -- HS does not raise the clock, it
  widens the data per clock. So HS does not by itself make timing harder; the
  added control logic does.

## Suggested order

1. Shorten the `injection_plane` control cone -- buy margin before spending it.
2. Flip the device half to HS (`full_speed_only.eq(0)`) and validate AUX-side
   HS against the PC in isolation, with the host half still FS. This exercises
   LUNA's chirp with no new code and proves the AUX path end to end.
3. Host-side chirp FSM with FS fallback, validated against an HS-capable device
   *and* an FS-only mouse, since the FS path must not regress.
4. Timing model, bInterval decode, buffer resize.
5. Descriptor translation between speed encodings.

Steps 1-2 are low risk and independently valuable. Step 3 is where the novel
work is.
