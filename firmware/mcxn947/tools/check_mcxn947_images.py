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
                          region, and requires core1's lowest LOAD PhysAddr to
                          equal CORE1_OFFSET before bounding it to 1 MiB.

  4   RAM geometry      -- `readelf -SW` plus `nm`. Bounds allocated content
                          against CPU0's `m_data`, checks the linker script's
                          own stack and heap symbols against the sizes the
                          Makefile declares. The shared window may contain only
                          declared symbols, at the same address in both ELFs;
                          ordinary .data/.bss remain disjoint.

  5   ownership         -- reject strong symbols for the other core's serial,
                          DMA, timer, display, power, glitch and tamper blocks.
                          Weak vector stubs from the byte-exact startup are not
                          ownership and are allowed. Disassemble CPU1's owned
                          SystemInit and allow only CPACR and VTOR stores.

  6   CPU1 release      -- core1_release is strong and its Thumb-2 body stores
                          CPBOOT then CPUCTRL twice, with CORE1_OFFSET and the
                          0xC0C4 key among its constants -- pool words and
                          Thumb-2 immediates alike, since which one GCC picks
                          is a codegen detail.

  7   merged bounds     -- core0 bytes end before CORE1_OFFSET and the merged
                          binary ends at or below the device flash limit.

Usage (see firmware/mcxn947/Makefile, target `check`):

    python3 tools/check_mcxn947_images.py --cross-compile arm-none-eabi- \
        --core0 build/core0.elf --core1 build/core1.elf \
        --core0-bin build/hurra-mcxn947-core0.bin \
        --core1-bin build/hurra-mcxn947-core1.bin \
        --merged-bin build/hurra-mcxn947.bin \
        --core0-flash-start 0x00000000 --core0-flash-end 0x000C0000 \
        --core1-offset 0x000C0000 --flash-limit 0x00100000 \
        --core0-ram-start 0x20000000 --core0-ram-end 0x2004C000 \
        --core1-ram-start 0x2004E000 --core1-ram-end 0x20068000 \
        --ram-shared-start 0x2004C000 --ram-shared-end 0x2004E000 \
        --stack-size 0x0800 --heap-size 0x0400 --shared-symbol g_shared_window \
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

CORE0 = "core0.elf"
CORE1 = "core1.elf"

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


