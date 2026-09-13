# Synthetic High Speed HID mouse

A test instrument for the hurra-cynthion relay: a bare-metal USB device on a
SparkFun MicroMod Teensy (iMXRT1062) that presents a minimal boot mouse at
High Speed and emits reports at a fixed, known rate.

## What it is for

Two things a real mouse cannot do:

- **Prove the link speed by measurement.** It offers `bInterval = 1`, which at
  High Speed means one 125 us microframe -- 8000 reports/sec. Full Speed
  schedules interrupt endpoints on 1 ms frames, so its ceiling is 1000 Hz. If
  the relay's `native_reports` counter advances at ~8000/sec, the TARGET link
  negotiated High Speed. That is a measurement, not an inference from a status
  bit.
- **Make drops unambiguous.** It emits at a steady rate with a sequence counter
  in the report payload, so a shortfall in the relay's counters is a real drop
  rather than a hand holding still.

## Why not Teensyduino

Teensyduino's `USB_HID` type declares 5 interfaces on endpoints 2..6 and
silently includes a Seremu debug interface and a media-keys interface.
hurra-cynthion's enumerator is deliberately bounded at 4 interfaces with
endpoint numbers <= 4, and requires a boot-mouse interface
(class 3 / subclass 1 / protocol 2) before it will commit a capture. No stock
Teensyduino USB type has a mouse *and* fits inside those bounds, so the
descriptors here are built by hand.

## Build and flash

    make                      # 8000 Hz (High Speed only)
    make REPORT_PERIOD_US=1000  # 1000 Hz, legal at Full Speed -- for comparison
    make flash                # requires the Teensy in HalfKay bootloader mode

`make flash` needs the Teensy connected to the build machine. Press the board's
button to enter the bootloader if it does not appear.

## Wiring for a test run

    MicroMod Teensy in ATP carrier
      ATP USB-C  --cable-->  Cynthion TARGET-A     (bus powered from Cynthion)
      Cynthion AUX           -->  PC
      Cynthion CONTROL       -->  build machine    (for apollo / regdebug)

## Reading the result

    PYTHONPATH=src python3 -m hurra_cynthion.regdebug --map report-injection dump

`usb_speed.target_*` shows the PHY mode the enumerator commanded;
`native_reports` sampled over a known interval gives the achieved report rate.

## Measuring the generation rate (DIAG_SEND_EVERY)

`native_reports` on the relay counts reports *received*, so it cannot tell a
device that generates 8000/sec and delivers 2000 from one that generates 2000
and delivers all of them. Sweeping `REPORT_PERIOD_US` does not separate them
either: both are `min(generation, delivery)` over the same two numbers, so they
predict the same rate at every period.

`DIAG_SEND_EVERY=N` generates on the normal `REPORT_PERIOD_US` schedule but
transmits one generation in N, which makes the observed rate `generation / N`:

```
make clean && make DIAG_SEND_EVERY=8 && make flash
```

Against a host polling every microframe, `native_reports` then reads

| observed | generation | meaning |
|---|---|---|
| ~250/sec | ~2000/sec | the main loop is the limit |
| ~1000/sec | ~8000/sec | the loop keeps up; delivery turnaround is the limit |

Restore the instrument with `make clean && make && make flash`. The `.o` files
carry a stamp of the `-D` flags they were built with, so an override rebuilds
rather than silently reusing objects from the previous setting.
