#!/usr/bin/env python3
"""Static verification ladder for the CH32H417 dual-core firmware images.

`make all` exits 0 while `--gc-sections` throws the application away. This
script asserts on the *built artifacts* instead of on make's exit status.

Rungs (cheapest first, none require hardware):

  1   symbol survival   -- `nm` on the linked ELF. A symbol the firmware needs
                          must actually be defined in the image.
  1b  object survival   -- every translation unit listed in the Makefile's
                          object list must contribute at least one symbol to
                          the final ELF, unless it is a *named, deliberate*
                          exemption in DISCARD_EXEMPTIONS below. A TU that is
                          compiled, linked, and then silently discarded is a
                          build lie; this is exactly how W4 and W8 survived.
  2   image geometry    -- `readelf -lW`. `objcopy -O binary` rebases silently
                          to the lowest loadable PhysAddr, so a stray low LOAD
                          segment would shift a whole image under the merge
                          tool with no error anywhere. Also bounds each image
                          against its declared FLASH region and the merged
                          image against the part's ceiling.
  2b  RAM geometry      -- `readelf -SW`. Bounds PROGBITS and NOBITS RAM
                          content plus the configured stack, reports remaining
                          headroom, and keeps V3F out of the shared window.
  3   disassembly       -- `objdump -d`, scoped to one symbol. Added by later
                          tasks in the bring-up plan.

Every failure is reported before exiting non-zero: a stop-at-first-failure
gate makes a multi-defect regression take N runs to diagnose.

Toolchain sensitivity: the disassembly rung matches against output from
xPack riscv-none-elf-gcc EXPECTED_TOOLCHAIN. A toolchain bump can change
register allocation or instruction selection and break a matcher with no
source change, so parse failures are reported distinctly from assertion
failures ("could not parse" vs "assertion failed").

Usage (see firmware/ch32h417/Makefile, target `check`):

    python3 tools/check_images.py --prefix PREFIX \
        --v3f build/v3f.elf --v5f build/v5f.elf \
        --merged build/hurra-ch32h417.bin --v5f-offset 0x10000 \
        --v3f-ram-start START --v3f-ram-end END \
        --v5f-ram-start START --v5f-ram-end END --stack-size SIZE \
        --ram-shared-start START --ram-shared-end END \
        --object v3f.elf:build/v3f/main.o ...

Self-test:  python3 tools/check_images.py --self-test
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile

EXPECTED_TOOLCHAIN = "15.2.0"

# The two ELF keys used in every ELF:NAME command-line argument.
V3F = "v3f.elf"
V5F = "v5f.elf"
ELF_KEYS = (V3F, V5F)

# ---------------------------------------------------------------------------
# Rung 1 -- symbols the firmware must actually contain.
#
# Later tasks in the bring-up plan append to this list; it is the plan's
# regression mechanism. Each entry is (elf_key, symbol, why).
# ---------------------------------------------------------------------------
REQUIRED_SYMBOLS = [
    (V3F, "main", "V3F entry; the core boots straight into it"),
    (V5F, "main", "V5F entry; reached via NVIC_WakeUp_V5F"),
    (V5F, "SystemAndCoreClockUpdate", "main_v5f.c calls it to publish clock globals"),
    (V5F, "TIM3_IRQHandler", "core/timebase.c 1 kHz tick ISR"),
    (V5F, "millis", "core/timebase.c millisecond accessor"),
    (V3F, "SystemInit", "V3F programs the PLL and switches SYSCLK for both cores"),
    (V3F, "SystemCoreClock", "clock globals must exist in the V3F address space"),
    (V3F, "HardFault_Handler", "V3F faults must leave an observable witness"),
    (V5F, "HardFault_Handler", "V5F faults must leave an observable witness"),
]

# (elf_key, caller, callee, why)
EXPECTED_CALLS = [
    (
        V3F,
        "main",
        "SystemInit",
        "without it SetSysClock() never runs and the part stays on the "
        "startup's hand-rolled ~70 MHz .load PLL block",
    ),
    (
        V3F,
        "main",
        "SystemAndCoreClockUpdate",
        "each core must run the updater on its own copy of the clock globals; "
        "system_ch32h417.c:258-266 branches on NVIC_GetCurrentCoreID()",
    ),
]

# (elf_key, symbol, expected little-endian word, why)
EXPECTED_INITIALIZERS = [
    (
        V3F,
        "SystemCoreClock",
        100_000_000,
        "pins the enabled profile SYSCLK_400M_CoreCLK_V5F_400M_V3F_100M_HSE; "
        "HCLK == the V3F core clock is what the FPGA's NSS budget is derived "
        "from, so a profile switch must fail the build, not go unnoticed",
    ),
]

# ---------------------------------------------------------------------------
# Rung 3 -- disassembly assertions. (elf_key, symbol, why).
# ---------------------------------------------------------------------------
MRET_HANDLERS = [
    (
        V5F,
        "TIM3_IRQHandler",
        "WCH_IRQ must emit an mret epilogue; a plain `ret` returns into a "
        "garbage `ra` and wedges the core (see include/ch32h417_port.h)",
    ),
]

# (elf_key, symbol, must-differ-from, why)
DISTINCT_SYMBOLS = [
    (
        V5F,
        "TIM3_IRQHandler",
        "WWDG_IRQHandler",
        "the real definition must displace the shared weak stub at "
        "core/startup_v5f.S, not merely resolve to it",
    ),
    (
        V3F,
        "HardFault_Handler",
        "WWDG_IRQHandler",
        "the V3F trap witness must displace the shared weak spin stub",
    ),
    (
        V5F,
        "HardFault_Handler",
        "WWDG_IRQHandler",
        "the V5F trap witness must displace the shared weak spin stub",
    ),
]

# (elf_key, symbol, mnemonic, why)
EXPECTED_INSTRUCTIONS = [
    (
        V5F,
        "HardFault_Handler",
        "fence",
        "the V5F trap witness must release its Normal-memory SRAM payload "
        "before publishing the witness code",
    ),
]

SHARED_DIAG_WITNESS_ADDR = 0x2017FF00

# ---------------------------------------------------------------------------
# Rung 1b -- translation units knowingly linked but not (yet) reachable.
#
# Anything NOT listed here must contribute at least one symbol to its ELF.
# Every entry is a debt with an owner: deleting a line here is how a later
# task proves its code actually landed in the image.
# ---------------------------------------------------------------------------
DISCARD_EXEMPTIONS = {}
# The vendor StdPeriph drivers are compiled as a selected-driver set; each
# becomes reachable only when a Phase 2 subsystem calls into it. `rcc`, `tim`,
# `dma`, `gpio`, and `spi` are deliberately absent: core/timebase.c calls into
# rcc/tim, and the SPI link (src/ch32_link.c, roadmap Phase 2 Task 3) calls into
# dma/gpio/spi, so all five -- along with spi_frame.o, whose codec the link now
# drives -- are required to contribute. `iwdg` also became live once the
# watchdog (src/watchdog.c) started reloading it.
for _drv in ("eth",):
    DISCARD_EXEMPTIONS[(V5F, f"vendor_{_drv}.o")] = (
        f"WCH StdPeriph {_drv} driver: linked for Phase 2, no caller yet."
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
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
        """Distinct from fail(): the artifact could not be read/parsed at all.

        A toolchain bump that changes disassembly formatting lands here, not in
        failures, so "the matcher broke" is never mistaken for "the code is
        wrong".
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
            f"check_images: {len(self.passes)} passed, "
            f"{len(self.failures)} failed, {len(self.parse_errors)} unparsable"
        )
        return 1 if (self.failures or self.parse_errors) else 0