def parse_nm_posix_sized(text: str) -> dict[str, tuple[str, int, int]]:
    """Parse POSIX nm output with optional SIZE into name -> (type, address, size)."""
    symbols: dict[str, tuple[str, int, int]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or len(parts[1]) != 1:
            continue
        try:
            size = int(parts[3], 16) if len(parts) >= 4 else 0
            symbols[parts[0]] = (parts[1], int(parts[2], 16), size)
        except ValueError:
            continue
    return symbols


def elf_symbols_sized(cross: str, path: str) -> dict[str, tuple[str, int, int]]:
    return parse_nm_posix_sized(
        run(cross, "nm", "--defined-only", "--print-size", "--format=posix", path)
    )


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


_BLOCK_RE = re.compile(r"^\s*([0-9a-fA-F]+)\s+<(.+)>:$")
_INSN_RE = re.compile(r"^\s*([0-9a-fA-F]+):\s+[0-9a-fA-F ]+\t(\S+)\s*(.*)$")
_ARM_RESOLVED_RE = re.compile(r"[;@]\s*\((?:0x)?(?P<address>[0-9a-fA-F]+)(?:\s|$)")
_ARM_STORE_RE = re.compile(
    r"^(?P<value>r(?:1[0-5]|[0-9])),\s*"
    r"\[(?P<base>r(?:1[0-5]|[0-9]))"
    r"(?:,\s*#(?P<offset>-?(?:0x[0-9a-fA-F]+|\d+)))?\]!?$"
)


def parse_disassembly(text: str) -> dict[int, list[tuple[int, str, str]]]:
    """Parse ARM objdump -d output into address-keyed symbol bodies."""
    blocks: dict[int, list[tuple[int, str, str]]] = {}
    current: list[tuple[int, str, str]] | None = None
    for line in text.splitlines():
        head = _BLOCK_RE.match(line)
        if head:
            current = []
            blocks[int(head.group(1), 16)] = current
            continue
        instruction = _INSN_RE.match(line)
        if instruction is not None and current is not None:
            operands = re.split(r"[;@]", instruction.group(3), maxsplit=1)[0].strip()
            current.append(
                (int(instruction.group(1), 16), instruction.group(2), operands)
            )
    return blocks


def parse_resolved_addresses(text: str) -> dict[int, int]:
    """Instruction address -> literal-pool address from objdump's comment."""
    resolved: dict[int, int] = {}
    for line in text.splitlines():
        instruction = _INSN_RE.match(line)
        if instruction is None:
            continue
        comment = _ARM_RESOLVED_RE.search(instruction.group(3))
        if comment is not None:
            resolved[int(instruction.group(1), 16)] = int(comment.group("address"), 16)
    return resolved


def analyze_core1_release(
    body: list[tuple[int, str, str]], resolved: dict[int, int]
) -> tuple[list[int], set[int]]:
    """Resolve simple Thumb-2 stores in core1_release, and the constants it uses.

    A store whose base cannot be reconstructed is deliberately a non-match,
    not a parse error. That is the same distinction the CH32 checker makes.

    "Constants it uses" is deliberately NOT "its literal pool". A Thumb-2
    modified immediate can carry an 8-bit value in any even rotation, so GCC
    materialises 0x50000000 and 0x000C0000 with `mov.w` and never emits a pool
    word for either -- measured, not assumed:

        mov.w r3, #1342177280   @ 0x50000000
        mov.w r2, #786432       @ 0xc0000
        str.w r2, [r3, #2052]   @ 0x804

    An earlier version of this function returned `set(pool.values())` alone and
    failed CORE1_OFFSET on a correct image. Whether a constant reaches the
    instruction stream as a pool word or as an immediate is a codegen detail
    the assertion must not depend on.
    """
    pool: dict[int, int] = {}
    for address, mnemonic, operands in body:
        if mnemonic == ".word":
            try:
                pool[address] = int(operands, 0)
            except ValueError:
                continue

    immediates: set[int] = set()
    for _, mnemonic, operands in body:
        if mnemonic not in ("mov", "mov.w", "movs", "movw", "mvn", "mvn.w"):
            continue
        fields = [field.strip() for field in operands.split(",", 1)]
        if len(fields) != 2 or not fields[1].startswith("#"):
            continue
        try:
            value = int(fields[1].removeprefix("#"), 0)
        except ValueError:
            continue
        immediates.add((~value if mnemonic.startswith("mvn") else value) & 0xFFFFFFFF)

    stores: list[int] = []
    for index, (_, mnemonic, operands) in enumerate(body):
        if not mnemonic.startswith("str"):
            continue
        memory = _ARM_STORE_RE.fullmatch(operands)
        if memory is None:
            continue
        base_register = memory.group("base")
        base = None
        for previous in range(index - 1, -1, -1):
            previous_address, previous_mnemonic, previous_operands = body[previous]
            fields = [field.strip() for field in previous_operands.split(",", 1)]
            if not fields or fields[0] != base_register:
                continue
            if previous_mnemonic in ("ldr", "ldr.w"):
                literal_address = resolved.get(previous_address)
                if literal_address in pool:
                    base = pool[literal_address]
            elif previous_mnemonic in ("mov", "mov.w", "movs") and len(fields) == 2:
                try:
                    base = int(fields[1].removeprefix("#"), 0)
                except ValueError:
                    base = None
            # The closest write owns the register. An unrecognised write is a
            # non-match; never continue backward to a stale value.
            break
        if base is None:
            continue
        displacement = int(memory.group("offset") or "0", 0)
        stores.append((base + displacement) & 0xFFFFFFFF)

    return stores, set(pool.values()) | immediates


def release_transaction_issues(
    body: list[tuple[int, str, str]],
    resolved: dict[int, int],
    core1_offset: int = 0x000C0000,
) -> list[str]:
    """Check the one-snapshot CPUCTRL transaction and its data dependency."""
    pool: dict[int, int] = {}
    for address, mnemonic, operands in body:
        if mnemonic == ".word":
            try:
                pool[address] = int(operands, 0)
            except ValueError:
                continue

    def memory_address(index: int, base_register: str, displacement: int) -> int | None:
        for previous in range(index - 1, -1, -1):
            previous_address, previous_mnemonic, previous_operands = body[previous]
            fields = [field.strip() for field in previous_operands.split(",", 1)]
            if not fields or fields[0] != base_register:
                continue
            base = None
            if previous_mnemonic in ("ldr", "ldr.w"):
                literal_address = resolved.get(previous_address)
                if literal_address in pool:
                    base = pool[literal_address]
            elif previous_mnemonic in ("mov", "mov.w", "movs") and len(fields) == 2:
                try:
                    base = int(fields[1].removeprefix("#"), 0)
                except ValueError:
                    pass
            if base is not None:
                return (base + displacement) & 0xFFFFFFFF
            return None
        return None

    cpuctrl = 0x50000800
    cpboot = 0x50000804
    cpuctrl_loads: list[int] = []
    snapshot = ("snapshot",)
    values: dict[str, tuple[object, ...]] = {}
    cpboot_store_values: list[tuple[object, ...] | None] = []
    cpuctrl_store_values: list[tuple[object, ...] | None] = []

    def constant(value: int) -> tuple[object, ...]:
        return ("constant", value)

    def combine(
        operator: str, left: tuple[object, ...], right: tuple[object, ...]
    ) -> tuple[object, ...]:
        operands = sorted((left, right), key=repr)
        return (operator, operands[0], operands[1])

    for index, (instruction_address, mnemonic, operands) in enumerate(body):
        memory = _ARM_STORE_RE.fullmatch(operands)
        if mnemonic.startswith("ldr"):
            if memory is None:
                registers = re.findall(r"\br(?:1[0-5]|[0-9])\b", operands)
                literal_address = resolved.get(instruction_address)
                if registers and literal_address in pool:
                    values[registers[0]] = constant(pool[literal_address])
                elif registers:
                    values.pop(registers[0], None)
                continue
            register = memory.group("value")
            address = memory_address(
                index,
                memory.group("base"),
                int(memory.group("offset") or "0", 0),
            )
            if address == cpuctrl:
                cpuctrl_loads.append(index)
                values[register] = snapshot
            else:
                values.pop(register, None)
            continue

        if mnemonic.startswith("str") and memory is not None:
            register = memory.group("value")
            address = memory_address(
                index,
                memory.group("base"),
                int(memory.group("offset") or "0", 0),
            )
            if address == cpuctrl:
                cpuctrl_store_values.append(values.get(register))
            elif address == cpboot:
                cpboot_store_values.append(values.get(register))
            continue

        registers = re.findall(r"\br(?:1[0-5]|[0-9])\b", operands)
        if not registers:
            continue
        destination = registers[0]
        if mnemonic.startswith(("orr", "and")):
            # Thumb's two-operand forms read the old destination too.
            sources = registers[1:]
            if len(registers) == 2:
                sources = [destination, registers[1]]
            if len(sources) == 2 and all(source in values for source in sources):
                operator = "or" if mnemonic.startswith("orr") else "and"
                values[destination] = combine(
                    operator, values[sources[0]], values[sources[1]]
                )
            else:
                values.pop(destination, None)
        elif mnemonic.startswith("mov"):
            fields = [field.strip() for field in operands.split(",", 1)]
            immediate = None
            if len(fields) == 2 and fields[1].startswith("#"):
                try:
                    immediate = int(fields[1].removeprefix("#"), 0) & 0xFFFFFFFF
                except ValueError:
                    immediate = None
            if immediate is not None:
                # A Thumb-2 modified immediate, not a pool word. Both 0x50000000
                # and 0x000C0000 arrive this way at -Os; see
                # analyze_core1_release's docstring.
                values[destination] = constant(immediate)
            elif len(registers) == 2 and registers[1] in values:
                values[destination] = values[registers[1]]
            else:
                values.pop(destination, None)
        elif not mnemonic.startswith(("cmp", "tst", "b")):
            # Unknown writes cannot be credited with a known transformation.
            values.pop(destination, None)

    issues = []
    if len(cpuctrl_loads) != 1:
        issues.append(
            f"CPUCTRL is read {len(cpuctrl_loads)} times, expected exactly one"
        )
    if cpboot_store_values != [constant(core1_offset)]:
        issues.append(
            f"CPBOOT store does not use CORE1_OFFSET {core1_offset:#010x}"
        )
    expected_store_values = [
        combine("or", snapshot, constant(0xC0C40028)),
        combine(
            "or",
            constant(0xC0C40008),
            combine("and", snapshot, constant(0x3F3BFFD7)),
        ),
    ]
    if cpuctrl_store_values != expected_store_values:
        issues.append(
            "CPUCTRL stores do not use the exact assert/release transformations "
            "of the one captured read"
        )

    literals = set(pool.values())
    required = {
        0xC0C40028,  # key | clock | reset
        0xC0C40008,  # key | clock, reset released
        0x3F3BFFD7,  # ~(key | clock | reset), applied to the captured read
    }
    missing = sorted(required - literals)
    if missing:
        issues.append(
            "missing exact CPUCTRL transformation literal(s): "
            + ", ".join(f"{value:#010x}" for value in missing)
        )
    return issues


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
    # survived into the link. The cross-image check below owns its contents;
    # this local check keeps ordinary RAM sections out.
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
            f"{shared_start:#010x}..{shared_end:#010x} carries no ordinary {key} content",
        )
    else:
        report.fail(
            disjoint_name,
            f"{[(r[0], hex(r[2]), r[3]) for r in intruders]} allocate into the "
            f"shared window {shared_start:#010x}..{shared_end:#010x}",
        )


