from __future__ import annotations

import io
import json
import os
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from srhd_modkit.cli import main
from srhd_modkit.rangers_exe import (
    HOOKS_SCHEMA,
    KNOWN_BUILDS,
    find_pattern,
    find_symbol,
    hooks_from_manifest,
    hooks_from_source,
    identify_executable,
    load_pe,
    load_symbols,
    make_signature,
    parse_pattern,
    port_address,
    probe_hooks,
    symbol_at,
    volatile_mask,
)

BASE = 0x400000
TEXT_RVA = 0x1000
DATA_RVA = 0x20000


def _pe(code: bytes, data: bytes, relocs: list[int], *, timestamp: int = 0x6A7AFC5C) -> bytes:
    """Minimal PE32 with .text, .data and .reloc; ``relocs`` are RVAs."""

    def align(value: int, to: int = 0x200) -> int:
        return (value + to - 1) // to * to

    pages: dict[int, list[int]] = {}
    for rva in relocs:
        pages.setdefault(rva & ~0xFFF, []).append(rva & 0xFFF)
    reloc = bytearray()
    for page, entries in sorted(pages.items()):
        block = b"".join(struct.pack("<H", (3 << 12) | entry) for entry in entries)
        if len(entries) % 2:
            block += b"\0\0"
        reloc += struct.pack("<II", page, 8 + len(block)) + block
    reloc_rva = 0x40000
    sections = [
        (b".text", TEXT_RVA, code, 0x60000020),
        (b".data", DATA_RVA, data, 0xC0000040),
        (b".reloc", reloc_rva, bytes(reloc), 0x42000040),
    ]
    header_size = 0x400
    raw = header_size
    table = bytearray()
    body = bytearray()
    for name, rva, content, flags in sections:
        size = align(len(content)) or 0x200
        table += struct.pack("<8sIIIIIIHHI", name, max(len(content), 1), rva, size, raw, 0, 0, 0, 0, flags)
        body += content.ljust(size, b"\0")
        raw += size
    optional = bytearray(224)
    struct.pack_into("<HBB", optional, 0, 0x10B, 2, 25)
    struct.pack_into("<I", optional, 16, TEXT_RVA)
    struct.pack_into("<III", optional, 28, BASE, 0x1000, 0x200)
    struct.pack_into("<II", optional, 56, 0x50000, header_size)
    struct.pack_into("<I", optional, 92, 16)
    struct.pack_into("<II", optional, 96 + 5 * 8, reloc_rva, len(reloc))
    header = bytearray(b"MZ".ljust(0x40, b"\0"))
    struct.pack_into("<I", header, 0x3C, 0x40)
    header += b"PE\0\0" + struct.pack("<HHIIIHH", 0x14C, len(sections), timestamp, 0, 0, 224, 0x0102)
    header += optional + table
    return bytes(header.ljust(header_size, b"\0") + body)


def _function(data_va: int, *, legacy: bool, filler: int = 0) -> tuple[bytes, list[int]]:
    """A routine that loads a string address and stores two constants."""

    code = bytearray(b"\x55\x8b\xec\x83\xc4\xf0")
    if legacy:
        code += b"\x89\x45\xf8\x8b\x45\xf8" * 3
    else:
        code += b"\x8b\xd8\x8b\xf2"
    reloc_at = len(code) + 1
    code += b"\xb8" + struct.pack("<I", data_va)  # mov eax, offset string
    code += b"\xc7\x43\x6c\x00\x00\x80\x3f"  # mov [ebx+0x6C], 1.0f
    code += b"\xc7\x83\x60\x01\x00\x00\x14\x00\x00\x00"  # mov [ebx+0x160], 20
    code += b"\xe8\x10\x00\x00\x00"  # call +0x10
    code += bytes([filler]) * 8
    code += b"\x5d\xc3"
    return bytes(code), [reloc_at]


def _build(path: Path, *, legacy: bool, shift: int, rangers: bool = True, extra: bytes = b"") -> int:
    """Write a fake Rangers.exe; return the VA of the routine."""

    data = bytearray(b"\0" * 0x40)
    string_offset = len(data)
    data += "Script file incorrect version".encode("utf-16-le") + b"\0\0"
    if rangers:
        data += b"\x07TGalaxy\x07TScript"
    data += extra
    routine, relocs = _function(BASE + DATA_RVA + string_offset, legacy=legacy)
    padding = (b"\x89\x45\xf8" if legacy else b"\x90\x90\x90") * 400
    code = padding[:shift] + routine + padding
    routine_rva = TEXT_RVA + shift
    path.write_bytes(_pe(code, bytes(data), [routine_rva + r for r in relocs]))
    return BASE + routine_rva


