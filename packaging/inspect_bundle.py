#!/usr/bin/env python3
"""Check a built bundle will run on the Macs it is meant for.

    python packaging/inspect_bundle.py build/dist/elstin-backend [--max-macos 15.0]

Reads the header of every Mach-O file (standard library only) and fails if

* any of them needs a newer macOS than --max-macos: the app promises "macOS 15 and
  up", and one dylib built on a newer SDK breaks that promise only on the Macs that
  matter, and only when that file loads;
* any of them links to a library outside the bundle and the system's own: a path
  into Homebrew or the developer's home works here and nowhere else;
* any of them is not arm64.

Prints the oldest macOS that satisfies every file, so the minimum is measured, not
assumed.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

MH_MAGIC_64 = 0xFEEDFACF
FAT_MAGIC = 0xCAFEBABE
CPU_TYPE_ARM64 = 0x0100000C
LC_BUILD_VERSION = 0x32
LC_VERSION_MIN_MACOSX = 0x24
LC_LOAD_DYLIB = {0xC, 0x80000018, 0x8000001F, 0x80000023}  # load, weak, reexport, upward
ALLOWED_LINKS = ("@rpath/", "@loader_path/", "@executable_path/", "/usr/lib/", "/System/Library/")


def _version(packed: int) -> tuple[int, int]:
    return (packed >> 16) & 0xFFFF, (packed >> 8) & 0xFF


def read_macho(path: Path):
    """(cpu type, oldest macOS it needs or None, linked libraries) — or None if not Mach-O."""
    try:
        with path.open("rb") as f:
            head = f.read(32)
            if len(head) < 32:
                return None
            magic = struct.unpack("<I", head[:4])[0]
            if magic == FAT_MAGIC or struct.unpack(">I", head[:4])[0] == FAT_MAGIC:
                return ("fat", None, [])
            if magic != MH_MAGIC_64:
                return None
            _, cputype, _, _, ncmds, sizeofcmds, _, _ = struct.unpack("<IiiIIIII", head)
            commands = f.read(sizeofcmds)
    except OSError:
        return None
    minos, links, offset = None, [], 0
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", commands, offset)
        if cmd == LC_BUILD_VERSION:
            _platform, packed, _sdk, _ntools = struct.unpack_from("<IIII", commands, offset + 8)
            minos = _version(packed)
        elif cmd == LC_VERSION_MIN_MACOSX:
            minos = _version(struct.unpack_from("<I", commands, offset + 8)[0])
        elif cmd in LC_LOAD_DYLIB:
            name_offset = struct.unpack_from("<I", commands, offset + 8)[0]
            links.append(commands[offset + name_offset: offset + size].split(b"\0", 1)[0].decode())
        offset += size
    return cputype, minos, links


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--max-macos", default="15.0", help="the oldest macOS the app claims to support (default 15.0)")
    args = parser.parse_args()
    ceiling = tuple(int(part) for part in args.max_macos.split("."))[:2]

    count, oldest_needed, problems = 0, (0, 0), []
    for path in sorted(p for p in args.bundle.rglob("*") if p.is_file() and not p.is_symlink()):
        info = read_macho(path)
        if info is None:
            continue
        count += 1
        cputype, minos, links = info
        rel = path.relative_to(args.bundle)
        if cputype == "fat":
            problems.append(f"{rel}: a universal binary; this build is arm64 only")
            continue
        if cputype != CPU_TYPE_ARM64:
            problems.append(f"{rel}: not arm64")
        if minos:
            oldest_needed = max(oldest_needed, minos)
            if minos > ceiling:
                problems.append(f"{rel}: needs macOS {minos[0]}.{minos[1]}, above the promised {args.max_macos}")
        for link in links:
            if not link.startswith(ALLOWED_LINKS):
                problems.append(f"{rel}: links to {link}")

    print(f"{count} Mach-O files; the oldest macOS that runs all of them: {oldest_needed[0]}.{oldest_needed[1]}")
    for problem in problems:
        print(f"  PROBLEM {problem}")
    return 1 if problems or count == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
