#!/usr/bin/env python3
"""Static verification ladder for the MCXN947 controller firmware images.

`make all` exits 0 while `--gc-sections` throws the application away, so this
script asserts on the *built artifacts* instead of on make's exit status. It is
the analogue of firmware/ch32h417/tools/check_images.py.

It is a separate script rather than an extension of that one, deliberately.
That checker is 1700 lines built around a two-image RISC-V part: V3F/V5F are
baked into its ELF keys, its exemption table, its geometry rungs and its
disassembly matchers, and its rung 3 pins xPack riscv-none-elf objdump output.
Sharing it would mean parameterising every one of those on architecture for no
gain -- the two trees have no artifact in common and never link together. What
*is* worth sharing is the shape: same all-failures-before-exit reporting, same
"parse error" / "assertion failure" distinction, same rule that geometry is
declared once in the Makefile and passed in.

Rungs (cheapest first, none require hardware):

  1   retention roots  -- for each `SYMBOL=OWNING_OBJECT` the Makefile passes
                          to the linker as `-Wl,--undefined=SYMBOL`, assert the
                          symbol is (a) defined in the ELF, (b) a *strong*
                          definition, not the vendor's weak stub, (c) not
                          aliased onto DefaultISR, and (d) owned by the object
                          the Makefile claims owns it.

                          All four are needed on this part, because
                          startup_MCXN947_cm33_core0.S has three shapes of weak
                          handler and only one of them is caught by an address
                          comparison against DefaultISR:

                            CTIMER2_DriverIRQHandler  W  0x4f8  <- == DefaultISR
                            CTIMER2_IRQHandler        W  0x598  <- trampoline
                            HardFault_Handler         W  0x500  <- self-loop

                          A gate that only asked "is it defined and not
                          DefaultISR?" would pass the last two while the real
                          handler had been discarded. The binding letter is the
                          discriminator: `T` means our strong definition won,
                          `W` means the vendor stub is what got linked.

  2   object survival   -- every translation unit in the Makefile's object list
                          must contribute at least one symbol to the ELF unless
                          it is a named, deliberate exemption below. A TU that
                          is compiled, linked, and then silently discarded is a
                          build lie.

  3   flash geometry    -- `readelf -lW`. `objcopy -O binary` rebases silently
                          to the lowest loadable PhysAddr, so a stray low LOAD
                          segment would shift the whole image with no error
                          anywhere. Bounds the image against CPU0's flash
                          region, whose ceiling is `m_core1_image` ORIGIN --
                          the CORE1_OFFSET that step 5 will merge at.

  4   RAM geometry      -- `readelf -SW` plus `nm`. Bounds allocated content
                          against CPU0's `m_data`, checks the linker script's
                          own stack and heap symbols against the sizes the
                          Makefile declares, and asserts the CPU0 -> CPU1
                          shared window is both reserved and empty.

Usage (see firmware/mcxn947/Makefile, target `check`):

    python3 tools/check_mcxn947_images.py --cross-compile arm-none-eabi- \
        --core0 build/core0.elf --core0-bin build/hurra-mcxn947-core0.bin \
        --core0-flash-start 0x00000000 --core0-flash-end 0x000C0000 \
        --core0-ram-start 0x20000000 --core0-ram-end 0x2004C000 \
        --ram-shared-start 0x2004C000 --ram-shared-end 0x2004E000 \
        --stack-size 0x0800 --heap-size 0x0400 \
        --retain core0.elf:SysTick_Handler=build/core0/heartbeat.o \
        --object core0.elf:build/core0/heartbeat.o ...

Self-test:  python3 tools/check_mcxn947_images.py --self-test
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import subprocess
import sys

# The single ELF key used in every ELF:NAME command-line argument. Step 5 adds
# CORE1 = "core1.elf" here and everything below already takes a key.
CORE0 = "core0.elf"

# The weak stub every unhandled vector ultimately lands on. A root that
# resolves to this address was discarded and replaced.
DEFAULT_ISR = "DefaultISR"

# The output section the vendored linker scripts emit for the CPU0 -> CPU1
# shared window. It is RPMsg's address reservation, reused; see the Makefile.
SHARED_WINDOW_SECTION = ".noinit_rpmsg_sh_mem"

# nm binding letters that mean "this is a weak definition", i.e. nothing
# stronger overrode the vendor's stub.
WEAK_NM_TYPES = frozenset("wWvV")

# ---------------------------------------------------------------------------
# Rung 2 -- translation units knowingly linked but not (yet) reachable.
#
# Anything NOT listed here must contribute at least one symbol to its ELF.
# Every entry is a debt with an owner: deleting a line here is how a later
# migration step proves its code actually landed in the image.
# ---------------------------------------------------------------------------
DISCARD_EXEMPTIONS: dict[tuple[str, str], str] = {}


class Report:
    """Collects every assertion result so a run reports all failures at once."""

    def __init__(self) -> None:
        self.passes: list[str] = []
        self.failures: list[str] = []
        self.parse_errors: list[str] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.passes.append(f"{name}{': ' + detail if detail else ''}")

    def fail(self, name: str, detail: str) -> None:
        self.failures.append(f"{name}: {detail}")

    def unparsable(self, name: str, detail: str) -> None:
        """Distinct from fail(): the artifact could not be read or parsed.

        A toolchain bump that changes `nm` or `readelf` formatting lands here,
        not in failures, so "the matcher broke" is never mistaken for "the code
        is wrong".
        """
        self.parse_errors.append(f"{name}: {detail}")

    def emit(self) -> int:
        for line in self.passes:
            print(f"  PASS  {line}")
        for line in self.parse_errors:
            print(f"  ERROR {line}  (could not parse -- not an assertion failure)")
        for line in self.failures:
            print(f"  FAIL  {line}")
        print(
            f"check_mcxn947_images: {len(self.passes)} passed, "
            f"{len(self.failures)} failed, {len(self.parse_errors)} unparsable"
        )
        return 1 if (self.failures or self.parse_errors) else 0


# ---------------------------------------------------------------------------
# Toolchain helpers
# ---------------------------------------------------------------------------
def run(cross: str, tool: str, *args: str) -> str:
    exe = f"{cross}{tool}"
    proc = subprocess.run([exe, *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{exe} {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def parse_nm_posix(text: str) -> dict[str, tuple[str, int]]:
    """Parse `nm --defined-only --format=posix`.

    Columns are `NAME TYPE VALUE [SIZE]`; absolute symbols (`A`) carry a value
    but no size. Returns name -> (type letter, address).
    """
    symbols: dict[str, tuple[str, int]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or len(parts[1]) != 1:
            continue
        try:
            symbols[parts[0]] = (parts[1], int(parts[2], 16))
        except ValueError:
            continue
    return symbols


def elf_symbols(cross: str, path: str) -> dict[str, tuple[str, int]]:
    return parse_nm_posix(run(cross, "nm", "--defined-only", "--format=posix", path))


def object_symbol_names(cross: str, path: str) -> set[str]:
    """Names of every symbol *defined* by an object file."""
    return set(parse_nm_posix(run(cross, "nm", "--defined-only", "--format=posix", path)))


def parse_load_segments(text: str) -> list[tuple[int, int]]:
    """Parse `readelf -lW` into (PhysAddr, FileSiz) for file-backed LOADs.

    Columns: Type Offset VirtAddr PhysAddr FileSiz MemSiz Flg Align -- `Flg`
    may be one or two tokens ("RW " vs "R E"), so only fields 0..5 are relied
    on. Segments with FileSiz 0 carry nothing into `objcopy -O binary` and must
    not be counted.
    """
    segments = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 6 and parts[0] == "LOAD":
            try:
                phys = int(parts[3], 16)
                filesz = int(parts[4], 16)
            except ValueError:
                continue
            if filesz:
                segments.append((phys, filesz))
    return segments


_SECTION_RE = re.compile(
    r"^\s*\[\s*\d+\]\s+(?P<name>\S+)\s+(?P<type>\S+)\s+"
    r"(?P<addr>[0-9a-fA-F]+)\s+(?P<off>[0-9a-fA-F]+)\s+(?P<size>[0-9a-fA-F]+)\s+"
    r"(?P<es>[0-9a-fA-F]+)\s+(?P<flags>[A-Za-z]*)\s"
)


def parse_sections(text: str) -> list[tuple[str, str, int, int, str]]:
    """Parse `readelf -SW` into (name, type, addr, size, flags).

    A section with no flags column (`[ 0]` NULL, debug sections) still matches
    with an empty flag string, which is what the ALLOC filter wants.
    """
    sections = []
    for line in text.splitlines():
        match = _SECTION_RE.match(line)
        if not match:
            continue
        sections.append(
            (
                match.group("name"),
                match.group("type"),
                int(match.group("addr"), 16),
                int(match.group("size"), 16),
                match.group("flags"),
            )
        )
    return sections


# ---------------------------------------------------------------------------
# Rung 1 -- retention roots
# ---------------------------------------------------------------------------
def check_retained(
    report: Report,
    cross: str,
    elves: dict[str, str],
    roots: list[tuple[str, str, str]],
) -> None:
    elf_cache: dict[str, dict[str, tuple[str, int]]] = {}
    obj_cache: dict[str, set[str]] = {}

    for key, symbol, owner in roots:
        name = f"retain {key}:{symbol}"
        elf_path = elves.get(key)
        if elf_path is None:
            report.unparsable(name, f"no path supplied for {key}")
            continue
        try:
            if key not in elf_cache:
                elf_cache[key] = elf_symbols(cross, elf_path)
            if owner not in obj_cache:
                obj_cache[owner] = object_symbol_names(cross, owner)
        except (RuntimeError, FileNotFoundError) as exc:
            report.unparsable(name, str(exc))
            continue

        symbols = elf_cache[key]
        if symbol not in symbols:
            report.fail(
                name,
                f"'{symbol}' is not defined in {elf_path}; the -Wl,--undefined "
                f"root and the definition have drifted apart",
            )
            continue

        kind, address = symbols[symbol]

        if symbol not in obj_cache[owner]:
            report.fail(
                name,
                f"'{symbol}' is not defined by {owner}; the Makefile's "
                f"SYMBOL=OWNING_OBJECT pairing is wrong",
            )
            continue

        if kind in WEAK_NM_TYPES:
            report.fail(
                name,
                f"'{symbol}' is a weak definition ({kind}) at {address:#x}; no "
                f"strong definition from {owner} overrode the vendor stub in "
                f"startup_MCXN947_cm33_core0.S, so this vector spins",
            )
            continue

        default_isr = symbols.get(DEFAULT_ISR)
        if default_isr is not None and address == default_isr[1]:
            report.fail(
                name,
                f"'{symbol}' resolves to {DEFAULT_ISR} at {address:#x}; the "
                f"vector is unhandled and spins",
            )
            continue

        report.ok(name, f"{kind} {address:#x} from {os.path.basename(owner)}")


# ---------------------------------------------------------------------------
# Rung 2 -- object survival
# ---------------------------------------------------------------------------
def check_objects(
    report: Report,
    cross: str,
    elves: dict[str, str],
    objects: list[tuple[str, str]],
    exemptions: dict[tuple[str, str], str],
) -> None:
    cache: dict[str, set[str]] = {}
    seen: set[tuple[str, str]] = set()

    for key, obj_path in objects:
        base = os.path.basename(obj_path)
        seen.add((key, base))
        name = f"object-contributes {key}:{base}"
        elf_path = elves.get(key)
        if elf_path is None:
            report.unparsable(name, f"no path supplied for {key}")
            continue
        try:
            if key not in cache:
                cache[key] = set(elf_symbols(cross, elf_path))
            obj_syms = object_symbol_names(cross, obj_path)
        except (RuntimeError, FileNotFoundError) as exc:
            report.unparsable(name, str(exc))
            continue

        kept = obj_syms & cache[key]
        exempt = exemptions.get((key, base))
        if kept:
            if exempt is not None:
                report.fail(
                    f"stale-exemption {key}:{base}",
                    f"listed in DISCARD_EXEMPTIONS but now contributes "
                    f"{len(kept)} symbol(s); delete the exemption",
                )
            else:
                report.ok(name, f"{len(kept)}/{len(obj_syms)} symbols kept")
        elif exempt is not None:
            report.ok(f"expect-discarded {key}:{base}", "known-unreferenced")
        else:
            report.fail(
                name,
                f"contributes 0 of {len(obj_syms)} symbols to {elf_path}; "
                f"--gc-sections discarded the whole translation unit",
            )

    for (key, base), why in sorted(exemptions.items()):
        if (key, base) not in seen and key in elves:
            report.fail(
                f"stale-exemption {key}:{base}",
                f"exempted but not in the object list any more ({why})",
            )


# ---------------------------------------------------------------------------
# Rung 3 -- flash geometry
# ---------------------------------------------------------------------------
def check_flash_geometry(
    report: Report,
    cross: str,
    key: str,
    elf_path: str,
    binary: str | None,
    flash_start: int,
    flash_end: int,
) -> None:
    try:
        segments = parse_load_segments(run(cross, "readelf", "-lW", elf_path))
    except (RuntimeError, FileNotFoundError) as exc:
        report.unparsable(f"flash-geometry {key}", str(exc))
        return
    if not segments:
        report.unparsable(f"flash-geometry {key}", "no file-backed LOAD segments")
        return

    low = min(phys for phys, _ in segments)
    high = max(phys + size for phys, size in segments)

    base_name = f"load-base {key}"
    if low == flash_start:
        report.ok(base_name, f"{low:#010x}")
    else:
        report.fail(
            base_name,
            f"lowest file-backed LOAD PhysAddr is {low:#010x}, expected "
            f"{flash_start:#010x}; objcopy -O binary would rebase the image",
        )

    fit_name = f"flash-fit {key}"
    if high <= flash_end:
        report.ok(
            fit_name,
            f"ends {high:#010x} <= {flash_end:#010x} " f"({flash_end - high} B headroom)",
        )
    else:
        report.fail(
            fit_name,
            f"ends {high:#010x}, past the CPU0 flash ceiling {flash_end:#010x} "
            f"(m_core1_image ORIGIN, and the step-5 CORE1_OFFSET)",
        )

    if binary is None:
        return
    size_name = f"binary-size {key}"
    try:
        size = os.path.getsize(binary)
    except OSError as exc:
        report.unparsable(size_name, str(exc))
        return
    limit = flash_end - flash_start
    if size <= limit:
        report.ok(size_name, f"{size} B <= {limit:#x}")
    else:
        report.fail(size_name, f"{size} B exceeds the CPU0 flash region {limit:#x}")


# ---------------------------------------------------------------------------
# Rung 4 -- RAM geometry
# ---------------------------------------------------------------------------
def allocated_ram_sections(
    sections: list[tuple[str, str, int, int, str]], start: int, end: int
) -> list[tuple[str, str, int, int, str]]:
    return [
        row
        for row in sections
        if "A" in row[4] and row[3] > 0 and row[2] < end and row[2] + row[3] > start
    ]


def check_ram_geometry(
    report: Report,
    cross: str,
    key: str,
    elf_path: str,
    ram_start: int,
    ram_end: int,
    stack_size: int,
    heap_size: int,
    shared_start: int,
    shared_end: int,
) -> None:
    try:
        sections = parse_sections(run(cross, "readelf", "-SW", elf_path))
        symbols = elf_symbols(cross, elf_path)
    except (RuntimeError, FileNotFoundError) as exc:
        report.unparsable(f"ram-geometry {key}", str(exc))
        return

    content = allocated_ram_sections(sections, ram_start, ram_end)
    if not content:
        report.unparsable(
            f"ram-geometry {key}",
            f"no allocated sections in {ram_start:#010x}..{ram_end:#010x}",
        )
        return

    # The vendored script puts the real stack at the top of m_data
    # (__StackTop = ORIGIN + LENGTH) while ALSO emitting a same-sized `.stack`
    # placeholder right after `.heap`. Both are asserted rather than one being
    # assumed away, because the sizes come from the Makefile and a change to
    # either has to show up here.
    for symbol, expected, why in (
        ("__StackTop", ram_end, "stack grows down from the top of m_data"),
        ("__StackLimit", ram_end - stack_size, "STACK_SIZE from the linker script"),
    ):
        name = f"ram-{symbol} {key}"
        if symbol not in symbols:
            report.fail(name, f"{symbol} is not defined ({why})")
        elif symbols[symbol][1] == expected:
            report.ok(name, f"{expected:#010x}")
        else:
            report.fail(
                name,
                f"{symbol} is {symbols[symbol][1]:#010x}, expected {expected:#010x} ({why})",
            )

    heap_name = f"ram-heap {key}"
    if "__HeapBase" in symbols and "__HeapLimit" in symbols:
        measured = symbols["__HeapLimit"][1] - symbols["__HeapBase"][1]
        if measured == heap_size:
            report.ok(heap_name, f"{measured} B at {symbols['__HeapBase'][1]:#010x}")
        else:
            report.fail(
                heap_name,
                f"__HeapLimit - __HeapBase is {measured} B, Makefile declares {heap_size} B",
            )
    else:
        report.fail(heap_name, "__HeapBase / __HeapLimit are not both defined")

    content_end = max(addr + size for _, _, addr, size, _ in content)
    stack_floor = ram_end - stack_size
    fit_name = f"ram-fit {key}"
    if content_end <= stack_floor:
        report.ok(
            fit_name,
            f"content ends {content_end:#010x}; {stack_floor - content_end} B "
            f"below the stack floor {stack_floor:#010x}",
        )
    else:
        report.fail(
            fit_name,
            f"content ends {content_end:#010x}, past the stack floor {stack_floor:#010x}",
        )

    # The shared window must be *reserved* -- proving -Wl,--defsym,__use_shmem__=1
    # survived into the link -- and, until step 6, empty of anything else.
    reserved = [row for row in sections if row[0] == SHARED_WINDOW_SECTION]
    reserve_name = f"shared-window-reserved {key}"
    if len(reserved) == 1 and reserved[0][2] == shared_start:
        report.ok(
            reserve_name,
            f"{SHARED_WINDOW_SECTION} at {shared_start:#010x}; m_data ends there",
        )
    else:
        report.fail(
            reserve_name,
            f"expected one {SHARED_WINDOW_SECTION} at {shared_start:#010x}, found "
            f"{[(r[0], hex(r[2])) for r in reserved]}; without "
            f"-Wl,--defsym,__use_shmem__=1 m_data runs to {shared_end:#010x} and "
            f"the CPU0 -> CPU1 window is not reserved at all",
        )

    intruders = [
        row
        for row in allocated_ram_sections(sections, shared_start, shared_end)
        if row[0] != SHARED_WINDOW_SECTION
    ]
    disjoint_name = f"shared-window-disjoint {key}"
    if not intruders:
        report.ok(
            disjoint_name,
            f"{shared_start:#010x}..{shared_end:#010x} carries no {key} content",
        )
    else:
        report.fail(
            disjoint_name,
            f"{[(r[0], hex(r[2]), r[3]) for r in intruders]} allocate into the "
            f"shared window {shared_start:#010x}..{shared_end:#010x}",
        )


# ---------------------------------------------------------------------------
# Self-test -- exercises the parsers and every rung's decision logic against
# fabricated toolchain output, so the gate itself is not the untested part.
# ---------------------------------------------------------------------------
_NM_SAMPLE = """\
CTIMER2_DriverIRQHandler W 4f8 4
CTIMER2_IRQHandler W 598 4
DefaultISR W 4f8 4
HardFault_Handler W 500 4
SysTick_Handler T 1830 1c
__HeapBase B 200001e0
__HeapLimit B 200005e0
__StackLimit A 2004b800
__StackTop B 2004c000
main T 184c 10
"""

_READELF_L_SAMPLE = """\
Program Headers:
  Type           Offset   VirtAddr   PhysAddr   FileSiz MemSiz  Flg Align
  LOAD           0x001000 0x00000000 0x00000000 0x002b0 0x002b0 R   0x1000
  LOAD           0x001400 0x00000400 0x00000400 0x02684 0x02684 RWE 0x1000
  LOAD           0x004000 0x20000000 0x00002a84 0x00068 0x00de0 RW  0x1000
  LOAD           0x004068 0x400ba000 0x400ba000 0x00000 0x00000 RW  0x1000
