# Task F6 report: kmcmd `pad.*`

Commit: `9d882f3 mcxn947: kmcmd pad.* namespace drives ABSOLUTE; device class replaces no_mouse`

## What I implemented

- Added the `pad.` namespace to kmcmd with `pad.lx|ly|rx|ry|lt|rt(v)`,
  `pad.hat(0..8)`, `pad.btn(n,0|1)`, `pad.release()`, `pad.hold(ms)`, and bare
  axis/hat getters.
- Added kmcmd-local pad channel and device-class enums without exposing kmcmd to
  `injection_wire.h`, TinyUSB, or MCUX headers.
- Replaced the `no_mouse` callback with `device_class`, and added `absolute`
  and `absolute_hold` sink callbacks.
- Added the seven-value held vector and held mask. Setters resend the complete
  vector, refuse out-of-range values, avoid redundant frames, and roll state
  back when the sink refuses.
- Reused the existing BUTTON_STATE sink for `pad.btn`; successful sends update
  `s_buttons_sent`, and unchanged decisions compare against `s_buttons_sent`,
  preserving P1's silent-release behavior.
- Bound the new kmcmd callbacks to F5's link sinks in `usb_console.c`, with
  compile-time device-class/channel numbering assertions at the one translation
  unit that sees kmcmd, the session, and the generated wire contract.
- Removed `link_inject_no_mouse` and added the new direct header dependencies to
  the `usb_console.o` Makefile rule.

## TDD evidence

### RED

Command:

```text
make -C firmware/mcxn947 test-kmcmd
```

Observed exit status: 2. The new tests failed at compile time for the intended
missing interface, beginning with:

```text
test/kmcmd_test.c:48:39: error: use of undeclared identifier 'KMCMD_PAD_CHANNELS'
test/kmcmd_test.c:54:8: error: unknown type name 'kmcmd_device_class_t'
test/kmcmd_test.c:152:22: error: use of undeclared identifier 'KMCMD_DEVICE_MOUSE'
```

This is the brief's expected RED: the pad-facing kmcmd API did not exist yet.

### GREEN

Focused command:

```text
make -C firmware/mcxn947 test-kmcmd
```

Passing output:

```text
cc -std=c11 -Wall -Wextra -Werror -Wconversion -Wshadow -Isrc test/kmcmd_test.c src/kmcmd.c -o build/host/kmcmd_test
./build/host/kmcmd_test
kmcmd_test: all cases passed
```

Full host suite command:

```text
make -C firmware/mcxn947 test
```

Tail:

```text
./build/host/hid_pad_layout_test
hid_pad_layout_test: ok
./build/host/inj_map_build_test
inj_map_build_test: ok
./build/host/hid_descriptor_set_test
hid_descriptor_set_test: ok
python3 test/merge_mcxn947_images_test.py
merge_mcxn947_images_test: ok
```

The same run also reported `console_test: all cases passed`,
`kmcmd_test: all cases passed`, and `link_test: ok`.

Cross-build/image command:

```text
make -C firmware/mcxn947 check
```

Result: exit 0. Representative final rungs:

```text
PASS  object-contributes core0.elf:kmcmd.o: 44/45 symbols kept
PASS  object-contributes core0.elf:usb_console.o: 24/25 symbols kept
PASS  flash-fit core0.elf: ends 0x0000dc54 <= 0x000c0000 (730028 B headroom)
PASS  ram-fit core0.elf: content ends 0x20007350; 279728 B below the stack floor 0x2004b800
PASS  shared-window: g_shared_window shared at 0x2004c000, size 0x870; no ordinary allocations
PASS  merged-image: core0 56404 B <= 0xc0000; merged 817644 B <= 0x100000
check_mcxn947_images: 120 passed, 0 failed, 0 unparsable
```

The cross-link emitted the existing newlib syscall-stub warnings (`_close`,
`_fstat`, `_getpid`, `_isatty`, `_kill`, `_lseek`, `_read`, `_write`); the build
and all image assertions still completed successfully.

## Files changed

- `firmware/mcxn947/src/kmcmd.h`
- `firmware/mcxn947/src/kmcmd.c`
- `firmware/mcxn947/src/usb_console.c`
- `firmware/mcxn947/src/link.h`
- `firmware/mcxn947/src/link.c`
- `firmware/mcxn947/test/kmcmd_test.c`
- `firmware/mcxn947/Makefile`
- `.superpowers/sdd/2026-10-06-pad-injection/task-F6-report.md`

## Self-review findings

- Refusal matrix: `km.*` on PAD and NONE reports `nomouse`; `pad.*` on MOUSE
  and NONE reports `nopad`; UNKNOWN falls through to `ready` and reports
  `notready`; a missing required callback reports `nosink` first.
- Held vector rollback: an ABSOLUTE sink refusal restores both the previous mask
  and all seven previous values before reporting `busy`.
- Redundant setter: an unchanged pad channel or empty `pad.release()` is
  accepted and acknowledged without emitting an ABSOLUTE frame.
- Button-state truth: all four successful `s_ops.buttons(...)` sites set
  `s_buttons_sent` to the mask the sink received. `pad.btn` compares against
  that sent mask and updates it on success. Both P1 regression tests,
  `test_silent_release_does_not_emit` and
  `test_release_after_silent_release_still_emits`, pass in the full suite.
- Header isolation: neither `kmcmd.c` nor `kmcmd.h` includes
  `injection_wire.h`, TinyUSB, or MCUX SDK headers; `test-kmcmd` still compiles
  with only `-Isrc`.
- Mode gate: `KMCMD_MODE_OFF` returns `KMCMD_NOT_MINE` before namespace parsing,
  so it applies equally to `km.*` and `pad.*`.
