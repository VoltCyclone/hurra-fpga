"""Cross-language equivalence between the C firmware and the Python reference.

The generated-file gates in ``test_injection_wire.py`` check self-consistency
(``header == render_c(schema)`` and regeneration idempotency); neither compares
C semantics against Python semantics. Behaviour in this contract is hand-written
per language - CRC-16, slot pack/unpack, and sequence classification are all C by
hand against generated Python - so a structural test cannot catch the two ends
drifting apart. Only compiling the real firmware source and diffing it against
the real reference can.

The three implementations deliberately disagree about rejection *reasons*: the C
codec checks SOF then length then CRC, ``unpack_slot`` checks size, SOF, length,
CRC, type, exact length, reject-flags, and the FPGA ANDs every term at once and
buckets its counters in a third order. Reasons are diagnostics. The one predicate
all three must share is the FPGA's ``valid_return`` (``spi_link.py:150-157``)::

    deliverable  <=>  SOF == 0x68  and  known_type  and  length == expected(type)
                      and  CRC ok  and  type != IDLE

So the admissibility oracle below is three-valued - REJECT / IDLE / DELIVER - not
boolean. ``_VECTORS`` is the shared table that pins it, and every later change to
the codec's admission rules appends its cases there.
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from hurra_cynthion.injection_wire import (
    CRC_INIT,
    INJ_TYPE_IDLE,
    INJ_TYPE_MAP_ENTRY,
    INJ_TYPE_RELATIVE,
    MAP_ENTRY_GOLDEN_PAYLOAD,
    MESSAGE_TYPES,
    RELATIVE_GOLDEN_PAYLOAD,
    FrameError,
    MapEntryPayload,
    RelativePayload,
    classify_sequence,
    crc16_ccitt_false,
    pack_slot,
    unpack_slot,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIRMWARE_ROOT = REPO_ROOT / "firmware" / "ch32h417"
FIRMWARE_SRC = FIRMWARE_ROOT / "src"
# The generated header lives in include/; only spi_frame.{c,h} are in src/. Note
# this must be the absolute repo path, not the firmware Makefile's relative
# "-Isrc", which from the repo root resolves to the Python package.
FIRMWARE_INCLUDE = FIRMWARE_ROOT / "include"

# The same warning set the real gate uses (firmware/ch32h417/Makefile:120).
# Without -Wconversion and -Wshadow the oracle could accept C that `make test`
# rejects, which would make this file a weaker gate than the one it guards.
HOST_WARNINGS = ("-Wall", "-Wextra", "-Werror", "-Wconversion", "-Wshadow")


def _run_c_harness(tmp_path: Path, source: str, *, name: str) -> str:
    """Compile ``source`` against the real spi_frame.c and return its stdout."""
    harness = tmp_path / f"{name}.c"
    harness.write_text(source)
    binary = tmp_path / name

    subprocess.run(
        [
            "cc",
            "-std=c11",
            *HOST_WARNINGS,
            "-isystem",
            str(FIRMWARE_INCLUDE),
            "-I",
            str(FIRMWARE_SRC),
            str(harness),
            str(FIRMWARE_SRC / "spi_frame.c"),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
    )
    completed = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
    return completed.stdout


def _c_bytes(data: bytes) -> str:
    return ", ".join(f"0x{byte:02X}u" for byte in data)


_HARNESS = textwrap.dedent(
    """
    #include <stdint.h>
    #include <stdio.h>

    #include "spi_frame.h"

    int main(void)
    {
        static const char *const names[] = {"NEXT", "DUPLICATE", "GAP", "STALE"};

        printf("[");
        for (unsigned previous = 0u; previous < 256u; ++previous) {
            for (unsigned received = 0u; received < 256u; ++received) {
                uint8_t gap = 0u;
                const spi_frame_seq_class_t class_ = spi_frame_seq_classify(
                    (uint8_t)previous, (uint8_t)received, &gap);
                const uint8_t wrapper_gap =
                    spi_frame_seq_gap((uint8_t)previous, (uint8_t)received);
                if (previous != 0u || received != 0u) {
                    printf(",");
                }
                printf("[\\"%s\\",%u,%u]", names[(int)class_], gap, wrapper_gap);
            }
        }
        printf("]\\n");
        return 0;
    }
    """
)


@pytest.mark.skipif(shutil.which("cc") is None, reason="no host C compiler available")
def test_c_sequence_classifier_matches_python_reference(tmp_path: Path) -> None:
    observed = json.loads(_run_c_harness(tmp_path, _HARNESS, name="sequence"))
    assert len(observed) == 65536

    divergences = []
    for index, (class_name, gap, wrapper_gap) in enumerate(observed):
        previous, received = divmod(index, 256)
        disposition, expected_gap = classify_sequence(previous, received)
        if (class_name, gap) != (disposition.name, expected_gap) or wrapper_gap != gap:
            divergences.append(
                f"previous={previous} received={received} "
                f"c=({class_name},{gap},wrapper={wrapper_gap}) "
                f"python=({disposition.name},{expected_gap})"
            )

    assert not divergences, "\n".join(
        [f"{len(divergences)} of 65536 pairs diverge", *divergences[:10]]
    )


# A fixed, deterministic 30-byte pattern - the widest CRC input the contract can
# produce, since spi_frame_crc16 is only ever called over slot[0:30]. Defined
# once here and emitted into the C harness so both ends provably see the same
# bytes rather than two hand-copied literals.
CRC_PATTERN = bytes((index * 37 + 11) & 0xFF for index in range(30))

_CRC_HARNESS = """
#include <stdint.h>
#include <stdio.h>