"""

_READELF_S_SAMPLE = """\
Section Headers:
  [Nr] Name              Type            Addr     Off    Size   ES Flg Lk Inf Al
  [ 0]                   NULL            00000000 000000 000000 00      0   0  0
  [ 2] .noinit_rpmsg_sh_mem PROGBITS        2004c000 004068 000000 00   W  0   0  4
  [ 4] .interrupts       PROGBITS        00000000 001000 0002b0 00   A  0   0  4
  [ 5] .text             PROGBITS        00000400 001400 002674 00  AX  0   0  4
  [ 9] .data             PROGBITS        20000000 004000 000068 00  WA  0   0  4
  [10] .bss              NOBITS          20000068 004068 000178 00  WA  0   0  4
  [11] .heap             NOBITS          200001e0 004068 000400 00  WA  0   0  1
  [12] .stack            NOBITS          200005e0 004068 000800 00  WA  0   0  1
"""


def _self_test() -> int:
    failures: list[str] = []

    def expect(condition: bool, what: str) -> None:
        if not condition:
            failures.append(what)

    symbols = parse_nm_posix(_NM_SAMPLE)
    expect(symbols["SysTick_Handler"] == ("T", 0x1830), "nm: strong symbol")
    expect(symbols["DefaultISR"] == ("W", 0x4F8), "nm: weak symbol")
    expect(symbols["__StackLimit"] == ("A", 0x2004B800), "nm: absolute symbol, no size")
    expect(len(symbols) == 10, f"nm: symbol count {len(symbols)}")

    segments = parse_load_segments(_READELF_L_SAMPLE)
    expect(len(segments) == 3, "readelf -l: zero-FileSiz LOAD dropped")
    expect(min(p for p, _ in segments) == 0x0, "readelf -l: lowest PhysAddr")
    expect(max(p + s for p, s in segments) == 0x2AEC, "readelf -l: highest PhysAddr")

    sections = parse_sections(_READELF_S_SAMPLE)
    expect(len(sections) == 8, f"readelf -S: section count {len(sections)}")
    names = {row[0] for row in sections}
    expect(SHARED_WINDOW_SECTION in names, "readelf -S: shared window section")
    ram = allocated_ram_sections(sections, 0x20000000, 0x2004C000)
    expect({row[0] for row in ram} == {".data", ".bss", ".heap", ".stack"}, "RAM filter")
    expect(
        allocated_ram_sections(sections, 0x2004C000, 0x2004E000) == [],
        "RAM filter: zero-size, non-alloc window section is not content",
    )
    expect(
        max(addr + size for _, _, addr, size, _ in ram) == 0x20000DE0,
        "RAM filter: content end",
    )

    # Rung 1's decision logic, against the three weak shapes this part has.
    def verdict(symbol: str) -> str:
        kind, address = symbols[symbol]
        if kind in WEAK_NM_TYPES:
            return "weak"
        if address == symbols[DEFAULT_ISR][1]:
            return "default-isr"
        return "ok"

    expect(verdict("SysTick_Handler") == "ok", "rung 1: strong handler accepted")
    expect(verdict("main") == "ok", "rung 1: entry accepted")
    expect(verdict("CTIMER2_DriverIRQHandler") == "weak", "rung 1: DefaultISR alias")
    expect(verdict("CTIMER2_IRQHandler") == "weak", "rung 1: weak trampoline")
    expect(verdict("HardFault_Handler") == "weak", "rung 1: weak self-loop")

    # emit() prints; swallow it so the self-test's own output stays clean.
    report = Report()
    report.ok("passing assertion")
    report.fail("failing assertion", "why")
    with contextlib.redirect_stdout(io.StringIO()) as sink:
        code = report.emit()
    expect(code == 1, "Report: a failure exits non-zero")
    expect("FAIL" in sink.getvalue(), "Report: a failure is printed")

    clean = Report()
    clean.ok("passing assertion")
    with contextlib.redirect_stdout(io.StringIO()):
        expect(clean.emit() == 0, "Report: passes alone exit zero")

    noisy = Report()
    noisy.unparsable("broken matcher", "why")
    with contextlib.redirect_stdout(io.StringIO()):
        expect(noisy.emit() == 1, "Report: an unparsable artifact exits non-zero")

    if failures:
        for line in failures:
            print(f"  FAIL  self-test: {line}")
        print(f"check_mcxn947_images --self-test: {len(failures)} failed")
        return 1
    print("check_mcxn947_images --self-test: ok")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def split_retain(value: str) -> tuple[str, str, str]:
    """`core0.elf:SysTick_Handler=build/core0/heartbeat.o`."""
    key, _, rest = value.partition(":")
    symbol, _, owner = rest.partition("=")
    if not key or not symbol or not owner:
        raise argparse.ArgumentTypeError(
            f"--retain expects ELF:SYMBOL=OWNING_OBJECT, got {value!r}"
        )
    return key, symbol, owner


def split_object(value: str) -> tuple[str, str]:
    """`core0.elf:build/core0/heartbeat.o`."""
    key, sep, path = value.partition(":")
    if not key or not sep or not path:
        raise argparse.ArgumentTypeError(f"--object expects ELF:PATH, got {value!r}")
    return key, path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cross-compile", default="arm-none-eabi-", help="toolchain prefix")
    parser.add_argument("--core0", help="path to core0.elf")
    parser.add_argument("--core0-bin", help="path to the flat CPU0 image")
    parser.add_argument("--core0-flash-start", default="0x00000000")
    parser.add_argument("--core0-flash-end", default="0x000C0000")
    parser.add_argument("--core0-ram-start", default="0x20000000")
    parser.add_argument("--core0-ram-end", default="0x2004C000")
    parser.add_argument("--ram-shared-start", default="0x2004C000")
    parser.add_argument("--ram-shared-end", default="0x2004E000")
    parser.add_argument("--stack-size", default="0x0800")
    parser.add_argument("--heap-size", default="0x0400")
    parser.add_argument(
        "--retain",
        action="append",
        default=[],
        type=split_retain,
        metavar="ELF:SYMBOL=OBJECT",
        help="a -Wl,--undefined root and the object that must define it",
    )
    parser.add_argument(
        "--object",
        action="append",
        default=[],
        type=split_object,
        metavar="ELF:PATH",
        help="a translation unit that must survive into the image",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    if not args.core0:
        parser.error("--core0 is required unless --self-test is given")

    elves = {CORE0: args.core0}
    report = Report()

    check_retained(report, args.cross_compile, elves, args.retain)
    check_objects(report, args.cross_compile, elves, args.object, DISCARD_EXEMPTIONS)
    check_flash_geometry(
        report,
        args.cross_compile,
        CORE0,
        args.core0,
        args.core0_bin,
        int(args.core0_flash_start, 0),
        int(args.core0_flash_end, 0),
    )
    check_ram_geometry(
        report,
        args.cross_compile,
        CORE0,
        args.core0,
        int(args.core0_ram_start, 0),
        int(args.core0_ram_end, 0),
        int(args.stack_size, 0),
        int(args.heap_size, 0),
        int(args.ram_shared_start, 0),
        int(args.ram_shared_end, 0),
    )

    return report.emit()


if __name__ == "__main__":
    sys.exit(main())