# ---------------------------------------------------------------------------
# Toolchain helpers
# ---------------------------------------------------------------------------
def run(prefix: str, tool: str, *args: str) -> str:
    exe = f"{prefix}-{tool}"
    proc = subprocess.run([exe, *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{exe} {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def defined_symbols(prefix: str, path: str) -> set[str]:
    """Names of every symbol *defined* by an ELF or object file."""
    out = run(prefix, "nm", "--defined-only", path)
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            names.add(parts[-1])
    return names


def load_segments(prefix: str, path: str) -> list[tuple[int, int]]:
    """(PhysAddr, FileSiz) for every PT_LOAD segment with file-backed content.

    NOBITS-only segments (FileSiz 0 -- the stack reservation at 0x20177800 /
    0x200ff800) carry nothing into `objcopy -O binary` and must not be counted.
    """
    return parse_load_segments(run(prefix, "readelf", "-lW", path))


def parse_load_segments(text: str) -> list[tuple[int, int]]:
    """Parse `readelf -lW` output. Columns: Type Offset VirtAddr PhysAddr
    FileSiz MemSiz Flg Align -- `Flg` may be one or two tokens ("RW " vs
    "R E"), so only fields 0..5 are relied on."""
    segments = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 6 and parts[0] == "LOAD":
            phys = int(parts[3], 16)
            filesz = int(parts[4], 16)
            if filesz:
                segments.append((phys, filesz))
    return segments


def toolchain_version(prefix: str) -> str:
    try:
        first = run(prefix, "gcc", "--version").splitlines()[0]
    except (RuntimeError, IndexError, FileNotFoundError):
        return "unknown"
    match = re.search(r"(\d+\.\d+\.\d+)", first)
    return match.group(1) if match else "unknown"


def symbol_addresses(prefix: str, path: str) -> dict[str, int]:
    """name -> address for every defined symbol, weak included."""
    out = run(prefix, "nm", "--defined-only", path)
    addrs = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            try:
                addrs[parts[2]] = int(parts[0], 16)
            except ValueError:
                continue
    return addrs


_BLOCK_RE = re.compile(r"^([0-9a-fA-F]+)\s+<(.+)>:$")
_INSN_RE = re.compile(r"^\s*([0-9a-fA-F]+):\s+[0-9a-fA-F ]+\t(\S+)\s*(.*)$")
# Pinned output contract: xPack riscv-none-elf-gcc 15.2.0-1.1 objdump emits
# resolved MMIO addresses as `# e000e724 <symbol+offset>`. Register names are
# deliberately not captured: they are allocation artifacts.
_RESOLVED_ADDRESS_RE = re.compile(r"#\s*(?:0x)?(?P<address>[0-9a-fA-F]+)(?:\s|$)")


def parse_disassembly(text: str) -> dict[int, list[tuple[int, str, str]]]:
    """Parse `objdump -d` into {block_start_address: [(addr, mnemonic, ops)]}.

    Keyed by ADDRESS, not by label. Both startups alias ~130 weak handlers onto
    a single spin stub, and objdump labels that block with whichever alias sorts
    first (ADC1_2_IRQHandler). Looking a handler up by its nm address therefore
    finds the stub it actually resolves to, which is the whole point of the
    not-the-spin-stub assertion.
    """
    blocks: dict[int, list[tuple[int, str, str]]] = {}
    current: list[tuple[int, str, str]] | None = None
    for line in text.splitlines():
        head = _BLOCK_RE.match(line)
        if head:
            current = []
            blocks[int(head.group(1), 16)] = current
            continue
        insn = _INSN_RE.match(line)
        if insn and current is not None:
            # Drop objdump's trailing `# <resolved value>` annotation: it is a
            # convenience, not part of the instruction, and its presence
            # depends on whether a symbol happens to be nearby.
            ops = insn.group(3).split("#")[0].strip()
            current.append((int(insn.group(1), 16), insn.group(2), ops))
    return blocks


def parse_resolved_addresses(text: str) -> dict[int, int]:
    """Instruction address -> objdump's resolved address comment, when present."""
    resolved = {}
    for line in text.splitlines():
        insn = _INSN_RE.match(line)
        if insn is None:
            continue
        comment = _RESOLVED_ADDRESS_RE.search(insn.group(3))
        if comment is not None:
            resolved[int(insn.group(1), 16)] = int(comment.group("address"), 16)
    return resolved


def flat_instructions(
    blocks: dict[int, list[tuple[int, str, str]]],
) -> list[tuple[int, str, str]]:
    """Every instruction in the image, in address order, blocks flattened."""
    out: list[tuple[int, str, str]] = []
    for start in sorted(blocks):
        out.extend(blocks[start])
    return out


def materialised_value(
    stream: list[tuple[int, str, str]], index: int, reg: str, window: int = 8
) -> int | None:
    """Reconstruct the constant in `reg` at `stream[index]` by walking back over
    the lui/addi/li pair GCC emits for a 32-bit immediate.

    Returns None if the pattern is not recognised -- reported as "could not
    parse", never as an assertion failure, so a toolchain bump that changes
    instruction selection is not mistaken for a defect.
    """
    value = 0
    for step in range(1, window + 1):
        if index - step < 0:
            break
        _, mnemonic, ops = stream[index - step]
        fields = [f.strip() for f in ops.split(",")]
        if not fields or fields[0] != reg:
            continue
        if mnemonic in ("lui", "c.lui") and len(fields) == 2:
            return ((int(fields[1], 0) << 12) + value) & 0xFFFFFFFF
        if mnemonic in ("addi", "c.addi", "addiw") and len(fields) == 3:
            value += int(fields[2], 0)
            continue
        if mnemonic in ("li", "c.li") and len(fields) == 2:
            return (int(fields[1], 0) + value) & 0xFFFFFFFF
        break
    return None


def disassembly(prefix: str, path: str) -> dict[int, list[tuple[int, str, str]]]:
    return parse_disassembly(run(prefix, "objdump", "-d", path))


# ---------------------------------------------------------------------------
# Rung 1 -- symbol survival
# ---------------------------------------------------------------------------
def check_symbols(
    report: Report, prefix: str, elves: dict[str, str], required: list[tuple[str, str, str]]
) -> None:
    cache: dict[str, set[str]] = {}
    for key, symbol, why in required:
        name = f"require-symbol {key}:{symbol}"
        path = elves.get(key)
        if path is None:
            report.unparsable(name, f"no path supplied for {key}")
            continue
        try:
            if key not in cache:
                cache[key] = defined_symbols(prefix, path)
        except (RuntimeError, FileNotFoundError) as exc:
            report.unparsable(name, str(exc))
            continue
        if symbol in cache[key]:
            report.ok(name)
        else:
            report.fail(name, f"symbol '{symbol}' is not defined in {path} ({why})")


# ---------------------------------------------------------------------------
# Rung 1b -- object survival
# ---------------------------------------------------------------------------
def check_objects(
    report: Report,
    prefix: str,
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
                cache[key] = defined_symbols(prefix, elf_path)
            obj_syms = defined_symbols(prefix, obj_path)
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
# Rung 2 -- image geometry
# ---------------------------------------------------------------------------
def check_geometry(
    report: Report,
    prefix: str,
    elves: dict[str, str],
    merged: str | None,
    v5f_offset: int,
    v3f_region: int,
    v5f_region: int,
    flash_limit: int,
) -> None:
    bounds = {
        V3F: ("lowest-load", 0x00000000, v5f_offset, "V3F must not reach the V5F offset"),
        V5F: ("lowest-load", v5f_offset, v5f_offset + v5f_region, "V5F FLASH region"),
    }
    del v3f_region  # V3F's ceiling is the V5F offset, which is the tighter bound.
    for key, (_, expect_low, ceiling, why) in bounds.items():
        path = elves.get(key)
        if path is None:
            continue
        try:
            segments = load_segments(prefix, path)
        except (RuntimeError, FileNotFoundError) as exc:
            report.unparsable(f"geometry {key}", str(exc))
            continue
        if not segments:
            report.unparsable(f"geometry {key}", "no file-backed LOAD segments")
            continue
        low = min(phys for phys, _ in segments)
        high = max(phys + size for phys, size in segments)
        base_name = f"load-base {key}"
        if low == expect_low:
            report.ok(base_name, f"{low:#010x}")
        else:
            report.fail(
                base_name,
                f"lowest file-backed LOAD PhysAddr is {low:#010x}, expected "
                f"{expect_low:#010x}; objcopy -O binary would rebase the image",
            )
        fit_name = f"region-fit {key}"
        if high <= ceiling:
            report.ok(fit_name, f"ends {high:#010x} <= {ceiling:#010x}")
        else:
            report.fail(fit_name, f"ends {high:#010x}, past {ceiling:#010x} ({why})")

    if merged is not None:
        name = "merged-size"
        try:
            size = os.path.getsize(merged)
        except OSError as exc:
            report.unparsable(name, str(exc))
            return
        if size <= flash_limit:
            report.ok(name, f"{size} B <= {flash_limit:#x}")
        else:
            report.fail(name, f"{size} B exceeds flash limit {flash_limit:#x}")


def check_ram_geometry(
    report: Report,
    prefix: str,
    elves: dict[str, str],
    regions: dict[str, tuple[int, int, int]],
    shared_region: tuple[int, int],
) -> None:
    for key, (region_start, region_end, stack_size) in regions.items():
        path = elves.get(key)
        if path is None:
            continue
        try:
            sections = section_table(prefix, path)
        except (RuntimeError, FileNotFoundError) as exc:
            report.unparsable(f"RAM geometry {key}", str(exc))
            continue

        stack_start = region_end - stack_size
        stack_reservations = [
            row
            for row in sections
            if row[0] == stack_start and row[2] == stack_size and row[3] == "NOBITS"
        ]
        stack_name = f"ram-stack-geometry {key}"
        if len(stack_reservations) == 1:
            report.ok(
                stack_name,
                f"NOBITS {stack_start:#010x}..{region_end:#010x} ({stack_size} B)",
            )
        else:
            report.fail(
                stack_name,
                f"expected one {stack_size} B NOBITS stack reservation at "
                f"{stack_start:#010x}..{region_end:#010x}, found "
                f"{len(stack_reservations)}",
            )

        content = [
            row
            for row in sections
            if row[3] in ("PROGBITS", "NOBITS")
            and row[2] > 0
            and row[0] < region_end
            and row[0] + row[2] > region_start
            and row not in stack_reservations
        ]
        if not content:
            report.unparsable(
                f"RAM geometry {key}",
                f"no PROGBITS or NOBITS content in {region_start:#010x}..{region_end:#010x}",
            )
            continue

        content_end = max(address + size for address, _, size, _ in content)
        occupied_end = content_end + stack_size
        headroom = region_end - occupied_end
        fit_name = f"ram-fit {key}"
        if headroom >= 0:
            report.ok(
                fit_name,
                f"headroom {headroom} B; content ends {content_end:#010x} + "
                f"stack {stack_size} B <= {region_end:#010x}",
            )
        else:
            report.fail(
                fit_name,
                f"headroom {headroom} B; content ends {content_end:#010x} + "
                f"stack {stack_size} B ends {occupied_end:#010x}, past "
                f"{region_end:#010x}",
            )

        if key == V3F:
            shared_start, shared_end = shared_region
            shared_name = f"ram-shared-disjoint {key}"
            if occupied_end <= shared_start or region_start >= shared_end:
                report.ok(
                    shared_name,
                    f"projected content+stack ends {occupied_end:#010x}; shared window "
                    f"{shared_start:#010x}..{shared_end:#010x}",
                )
            else:
                report.fail(
                    shared_name,
                    f"projected content+stack ends {occupied_end:#010x}, overlapping shared "
                    f"window {shared_start:#010x}..{shared_end:#010x}",
                )


# ---------------------------------------------------------------------------
# Rung 3 -- disassembly assertions
#
# Reaches the findings a symbol check cannot: that an ISR ends in `mret` and
# not `ret`, and that a handler is a real definition rather than an alias onto
# the shared weak spin stub.
# ---------------------------------------------------------------------------
class ElfView:
    """Lazily-loaded nm addresses and objdump blocks for each ELF."""

    def __init__(self, prefix: str, elves: dict[str, str]) -> None:
        self.prefix = prefix
        self.elves = elves
        self._addrs: dict[str, dict[str, int]] = {}
        self._blocks: dict[str, dict[int, list[tuple[int, str, str]]]] = {}
        self._resolved: dict[str, dict[int, int]] = {}

    def address(self, key: str, symbol: str) -> tuple[int | None, str | None]:
        path = self.elves.get(key)
        if path is None:
            return None, f"no path supplied for {key}"
        try:
            if key not in self._addrs:
                self._addrs[key] = symbol_addresses(self.prefix, path)
        except (RuntimeError, FileNotFoundError) as exc:
            return None, str(exc)
        if symbol not in self._addrs[key]:
            return None, f"symbol '{symbol}' is not defined in {path}"
        return self._addrs[key][symbol], None

    def body(self, key: str, symbol: str) -> tuple[list[tuple[int, str, str]] | None, str | None]:
        addr, err = self.address(key, symbol)
        if err is not None:
            return None, err
        path = self.elves[key]
        try:
            if key not in self._blocks:
                out = run(self.prefix, "objdump", "-d", path)
                self._blocks[key] = parse_disassembly(out)
                self._resolved[key] = parse_resolved_addresses(out)
        except (RuntimeError, FileNotFoundError) as exc:
            return None, str(exc)
        blocks = self._blocks[key]
        if not blocks:
            return None, f"objdump -d {path} yielded no disassembly at all"
        body = blocks.get(addr)
        if body is None:
            return None, f"no instruction block starts at {symbol}'s address {addr:#010x}"
        return body, None

    def resolved_address(self, key: str, instruction: int) -> tuple[int | None, str | None]:
        """Return objdump's resolved-address comment for one instruction."""
        path = self.elves.get(key)
        if path is None:
            return None, f"no path supplied for {key}"
        try:
            if key not in self._resolved:
                out = run(self.prefix, "objdump", "-d", path)
                if key not in self._blocks:
                    self._blocks[key] = parse_disassembly(out)
                self._resolved[key] = parse_resolved_addresses(out)
        except (RuntimeError, FileNotFoundError) as exc:
            return None, str(exc)
        return self._resolved[key].get(instruction), None


def check_mret(report: Report, view: ElfView, cases: list[tuple[str, str, str]]) -> None:
    """The `interrupt("WCH-Interrupt-fast")` attribute is a MounRiver-GCC
    extension that stock GCC silently ignores, compiling the handler as an
    ordinary function that returns with `ret` -- which "returns" into a garbage
    `ra` and wedges the core. include/ch32h417_port.h remedies it with
    `interrupt("machine")`. Only the emitted instruction settles whether that
    worked, so assert on it."""
    for key, symbol, why in cases:
        name = f"expect-mret {key}:{symbol}"
        body, err = view.body(key, symbol)
        if err is not None:
            report.unparsable(name, err)
            continue
        if not body:
            report.fail(name, f"{symbol} has an empty instruction block ({why})")
            continue
        last = body[-1][1]
        if last == "mret":
            report.ok(name)
        else:
            report.fail(
                name,
                f"last instruction of {symbol} is '{last}', expected 'mret' ({why})",
            )


def check_fpu_dividers(report: Report, view: ElfView, key: str, core_hz: int) -> None:
    """CPU_RUN_CTLR (CSR 0xBC0) bits [31:16] are the four FPU clock dividers.

    RM V1.7 section 4.2.3.7 (V5F): fadd_clkdiv[31:28] reset 1, fmul_clkdiv
    [27:24] reset 2, fmac_clkdiv[23:20] reset 3, fdiv_clkdiv[19:16] reset 7,
    each documented as "default N, i.e. main clock / (N+1)". The rule, verbatim,
    is `fxxx_freq(max) >= core_clock / fxxx_clkdiv`, with V5F maxima of
    128 / 96 / 76 / 38 MHz.

    Parameterised on the CONFIGURED profile, not the clock the part happens to
    run at today, because the whole point is that the shipped defaults are
    legal at ~70 MHz and violate all four limits the instant the clock tree is
    programmed to its intended 400 MHz.
    """
    name = "check-fpu-dividers"
    blocks = None
    try:
        path = view.elves.get(key)
        if path is None:
            report.unparsable(name, f"no path supplied for {key}")
            return
        if key not in view._blocks:
            view._blocks[key] = disassembly(view.prefix, path)
        blocks = view._blocks[key]
    except (RuntimeError, FileNotFoundError) as exc:
        report.unparsable(name, str(exc))
        return

    stream = flat_instructions(blocks)
    sites = [
        (i, ops.split(",")[-1].strip())
        for i, (_, mnemonic, ops) in enumerate(stream)
        if mnemonic == "csrw" and ops.split(",")[0].strip() in ("0xbc0", "0xBC0")
    ]
    if len(sites) != 1:
        report.unparsable(name, f"expected exactly one `csrw 0xbc0`, found {len(sites)}")
        return

    index, reg = sites[0]
    value = materialised_value(stream, index, reg)
    if value is None:
        report.unparsable(
            name,
            f"could not reconstruct the constant written to CPU_RUN_CTLR from {reg}",
        )
        return

    fields = [
        ("fadd", (value >> 28) & 0xF, 128_000_000),
        ("fmul", (value >> 24) & 0xF, 96_000_000),
        ("fmac", (value >> 20) & 0xF, 76_000_000),
        ("fdiv", (value >> 16) & 0xF, 38_000_000),
    ]
    for field, raw, limit in fields:
        case = f"{name} {key}:{field}"
        rate = core_hz / (raw + 1)
        if rate <= limit:
            report.ok(
                case,
                f"field {raw} -> /{raw + 1} -> {rate / 1e6:.1f} MHz <= {limit / 1e6:.0f} MHz",
            )
        else:
            report.fail(
                case,
                f"CPU_RUN_CTLR={value:#010x}: field {raw} -> /{raw + 1} -> "
                f"{rate / 1e6:.1f} MHz at the configured {core_hz / 1e6:.0f} MHz core "
                f"clock, above the {limit / 1e6:.0f} MHz maximum (RM 4.2.3.7)",
            )


_DIRECT_JUMPS = ("jal", "j", "c.jal", "c.j", "call", "tail", "jump")


def check_calls(report: Report, view: ElfView, cases: list[tuple[str, str, str, str]]) -> None:
    """Symbol presence is not reachability. A symbol can survive --gc-sections
    because something *else* references it, so `SystemInit is in the image`
    and `main calls SystemInit` are different claims. This asserts the second.
    """
    for key, caller, callee, why in cases:
        name = f"expect-call {key}:{caller}:{callee}"
        body, err = view.body(key, caller)
        if err is not None:
            report.unparsable(name, err)
            continue
        target, target_err = view.address(key, callee)
        if target_err is not None:
            report.fail(name, f"{target_err} ({why})")
            continue
        found = False
        for _, mnemonic, ops in body:
            if f"<{callee}>" in ops:
                found = True
                break
            if mnemonic in _DIRECT_JUMPS:
                for token in re.findall(r"\b[0-9a-fA-F]{4,}\b", ops.split("<")[0]):
                    if int(token, 16) == target:
                        found = True
                        break
            if found:
                break
        if found:
            report.ok(name)
        else:
            report.fail(
                name,
                f"{caller} contains no direct call to {callee} at {target:#010x} ({why})",
            )


def section_table(prefix: str, path: str) -> list[tuple[int, int, int, str]]:
    """(Addr, Offset, Size, Type) for every section, from `readelf -SW`."""
    out = run(prefix, "readelf", "-SW", path)
    rows = []
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped.startswith("["):
            continue
        after = stripped.split("]", 1)
        if len(after) != 2:
            continue
        parts = after[1].split()
        # name type addr off size ...
        if len(parts) < 5:
            continue
        try:
            rows.append((int(parts[2], 16), int(parts[3], 16), int(parts[4], 16), parts[1]))
        except ValueError:
            continue
    return rows


def check_initializers(
    report: Report, view: ElfView, cases: list[tuple[str, str, int, str]]
) -> None:
    """Read the bytes actually backing a data symbol out of the ELF.

    Pins the SELECTED clock profile into the gate: switching
    core/system_ch32h417.c:20 to the 480 MHz profile (V3F 120 MHz) then fails
    the build, rather than silently invalidating the NSS timing contract that
    is derived from HCLK.
    """
    for key, symbol, expected, why in cases:
        name = f"expect-initializer {key}:{symbol}"
        path = view.elves.get(key)
        if path is None:
            report.unparsable(name, f"no path supplied for {key}")
            continue
        addr, err = view.address(key, symbol)
        if err is not None:
            report.fail(name, f"{err} ({why})")
            continue
        try:
            sections = section_table(view.prefix, path)
            holder = next(
                (
                    row
                    for row in sections
                    if row[3] == "PROGBITS" and row[0] <= addr < row[0] + row[2]
                ),
                None,
            )
            if holder is None:
                report.fail(
                    name,
                    f"{symbol} at {addr:#010x} is not in a PROGBITS section; it has "
                    f"no initialiser in the image ({why})",
                )
                continue
            sec_addr, sec_off, _, _ = holder
            with open(path, "rb") as handle:
                handle.seek(sec_off + (addr - sec_addr))
                raw = handle.read(4)
        except (RuntimeError, OSError) as exc:
            report.unparsable(name, str(exc))
            continue
        if len(raw) != 4:
            report.unparsable(name, f"short read for {symbol}")
            continue
        actual = int.from_bytes(raw, "little")
        if actual == expected:
            report.ok(name, str(actual))
        else:
            report.fail(name, f"{symbol} initialises to {actual}, expected {expected} ({why})")


def check_distinct(report: Report, view: ElfView, cases: list[tuple[str, str, str, str]]) -> None:
    """Both startups declare ~130 handlers `.weak` and alias them to one
    `1: j 1b` stub. A real definition must displace that stub, not merely
    resolve to it -- which a symbol-presence check cannot tell apart."""
    for key, symbol, other, why in cases:
        name = f"expect-distinct {key}:{symbol}!={other}"
        addr_a, err_a = view.address(key, symbol)
        addr_b, err_b = view.address(key, other)
        if err_a is not None or err_b is not None:
            report.unparsable(name, err_a or err_b or "")
            continue
        if addr_a != addr_b:
            report.ok(name, f"{addr_a:#010x} vs {addr_b:#010x}")
        else:
            report.fail(
                name,
                f"{symbol} and {other} share address {addr_a:#010x}; {symbol} is "
                f"still the shared weak spin stub ({why})",
            )


def check_instructions(
    report: Report, view: ElfView, cases: list[tuple[str, str, str, str]]
) -> None:
    """Assert that a linked symbol contains a required instruction."""
    for key, symbol, mnemonic, why in cases:
        name = f"expect-instruction {key}:{symbol}:{mnemonic}"
        body, err = view.body(key, symbol)
        if err is not None:
            report.unparsable(name, err)
            continue
        if any(actual == mnemonic for _, actual, _ in body):
            report.ok(name)
        else:
            report.fail(name, f"{symbol} contains no '{mnemonic}' instruction ({why})")


_MEMORY_OPERAND_RE = re.compile(
    r"^(?P<offset>-?(?:0x[0-9a-fA-F]+|\d+))\((?P<base>[a-z][a-z0-9]*)\)$"
)


def memory_store_addresses(body: list[tuple[int, str, str]]) -> set[int]:
    """Resolve absolute addresses used by simple RISC-V integer stores.

    GCC materialises the high address in a register, then puts the low signed
    displacement on the store (for example `lui a5,0x20180` followed by
    `sw a4,-256(a5)`). Match the address and displacement, not register names,
    so ordinary register-allocation changes do not break the assertion.
    """
    addresses: set[int] = set()
    for index, (_, mnemonic, operands) in enumerate(body):
        if mnemonic not in ("sb", "sh", "sw", "c.sw"):
            continue
        memory_operand = operands.rsplit(",", 1)[-1].strip()
        match = _MEMORY_OPERAND_RE.fullmatch(memory_operand)
        if match is None:
            continue
        base = materialised_value(body, index, match.group("base"), window=len(body))
        if base is None:
            continue
        addresses.add((base + int(match.group("offset"), 0)) & 0xFFFFFFFF)
    return addresses


def check_mmio_stores(
    report: Report,
    view: ElfView,
    cases: list[tuple[str, str, int, int]],
) -> None:
    """Assert that a symbol stores one exact 32-bit value to one MMIO address.

    Prefer objdump's resolved-address comment and fall back to a materialised
    page plus signed displacement. Failure to reconstruct an unrelated store is
    a non-match, not evidence that the instruction stream was unparsable.
    """
    for key, symbol, expected_address, expected_value in cases:
        name = (
            f"expect-mmio-store {key}:{symbol}:"
            f"{expected_address:#010x}:{expected_value:#010x}"
        )
        body, err = view.body(key, symbol)
        if err is not None:
            report.unparsable(name, f"could not parse disassembly: {err}")
            continue
        if not body:
            report.unparsable(name, f"could not parse disassembly: {symbol} has no instructions")
            continue

        store_instructions: list[int] = []
        target_stores: list[tuple[int, int | None, str]] = []
        matched = False

        for index, (instruction, mnemonic, operands) in enumerate(body):
            if mnemonic != "sw":
                continue
            store_instructions.append(instruction)
            resolved, resolved_err = view.resolved_address(key, instruction)
            if resolved_err is not None:
                resolved = None

            fields = [field.strip() for field in operands.split(",", 1)]
            if len(fields) != 2:
                if resolved == expected_address:
                    target_stores.append(
                        (instruction, None, f"unrecognised operands {operands!r}")
                    )
                continue
            value_register, memory_operand = fields
            memory = _MEMORY_OPERAND_RE.fullmatch(memory_operand)
            if memory is None:
                if resolved == expected_address:
                    target_stores.append(
                        (
                            instruction,
                            None,
                            f"unrecognised memory operand {memory_operand!r}",
                        )
                    )
                continue

            try:
                displacement = int(memory.group("offset"), 0)
            except ValueError:
                if resolved == expected_address:
                    target_stores.append(
                        (instruction, None, f"invalid displacement {memory.group('offset')!r}")
                    )
                continue

            try:
                page = materialised_value(
                    body, index, memory.group("base"), window=len(body)
                )
            except ValueError:
                page = None
            try:
                value = materialised_value(body, index, value_register, window=len(body))
            except ValueError:
                value = None

            address = resolved
            if address is None and page is not None:
                address = (page + displacement) & 0xFFFFFFFF
            if address != expected_address:
                continue

            source = "objdump comment" if resolved is not None else "page+displacement"
            if value == expected_value:
                address_detail = ""
                if page is not None:
                    address_detail = f" (page {page:#010x} + {displacement:#x})"
                report.ok(
                    name,
                    f"sw at {instruction:#010x} via {source}{address_detail}",
                )
                matched = True
                break
            if value is None:
                reason = "stored value could not be reconstructed"
            else:
                reason = f"value {value:#010x}"
            target_stores.append((instruction, value, reason))

        if matched:
            continue

        if target_stores:
            found = "; ".join(
                (
                    f"store to {expected_address:#010x} at {instruction:#010x} "
                    f"has value {value:#010x}, expected {expected_value:#010x}"
                    if value is not None
                    else f"store to {expected_address:#010x} at {instruction:#010x}: "
                    f"{reason}; expected {expected_value:#010x}"
                )
                for instruction, value, reason in target_stores
            )
            report.fail(name, f"assertion FAILED: {found}")
            continue

        if store_instructions:
            observed = ", ".join(f"{instruction:#010x}" for instruction in store_instructions)
            observed = f"stores found at {observed}"
        else:
            observed = "no stores found"
        report.fail(
            name,
            f"assertion FAILED: store to {expected_address:#010x} is absent; "
            f"expected value {expected_value:#010x}; {observed}",
        )


def check_shared_witness_address(report: Report, view: ElfView) -> None:
    """Assert both HardFault handlers publish at the same absolute address."""
    name = "shared-witness-address"
    observed: dict[str, set[int]] = {}
    for key in (V3F, V5F):
        body, err = view.body(key, "HardFault_Handler")
        if err is not None:
            report.unparsable(name, f"{key}: {err}")
            return
        observed[key] = memory_store_addresses(body)

    if all(SHARED_DIAG_WITNESS_ADDR in observed[key] for key in (V3F, V5F)):
        report.ok(
            name,
            f"{V3F} == {V5F} == {SHARED_DIAG_WITNESS_ADDR:#010x}",
        )
        return

    details = ", ".join(
        f"{key}={','.join(f'{address:#010x}' for address in sorted(addresses)) or 'no stores'}"
        for key, addresses in observed.items()
    )
    report.fail(
        name,
        f"both HardFault handlers must store the witness at "
        f"{SHARED_DIAG_WITNESS_ADDR:#010x}; observed {details}",
    )


# ---------------------------------------------------------------------------
# Self-test
#
# A gate that has never been observed to fail is not a gate. These cases drive
# the checker with synthetic inputs so its *discrimination* is proven, not just
# its behaviour on today's happy-path tree. They deliberately do not depend on
# the live build, so they cannot silently start passing for the wrong reason.
# ---------------------------------------------------------------------------
def _self_test() -> int:
    failures = []

    def expect(condition: bool, label: str) -> None:
        print(f"  {'PASS' if condition else 'FAIL'}  self-test: {label}")
        if not condition:
            failures.append(label)

    def texts(report: Report) -> str:
        return "\n".join(report.failures)

    # -- readelf parsing: NOBITS-only segments must be excluded -------------
    sample = (
        "  LOAD           0x001000 0x00000000 0x00000000 0x00008 0x00008 R E 0x1000\n"
        "  LOAD           0x00335c 0x0000035c 0x0000035c 0x00084 0x00084 R E 0x1000\n"
        "  LOAD           0x000800 0x20177800 0x20177800 0x00000 0x00800 RW  0x1000\n"
    )
    segs = parse_load_segments(sample)
    expect(
        segs == [(0x0, 0x8), (0x35C, 0x84)], "parse_load_segments drops the FileSiz=0 stack segment"
    )

    # -- objdump parsing and the Rung-3 assertions -------------------------
    dis = (
        "build/v5f.elf:     file format elf32-littleriscv\n"
        "\n"
        "Disassembly of section .text:\n"
        "\n"
        "200a0256 <ADC1_2_IRQHandler>:\n"
        "200a0256:\ta001                \tj\t200a0256 <ADC1_2_IRQHandler>\n"
        "\n"
        "200a0300 <TIM3_IRQHandler>:\n"
        "200a0300:\t1141                \taddi\tsp,sp,-16\n"
        "200a0302:\t30200073            \tmret\n"
        "\n"
        "200a0400 <plain_ret_handler>:\n"
        "200a0400:\t8082                \tret\n"
    )
    blocks = parse_disassembly(dis)
    expect(
        sorted(blocks) == [0x200A0256, 0x200A0300, 0x200A0400]
        and blocks[0x200A0300][-1][1] == "mret"
        and len(blocks[0x200A0300]) == 2,
        "parse_disassembly keys blocks by address and reads the last mnemonic",
    )
    resolved_parser = globals().get("parse_resolved_addresses")
    expect(resolved_parser is not None, "objdump resolved-address parser is available")
    if resolved_parser is not None:
        resolved = resolved_parser(
            "20100268:\t72e7a223          \tsw\ts1,1828(t2) "
            "# e000e724 <_eusrstack+0xbfe96724>\n"
        )
        expect(
            resolved == {0x20100268: 0xE000E724},
            "objdump resolved-address parser reads the MMIO comment",
        )

    # -- command-line MMIO-store assertion ---------------------------------
    class _MmioView:
        def __init__(self, body, resolved=None):
            self._body = body
            self._resolved = resolved or {}

        def body(self, _key, _symbol):
            return self._body, None

        def resolved_address(self, _key, instruction):
            return self._resolved.get(instruction), None

    mmio_cases = [(V3F, "main", 0xE000E724, 0x00010000)]
    mmio_check = globals().get("check_mmio_stores")
    expect(mmio_check is not None, "expect-mmio-store checker is available")
    if mmio_check is not None:
        actual_store = [
            (0x20100262, "lui", "t2,0xe000e"),
            (0x20100266, "lui", "s1,0x10"),
            (0x20100268, "sw", "s1,1828(t2)"),
        ]
        report = Report()
        mmio_check(
            report,
            _MmioView(actual_store, {0x20100268: 0xE000E724}),
            mmio_cases,
        )
        expect(
            not report.failures and not report.parse_errors,
            "expect-mmio-store accepts the wake store without depending on register names",
        )

        report = Report()
        mmio_check(report, _MmioView(actual_store), mmio_cases)
        expect(
            not report.failures and not report.parse_errors,
            "expect-mmio-store computes page plus displacement when objdump omits its comment",
        )

        report = Report()
        wrong_value_store = [
            (0x20100262, "lui", "t2,0xe000e"),
            (0x20100266, "li", "s1,7"),
            (0x20100268, "sw", "s1,1828(t2)"),
        ]
        mmio_check(
            report,
            _MmioView(wrong_value_store, {0x20100268: 0xE000E724}),
            mmio_cases,
        )
        expect(
            any(
                "store to 0xe000e724 at 0x20100268 has value 0x00000007" in item
                and "expected 0x00010000" in item
                for item in report.failures
            )
            and not report.parse_errors,
            "expect-mmio-store reports the found value when the target store is wrong",
        )

        report = Report()
        missing_store_disassembly = (
            "20100258 <main>:\n"
            "20100258:\t1141                \taddi\tsp,sp,-16\n"
            "2010025a:\tc606                \tsw\tra,12(sp)\n"
            "2010025c:\te000e7b7            \tlui\ta5,0xe000e\n"
            "20100260:\t7047a703            \tlw\ta4,1796(a5)\n"
            "20100264:\t9b65                \tandi\ta4,a4,-7\n"
            "20100266:\t0007071b            \tsext.w\ta4,a4\n"
            "2010026a:\t70e7a223            \tsw\ta4,1796(a5) "
            "# e000e704 <_eusrstack+0xbfe96704>\n"
            "2010026e:\t10500073            \twfi\n"
        )
        mmio_check(
            report,
            _MmioView(
                parse_disassembly(missing_store_disassembly)[0x20100258],
                {0x2010026A: 0xE000E704},
            ),
            mmio_cases,
        )
        expect(
            any(
                "expect-mmio-store v3f.elf:main:0xe000e724:0x00010000" in item
                and "store to 0xe000e724 is absent" in item
                and "0x2010025a" in item
                and "0x2010026a" in item
                for item in report.failures
            )
            and not report.parse_errors,
            "expect-mmio-store classifies unrelated stores as a missing-store failure",
        )

        report = Report()
        mmio_check(
            report,
            _MmioView([(0x20100268, "sw", "<new objdump operand shape>")]),
            mmio_cases,
        )
        expect(
            report.failures
            and not report.parse_errors
            and "0x20100268" in texts(report),
            "expect-mmio-store skips one unrecognised unrelated store",
        )

        report = Report()
        mmio_check(report, _MmioView([]), mmio_cases)
        expect(
            not report.failures
            and any(
                "expect-mmio-store v3f.elf:main:0xe000e724:0x00010000" in item
                for item in report.parse_errors
            ),
            "expect-mmio-store reports zero parsed instructions as unparsable",
        )

    class _FakeView:
        """Stands in for ElfView so the Rung-3 logic is exercised without a
        toolchain. Three handler names share the spin-stub address, exactly as
        both startups alias ~130 of them."""

        addrs = {
            "TIM3_IRQHandler": 0x200A0300,
            "WWDG_IRQHandler": 0x200A0256,
            "stub_handler": 0x200A0256,
            "plain_ret_handler": 0x200A0400,
            "HardFault_Handler": 0x200A0500,
        }

        bodies = {
            "HardFault_Handler": [
                (0x200A0500, "lui", "a5,0x20180"),
                (0x200A0504, "fence", "iorw,iorw"),
                (0x200A0508, "sw", "a4,-256(a5)"),
                (0x200A050C, "j", "200a050c <HardFault_Handler+0xc>"),
            ]
        }

        def address(self, _key, symbol):
            if symbol not in self.addrs:
                return None, f"symbol '{symbol}' is not defined"
            return self.addrs[symbol], None

        def body(self, _key, symbol):
            if symbol in self.bodies:
                return self.bodies[symbol], None
            addr, err = self.address(_key, symbol)
            if err is not None:
                return None, err
            return blocks[addr], None

    fake = _FakeView()

    report = Report()
    check_mret(report, fake, [(V5F, "TIM3_IRQHandler", "real ISR")])
    expect(not report.failures, "expect-mret passes on a handler ending in mret")

    report = Report()
    check_mret(report, fake, [(V5F, "plain_ret_handler", "attribute was ignored")])
    expect(
        "expect-mret v5f.elf:plain_ret_handler" in texts(report)
        and "is 'ret', expected 'mret'" in texts(report),
        "expect-mret fails by name on a handler compiled with a plain ret",
    )

    report = Report()
    check_mret(report, fake, [(V5F, "stub_handler", "still the weak stub")])
    expect(
        "is 'j', expected 'mret'" in texts(report),
        "expect-mret fails on a handler still aliased to the spin stub",
    )

    report = Report()
    check_distinct(report, fake, [(V5F, "TIM3_IRQHandler", "WWDG_IRQHandler", "must displace")])
    expect(not report.failures, "expect-distinct passes on a real definition")

    report = Report()
    check_distinct(report, fake, [(V5F, "stub_handler", "WWDG_IRQHandler", "must displace")])
    expect(
        "share address 0x200a0256" in texts(report),
        "expect-distinct fails when the handler is still the shared stub",
    )

    report = Report()
    check_instructions(
        report,
        fake,
        [(V5F, "HardFault_Handler", "fence", "shared-SRAM release ordering")],
    )
    expect(
        not report.failures,
        "expect-instruction passes when HardFault_Handler contains a data fence",
    )

    report = Report()
    check_instructions(
        report,
        fake,
        [(V5F, "HardFault_Handler", "mret", "negative control")],
    )
    expect(
        "expect-instruction v5f.elf:HardFault_Handler:mret" in texts(report),
        "expect-instruction fails by name when the mnemonic is absent",
    )

    report = Report()
    check_shared_witness_address(report, fake)
    expect(
        not report.failures,
        "shared-witness-address accepts matching V3F/V5F absolute stores",
    )

    class _MismatchedView(_FakeView):
        def body(self, key, symbol):
            if key == V5F and symbol == "HardFault_Handler":
                return [
                    (0x200A0500, "lui", "a5,0x20180"),
                    (0x200A0504, "sw", "a4,-252(a5)"),
                ], None
            return super().body(key, symbol)

    report = Report()
    check_shared_witness_address(report, _MismatchedView())
    expect(
        "shared-witness-address" in texts(report)
        and "v5f.elf=0x2017ff04" in texts(report),
        "shared-witness-address rejects images that disagree on the absolute store",
    )

    # -- merged image ceiling ----------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        big = os.path.join(tmp, "oversized.bin")
        with open(big, "wb") as handle:
            handle.write(b"\x00" * (0x30000 + 1))
        report = Report()
        check_geometry(report, "unused", {}, big, 0x10000, 0x10000, 0x20000, 0x30000)
        expect(
            "exceeds flash limit 0x30000" in texts(report),
            "oversized merged image fails with the ceiling message",
        )

        small = os.path.join(tmp, "ok.bin")
        with open(small, "wb") as handle:
            handle.write(b"\x00" * 1024)
        report = Report()
        check_geometry(report, "unused", {}, small, 0x10000, 0x10000, 0x20000, 0x30000)
        expect(not report.failures, "an in-bounds merged image passes")

    # -- symbol survival, driven by injected nm output ---------------------
    real_defined = globals()["defined_symbols"]
    real_segments = globals()["load_segments"]
    real_sections = globals()["section_table"]
    try:
        globals()["defined_symbols"] = lambda _p, path: {
            "build/v3f.elf": {"main", "handle_reset"},
            "build/v3f/main.o": {"main"},
            "build/v3f/system.o": {"SystemInit", "SetSysClock"},
        }[path]
        elves = {V3F: "build/v3f.elf"}

        report = Report()
        check_symbols(report, "unused", elves, [(V3F, "SystemInit", "clock task")])
        expect(
            "SystemInit" in texts(report) and "require-symbol v3f.elf:SystemInit" in texts(report),
            "a missing required symbol fails by name",
        )

        report = Report()
        check_symbols(report, "unused", elves, [(V3F, "main", "entry")])
        expect(not report.failures, "a present required symbol passes")

        objects = [(V3F, "build/v3f/main.o"), (V3F, "build/v3f/system.o")]
        report = Report()
        check_objects(report, "unused", elves, objects, {})
        expect(
            "object-contributes v3f.elf:system.o" in texts(report)
            and "contributes 0 of 2 symbols" in texts(report),
            "a fully discarded object fails by name",
        )

        report = Report()
        check_objects(report, "unused", elves, objects, {(V3F, "system.o"): "known dead"})
        expect(not report.failures, "an exempted discarded object passes")

        report = Report()
        check_objects(report, "unused", elves, objects, {(V3F, "main.o"): "known dead"})
        expect(
            "stale-exemption v3f.elf:main.o" in texts(report),
            "an exemption that no longer applies fails",
        )

        report = Report()
        check_objects(report, "unused", elves, [], {(V3F, "gone.o"): "vanished"})
        expect(
            "stale-exemption v3f.elf:gone.o" in texts(report),
            "an exemption for an object no longer built fails",
        )

        # -- geometry: a stray low LOAD segment would silently rebase -------
        globals()["load_segments"] = lambda _p, _path: [(0x00000004, 0x10), (0x400, 0x20)]
        report = Report()
        check_geometry(
            report, "unused", {V3F: "build/v3f.elf"}, None, 0x10000, 0x10000, 0x20000, 0x30000
        )
        expect("load-base v3f.elf" in texts(report), "a wrong lowest LOAD PhysAddr fails by name")

        globals()["load_segments"] = lambda _p, _path: [(0x0, 0x10), (0x20000, 0x20)]
        report = Report()
        check_geometry(
            report, "unused", {V3F: "build/v3f.elf"}, None, 0x10000, 0x10000, 0x20000, 0x30000
        )
        expect(
            "region-fit v3f.elf" in texts(report),
            "an image overrunning the V5F offset fails by name",
        )

        # -- RAM geometry: NOBITS content plus stack must fit ---------------
        globals()["section_table"] = lambda _p, path: {
            "build/v3f.elf": [
                (0x1000, 0x1000, 0x20, "PROGBITS"),
                (0x1020, 0x1020, 0x20, "NOBITS"),
                (0x1F00, 0x1800, 0x100, "NOBITS"),
            ],
            "build/v5f.elf": [
                (0x3000, 0x2000, 0x40, "PROGBITS"),
                (0x3040, 0x2040, 0x40, "NOBITS"),
                (0x3F00, 0x2800, 0x100, "NOBITS"),
            ],
        }[path]
        regions = {
            V3F: (0x1000, 0x2000, 0x100),
            V5F: (0x3000, 0x4000, 0x100),
        }
        report = Report()
        check_ram_geometry(
            report,
            "unused",
            {V3F: "build/v3f.elf", V5F: "build/v5f.elf"},
            regions,
            (0x2000, 0x2200),
        )
        expect(
            not report.failures
            and "ram-fit v3f.elf: headroom 3776 B" in "\n".join(report.passes)
            and "ram-fit v5f.elf: headroom 3712 B" in "\n".join(report.passes),
            "RAM geometry includes NOBITS content, adds stack once, and reports headroom",
        )

        globals()["section_table"] = lambda _p, path: {
            "build/v3f.elf": [
                (0x1000, 0x1000, 0x20, "PROGBITS"),
                (0x1F80, 0x1020, 0x40, "NOBITS"),
                (0x1F00, 0x1800, 0x100, "NOBITS"),
            ],
            "build/v5f.elf": [
                (0x3000, 0x2000, 0x40, "PROGBITS"),
                (0x3F80, 0x2040, 0x40, "NOBITS"),
                (0x3F00, 0x2800, 0x100, "NOBITS"),
            ],
        }[path]
        report = Report()
        check_ram_geometry(
            report,
            "unused",
            {V3F: "build/v3f.elf", V5F: "build/v5f.elf"},
            regions,
            (0x2000, 0x2200),
        )
        expect(
            "ram-fit v3f.elf" in texts(report)
            and "ram-fit v5f.elf" in texts(report)
            and "ram-shared-disjoint v3f.elf" in texts(report),
            "synthetic over-large NOBITS images fail every RAM and shared-window assertion",
        )
    finally:
        globals()["defined_symbols"] = real_defined
        globals()["load_segments"] = real_segments
        globals()["section_table"] = real_sections

    print(f"check_images --self-test: {len(failures)} failed")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def split_pair(value: str, what: str) -> tuple[str, str]:
    key, _, rest = value.partition(":")
    if key not in ELF_KEYS or not rest:
        raise argparse.ArgumentTypeError(
            f"{what} must be '<{'|'.join(ELF_KEYS)}>:NAME', got {value!r}"
        )
    return key, rest


def split_mmio_store(value: str) -> tuple[str, str, int, int]:
    parts = value.split(":")
    if len(parts) != 4 or parts[0] not in ELF_KEYS or not parts[1]:
        raise argparse.ArgumentTypeError(
            f"--expect-mmio-store must be "
            f"'<{'|'.join(ELF_KEYS)}>:SYMBOL:ADDR:VALUE', got {value!r}"
        )
    try:
        address = int(parts[2], 0)
        stored_value = int(parts[3], 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--expect-mmio-store ADDR and VALUE must be integers, got {value!r}"
        ) from exc
    if not (0 <= address <= 0xFFFFFFFF and 0 <= stored_value <= 0xFFFFFFFF):
        raise argparse.ArgumentTypeError(
            f"--expect-mmio-store ADDR and VALUE must fit in 32 bits, got {value!r}"
        )
    return parts[0], parts[1], address, stored_value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prefix", help="toolchain prefix, e.g. .../riscv-none-elf")
    parser.add_argument("--v3f", help="path to v3f.elf")
    parser.add_argument("--v5f", help="path to v5f.elf")
    parser.add_argument("--merged", help="path to the merged flash image")
    parser.add_argument("--v5f-offset", default="0x10000")
    parser.add_argument(
        "--v5f-core-hz",
        default="400000000",
        help="V5F core clock of the CONFIGURED profile in core/system_ch32h417.c",
    )
    parser.add_argument(
        "--v3f-region", default="0x10000", help="V3F FLASH LENGTH from link_v3f.ld (64K)"
    )
    parser.add_argument(
        "--v5f-region", default="0x20000", help="V5F FLASH LENGTH from link_v5f.ld (128K)"
    )
    parser.add_argument("--flash-limit", default="0x30000", help="ceiling: v5f-offset + v5f-region")
    parser.add_argument("--v3f-ram-start", help="V3F RAM ORIGIN from link_v3f.ld")
    parser.add_argument("--v3f-ram-end", help="V3F RAM ORIGIN + LENGTH from link_v3f.ld")
    parser.add_argument("--v5f-ram-start", help="V5F RAM ORIGIN from link_v5f.ld")
    parser.add_argument("--v5f-ram-end", help="V5F RAM ORIGIN + LENGTH from link_v5f.ld")
    parser.add_argument("--stack-size", help="__stack_size from both linker scripts")
    parser.add_argument("--ram-shared-start", help="RAM_SHARED ORIGIN from link_v3f.ld")
    parser.add_argument("--ram-shared-end", help="RAM_SHARED ORIGIN + LENGTH from link_v3f.ld")
    parser.add_argument(
        "--object",
        action="append",
        default=[],
        metavar="ELF:PATH",
        help="a Makefile object-list entry; repeatable",
    )
    parser.add_argument(
        "--require-symbol",
        action="append",
        default=[],
        metavar="ELF:NAME",
        help="extra symbol requirement",
    )
    parser.add_argument(
        "--expect-discarded",
        action="append",
        default=[],
        metavar="ELF:OBJECT",
        help="extra discard exemption",
    )
    parser.add_argument(
        "--expect-mmio-store",
        action="append",
        default=[],
        metavar="ELF:SYMBOL:ADDR:VALUE",
        help="require SYMBOL to store VALUE to the MMIO address ADDR; repeatable",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    if not args.prefix:
        parser.error("--prefix is required unless --self-test is given")
    ram_args = (
        args.v3f_ram_start,
        args.v3f_ram_end,
        args.v5f_ram_start,
        args.v5f_ram_end,
        args.stack_size,
        args.ram_shared_start,
        args.ram_shared_end,
    )
    if any(value is None for value in ram_args):
        parser.error(
            "--v3f-ram-start, --v3f-ram-end, --v5f-ram-start, "
            "--v5f-ram-end, --stack-size, --ram-shared-start, and "
            "--ram-shared-end are required unless --self-test is given"
        )

    version = toolchain_version(args.prefix)
    if version != EXPECTED_TOOLCHAIN:
        print(
            f"check_images: NOTE toolchain is {version}, this gate was written "
            f"against {EXPECTED_TOOLCHAIN}; disassembly matchers may need review"
        )

    elves = {}
    if args.v3f:
        elves[V3F] = args.v3f
    if args.v5f:
        elves[V5F] = args.v5f

    required = list(REQUIRED_SYMBOLS)
    for item in args.require_symbol:
        key, symbol = split_pair(item, "--require-symbol")
        required.append((key, symbol, "requested on the command line"))

    exemptions = dict(DISCARD_EXEMPTIONS)
    for item in args.expect_discarded:
        key, obj = split_pair(item, "--expect-discarded")
        exemptions[(key, obj)] = "requested on the command line"

    objects = [split_pair(item, "--object") for item in args.object]
    mmio_stores = [split_mmio_store(item) for item in args.expect_mmio_store]
    # Only police exemptions for ELFs whose object list was actually supplied.
    supplied = {key for key, _ in objects}
    exemptions = {k: v for k, v in exemptions.items() if k[0] in supplied}

    report = Report()
    check_symbols(report, args.prefix, elves, required)
    check_objects(report, args.prefix, elves, objects, exemptions)
    view = ElfView(args.prefix, elves)
    check_mret(report, view, MRET_HANDLERS)
    check_distinct(report, view, DISTINCT_SYMBOLS)
    check_instructions(report, view, EXPECTED_INSTRUCTIONS)
    check_shared_witness_address(report, view)
    check_mmio_stores(report, view, mmio_stores)
    check_calls(report, view, EXPECTED_CALLS)
    check_initializers(report, view, EXPECTED_INITIALIZERS)
    if V5F in elves:
        check_fpu_dividers(report, view, V5F, int(args.v5f_core_hz, 0))
    check_geometry(
        report,
        args.prefix,
        elves,
        args.merged,
        int(args.v5f_offset, 0),
        int(args.v3f_region, 0),
        int(args.v5f_region, 0),
        int(args.flash_limit, 0),
    )
    stack_size = int(args.stack_size, 0)
    check_ram_geometry(
        report,
        args.prefix,
        elves,
        {
            V3F: (int(args.v3f_ram_start, 0), int(args.v3f_ram_end, 0), stack_size),
            V5F: (int(args.v5f_ram_start, 0), int(args.v5f_ram_end, 0), stack_size),
        },
        (int(args.ram_shared_start, 0), int(args.ram_shared_end, 0)),
    )
    return report.emit()


if __name__ == "__main__":
    sys.exit(main())