# ---------------------------------------------------------------------------
# Rung 4 -- shared-window contents and cross-image placement
# ---------------------------------------------------------------------------
def shared_window_issues(
    images: dict[
        str,
        tuple[
            list[tuple[str, str, int, int, str]],
            dict[str, tuple[str, int, int]],
        ],
    ],
    declared: list[str],
    expected_size: int,
    shared_start: int,
    shared_end: int,
) -> list[str]:
    issues: list[str] = []
    addresses: dict[str, dict[str, int]] = {}

    for key, (sections, symbols) in images.items():
        reserved = [row for row in sections if row[0] == SHARED_WINDOW_SECTION]
        if (
            len(reserved) != 1
            or reserved[0][2] != shared_start
            or reserved[0][2] + reserved[0][3] > shared_end
        ):
            issues.append(
                f"{key}: expected one {SHARED_WINDOW_SECTION} inside "
                f"{shared_start:#010x}..{shared_end:#010x}"
            )

        intruders = [
            row
            for row in allocated_ram_sections(sections, shared_start, shared_end)
            if row[0] != SHARED_WINDOW_SECTION
        ]
        if intruders:
            issues.append(
                f"{key}: ordinary section(s) allocate into the shared window: "
                f"{[(row[0], hex(row[2]), row[3]) for row in intruders]}"
            )

        content_symbols = {
            name
            for name, (_, address, size) in symbols.items()
            if size > 0 and address < shared_end and address + size > shared_start
        }
        unexpected = sorted(content_symbols - set(declared))
        if unexpected:
            issues.append(f"{key}: undeclared shared symbol(s): {unexpected}")

        addresses[key] = {}
        for name in declared:
            record = symbols.get(name)
            if record is None:
                issues.append(f"{key}: declared shared symbol {name!r} is absent")
                continue
            _, address, size = record
            if size != expected_size:
                issues.append(
                    f"{key}: {name} has size {size:#x}, expected size "
                    f"{expected_size:#x}"
                )
                continue
            if size == 0 or address < shared_start or address + size > shared_end:
                issues.append(
                    f"{key}: {name} at {address:#010x} size {size:#x} is outside "
                    f"the shared window"
                )
                continue
            if len(reserved) == 1:
                section_start = reserved[0][2]
                section_end = section_start + reserved[0][3]
                if address < section_start or address + size > section_end:
                    issues.append(
                        f"{key}: {name} at {address:#010x} size {size:#x} is not "
                        f"contained by {SHARED_WINDOW_SECTION}"
                    )
                    continue
            addresses[key][name] = address

    for name in declared:
        observed = {
            key: per_image[name]
            for key, per_image in addresses.items()
            if name in per_image
        }
        if len(observed) == len(images) and len(set(observed.values())) != 1:
            details = ", ".join(f"{key}={value:#010x}" for key, value in observed.items())
            issues.append(f"{name} must have the same address in both images; {details}")

    return issues