#include "injection_wire.h"
#include "spi_frame.h"

int main(void)
{{
    static const uint8_t pattern[] = {{{pattern}}};
    uint8_t single[1];

    for (unsigned value = 0u; value < 256u; ++value) {{
        single[0] = (uint8_t)value;
        printf("%u\\n", spi_frame_crc16(single, 1u));
    }}
    /* A zero-length message must not dereference the pointer at all. */
    printf("%u\\n", spi_frame_crc16(NULL, 0u));
    for (uint32_t used = 0u; used <= (uint32_t)sizeof(pattern); ++used) {{
        printf("%u\\n", spi_frame_crc16(pattern, used));
    }}
    printf("%u\\n", spi_frame_crc16(inj_golden_relative_payload,
                                    INJ_FRAME_PAYLOAD_SIZE));
    printf("%u\\n", spi_frame_crc16(inj_golden_map_entry_payload,
                                    INJ_FRAME_PAYLOAD_SIZE));
    return 0;
}}
"""


@pytest.mark.skipif(shutil.which("cc") is None, reason="no host C compiler available")
def test_c_crc16_matches_python_reference(tmp_path: Path) -> None:
    # CRC-16 is linear in its input, so every single byte plus every prefix
    # length up to the frame's 30 pins both the state machine and the length
    # loop; there is no longer message the contract can produce.
    expected = [crc16_ccitt_false(bytes([value])) for value in range(256)]
    expected.append(crc16_ccitt_false(b""))
    expected.extend(crc16_ccitt_false(CRC_PATTERN[:used]) for used in range(len(CRC_PATTERN) + 1))
    expected.append(crc16_ccitt_false(RELATIVE_GOLDEN_PAYLOAD))
    expected.append(crc16_ccitt_false(MAP_ENTRY_GOLDEN_PAYLOAD))

    source = _CRC_HARNESS.format(pattern=_c_bytes(CRC_PATTERN))
    observed = [int(line) for line in _run_c_harness(tmp_path, source, name="crc16").split()]

    assert len(observed) == len(expected) == 290
    divergences = [
        f"case {index}: c=0x{got:04X} python=0x{want:04X}"
        for index, (got, want) in enumerate(zip(observed, expected, strict=True))
        if got != want
    ]
    assert not divergences, "\n".join(
        [f"{len(divergences)} of {len(expected)} CRC inputs diverge", *divergences[:10]]
    )


_CORRUPTION_HARNESS = """
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "injection_wire.h"
#include "spi_frame.h"