class PatternTests(unittest.TestCase):
    def test_pattern_spellings(self) -> None:
        spaced = parse_pattern("8B 45 ?? 89")
        packed = parse_pattern("8B45??89")
        c_list = parse_pattern("{0x8B, 0x45, -1, 0x89}")
        self.assertEqual(spaced, packed)
        self.assertEqual(spaced, c_list)
        self.assertEqual(spaced.text(), "8B 45 ?? 89")
        self.assertEqual(spaced.c_array(), "0x8B, 0x45, -1, 0x89")
        for bad in ("", "?? ??", "8B zz"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_pattern(bad)

    def test_source_arrays_masks_and_profiles(self) -> None:
        source = """
        // const int kCommentedPattern[] = { 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08 };
        const int kSectorPattern[] = { 0x8B, 0x45, 0xF8, -1, 0xC7, 0x40, 0x6C, 0x00 };
        const int kSectorOptimizedPattern[] = { 0xC7, 0x43, 0x6C, 0x00, 0x00, 0x80, 0x3F, -1 };
        const int kRetryPattern[] = { 0x81, 0xFB, 0xE8, 0x03, 0x00, 0x00, 0x7E, -1 };
        const BYTE kRefreshSignature[] = { 0x55, 0x8B, 0xEC, 0x83, 0xC4, 0x00, 0x53, 0x56 };
        const char kRefreshMask[] = "xxxxx?" "xx";
        const BYTE replacement[] = { 0x90, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90 };
        const BYTE kProloguePattern[] = { 0x55, 0x8B, 0xEC };
        """
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "runtime_scan.cpp"
            path.write_text(source, encoding="utf-8")
            hooks = {hook.name: hook for hook in hooks_from_source(path)}
            self.assertEqual(
                set(hooks),
                {"kSectorPattern", "kSectorOptimizedPattern", "kRetryPattern", "kRefreshSignature"},
            )
            self.assertEqual(hooks["kSectorPattern"].profile, "legacy")
            self.assertEqual(hooks["kSectorOptimizedPattern"].profile, "optimized")
            self.assertEqual(hooks["kRetryPattern"].profile, "any")
            self.assertEqual(hooks["kRefreshSignature"].pattern.text(), "55 8B EC 83 C4 ?? 53 56")
            self.assertIn("replacement", {h.name for h in hooks_from_source(path, all_arrays=True)})


class ExecutableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_identify_profiles_and_foreign_files(self) -> None:
        legacy = self.root / "legacy.exe"
        optimized = self.root / "optimized.exe"
        foreign = self.root / "other.exe"
        fpc = self.root / "fpc.exe"
        _build(legacy, legacy=True, shift=0x40)
        _build(optimized, legacy=False, shift=0x40)
        _build(foreign, legacy=True, shift=0x40, rangers=False)
        _build(fpc, legacy=True, shift=0x40, extra=b"FPC 3.2.2 [2021/05/16] for i386")
        self.assertEqual(identify_executable(legacy)["profile"], "legacy")
        self.assertEqual(identify_executable(optimized)["profile"], "optimized")
        self.assertEqual(identify_executable(foreign)["profile"], "unknown")
        self.assertEqual(identify_executable(fpc)["compiler"], "fpc")
        info = identify_executable(legacy)
        self.assertEqual(info["build_date"], "2026-08-11")
        self.assertIsNone(info["known_build"])
        self.assertEqual(info["relocations"], 1)
        bad = self.root / "bad.exe"
        bad.write_bytes(b"not a pe file at all, sorry" * 4)
        with self.assertRaises(ValueError):
            identify_executable(bad)

    def test_known_build_table_is_well_formed(self) -> None:
        for sha, item in KNOWN_BUILDS.items():
            self.assertRegex(sha, r"^[0-9a-f]{64}$")
            self.assertIn(item["family"], {"release", "patched", "prerelease"})

    def test_scan_only_code_and_mask_addresses(self) -> None:
        path = self.root / "a.exe"
        routine = _build(path, legacy=False, shift=0x80)
        image = load_pe(path)
        string = parse_pattern(" ".join(f"{b:02X}" for b in "Script".encode("utf-16-le")))
        self.assertEqual(find_pattern(image, string), [])
        self.assertEqual(len(find_pattern(image, string, ranges=[(0, len(image.data))])), 1)
        start = image.va_to_offset(routine)
        keep = volatile_mask(image, start, 40)
        mov = start + 10  # after prologue (6) and optimized setup (4)
        self.assertEqual(image.data[mov], 0xB8)
        self.assertEqual(list(keep[mov - start + 1:mov - start + 5]), [0, 0, 0, 0])
        call = image.data.index(b"\xe8\x10\x00\x00\x00", start)
        self.assertEqual(list(keep[call - start + 1:call - start + 5]), [0, 0, 0, 0])
        self.assertEqual(keep[call - start], 1)

    def test_signature_is_unique_and_reloc_safe(self) -> None:
        path = self.root / "a.exe"
        routine = _build(path, legacy=False, shift=0x80)
        result = make_signature(load_pe(path), routine)
        self.assertTrue(result["unique"])
        pattern = parse_pattern(result["pattern"])
        moved = self.root / "b.exe"
        moved_routine = _build(moved, legacy=False, shift=0x1F0)
        image = load_pe(moved)
        self.assertEqual(
            [image.offset_to_va(o) for o in find_pattern(image, pattern)], [moved_routine]
        )

    def test_port_same_profile_is_exact(self) -> None:
        old = self.root / "old.exe"
        new = self.root / "new.exe"
        site = _build(old, legacy=False, shift=0x40) + 6
        moved = _build(new, legacy=False, shift=0x2C4) + 6
        result = port_address(old, site, new)
        self.assertEqual(result["method"], "exact-window")
        self.assertEqual(result["va"], f"{moved:#010x}")
        self.assertTrue(result["signature"]["unique"])

    def test_port_across_profiles_points_at_the_routine(self) -> None:
        old = self.root / "old.exe"
        new = self.root / "new.exe"
        site = _build(old, legacy=True, shift=0x40)
        target = _build(new, legacy=False, shift=0x300)
        result = port_address(old, site + 6, new)
        self.assertNotIn("va", result)
        self.assertIn(result["method"], {"approximate", "references"})
        top = result["candidates"][0]
        begin, end = (int(value, 16) for value in top["region"].split("-"))
        self.assertTrue(begin <= target + 10 <= end, result)
        self.assertEqual(top["function_start_guess"], f"{target:#010x}")
        self.assertTrue(any(ref["kind"] == "data" for ref in result["references"]))

    def test_probe_reports_profile_aware_status(self) -> None:
        legacy = self.root / "legacy.exe"
        optimized = self.root / "optimized.exe"
        _build(legacy, legacy=True, shift=0x40)
        _build(optimized, legacy=False, shift=0x40)
        manifest = self.root / "hooks.json"
        manifest.write_text(json.dumps({
            "schema": HOOKS_SCHEMA,
            "hooks": [
                {"name": "store", "pattern": "C7 43 6C 00 00 80 3F", "profile": "any"},
                {"name": "legacy-frame", "pattern": "89 45 F8 8B 45 F8 89 45 F8 8B 45 F8 89 45 F8 8B 45 F8 B8",
                 "profile": "legacy"},
                {"name": "never", "pattern": "DE AD BE EF 00 11 22 33", "profile": "optimized"},
            ],
        }), encoding="utf-8")
        report = probe_hooks(hooks_from_manifest(manifest), [legacy, optimized])
        rows = {
            (Path(exe["exe"]).name, row["name"]): row["status"]
            for exe in report["executables"]
            for row in exe["hooks"]
        }
        self.assertEqual(rows[("legacy.exe", "store")], "ok")
        self.assertEqual(rows[("optimized.exe", "store")], "ok")
        self.assertEqual(rows[("legacy.exe", "legacy-frame")], "ok")
        self.assertEqual(rows[("optimized.exe", "legacy-frame")], "other-profile")
        self.assertEqual(rows[("optimized.exe", "never")], "missing")
        self.assertEqual(report["failures"], 1)

    def test_cli_identify_scan_and_port(self) -> None:
        old = self.root / "old.exe"
        new = self.root / "new.exe"
        site = _build(old, legacy=False, shift=0x40) + 6
        _build(new, legacy=False, shift=0x100)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["exe", "identify", str(self.root), "--json"]), 0)
        identities = json.loads(output.getvalue())["executables"]
        self.assertEqual({item["profile"] for item in identities}, {"optimized"})
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["exe", "scan", str(old), str(new), "--pattern", "{0xC7, 0x43, 0x6C, -1}", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual([row["matches"] for row in json.loads(output.getvalue())["results"]], [1, 1])
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["exe", "port", str(old), str(new), "--va", f"{site:#x}", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["method"], "exact-window")


class SymbolTests(unittest.TestCase):
    def test_symbols_keep_every_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "native_ranges.json"
            path.write_text(json.dumps({"ranges": {
                "4202496": {"name": "aScript.TScript.LoadFromBuffer",
                            "chunks": [{"start": 4202496, "end": 4202600},
                                       {"start": 4190000, "end": 4190040}]},
                "4203000": {"name": "aScript.TScript.LoadFromFile",
                            "chunks": [{"start": 4203000, "end": 4203100}]},
            }}), encoding="utf-8")
            symbols = load_symbols(path)
            self.assertEqual(find_symbol(symbols, "aScript.TScript.LoadFromBuffer")[0][1], 4202496)
            self.assertEqual(len(find_symbol(symbols, "TScript.Load")), 2)
            self.assertEqual(symbol_at(symbols, 4190010), ("aScript.TScript.LoadFromBuffer", 4202496))
            self.assertIsNone(symbol_at(symbols, 4300000))


@unittest.skipUnless(os.environ.get("SRHD_RANGERS_EXES"), "SRHD_RANGERS_EXES не задан")
class RealExecutableTests(unittest.TestCase):
    """Optional: SRHD_RANGERS_EXES=<folder with Rangers.exe builds>."""

    def test_known_builds_get_their_profile(self) -> None:
        folder = Path(os.environ["SRHD_RANGERS_EXES"])
        for path in sorted(folder.glob("*.exe")):
            info = identify_executable(path)
            known = KNOWN_BUILDS.get(info["sha256"])
            if known is None:
                continue
            with self.subTest(exe=path.name):
                expected = "optimized" if known["family"] == "prerelease" else "legacy"
                self.assertEqual(info["profile"], expected)


if __name__ == "__main__":
    unittest.main()
