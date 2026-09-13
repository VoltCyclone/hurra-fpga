# Provenance

Everything under `vendor/` is copied unmodified from **Hurra-v2** at commit
`d43fd03` (`~/code/Hurra-v2`). Nothing in `vendor/` has been edited; all
firmware written for this instrument lives in `src/`.

| Vendored file | Origin | Why |
|---|---|---|
| `vendor/core/startup.c` | `core/startup.c` | Reset handler, cache/systick setup, USB PLL bring-up, `millis()` / `micros()`. |
| `vendor/core/bootdata.c` | `core/bootdata.c` | iMXRT boot header (FlexSPI config, IVT, boot data). |
| `vendor/core/imxrt1062_mm.ld` | `core/imxrt1062_mm.ld` | Linker script for the **MicroMod** memory map specifically. |
| `vendor/include/imxrt.h` | `include/imxrt.h` | Register definitions. |
| `vendor/src/usb_device.c` | `src/usb_device.c` | Bare-metal USB **device** stack on USB1, the iMXRT1062's High Speed controller. |
| `vendor/src/usb_device.h` | `src/usb_device.h` | dQH/dTD layouts and the public API. |
| `vendor/src/desc_capture.h` | `src/desc_capture.h` | `captured_descriptors_t`, the struct `usb_device_init()` serves from. |
| `vendor/src/usb_host.h` | `src/usb_host.h` | Only for the three `usb_host_*` prototypes `usb_device.c` links against. |

## Why vendor rather than write from scratch

`usb_device.c` is a working High Speed USB device implementation on the exact
silicon this instrument runs on, already carrying fixes for endpoint halt
recovery, data-toggle reset, per-slot OUT packet sizes and descriptor bounds.
Re-deriving that to emit one mouse report every 125 us would be a poor trade,
and a subtly broken device stack would produce test results that look like
gateware bugs.

## The one adaptation

Hurra-v2 is a *passthrough*: `usb_device.c` serves descriptors captured from a
real upstream mouse and forwards HID class requests to it. This instrument has
no upstream -- it is the mouse. Two consequences:

1. `src/main.c` fills a `captured_descriptors_t` by hand instead of from a
   capture. This is the "static descriptors" path.
2. `src/usb_host_shim.c` supplies the three `usb_host_*` symbols locally rather
   than linking Hurra-v2's host stack.

## Updating

`vendor/` is a snapshot, not a submodule. If Hurra-v2's device stack gains a
fix worth having, re-copy the file and update the commit hash above. Do not
edit `vendor/` in place -- a local change there would be invisible to anyone
diffing against upstream.