int main(void)
{{
    static const uint8_t base[INJ_FRAME_SIZE] = {{{slot}}};

    for (unsigned bit = 0u; bit < 8u * INJ_FRAME_SIZE; ++bit) {{
        uint8_t slot[INJ_FRAME_SIZE];

        memcpy(slot, base, sizeof(slot));
        slot[bit / 8u] ^= (uint8_t)(1u << (bit % 8u));
        printf("%d\\n", (int)spi_frame_unpack(slot, NULL, NULL, NULL, NULL));
    }}
    return 0;
}}
"""


@pytest.mark.skipif(shutil.which("cc") is None, reason="no host C compiler available")
def test_single_bit_corruption_is_rejected_by_both_ends(tmp_path: Path) -> None:
    valid = pack_slot(INJ_TYPE_RELATIVE, 0x11, RELATIVE_GOLDEN_PAYLOAD)
    source = _CORRUPTION_HARNESS.format(slot=_c_bytes(valid))
    observed = [int(line) for line in _run_c_harness(tmp_path, source, name="corrupt").split()]
    assert len(observed) == 256

    # Admissibility only, never the reason: the three ends check in different
    # orders by design, so a frame with two faults gets different names.
    divergences = []
    for bit, c_result in enumerate(observed):
        corrupted = bytearray(valid)
        corrupted[bit // 8] ^= 1 << (bit % 8)
        try:
            unpack_slot(bytes(corrupted))
        except FrameError:
            python_rejected = True
        else:
            python_rejected = False

        if c_result == 0 or not python_rejected:
            divergences.append(
                f"bit {bit} (byte {bit // 8}): c_accepted={c_result == 0} "
                f"python_accepted={not python_rejected}"
            )

    assert not divergences, "\n".join(
        [f"{len(divergences)} of 256 single-bit flips are admitted", *divergences[:10]]
    )


def _crc_slot(header: bytes, payload: bytes) -> bytes:
    """Build a slot by hand with a correct CRC, bypassing pack_slot's rules.

    Needed for the malformed vectors: pack_slot refuses to emit them, so the only
    way to ask "what do the two ends do with these bytes" is to lay them out
    directly. The CRC is always made correct so that the vector isolates exactly
    one fault.
    """
    slot = bytearray(32)
    slot[: len(header)] = header
    slot[4 : 4 + len(payload)] = payload
    slot[-2:] = crc16_ccitt_false(bytes(slot[:-2])).to_bytes(2, "little")
    return bytes(slot)


def _corrupt_crc(slot: bytes) -> bytes:
    corrupted = bytearray(slot)
    corrupted[-1] ^= 0x01
    return bytes(corrupted)


def _replace_sof(slot: bytes, sof: int) -> bytes:
    corrupted = bytearray(slot)
    corrupted[0] = sof
    return bytes(corrupted)


_VALID_IDLE = pack_slot(INJ_TYPE_IDLE, 0, b"")
_VALID_RELATIVE = pack_slot(INJ_TYPE_RELATIVE, 1, RELATIVE_GOLDEN_PAYLOAD)
_VALID_MAP_ENTRY = pack_slot(INJ_TYPE_MAP_ENTRY, 2, MAP_ENTRY_GOLDEN_PAYLOAD)

# (name, slot bytes, expected admissibility) where expected is one of
# "REJECT" / "IDLE" / "DELIVER". Later changes to the codec's admission rules
# append their diverging cases here rather than adding a parallel mechanism.
#
_VECTORS: list[tuple[str, bytes, str]] = [
    ("idle keepalive", _VALID_IDLE, "IDLE"),
    ("well-formed RELATIVE", _VALID_RELATIVE, "DELIVER"),
    ("well-formed MAP_ENTRY", _VALID_MAP_ENTRY, "DELIVER"),
    ("bad SOF", _replace_sof(_VALID_RELATIVE, 0x69), "REJECT"),
    ("bad CRC", _corrupt_crc(_VALID_RELATIVE), "REJECT"),
    ("length 27", _crc_slot(bytes((0x68, INJ_TYPE_RELATIVE, 3, 27)), b""), "REJECT"),
    # Length is per-type EXACT, not a maximum, and the type must be one of the
    # 15 the contract assigns. All four carry a correct CRC, so the only thing
    # that can reject them is the rule under test.
    (
        "RELATIVE with length 20",
        _crc_slot(bytes((0x68, INJ_TYPE_RELATIVE, 4, 20)), RELATIVE_GOLDEN_PAYLOAD[:20]),
        "REJECT",
    ),
    ("unknown type 0x7F", _crc_slot(bytes((0x68, 0x7F, 5, 26)), RELATIVE_GOLDEN_PAYLOAD), "REJECT"),
    (
        "IDLE with a 26-byte payload",
        _crc_slot(bytes((0x68, INJ_TYPE_IDLE, 6, 26)), RELATIVE_GOLDEN_PAYLOAD),
        "REJECT",
    ),
    ("RELATIVE with length 0", _crc_slot(bytes((0x68, INJ_TYPE_RELATIVE, 7, 0)), b""), "REJECT"),
]
_VECTORS.extend(
    (f"IDLE with sequence {sequence}", pack_slot(INJ_TYPE_IDLE, sequence, b""), "IDLE")
    for sequence in range(1, 256)
)

_ADMISSIBILITY_HARNESS = """
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "injection_wire.h"
#include "spi_frame.h"