def check_shared_window(
    report: Report,
    cross: str,
    elves: dict[str, str],
    declared: list[str],
    expected_size: int,
    shared_start: int,
    shared_end: int,
) -> None:
    images = {}
    for key, path in elves.items():
        try:
            sections = parse_sections(run(cross, "readelf", "-SW", path))
            symbols = elf_symbols_sized(cross, path)
        except (RuntimeError, FileNotFoundError) as exc:
            report.unparsable(f"shared-window {key}", str(exc))
            return
        images[key] = (sections, symbols)

    issues = shared_window_issues(
        images, declared, expected_size, shared_start, shared_end
    )
    if issues:
        for issue in issues:
            report.fail("shared-window", issue)
    else:
        detail = ", ".join(declared)
        report.ok(
            "shared-window",
            f"{detail} shared at {shared_start:#010x}, size {expected_size:#x}; "
            "no ordinary allocations",
        )


# ---------------------------------------------------------------------------
# Rung 5 -- forbidden peripheral ownership
# ---------------------------------------------------------------------------
def forbidden_symbol_matches(
    symbols: dict[str, tuple[str, int]], patterns: list[str]
) -> dict[str, list[str]]:
    matches: dict[str, list[str]] = {}
    for pattern in patterns:
        expression = re.compile(pattern)
        found = sorted(
            name
            for name, (kind, _) in symbols.items()
            if kind not in WEAK_NM_TYPES and expression.search(name) is not None
        )
        if found:
            matches[pattern] = found
    return matches


def check_forbidden_symbols(
    report: Report,
    cross: str,
    elves: dict[str, str],
    forbidden: list[tuple[str, str]],
) -> None:
    cache: dict[str, dict[str, tuple[str, int]]] = {}
    for key, pattern in forbidden:
        name = f"forbid-symbol {key}:{pattern}"
        path = elves.get(key)
        if path is None:
            report.unparsable(name, f"no path supplied for {key}")
            continue
        try:
            if key not in cache:
                cache[key] = elf_symbols(cross, path)
            found = forbidden_symbol_matches(cache[key], [pattern]).get(pattern, [])
        except (RuntimeError, FileNotFoundError, re.error) as exc:
            report.unparsable(name, str(exc))
            continue
        if found:
            report.fail(name, f"strong matching definition(s): {found}")
        else:
            report.ok(name, "no strong match (vendor startup weak stubs ignored)")


def system_init_store_issues(
    body: list[tuple[int, str, str]], resolved: dict[int, int]
) -> list[str]:
    stores, _ = analyze_core1_release(body, resolved)
    store_count = sum(1 for _, mnemonic, _ in body if mnemonic.startswith("str"))
    issues = []
    if len(stores) != store_count:
        issues.append(
            f"resolved {len(stores)} of {store_count} SystemInit store instructions"
        )
    expected = [0xE000ED88, 0xE000ED08]
    if stores != expected:
        issues.append(
            f"store addresses are {[hex(value) for value in stores]}, expected "
            f"CPACR then VTOR {[hex(value) for value in expected]}"
        )
    return issues


def check_core1_system_init(
    report: Report,
    cross: str,
    core1_elf: str,
) -> None:
    name = "core1-SystemInit-stores"
    try:
        symbols = elf_symbols(cross, core1_elf)
        disassembly = run(cross, "objdump", "-d", core1_elf)
    except (RuntimeError, FileNotFoundError) as exc:
        report.unparsable(name, str(exc))
        return

    record = symbols.get("SystemInit")
    if record is None:
        report.fail(name, "SystemInit is not defined")
        return
    kind, address = record
    if kind in WEAK_NM_TYPES:
        report.fail(name, f"SystemInit is weak ({kind}), expected our strong replacement")
        return
    blocks = parse_disassembly(disassembly)
    body = blocks.get(address & ~1)
    if not body:
        report.unparsable(name, f"no disassembly block at {address:#010x}")
        return

    issues = system_init_store_issues(body, parse_resolved_addresses(disassembly))
    if issues:
        for issue in issues:
            report.fail(name, issue)
    else:
        report.ok(name, "only SCB->CPACR then SCB->VTOR")