- Reply framing: the selected namespace sets the reply prefix; accepted and
  refused pad commands use `pad.`, while km commands retain `km.`. KMBOX still
  suppresses successful acknowledgements but not refusals.
- Hat validation: `pad.hat(9)` and `pad.hat(-1)` both report `badstate` without
  reaching the sink.
- Scope: no files under vendor, build, or an MCUX SDK directory were opened or
  modified, and the Python suite was not run.
- `git diff --check` passed with no whitespace errors before the report was
  written.

## Concerns

None.

## Fix round 1

### Changes

- Finding 1, stale timed ABSOLUTE release:
  - `firmware/mcxn947/src/inj_session.c:484-506` now clears
    `abs_release_slots` only after an explicit ABSOLUTE request has passed the
    device-class and one-deep-request gates, so an accepted vector supersedes
    the older timer.
  - `firmware/mcxn947/src/kmcmd.c:37-44,811-830,956-979` adds the documented
    hold-window rule and `s_abs_hold_pending`. A successful `pad.hold` sets it;
    the next explicit ABSOLUTE bypasses the unchanged optimization and clears
    the flag only after the sink accepts the frame. Link loss and init clear it
    at `firmware/mcxn947/src/kmcmd.c:1185-1206,1224-1237`.
  - `firmware/mcxn947/test/inj_session_test.c:1169-1190` arms `release_in(3)`,
    emits an explicit LX vector on the next fill, ticks four more fills, and
    proves that this was the only frame and no timed release followed.
  - `firmware/mcxn947/test/kmcmd_test.c:1108-1133` proves both explicit actions
    in the hold window reach the sink: `pad.release()` sends mask 0 and a fresh
    `pad.hold` followed by the same `pad.lx(300)` sends the held vector.
- Finding 2, silent mouse release bypassed the class gate:
  - `firmware/mcxn947/src/kmcmd.c:647-668` preserves `badstate` ordering, then
    rejects state 2 with `nomouse` for PAD/NONE before changing `s_buttons`;
    MOUSE/UNKNOWN retain the existing no-frame silent-release behavior.
  - `firmware/mcxn947/test/kmcmd_test.c:1154-1168` extends the PAD refusal row:
    after pad button 1 is held, `km.left(2)` returns `nomouse`, and holding pad
    button 2 emits `0x3`, proving button 1 was untouched.

### TDD evidence

RED — `make -C firmware/mcxn947 test-kmcmd` (exit 2):

```text
cc -std=c11 -Wall -Wextra -Werror -Wconversion -Wshadow -Isrc test/kmcmd_test.c src/kmcmd.c -o build/host/kmcmd_test
./build/host/kmcmd_test
Assertion failed: (g_abs_count == 2u), function test_pad_hold_arms_the_sink_timer_and_clears_the_mirror, file kmcmd_test.c, line 1118.
make: *** [test-kmcmd] Abort trap: 6
```

RED — `make -C firmware/mcxn947 test-inj-session` (exit 2):

```text
cc -std=c11 -Wall -Wextra -Werror -Wconversion -Wshadow -Isrc -Itest -isystem include test/inj_session_test.c \
		src/inj_session.c src/inj_command.c src/spi_frame.c src/inj_map_build.c \
		src/hid_mouse_layout.c src/hid_pad_layout.c src/hid_fields.c src/hid_item.c -o build/host/inj_session_test
./build/host/inj_session_test
Assertion failed: (!inj_session_fill_tx(s, slot)), function assert_idle, file inj_session_test.c, line 43.
make: *** [test-inj-session] Abort trap: 6
```

GREEN — `make -C firmware/mcxn947 test-kmcmd` (exit 0):

```text
cc -std=c11 -Wall -Wextra -Werror -Wconversion -Wshadow -Isrc test/kmcmd_test.c src/kmcmd.c -o build/host/kmcmd_test
./build/host/kmcmd_test
kmcmd_test: all cases passed
```

GREEN — `make -C firmware/mcxn947 test-inj-session` (exit 0):

```text
cc -std=c11 -Wall -Wextra -Werror -Wconversion -Wshadow -Isrc -Itest -isystem include test/inj_session_test.c \
		src/inj_session.c src/inj_command.c src/spi_frame.c src/inj_map_build.c \
		src/hid_mouse_layout.c src/hid_pad_layout.c src/hid_fields.c src/hid_item.c -o build/host/inj_session_test
./build/host/inj_session_test
inj_session_test: ok
```

Full host suite — `make -C firmware/mcxn947 test` (exit 0), tail:

```text
./build/host/hid_pad_layout_test
hid_pad_layout_test: ok
./build/host/inj_map_build_test
inj_map_build_test: ok
./build/host/hid_descriptor_set_test
hid_descriptor_set_test: ok
python3 test/merge_mcxn947_images_test.py
merge_mcxn947_images_test: ok
```

Cross-build/image gate — `make -C firmware/mcxn947 check` (exit 0), tail:

```text
  PASS  forbid-symbol core1.elf:SPC: no strong match (vendor startup weak stubs ignored)
  PASS  forbid-symbol core1.elf:GDET: no strong match (vendor startup weak stubs ignored)
  PASS  forbid-symbol core1.elf:ITRC: no strong match (vendor startup weak stubs ignored)
  PASS  core1-SystemInit-stores: only SCB->CPACR then SCB->VTOR
  PASS  core1-release: strong T; one CPUCTRL read feeds assert/release; exact literals present
  PASS  merged-image: core0 56484 B <= 0xc0000; merged 817644 B <= 0x100000
check_mcxn947_images: 120 passed, 0 failed, 0 unparsable
```

The cross-link repeated the existing newlib syscall-stub warnings (`_close`,
`_fstat`, `_getpid`, `_isatty`, `_kill`, `_lseek`, `_read`, `_write`); the
build and every image assertion completed successfully.