int main(void)
{{
    static const uint8_t slots[][INJ_FRAME_SIZE] = {{
{slots}
    }};

    for (size_t index = 0u; index < sizeof(slots) / sizeof(slots[0]); ++index) {{
        const spi_frame_result_t result =
            spi_frame_unpack(slots[index], NULL, NULL, NULL, NULL);

        puts(result == SPI_FRAME_OK
                 ? "DELIVER"
                 : (result == SPI_FRAME_IDLE ? "IDLE" : "REJECT"));
    }}
    return 0;
}}
"""


def _python_admissibility(slot: bytes) -> str:
    """The Python end's view of the FPGA's valid_return predicate."""
    try:
        frame = unpack_slot(slot)
    except FrameError:
        return "REJECT"
    return "IDLE" if frame.type_ == INJ_TYPE_IDLE else "DELIVER"


@pytest.mark.skipif(shutil.which("cc") is None, reason="no host C compiler available")
def test_c_and_python_agree_on_frame_admissibility(tmp_path: Path) -> None:
    slots = "\n".join(f"        {{{_c_bytes(slot)}}}," for _, slot, _ in _VECTORS)
    source = _ADMISSIBILITY_HARNESS.format(slots=slots)
    observed = _run_c_harness(tmp_path, source, name="admit").split()
    assert len(observed) == len(_VECTORS)

    divergences = []
    for (name, slot, expected), c_result in zip(_VECTORS, observed, strict=True):
        python_result = _python_admissibility(slot)
        if (c_result, python_result) != (expected, expected):
            divergences.append(
                f"{name}: expected={expected} c={c_result} python={python_result} "
                f"slot={slot.hex()}"
            )

    assert not divergences, "\n".join(
        [f"{len(divergences)} of {len(_VECTORS)} vectors diverge", *divergences]
    )


_LENGTH_HARNESS = """
#include <stdint.h>
#include <stdio.h>

#include "injection_wire.h"
#include "spi_frame.h"