# ---------------------------------------------------------------------------
# Rung 6 -- CPU1 release MMIO
# ---------------------------------------------------------------------------
def check_core1_release(
    report: Report,
    cross: str,
    core0_elf: str,
    core1_offset: int,
) -> None:
    name = "core1-release"
    try:
        symbols = elf_symbols(cross, core0_elf)
        disassembly = run(cross, "objdump", "-d", core0_elf)
    except (RuntimeError, FileNotFoundError) as exc:
        report.unparsable(name, str(exc))
        return

    record = symbols.get("core1_release")
    if record is None:
        report.fail(name, "core1_release is not defined")
        return
    kind, address = record
    if kind in WEAK_NM_TYPES:
        report.fail(name, f"core1_release is weak ({kind}), expected a strong definition")
        return

    blocks = parse_disassembly(disassembly)
    body = blocks.get(address & ~1)
    if not body:
        report.unparsable(name, f"no disassembly block at {address:#010x}")
        return
    resolved = parse_resolved_addresses(disassembly)
    stores, literals = analyze_core1_release(body, resolved)
    transaction_issues = release_transaction_issues(body, resolved, core1_offset)

    # Only the SYSCON stores are the assertion. core1_release also records its
    # outcome in an ordinary SRAM variable so the console can report a skipped
    # release, and demanding that the function store nothing else would make
    # this rung fail on a correct image -- which it did, once.
    expected_stores = [0x50000804, 0x50000800, 0x50000800]
    stores = [value for value in stores if 0x50000000 <= value < 0x50001000]
    if stores != expected_stores:
        report.fail(
            name,
            f"resolved MMIO store order is {[hex(value) for value in stores]}, "
            f"expected {[hex(value) for value in expected_stores]}",
        )
    elif core1_offset not in literals:
        report.fail(name, f"neither a literal pool word nor an immediate carries CORE1_OFFSET "
                        f"{core1_offset:#010x}")
    elif not any((value >> 16) == 0xC0C4 for value in literals):
        report.fail(name, "no constant carries a CPUCTRL key with 0xC0C4 in bits 31:16; a "
                        "keyless CPUCTRL write is ignored by the hardware")
    elif transaction_issues:
        for issue in transaction_issues:
            report.fail(name, issue)
    else:
        report.ok(
            name,
            f"strong {kind}; one CPUCTRL read feeds assert/release; exact literals present",
        )


# ---------------------------------------------------------------------------
# Rung 7 -- merged-image bounds
# ---------------------------------------------------------------------------
def merged_image_issues(
    core0_size: int, merged_size: int, core1_offset: int, flash_limit: int
) -> list[str]:
    issues = []
    if core0_size > core1_offset:
        issues.append(
            f"core0 binary is {core0_size} B and overruns CORE1_OFFSET {core1_offset:#x}"
        )
    if merged_size > flash_limit:
        issues.append(
            f"merged image is {merged_size} B and exceeds flash limit {flash_limit:#x}"
        )
    return issues


