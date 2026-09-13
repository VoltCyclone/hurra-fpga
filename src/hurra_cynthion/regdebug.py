"""Read diagnostic registers from a Cynthion over Apollo JTAG."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Iterable

from .debug_regs import (
    LINE_STATE,
    MAGIC,
    MAP_ERROR,
    REGISTER_MAP_NAMES,
    TXN_STATE,
    TXN_STATUS,
    Register,
    RegisterMap,
    capture_register_map,
    register_map_for_name,
)

_ENUM_FIELDS = {
    "line_state": LINE_STATE,
    "dbg_state": TXN_STATE,
    "txn_status": TXN_STATUS,
    "control_status": TXN_STATUS,
    "error": MAP_ERROR,
}


class RegisterDebugError(RuntimeError):
    pass


class RegisterDebugLink:
    """Thin wrapper over ``apollo_fpga.ApolloDebugger`` for schema-aware register I/O."""

    def __init__(self, regmap: RegisterMap | None = None, *, force_offline: bool = True) -> None:
        self.regmap = regmap if regmap is not None else capture_register_map()
        try:
            from apollo_fpga import ApolloDebugger
        except ImportError as exc:  # pragma: no cover - depends on host env
            raise RegisterDebugError(
                "apollo_fpga is not installed; install the 'debug' extra "
                "(pip install -e '.[debug]') to use the register-debug tool"
            ) from exc

        try:
            self._debugger = ApolloDebugger(force_offline=force_offline)
        except Exception as exc:  # pragma: no cover - depends on hardware
            raise RegisterDebugError(f"could not connect to Apollo debugger: {exc}") from exc
        _spi, self._regs = self._debugger.create_jtag_spi(self._debugger.jtag)

    def read_raw(self, address: int) -> int:
        return self._regs.register_read(address) & 0xFFFFFFFF

    def write_raw(self, address: int, value: int) -> None:
        self._regs.register_write(address, value & 0xFFFFFFFF)

    def resolve(self, name_or_addr) -> Register | None:
        """Resolve a register name or numeric address to its Register (or None)."""
        if isinstance(name_or_addr, Register):
            return name_or_addr
        if isinstance(name_or_addr, int):
            return self.regmap.by_address(name_or_addr)
        text = str(name_or_addr)
        reg = self.regmap.get(text)
        if reg is not None:
            return reg
        try:
            return self.regmap.by_address(int(text, 0))
        except ValueError:
            return None

    def read(self, name_or_addr) -> tuple[Register | None, int, dict[str, int]]:
        """Read a register; return (Register|None, raw, decoded-fields)."""
        reg = self.resolve(name_or_addr)
        if reg is None:
            if isinstance(name_or_addr, str) and not name_or_addr.isdigit():
                raise RegisterDebugError(f"unknown register {name_or_addr!r}")
            addr = int(str(name_or_addr), 0)
            return None, self.read_raw(addr), {}
        raw = self.read_raw(reg.address)
        return reg, raw, reg.decode(raw)

    def write(self, name_or_addr, value: int) -> None:
        reg = self.resolve(name_or_addr)
        if reg is None:
            self.write_raw(int(str(name_or_addr), 0), value)
            return
        if not reg.writable:
            raise RegisterDebugError(f"register {reg.name!r} is read-only")
        self.write_raw(reg.address, value)

    def check_magic(self) -> tuple[int, bool]:
        raw = self.read_raw(self.regmap.magic_address)
        return raw, raw == MAGIC

    def read_memory(self, name: str) -> list[int]:
        """Dump an addr/data memory-readback buffer as a list of byte values."""
        addr_reg = self.regmap.get(f"{name}_addr")
        data_reg = self.regmap.get(f"{name}_data")
        if addr_reg is None or data_reg is None:
            raise RegisterDebugError(f"no memory named {name!r} in the register map")
        out = []
        for i in range(addr_reg.depth or 0):
            self.write_raw(addr_reg.address, i)
            out.append(self.read_raw(data_reg.address) & 0xFF)
        return out

    def status_snapshot(self, names: Iterable[str] | None = None) -> dict[str, dict[str, int]]:
        regs = (
            [self.regmap[n] for n in names] if names is not None else self.regmap.status_registers()
        )
        return {r.name: r.decode(self.read_raw(r.address)) for r in regs}

    def close(self) -> None:
        close = getattr(self._debugger, "close", None)
        if callable(close):  # pragma: no cover - depends on hardware
            close()


def format_fields(reg: Register, decoded: dict[str, int]) -> str:
    if reg.kind == "magic":
        raw = decoded.get("value", 0)
        return f"0x{raw:08x} ({'OK' if raw == MAGIC else 'MISMATCH'})"
    parts = []
    for key, val in decoded.items():
        enum = _ENUM_FIELDS.get(key)
        if enum is not None and val in enum:
            parts.append(f"{key}={val}({enum[val]})")
        else:
            parts.append(f"{key}={val}")
    return " ".join(parts)


def _connect(args) -> RegisterDebugLink:
    return RegisterDebugLink(
        register_map_for_name(args.map), force_offline=not args.no_force_offline
    )


def _cmd_magic(link: RegisterDebugLink, args) -> int:
    raw, ok = link.check_magic()
    print(f"magic = 0x{raw:08x} ({'OK' if ok else 'MISMATCH — wrong bitstream/register map'})")
    return 0 if ok else 1


def _cmd_read(link: RegisterDebugLink, args) -> int:
    reg, raw, decoded = link.read(args.register)
    label = reg.name if reg is not None else args.register
    print(f"{label} = 0x{raw:08x}")
    if reg is not None:
        print(f"  {format_fields(reg, decoded)}")
    return 0


def _cmd_write(link: RegisterDebugLink, args) -> int:
    value = int(args.value, 0)
    link.write(args.register, value)
    print(f"wrote 0x{value:08x} to {args.register}")
    return 0


def _cmd_dump(link: RegisterDebugLink, args) -> int:
    for reg in link.regmap.status_registers():
        raw = link.read_raw(reg.address)
        decoded = reg.decode(raw)
        print(f"[{reg.address:2d}] {reg.name:22s} 0x{raw:08x}  {format_fields(reg, decoded)}")
    return 0


def _cmd_mem(link: RegisterDebugLink, args) -> int:
    data = link.read_memory(args.name)
    print(f"{args.name} ({len(data)} bytes):")
    for off in range(0, len(data), 16):
        chunk = data[off : off + 16]
        print(f"  {off:3d}: " + " ".join(f"{b:02x}" for b in chunk))
    return 0


def _cmd_watch(link: RegisterDebugLink, args) -> int:
    names = args.registers or None
    print(
        f"watching {'all status registers' if names is None else ', '.join(names)} "
        f"every {args.interval}s (Ctrl-C to stop)"
    )
    previous: dict[str, dict[str, int]] = {}
    try:
        while True:
            snapshot = link.status_snapshot(names)
            if snapshot != previous:
                stamp = time.strftime("%H:%M:%S")
                for name, decoded in snapshot.items():
                    if previous.get(name) != decoded:
                        reg = link.regmap[name]
                        print(f"{stamp} {name:22s} {format_fields(reg, decoded)}")
                print("-" * 40)
                previous = snapshot
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hurra-regdebug",
        description="Read/write hurra-cynthion JTAG debug registers over Apollo.",
    )
    parser.add_argument(
        "--no-force-offline",
        action="store_true",
        help="do not force the FPGA offline before connecting (default: force offline)",
    )
    parser.add_argument(
        "--map",
        choices=REGISTER_MAP_NAMES,
        default="capture",
        help="register schema exposed by the loaded diagnostic (default: capture)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("magic", help="sanity-check the register readback path")

    p_read = sub.add_parser("read", help="read one register (decoded)")
    p_read.add_argument("register", help="register name or address (0x.. ok)")

    p_write = sub.add_parser("write", help="write one register (name or address)")
    p_write.add_argument("register", help="register name or address")
    p_write.add_argument("value", help="value to write (0x.. / decimal)")

    sub.add_parser("dump", help="read and decode all status registers")

    p_mem = sub.add_parser("mem", help="dump a memory-readback buffer as bytes")
    p_mem.add_argument("name", help="memory name (e.g. rx, tx, ls)")

    p_watch = sub.add_parser("watch", help="live-poll registers and print on change")
    p_watch.add_argument("registers", nargs="*", help="register names (default: all status)")
    p_watch.add_argument("--interval", type=float, default=0.5, help="poll interval seconds")

    return parser


_COMMANDS = {
    "magic": _cmd_magic,
    "read": _cmd_read,
    "write": _cmd_write,
    "dump": _cmd_dump,
    "mem": _cmd_mem,
    "watch": _cmd_watch,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    link = None
    try:
        link = _connect(args)
        return _COMMANDS[args.command](link, args)
    except RegisterDebugError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if link is not None:
            link.close()


if __name__ == "__main__":
    raise SystemExit(main())