int main(void)
{
    for (unsigned candidate = 0u; candidate < 256u; ++candidate) {
        printf("%u %d\\n", spi_frame_expected_payload_length((uint8_t)candidate),
               inj_type_is_known((uint8_t)candidate));
    }
    return 0;
}
"""


@pytest.mark.skipif(shutil.which("cc") is None, reason="no host C compiler available")
def test_c_expected_payload_length_matches_python(tmp_path: Path) -> None:
    """The IDLE-ships-length-0 rule, pinned across both ends.

    It is the one admission rule with no home in the schema: the JSON gives IDLE
    a full 26-byte reserved payload, and message_types carries no length at all.
    The rule exists only as pack_slot's `0 if type_ == INJ_TYPE_IDLE else
    MAX_PAYLOAD`, spi_link.py's expected_length Mux, and now
    spi_frame_expected_payload_length. Three hardcodings; this is what keeps
    them equal.
    """
    observed = _run_c_harness(tmp_path, _LENGTH_HARNESS, name="length").splitlines()
    assert len(observed) == 256

    divergences = []
    for candidate, line in enumerate(observed):
        c_length, c_known = (int(field) for field in line.split())
        # Unknown types agree at 26 on both sides; it is the type check, not the
        # length, that rejects them.
        want_length = 0 if candidate == INJ_TYPE_IDLE else 26
        want_known = candidate in MESSAGE_TYPES.values()
        if (c_length, bool(c_known)) != (want_length, want_known):
            divergences.append(
                f"type 0x{candidate:02X}: c=(length={c_length},known={bool(c_known)}) "
                f"python=(length={want_length},known={want_known})"
            )

    assert not divergences, "\n".join(
        [f"{len(divergences)} of 256 type values diverge", *divergences[:10]]
    )


_TRAFFIC_SLOTS = 2000
_TRAFFIC_PERIOD = 25
_TRAFFIC_MESSAGES = _TRAFFIC_SLOTS // _TRAFFIC_PERIOD

_TRAFFIC_HARNESS = """
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "injection_wire.h"
#include "spi_frame.h"

int main(void)
{{
    static const uint8_t idle[INJ_FRAME_SIZE] = {{{idle}}};
    static const uint8_t messages[][INJ_FRAME_SIZE] = {{
{messages}
    }};
    unsigned counts[4] = {{0u, 0u, 0u, 0u}};
    unsigned delivered = 0u;
    size_t pending = 0u;
    uint8_t window = 0u;

    for (unsigned index = 0u; index < {slots}u; ++index) {{
        const int is_message = (index % {period}u == {period}u - 1u);
        const uint8_t *const current = is_message ? messages[pending] : idle;
        uint8_t observed = 0u;
        spi_frame_seq_class_t verdict;

        if (is_message) {{
            ++pending;
        }}
        if (spi_frame_unpack(current, NULL, &observed, NULL, NULL) != SPI_FRAME_OK) {{
            continue;
        }}
        ++delivered;
        verdict = spi_frame_seq_classify(window, observed, NULL);
        counts[(int)verdict] += 1u;
        if (verdict == SPI_FRAME_SEQ_NEXT || verdict == SPI_FRAME_SEQ_GAP) {{
            window = observed;
        }}
    }}
    printf("%u %u %u %u %u\\n", delivered, counts[0], counts[1], counts[2],
           counts[3]);
    return 0;
}}
"""


@pytest.mark.skipif(shutil.which("cc") is None, reason="no host C compiler available")
def test_classifier_is_never_fed_an_idle_sequence(tmp_path: Path) -> None:
    """The trap, stated executably.

    The obvious receive loop is `unpack(...) == SPI_FRAME_OK` -> classify ->
    act/advance. IDLE frames are not sequenced - tx_sequence only increments
    under `with m.If(send_message)` (spi_link.py:284-285) and the hardwired idle
    carries sequence byte 0 - so on a link that is working perfectly this loop
    classifies *every inter-message idle* as STALE:

        window=0 ; idle seq 0   delta=0x00 -> DUPLICATE  (no advance)
                   msg  seq 1   delta=0x01 -> NEXT       window=1
                   idle seq 0   delta=0xFF -> STALE      (no advance)
                   msg  seq 2   delta=0x01 -> NEXT       window=2
                   idle seq 0   delta=0xFE -> STALE

    At 8000 slots/s with sparse traffic that is >99.9% of all classifications.
    The natural "fix" of letting the window follow idles is worse: it pins the
    window to 0 and every message with sequence >= 2 becomes a GAP.

    The classifier itself is correct (ff9ceba) and is not touched. What must
    change is the contract for what may be fed into it.
    """
    messages = "\n".join(
        f"        {{{_c_bytes(pack_slot(INJ_TYPE_RELATIVE, sequence, RELATIVE_GOLDEN_PAYLOAD))}}},"
        for sequence in range(1, _TRAFFIC_MESSAGES + 1)
    )
    source = _TRAFFIC_HARNESS.format(
        idle=_c_bytes(_VALID_IDLE),
        messages=messages,
        slots=_TRAFFIC_SLOTS,
        period=_TRAFFIC_PERIOD,
    )
    delivered, next_, duplicate, gap, stale = (
        int(field) for field in _run_c_harness(tmp_path, source, name="traffic").split()
    )

    assert stale == 0, (
        f"{stale} of {delivered} delivered frames classified STALE on a healthy link; "
        f"idle keepalives are reaching the classifier"
    )
    assert (delivered, next_, duplicate, gap) == (_TRAFFIC_MESSAGES, _TRAFFIC_MESSAGES, 0, 0)


def _riscv_tool(name: str) -> str | None:
    """Mirror the firmware Makefile's toolchain discovery (Makefile:3-10)."""
    found = shutil.which(f"riscv-none-elf-{name}")
    if found is not None:
        return found
    pattern = "Library/xPacks/@xpack-dev-tools/riscv-none-elf-gcc/*/.content/bin"
    candidates = sorted(Path.home().glob(f"{pattern}/riscv-none-elf-{name}"))
    return str(candidates[-1]) if candidates else None


# Makefile:17-25 - the exact flags build/v5f/spi_frame.o is compiled with.
_V5F_FLAGS = (
    "-march=rv32imafc_zicsr",
    "-mabi=ilp32f",
    "-DCH32H417",
    "-Os",
    "-ffunction-sections",
    "-fdata-sections",
    "-fno-common",
    "-DCore_V5F",
    "-Dsystick2",
)


@pytest.mark.skipif(_riscv_tool("gcc") is None, reason="no RISC-V cross toolchain available")
@pytest.mark.skipif(_riscv_tool("objcopy") is None, reason="no RISC-V objcopy available")
def test_target_crc_codegen_matches_the_reference_table(tmp_path: Path) -> None:
    """The codec runs as host code in every other test but ships as target code.

    They are materially different machine code: GCC 15's CRC loop recognition
    rewrites spi_frame_crc16's bitwise loop into a 256-entry lookup table, where
    host clang emits the loop verbatim. Nothing else in this repo ever looks at
    what actually gets flashed, so assert the emitted table directly rather than
    emulating the target.
    """
    obj = tmp_path / "spi_frame_v5f.o"
    subprocess.run(
        [
            _riscv_tool("gcc"),
            *_V5F_FLAGS,
            *HOST_WARNINGS,
            f"-I{FIRMWARE_ROOT / 'include'}",
            f"-I{FIRMWARE_ROOT / 'src'}",
            f"-I{FIRMWARE_ROOT / 'core'}",
            "-c",
            str(FIRMWARE_SRC / "spi_frame.c"),
            "-o",
            str(obj),
        ],
        check=True,
        capture_output=True,
    )
    assert obj.stat().st_size > 0

    rodata = tmp_path / "rodata.bin"
    subprocess.run(
        [_riscv_tool("objcopy"), "-O", "binary", "--only-section=.rodata", str(obj), str(rodata)],
        check=True,
        capture_output=True,
    )
    raw = rodata.read_bytes()
    if not raw:
        # A future compiler that stops table-izing is correct too: the bitwise
        # loop is already pinned against Python by the CRC differential above.
        pytest.skip("target build emitted the bitwise CRC loop, no table to check")

    assert len(raw) == 512, f"unexpected .rodata size {len(raw)}; expected a 256-entry uint16 table"
    table = [int.from_bytes(raw[index * 2 : index * 2 + 2], "little") for index in range(256)]

    def crc16_from_table(data: bytes) -> int:
        remainder = CRC_INIT
        for byte in data:
            remainder = ((remainder << 8) & 0xFFFF) ^ table[((remainder >> 8) ^ byte) & 0xFF]
        return remainder

    # Assert what the table *computes*, not its raw contents: GCC emits the
    # init-0 CCITT table, whose entries are not crc16_ccitt_false(bytes([i])).
    # Driving the standard table algorithm with it and comparing against the
    # reference is both stricter and independent of the table's convention.
    messages = [b"", RELATIVE_GOLDEN_PAYLOAD, MAP_ENTRY_GOLDEN_PAYLOAD]
    messages += [bytes([value]) for value in range(256)]
    messages += [CRC_PATTERN[:used] for used in range(len(CRC_PATTERN) + 1)]
    divergences = [
        f"{message.hex()}: table=0x{crc16_from_table(message):04X} "
        f"reference=0x{crc16_ccitt_false(message):04X}"
        for message in messages
        if crc16_from_table(message) != crc16_ccitt_false(message)
    ]
    assert not divergences, "\n".join(
        [f"{len(divergences)} of {len(messages)} messages diverge", *divergences[:10]]
    )


# Field values chosen so that every byte of both payloads is distinct from its
# neighbours and every multi-byte field has a different high and low byte: a
# transposed pair or a wrong-width field changes the bytes, rather than
# happening to agree.
_RELATIVE_VALUES = {
    "lease_generation": 0x1234,
    "map_generation": 0x5678,
    "command_sequence": 0x9ABC,
    "target_frame": 0x0DEF,
    "interface_number": 0x03,
    "endpoint_number": 0x02,
    "report_id": 0x07,
    "flags": 0x0B,
    "x": -300,
    "y": 301,
    "wheel": -7,
    "pan": 9,
    "hold_reports": 0x0102,
}
_MAP_ENTRY_VALUES = {
    "descriptor_generation": 0x2468,
    "map_generation": 0x1357,
    "entry_index": 0x05,
    "interface_number": 0x01,
    "endpoint_number": 0x04,
    "report_id": 0x09,
    "usage_page": 0x0102,
    "usage": 0x0030,
    "bit_offset": 0x0210,
    "bit_width": 0x0C,
    "flags": 0x1B,
    "logical_minimum": -2048,
    "logical_maximum": 2047,
    "report_length": 0x06,
}

_STRUCT_HARNESS = """
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "injection_wire.h"

static void emit(const uint8_t *bytes)
{{
    for (unsigned index = 0u; index < INJ_FRAME_PAYLOAD_SIZE; ++index) {{
        printf("%02x", bytes[index]);
    }}
    printf("\\n");
}}

int main(void)
{{
    uint8_t raw[INJ_FRAME_PAYLOAD_SIZE];
    inj_relative_payload_t relative;
    inj_map_entry_payload_t entry;

    memset(&relative, 0, sizeof(relative));
{relative_fields}
    memcpy(raw, &relative, sizeof(raw));
    emit(raw);

    memset(&entry, 0, sizeof(entry));
{entry_fields}
    memcpy(raw, &entry, sizeof(raw));
    emit(raw);

    emit(inj_golden_relative_payload);
    emit(inj_golden_map_entry_payload);
    return 0;
}}
"""


def _c_assignments(struct: str, values: dict[str, int]) -> str:
    return "\n".join(f"    {struct}.{name} = {value};" for name, value in values.items())


@pytest.mark.skipif(shutil.which("cc") is None, reason="no host C compiler available")
def test_c_payload_structs_match_the_python_packer(tmp_path: Path) -> None:
    """The half of the struct contract that offsetof cannot cover.

    offsetof pins where each field sits; it says nothing about its C *type*. A
    uint16_t where the schema says i16 has the same size and the same offset and
    a different sign extension. Only running the struct against the Python packer
    catches that. This also references inj_golden_relative_payload and
    inj_golden_map_entry_payload, which until now had no consumer anywhere.
    """
    source = _STRUCT_HARNESS.format(
        relative_fields=_c_assignments("relative", _RELATIVE_VALUES),
        entry_fields=_c_assignments("entry", _MAP_ENTRY_VALUES),
    )
    observed = _run_c_harness(tmp_path, source, name="structs").split()

    expected = [
        RelativePayload(**_RELATIVE_VALUES).to_bytes().hex(),
        MapEntryPayload(**_MAP_ENTRY_VALUES).to_bytes().hex(),
        RELATIVE_GOLDEN_PAYLOAD.hex(),
        MAP_ENTRY_GOLDEN_PAYLOAD.hex(),
    ]
    labels = ["RELATIVE struct", "MAP_ENTRY struct", "RELATIVE golden", "MAP_ENTRY golden"]

    divergences = [
        f"{label}: c={got} python={want}"
        for label, got, want in zip(labels, observed, expected, strict=True)
        if got != want
    ]
    assert not divergences, "\n".join(divergences)