def check_merged_image(
    report: Report,
    core0_binary: str,
    merged_binary: str,
    core1_offset: int,
    flash_limit: int,
) -> None:
    try:
        core0_size = os.path.getsize(core0_binary)
        merged_size = os.path.getsize(merged_binary)
    except OSError as exc:
        report.unparsable("merged-image", str(exc))
        return
    issues = merged_image_issues(core0_size, merged_size, core1_offset, flash_limit)
    if issues:
        for issue in issues:
            report.fail("merged-image", issue)
    else:
        report.ok(
            "merged-image",
            f"core0 {core0_size} B <= {core1_offset:#x}; merged {merged_size} B <= {flash_limit:#x}",
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

    core1_loads = _READELF_L_SAMPLE.replace("0x00000000", "0x000c0000").replace(
        "0x00000400", "0x000c0400"
    ).replace("0x00002a84", "0x000c2a84")
    real_run = globals()["run"]
    try:
        globals()["run"] = lambda _cross, _tool, *_args: core1_loads
        report = Report()
        check_flash_geometry(
            report, "unused-", CORE1, "core1.elf", None, 0x000C0000, 0x00100000
        )
        expect(
            not report.failures and not report.parse_errors,
            "rung 3: core1 LOAD base at CORE1_OFFSET accepted",
        )

        globals()["run"] = lambda _cross, _tool, *_args: _READELF_L_SAMPLE
        report = Report()
        check_flash_geometry(
            report, "unused-", CORE1, "core1.elf", None, 0x000C0000, 0x00100000
        )
        expect(
            any("load-base core1.elf" in failure for failure in report.failures),
            "rung 3: core1 LOAD below CORE1_OFFSET rejected",
        )
    finally:
        globals()["run"] = real_run

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

    # Step-5 parsers and decisions. These are looked up dynamically so this
    # self-test is the RED test before their implementations exist.
    sized_parser = globals().get("parse_nm_posix_sized")
    expect(sized_parser is not None, "step 5: sized-symbol parser is available")
    if sized_parser is not None:
        sized = sized_parser("g_shared_window B 2004c000 10\n__boundary A 2004c000\n")
        expect(
            sized["g_shared_window"] == ("B", 0x2004C000, 0x10),
            "rung 4: shared symbol size and address parse",
        )

    forbidden = globals().get("forbidden_symbol_matches")
    expect(forbidden is not None, "step 5: forbidden-symbol matcher is available")
    if forbidden is not None:
        candidate_symbols = {
            "FLEXIO_IRQHandler": ("W", 0x500),
            "display_FlexIO_start": ("T", 0x1200),
            "status_led_hw_set": ("T", 0x1300),
            "main": ("T", 0x1000),
        }
        expect(
            forbidden(candidate_symbols, [r"(?i)flexio"])
            == {r"(?i)flexio": ["display_FlexIO_start"]},
            "rung 5: strong peripheral definition rejected but startup weak stub allowed",
        )
        expect(
            forbidden(candidate_symbols, [r"(?i)status_led"])
            == {r"(?i)status_led": ["status_led_hw_set"]},
            "rung 5: CPU1 status LED ownership is rejected in core0",
        )

    system_init_analyzer = globals().get("system_init_store_issues")
    expect(
        system_init_analyzer is not None,
        "step 5: core1 SystemInit store allowlist is available",
    )
    if system_init_analyzer is not None:
        system_disassembly = """\
00002000 <SystemInit>:
    2000:\t4b02      \tldr\tr3, [pc, #8] @ (200c <SystemInit+0xc>)
    2002:\tf8c3 2088 \tstr.w\tr2, [r3, #136]
    2006:\t609a      \tstr\tr2, [r3, #8]
    200c:\te000ed00  \t.word\t0xe000ed00
"""
        system_blocks = parse_disassembly(system_disassembly)
        expect(
            system_init_analyzer(
                system_blocks[0x2000], parse_resolved_addresses(system_disassembly)
            )
            == [],
            "rung 5: CPACR and VTOR are the only accepted SystemInit stores",
        )
        system_blocks[0x2000].insert(3, (0x200A, "str", "r2, [r3, #12]"))
        expect(
            any("store addresses" in issue for issue in system_init_analyzer(
                system_blocks[0x2000], parse_resolved_addresses(system_disassembly)
            )),
            "rung 5: an additional SystemInit store is rejected",
        )

    shared_issues = globals().get("shared_window_issues")
    expect(shared_issues is not None, "step 5: shared-window checker is available")
    if shared_issues is not None:
        good_sections = [
            (SHARED_WINDOW_SECTION, "NOBITS", 0x2004C000, 0x30, "WA"),
            (".data", "PROGBITS", 0x2004E000, 0x20, "WA"),
        ]
        good_symbols = {"g_shared_window": ("B", 0x2004C000, 0x30)}
        images = {
            CORE0: (good_sections, good_symbols),
            "core1.elf": (good_sections, good_symbols),
        }
        expect(
            shared_issues(
                images, ["g_shared_window"], 0x30, 0x2004C000, 0x2004E000
            ) == [],
            "rung 4: matching shared symbol in both images accepted",
        )
        wrong_size = dict(images)
        wrong_size[CORE0] = (
            good_sections,
            {"g_shared_window": ("B", 0x2004C000, 0x2C)},
        )
        expect(
            any("expected size" in issue for issue in shared_issues(
                wrong_size, ["g_shared_window"], 0x30, 0x2004C000, 0x2004E000
            )),
            "rung 4: shared-window ABI size drift rejected",
        )
        mismatched = dict(images)
        mismatched["core1.elf"] = (
            [
                (SHARED_WINDOW_SECTION, "NOBITS", 0x2004C000, 0x34, "WA"),
                (".data", "PROGBITS", 0x2004E000, 0x20, "WA"),
            ],
            {"g_shared_window": ("B", 0x2004C004, 0x30)},
        )
        expect(
            any("same address" in issue for issue in shared_issues(
                mismatched, ["g_shared_window"], 0x30, 0x2004C000, 0x2004E000
            )),
            "rung 4: cross-image shared-symbol drift rejected",
        )
        intruding = dict(images)
        intruding[CORE0] = (
            good_sections + [(".bss", "NOBITS", 0x2004C008, 4, "WA")],
            good_symbols,
        )
        expect(
            any("ordinary section" in issue for issue in shared_issues(
                intruding, ["g_shared_window"], 0x30, 0x2004C000, 0x2004E000
            )),
            "rung 4: ordinary RAM allocation in shared window rejected",
        )

    dis_parser = globals().get("parse_disassembly")
    resolved_parser = globals().get("parse_resolved_addresses")
    release_analyzer = globals().get("analyze_core1_release")
    transaction_issues = globals().get("release_transaction_issues")
    expect(
        dis_parser is not None
        and resolved_parser is not None
        and release_analyzer is not None
        and transaction_issues is not None,
        "step 5: Thumb-2 release analyzers are available",
    )
    if (
        dis_parser is not None
        and resolved_parser is not None
        and release_analyzer is not None
        and transaction_issues is not None
    ):
        release_disassembly = """\
00001000 <core1_release>:
    1000:\t4808      \tldr\tr0, [pc, #32] ; (1024 <core1_release+0x24>)
    1002:\tf04f 43a0 \tmov.w\tr3, #1342177280
    1006:\tf8c3 0804 \tstr.w\tr0, [r3, #2052]
    100a:\tf8d3 0800 \tldr.w\tr0, [r3, #2048]
    100c:\t4a06      \tldr\tr2, [pc, #24] ; (1028 <core1_release+0x28>)
    100e:\t4302      \torrs\tr2, r0
    1010:\tf8c3 2800 \tstr.w\tr2, [r3, #2048]
    1014:\t4a05      \tldr\tr2, [pc, #20] ; (102c <core1_release+0x2c>)
    1016:\t4906      \tldr\tr1, [pc, #24] ; (1030 <core1_release+0x30>)
    1018:\t4001      \tands\tr1, r0
    101a:\t430a      \torrs\tr2, r1
    101c:\tf8c3 2800 \tstr.w\tr2, [r3, #2048]
    1024:\t000c0000  \t.word\t0x000c0000
    1028:\tc0c40028  \t.word\t0xc0c40028
    102c:\tc0c40008  \t.word\t0xc0c40008
    1030:\t3f3bffd7  \t.word\t0x3f3bffd7
"""
        blocks = dis_parser(release_disassembly)
        resolved = resolved_parser(release_disassembly)
        stores, literals = release_analyzer(
            blocks[0x1000], resolved
        )
        expect(
            stores == [0x50000804, 0x50000800, 0x50000800],
            "rung 6: CPBOOT then assert/release CPUCTRL stores resolve",
        )
        expect(
            0x000C0000 in literals and any((value >> 16) == 0xC0C4 for value in literals),
            "rung 6: vector offset and CPUCTRL key literals found",
        )
        expect(
            transaction_issues(blocks[0x1000], resolved) == [],
            "rung 6: exact CPBOOT value and one-read CPUCTRL transformations accepted",
        )

        reread_disassembly = release_disassembly.replace(
            "    1014:\t4a05      \tldr\tr2, [pc, #20] ; (102c <core1_release+0x2c>)",
            "    1012:\tf8d3 0800 \tldr.w\tr0, [r3, #2048]\n"
            "    1014:\t4a05      \tldr\tr2, [pc, #20] ; (102c <core1_release+0x2c>)",
        )
        reread_blocks = dis_parser(reread_disassembly)
        expect(
            any("exactly one" in issue for issue in transaction_issues(
                reread_blocks[0x1000], resolved_parser(reread_disassembly)
            )),
            "rung 6: a second CPUCTRL read is rejected",
        )

        dead_literals = release_disassembly.replace(
            "    100e:\t4302      \torrs\tr2, r0",
            "    100e:\t4602      \tmov\tr2, r0",
        )
        dead_blocks = dis_parser(dead_literals)
        expect(
            any("exact assert/release" in issue for issue in transaction_issues(
                dead_blocks[0x1000], resolved_parser(dead_literals)
            )),
            "rung 6: dead but present transformation literals are rejected",
        )

        wrong_cpboot = release_disassembly.replace(
            "    1024:\t000c0000  \t.word\t0x000c0000",
            "    1024:\t000d0000  \t.word\t0x000d0000",
        )
        wrong_cpboot_blocks = dis_parser(wrong_cpboot)
        expect(
            any("CPBOOT" in issue for issue in transaction_issues(
                wrong_cpboot_blocks[0x1000], resolved_parser(wrong_cpboot)
            )),
            "rung 6: a CPBOOT value other than CORE1_OFFSET is rejected",
        )

        # The REAL shape, copied verbatim from
        #   arm-none-eabi-objdump -d --disassemble=core1_release build/core0.elf
        # at -Os. Both MMIO constants arrive as Thumb-2 modified immediates and
        # never reach the literal pool, and objdump writes its resolved-address
        # comments with `@` rather than `;`. The synthetic vector above is a
        # pool-word shape the compiler does not in fact choose, so keeping only
        # it is how a checker passes its own self-test and fails a good image.
        real_disassembly = """\
00005fb0 <core1_release>:
    5fb0:\tf04f 43a0 \tmov.w\tr3, #1342177280\t@ 0x50000000
    5fb4:\tf44f 2240 \tmov.w\tr2, #786432\t@ 0xc0000
    5fb8:\tf8c3 2804 \tstr.w\tr2, [r3, #2052]\t@ 0x804
    5fbc:\tf8d3 0800 \tldr.w\tr0, [r3, #2048]\t@ 0x800
    5fc0:\t4a05      \tldr\tr2, [pc, #20]\t@ (5fd8 <core1_release+0x28>)
    5fc2:\t4906      \tldr\tr1, [pc, #24]\t@ (5fdc <core1_release+0x2c>)
    5fc4:\t4302      \torrs\tr2, r0
    5fc6:\tf8c3 2800 \tstr.w\tr2, [r3, #2048]\t@ 0x800
    5fca:\t4a05      \tldr\tr2, [pc, #20]\t@ (5fe0 <core1_release+0x30>)
    5fcc:\t4001      \tands\tr1, r0
    5fce:\t430a      \torrs\tr2, r1
    5fd0:\tf8c3 2800 \tstr.w\tr2, [r3, #2048]\t@ 0x800
    5fd4:\t4770      \tbx\tlr
    5fd6:\tbf00      \tnop
    5fd8:\tc0c40028 \t.word\t0xc0c40028
    5fdc:\t3f3bffd7 \t.word\t0x3f3bffd7
    5fe0:\tc0c40008 \t.word\t0xc0c40008
"""
        real_blocks = dis_parser(real_disassembly)
        real_resolved = resolved_parser(real_disassembly)
        real_stores, real_literals = release_analyzer(
            real_blocks[0x5FB0], real_resolved
        )
        expect(
            [v for v in real_stores if 0x50000000 <= v < 0x50001000]
            == [0x50000804, 0x50000800, 0x50000800],
            "rung 6: stores resolve when the SYSCON base is an immediate",
        )
        expect(
            0x000C0000 in real_literals,
            "rung 6: CORE1_OFFSET is found as an immediate, not only as a pool word",
        )
        expect(
            transaction_issues(real_blocks[0x5FB0], real_resolved) == [],
            "rung 6: the real -Os codegen is accepted",
        )

    merged_issues = globals().get("merged_image_issues")
    expect(merged_issues is not None, "step 5: merged-image bounds checker is available")
    if merged_issues is not None:
        expect(
            merged_issues(0x1000, 0xC2000, 0xC0000, 0x100000) == [],
            "rung 7: in-bounds merged image accepted",
        )
        expect(
            any("core0" in issue for issue in merged_issues(
                0xC0001, 0xC2000, 0xC0000, 0x100000
            )),
            "rung 7: core0 overrun rejected",
        )
        expect(
            any("merged" in issue for issue in merged_issues(
                0x1000, 0x100001, 0xC0000, 0x100000
            )),
            "rung 7: flash-limit overrun rejected",
        )

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


def split_forbid(value: str) -> tuple[str, str]:
    """`core1.elf:EDMA_0_` (the suffix is a regular expression)."""
    key, sep, pattern = value.partition(":")
    if not key or not sep or not pattern:
        raise argparse.ArgumentTypeError(f"--forbid expects ELF:REGEX, got {value!r}")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise argparse.ArgumentTypeError(f"invalid --forbid regex {pattern!r}: {exc}") from exc
    return key, pattern


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cross-compile", default="arm-none-eabi-", help="toolchain prefix")
    parser.add_argument("--core0", help="path to core0.elf")
    parser.add_argument("--core1", help="path to core1.elf")
    parser.add_argument("--core0-bin", help="path to the flat CPU0 image")
    parser.add_argument("--core1-bin", help="path to the flat CPU1 image")
    parser.add_argument("--merged-bin", help="path to the merged two-image binary")
    parser.add_argument("--core0-flash-start", default="0x00000000")
    parser.add_argument("--core0-flash-end", default="0x000C0000")
    parser.add_argument("--core1-offset", default="0x000C0000")
    parser.add_argument("--flash-limit", default="0x00100000")
    parser.add_argument("--core0-ram-start", default="0x20000000")
    parser.add_argument("--core0-ram-end", default="0x2004C000")
    parser.add_argument("--core1-ram-start", default="0x2004E000")
    parser.add_argument("--core1-ram-end", default="0x20068000")
    parser.add_argument("--ram-shared-start", default="0x2004C000")
    parser.add_argument("--ram-shared-end", default="0x2004E000")
    parser.add_argument("--stack-size", default="0x0800")
    parser.add_argument("--heap-size", default="0x0400")
    parser.add_argument(
        "--shared-symbol",
        action="append",
        default=[],
        help="the only content symbol allowed in the shared window (repeatable)",
    )
    parser.add_argument(
        "--shared-window-size",
        help="required byte size of every declared shared symbol",
    )
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
    parser.add_argument(
        "--forbid",
        action="append",
        default=[],
        type=split_forbid,
        metavar="ELF:REGEX",
        help="reject a strong symbol matching REGEX (weak startup stubs are allowed)",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    if not args.core0 or not args.core1:
        parser.error("--core0 and --core1 are required unless --self-test is given")
    if not args.core0_bin or not args.core1_bin or not args.merged_bin:
        parser.error("--core0-bin, --core1-bin and --merged-bin are required")
    if not args.shared_symbol:
        parser.error("at least one --shared-symbol is required")
    if not args.shared_window_size:
        parser.error("--shared-window-size is required")

    elves = {CORE0: args.core0, CORE1: args.core1}
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
    check_flash_geometry(
        report,
        args.cross_compile,
        CORE1,
        args.core1,
        args.core1_bin,
        int(args.core1_offset, 0),
        int(args.flash_limit, 0),
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
    check_ram_geometry(
        report,
        args.cross_compile,
        CORE1,
        args.core1,
        int(args.core1_ram_start, 0),
        int(args.core1_ram_end, 0),
        int(args.stack_size, 0),
        int(args.heap_size, 0),
        int(args.ram_shared_start, 0),
        int(args.ram_shared_end, 0),
    )
    check_shared_window(
        report,
        args.cross_compile,
        elves,
        args.shared_symbol,
        int(args.shared_window_size, 0),
        int(args.ram_shared_start, 0),
        int(args.ram_shared_end, 0),
    )
    check_forbidden_symbols(report, args.cross_compile, elves, args.forbid)
    check_core1_system_init(report, args.cross_compile, args.core1)
    check_core1_release(
        report,
        args.cross_compile,
        args.core0,
        int(args.core1_offset, 0),
    )
    check_merged_image(
        report,
        args.core0_bin,
        args.merged_bin,
        int(args.core1_offset, 0),
        int(args.flash_limit, 0),
    )

    return report.emit()


if __name__ == "__main__":
    sys.exit(main())
