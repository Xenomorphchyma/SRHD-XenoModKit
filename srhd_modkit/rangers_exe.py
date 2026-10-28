"""Identify Rangers.exe builds and carry native hook sites between them.

Everything here reads the PE file only; nothing is loaded or executed.

Two facts about Rangers.exe 2.1.2500 make hook work mechanical:

* every known build ships a ``.reloc`` table, so each absolute address inside
  code is known exactly and can be wildcarded in a signature without
  guessing;
* builds come in two code-generation profiles. The Steam/GOG release and its
  patched variants are compiled with Delphi ``$O-`` (locals live in
  ``[ebp-xx]``); the 2026-10 prerelease builds use ``$O+`` (``Self`` and
  locals in EBX/ESI/EDI). A hook signature written for one profile does not
  match the other, and functions move between builds of the same profile.

``port_address`` therefore tries, in order: the same bytes around the hook
site, unique byte chunks voting for one displacement (same profile, moved
code), and finally the data the surrounding code references, such as
string constants (works across profiles, gives candidate functions).
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

EXE_SCHEMA = "srhd-modkit-exe-v1"
HOOKS_SCHEMA = "srhd-modkit-exe-hooks-v1"

_IMAGE_SCN_MEM_EXECUTE = 0x20000000
_IMAGE_SCN_CNT_CODE = 0x00000020
_IMAGE_REL_BASED_HIGHLOW = 3

# ``mov r32,[ebp-disp8]`` / ``mov [ebp-disp8],r32`` per KB of .text. Measured
# on every build we have: 47.7 for all $O- builds, 14.5 for both $O+ ones.
_PROFILE_EBP_THRESHOLD = 30.0
_EBP_LOCAL = re.compile(rb"[\x89\x8b][\x45\x4d\x55\x5d\x75\x7d][\xb0-\xff]")

# Builds identified byte for byte. Labels only state what is known about
# each file; an unknown build is still classified by profile.
KNOWN_BUILDS: dict[str, dict[str, str]] = {
    "f00343363043cbb9c2d10c1216e9f4c37b5eaa5e4d58f37a45284cae528fdc1d": {
        "label": "Release 2.1.2500 (Steam/GOG), сборка 2026-08-11",
        "family": "release",
    },
    "ef7a9520b250b09214bf2bc7fffabea80e7b4047550cfe5101618730808111bb": {
        "label": "Release 2.1.2500, сборка 2026-05-28 (Rangers_backup)",
        "family": "release",
    },
    "8acd60dca9ae8e4a1dc7aeb676833617ec185281f6f0ca94aa0a2b6966ea95f6": {
        "label": "Сборка 2026-05-28 с секцией .srhd (XenoStable 1.4.0)",
        "family": "patched",
    },
    "9c13ab1115fb05a5fe73e32d8d1591b60e0b0144c9f4ee70e3ed680e01875264": {
        "label": "Сборка 2026-05-28 с секцией .srhd (XenoStable 1.4.1)",
        "family": "patched",
    },
    "179682b579bc7dfef30d0f647121ef522dec8580a2af37896608d4d6744d0ce5": {
        "label": "Сборка 2026-05-28 с секцией .srhd (XenoStable 1.4.2)",
        "family": "patched",
    },
    "e43a4b15503c705eebe2d8ce5778c2a2fb090db515a3fd86d319e5c5aa692ebf": {
        "label": "Prerelease 2026-10-06 (Space Rangers Universe - Update)",
        "family": "prerelease",
    },
    "c98a65bb9a9c4a629cbeb854aa8bb52c61a2d0062de468dac9b0384fabc62f2d": {
        "label": "Prerelease 2026-10-27 (Universe Redux Fixes)",
        "family": "prerelease",
    },
}


@dataclass(frozen=True)
class Section:
    name: str
    rva: int
    virtual_size: int
    raw_offset: int
    raw_size: int
    characteristics: int

    @property
    def executable(self) -> bool:
        return bool(self.characteristics & (_IMAGE_SCN_MEM_EXECUTE | _IMAGE_SCN_CNT_CODE))

    @property
    def file_end(self) -> int:
        return self.raw_offset + min(self.raw_size, max(self.virtual_size, self.raw_size))


@dataclass
class PeImage:
    path: Path
    data: bytes
    image_base: int
    timestamp: int
    sections: list[Section]
    reloc_offsets: list[int] = field(default_factory=list)
    _reloc_set: set[int] = field(default_factory=set, repr=False)
    _sha256: str | None = field(default=None, repr=False)

    @property
    def sha256(self) -> str:
        if self._sha256 is None:
            self._sha256 = hashlib.sha256(self.data).hexdigest()
        return self._sha256

    def section_at_offset(self, offset: int) -> Section | None:
        for section in self.sections:
            if section.raw_offset <= offset < section.raw_offset + section.raw_size:
                return section
        return None

    def va_to_offset(self, va: int) -> int:
        rva = va - self.image_base
        for section in self.sections:
            span = max(section.virtual_size, section.raw_size)
            if section.rva <= rva < section.rva + span:
                delta = rva - section.rva
                if delta >= section.raw_size:
                    raise ValueError(f"адрес {va:#010x} лежит в неинициализированной части {section.name}")
                return section.raw_offset + delta
        raise ValueError(f"адрес {va:#010x} не принадлежит ни одной секции")

    def offset_to_va(self, offset: int) -> int:
        section = self.section_at_offset(offset)
        if section is None:
            raise ValueError(f"смещение {offset:#x} вне секций")
        return self.image_base + section.rva + offset - section.raw_offset

    def executable_ranges(self) -> list[tuple[int, int]]:
        return [
            (section.raw_offset, section.raw_offset + section.raw_size)
            for section in self.sections
            if section.executable and section.raw_size
        ]

    def is_relocated(self, offset: int) -> bool:
        return offset in self._reloc_set

    def relocs_between(self, start: int, end: int) -> list[int]:
        return self.reloc_offsets[bisect_left(self.reloc_offsets, start):bisect_right(self.reloc_offsets, end - 1)]

    def dword(self, offset: int) -> int:
        return struct.unpack_from("<I", self.data, offset)[0]


def load_pe(path: str | Path) -> PeImage:
    path = Path(path)
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError(f"{path} не является PE-файлом (нет MZ)")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise ValueError(f"{path} не является PE-файлом (нет сигнатуры PE)")
    machine, section_count, timestamp = struct.unpack_from("<HHI", data, pe_offset + 4)
    optional_size = struct.unpack_from("<H", data, pe_offset + 20)[0]
    optional = pe_offset + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    if machine != 0x14C or magic != 0x10B:
        raise ValueError(f"{path} не является 32-битным x86 PE (Rangers.exe — PE32)")
    image_base = struct.unpack_from("<I", data, optional + 28)[0]
    directory_count = struct.unpack_from("<I", data, optional + 92)[0]
    sections: list[Section] = []
    table = optional + optional_size
    for index in range(section_count):
        entry = table + index * 40
        name = data[entry:entry + 8].rstrip(b"\0").decode("ascii", errors="replace")
        virtual_size, rva, raw_size, raw_offset = struct.unpack_from("<IIII", data, entry + 8)
        characteristics = struct.unpack_from("<I", data, entry + 36)[0]
        raw_size = max(0, min(raw_size, len(data) - raw_offset))
        sections.append(Section(name, rva, virtual_size, raw_offset, raw_size, characteristics))
    image = PeImage(path, data, image_base, timestamp, sections)
    if directory_count > 5:
        reloc_rva, reloc_size = struct.unpack_from("<II", data, optional + 96 + 5 * 8)
        if reloc_rva and reloc_size:
            image.reloc_offsets = _read_relocations(image, reloc_rva, reloc_size)
            image._reloc_set = set(image.reloc_offsets)
    return image


def _read_relocations(image: PeImage, rva: int, size: int) -> list[int]:
    try:
        cursor = image.va_to_offset(image.image_base + rva)
    except ValueError:
        return []
    end = min(len(image.data), cursor + size)
    offsets: list[int] = []
    while cursor + 8 <= end:
        page, block = struct.unpack_from("<II", image.data, cursor)
        if block < 8:
            break
        for item in range(cursor + 8, min(cursor + block, end) - 1, 2):
            entry = struct.unpack_from("<H", image.data, item)[0]
            if entry >> 12 != _IMAGE_REL_BASED_HIGHLOW:
                continue
            try:
                offsets.append(image.va_to_offset(image.image_base + page + (entry & 0xFFF)))
            except ValueError:
                continue
        cursor += block
    offsets.sort()
    return offsets


# --------------------------------------------------------------------------
# identification


def identify_executable(path: str | Path) -> dict[str, Any]:
    image = load_pe(path)
    text = next((s for s in image.sections if s.name == ".text"), None)
    code = image.data[text.raw_offset:text.raw_offset + text.raw_size] if text else b""
    ebp_per_kb = len(_EBP_LOCAL.findall(code)) / (len(code) / 1024) if code else 0.0
    is_fpc = b"FPC 3." in image.data or b"FPC 2." in image.data
    is_rangers = b"TGalaxy" in image.data and b"TScript" in image.data
    if is_fpc:
        profile = "fpc"
    elif not is_rangers or not code:
        profile = "unknown"
    else:
        profile = "optimized" if ebp_per_kb < _PROFILE_EBP_THRESHOLD else "legacy"
    known = KNOWN_BUILDS.get(image.sha256)
    built = datetime.fromtimestamp(image.timestamp, tz=timezone.utc)
    notes: list[str] = []
    if any(section.name == ".srhd" for section in image.sections):
        notes.append("есть дополнительная секция .srhd: exe пропатчен")
    if not image.reloc_offsets:
        notes.append("нет .reloc: абсолютные адреса в сигнатурах маскируются эвристически")
    return {
        "schema": EXE_SCHEMA,
        "path": str(image.path.resolve()),
        "sha256": image.sha256,
        "size": len(image.data),
        "timestamp": f"{image.timestamp:#010x}",
        "build_date": built.strftime("%Y-%m-%d"),
        "image_base": f"{image.image_base:#010x}",
        "rangers": is_rangers,
        "compiler": "fpc" if is_fpc else "delphi" if is_rangers else "unknown",
        "profile": profile,
        "profile_metric": {"ebp_locals_per_kb": round(ebp_per_kb, 1), "threshold": _PROFILE_EBP_THRESHOLD},
        "known_build": known["label"] if known else None,
        "family": known["family"] if known else None,
        "text_size": len(code),
        "relocations": len(image.reloc_offsets),
        "sections": [
            {"name": s.name, "rva": f"{s.rva:#x}", "size": s.raw_size, "executable": s.executable}
            for s in image.sections
        ],
        "notes": notes,
    }


# --------------------------------------------------------------------------
# patterns


@dataclass(frozen=True)
class BytePattern:
    values: bytes
    mask: bytes  # 1 = byte must match

    def __len__(self) -> int:
        return len(self.values)

    @property
    def fixed(self) -> int:
        return sum(self.mask)

    def text(self) -> str:
        return " ".join(f"{v:02X}" if m else "??" for v, m in zip(self.values, self.mask))

    def c_array(self) -> str:
        return ", ".join(f"0x{v:02X}" if m else "-1" for v, m in zip(self.values, self.mask))


_TOKEN = re.compile(r"0[xX][0-9A-Fa-f]{1,2}|-1|\?\?|\?|[0-9A-Fa-f]{2}")


def parse_pattern(text: str) -> BytePattern:
    """Accept ``8B 45 ?? 89``, ``8B45??89`` or a C list ``{0x8B, 0x45, -1}``."""

    cleaned = text.strip().strip("{}").replace(",", " ")
    values = bytearray()
    mask = bytearray()
    position = 0
    for match in _TOKEN.finditer(cleaned):
        gap = cleaned[position:match.start()].strip()
        if gap:
            raise ValueError(f"непонятный фрагмент сигнатуры: {gap!r}")
        token = match.group()
        if token in ("??", "?", "-1"):
            values.append(0)
            mask.append(0)
        else:
            values.append(int(token, 16))
            mask.append(1)
        position = match.end()
    if cleaned[position:].strip():
        raise ValueError(f"непонятный фрагмент сигнатуры: {cleaned[position:].strip()!r}")
    if not values:
        raise ValueError("пустая сигнатура")
    if not any(mask):
        raise ValueError("сигнатура состоит только из масок")
    return BytePattern(bytes(values), bytes(mask))


def _longest_fixed_run(pattern: BytePattern) -> tuple[int, int]:
    best_start = best_length = start = length = 0
    for index, fixed in enumerate(pattern.mask):
        if fixed:
            if length == 0:
                start = index
            length += 1
            if length > best_length:
                best_start, best_length = start, length
        else:
            length = 0
    return best_start, best_length


def find_pattern(
    image: PeImage,
    pattern: BytePattern,
    *,
    ranges: Sequence[tuple[int, int]] | None = None,
    limit: int | None = None,
) -> list[int]:
    """File offsets where ``pattern`` matches inside ``ranges`` (code by default)."""

    data = image.data
    anchor_start, anchor_length = _longest_fixed_run(pattern)
    anchor = pattern.values[anchor_start:anchor_start + anchor_length]
    checks = [(i, v) for i, (v, m) in enumerate(zip(pattern.values, pattern.mask)) if m]
    found: list[int] = []
    for begin, end in ranges if ranges is not None else image.executable_ranges():
        cursor = begin + anchor_start
        last = end - len(pattern) + anchor_start
        while cursor <= last:
            hit = data.find(anchor, cursor, last + anchor_length)
            if hit < 0:
                break
            origin = hit - anchor_start
            if all(data[origin + i] == v for i, v in checks):
                found.append(origin)
                if limit is not None and len(found) >= limit:
                    return found
            cursor = hit + 1
    return found


def volatile_mask(image: PeImage, start: int, length: int) -> bytearray:
    """1 for bytes that stay the same when the code moves, 0 for addresses.

    Absolute addresses come straight from ``.reloc``. Relative branch
    targets have no table, so the four bytes after ``E8``/``E9`` and
    ``0F 80..8F`` are masked; masking a byte that only looked like an opcode
    costs a little specificity, never a false match.
    """

    end = min(len(image.data), start + length)
    keep = bytearray(b"\x01" * (end - start))
    for reloc in image.relocs_between(start - 3, end):
        for index in range(reloc, reloc + 4):
            if start <= index < end:
                keep[index - start] = 0
    data = image.data
    for index in range(start, end):
        byte = data[index]
        operand = None
        if byte in (0xE8, 0xE9):
            operand = index + 1
        elif byte == 0x0F and index + 1 < len(data) and 0x80 <= data[index + 1] <= 0x8F:
            operand = index + 2
        if operand is None:
            continue
        for position in range(operand, operand + 4):
            if start <= position < end:
                keep[position - start] = 0
    return keep


def pattern_at(image: PeImage, offset: int, length: int) -> BytePattern:
    length = min(length, len(image.data) - offset)
    keep = volatile_mask(image, offset, length)
    values = bytes(b if m else 0 for b, m in zip(image.data[offset:offset + length], keep))
    return BytePattern(values, bytes(keep))


def make_signature(
    image: PeImage,
    va: int,
    *,
    min_length: int = 12,
    max_length: int = 160,
) -> dict[str, Any]:
    """Shortest relocation-safe signature starting at ``va`` that is unique."""

    offset = image.va_to_offset(va)
    if not any(begin <= offset < end for begin, end in image.executable_ranges()):
        raise ValueError(f"{va:#010x} не в исполняемой секции")
    length = max(4, min_length)
    while length <= max_length:
        pattern = pattern_at(image, offset, length)
        if pattern.fixed >= 6:
            matches = find_pattern(image, pattern, limit=2)
            if matches == [offset]:
                return {"va": f"{va:#010x}", "length": len(pattern), "unique": True,
                        "pattern": pattern.text(), "c_array": pattern.c_array()}
        length += 4
    pattern = pattern_at(image, offset, max_length)
    return {"va": f"{va:#010x}", "length": len(pattern), "unique": False,
            "pattern": pattern.text(), "c_array": pattern.c_array(),
            "matches": len(find_pattern(image, pattern, limit=50))}


# --------------------------------------------------------------------------
# patterns from plugin sources and manifests

_C_ARRAY = re.compile(
    r"(?:static\s+)?(?:constexpr\s+|const\s+)?(?:static\s+)?"
    r"(int|unsigned\s+char|BYTE|std::uint8_t|uint8_t|short|std::int16_t|int16_t)\s+"
    r"(\w+)\s*\[\s*\d*\s*\]\s*=\s*\{([^{}]*)\}",
)


@dataclass(frozen=True)
class HookPattern:
    name: str
    pattern: BytePattern
    profile: str = "any"  # legacy | optimized | any
    expect: int | None = 1
    source: str = ""


def _profile_from_name(name: str) -> str:
    folded = name.casefold()
    if "optimized" in folded or "optimised" in folded:
        return "optimized"
    if "legacy" in folded:
        return "legacy"
    return "any"


_C_MASK = re.compile(
    r'(?:static\s+)?(?:constexpr\s+|const\s+)?char\s+(\w+)\s*\[\s*\d*\s*\]\s*=\s*((?:"[^"]*"\s*)+);'
)
_HOOK_NAME = re.compile(r"pattern|signature|prologue|original", re.IGNORECASE)
_NAME_SUFFIX = re.compile(r"(signature|pattern|mask)$", re.IGNORECASE)


def _base_name(name: str) -> str:
    return _NAME_SUFFIX.sub("", name).casefold()


def hooks_from_source(
    path: str | Path,
    *,
    min_length: int = 8,
    all_arrays: bool = False,
) -> list[HookPattern]:
    """Byte arrays a plugin source scans for.

    Wildcards are ``-1`` in an ``int`` array or ``?`` in a companion IDA-style
    mask string (``kFooSignature`` + ``kFooMask = "xx?x"``). Only arrays whose
    name says Pattern/Signature/Prologue/Original are taken unless
    ``all_arrays``; replacement bytes and expected-value tables are not
    signatures. Arrays shorter than ``min_length`` (prologue probes matched
    relative to another hit) are skipped. A name with ``Optimized`` marks the
    $O+ pattern, and its twin without the word is then the $O- one; a lone
    name applies to both. ``expect`` is unknown, so counts are only shown.
    """

    path = Path(path)
    text = re.sub(r"//[^\n]*", "", path.read_text(encoding="utf-8", errors="replace"))
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    masks = {
        _base_name(match.group(1)): "".join(re.findall(r'"([^"]*)"', match.group(2)))
        for match in _C_MASK.finditer(text)
    }
    hooks: list[HookPattern] = []
    for match in _C_ARRAY.finditer(text):
        kind, name, body = match.groups()
        if not all_arrays and _HOOK_NAME.search(name) is None:
            continue
        tokens = [token.strip() for token in body.split(",") if token.strip()]
        if not tokens or not all(re.fullmatch(r"0[xX][0-9A-Fa-f]{1,2}|-1|\d{1,3}", t) for t in tokens):
            continue
        if "-1" in tokens and kind.replace(" ", "") not in ("int", "short", "std::int16_t", "int16_t"):
            continue
        values = bytearray()
        mask = bytearray()
        for token in tokens:
            if token == "-1":
                values.append(0)
                mask.append(0)
            else:
                number = int(token, 0)
                if number > 0xFF:
                    break
                values.append(number)
                mask.append(1)
        else:
            companion = masks.get(_base_name(name))
            if companion is not None and len(companion) == len(values):
                mask = bytearray(0 if ch == "?" else 1 for ch in companion)
                values = bytearray(v if m else 0 for v, m in zip(values, mask))
            if len(values) >= min_length and any(mask):
                hooks.append(
                    HookPattern(name, BytePattern(bytes(values), bytes(mask)),
                                _profile_from_name(name), None, path.name)
                )
    optimized_twins = {
        re.sub("optimi[sz]ed", "", hook.name, flags=re.IGNORECASE).casefold()
        for hook in hooks
        if hook.profile == "optimized"
    }
    return [
        HookPattern(hook.name, hook.pattern, "legacy", hook.expect, hook.source)
        if hook.profile == "any" and hook.name.casefold() in optimized_twins
        else hook
        for hook in hooks
    ]


def hooks_from_manifest(path: str | Path) -> list[HookPattern]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema") != HOOKS_SCHEMA:
        raise ValueError(f"ожидается schema {HOOKS_SCHEMA}")
    hooks: list[HookPattern] = []
    for item in raw.get("hooks", []):
        profile = item.get("profile", "any")
        if profile not in ("any", "legacy", "optimized"):
            raise ValueError(f"hook {item.get('name')}: profile должен быть any, legacy или optimized")
        expect = item.get("expect", 1)
        hooks.append(
            HookPattern(str(item["name"]), parse_pattern(str(item["pattern"])), profile,
                        None if expect is None else int(expect), Path(path).name)
        )
    return hooks


def probe_hooks(hooks: Sequence[HookPattern], executables: Sequence[str | Path]) -> dict[str, Any]:
    """Match every hook against every EXE and say which ones broke.

    A hook marked for one profile is only expected to match EXEs of that
    profile; for the other profile it is reported but never counted as
    broken. Without ``expect`` (patterns read from sources) zero matches in
    an EXE of the hook's profile is still a failure, because a scan that
    finds nothing disables the plugin.
    """

    results: list[dict[str, Any]] = []
    failures = 0
    for exe in executables:
        identity = identify_executable(exe)
        image = load_pe(exe)
        rows: list[dict[str, Any]] = []
        for hook in hooks:
            matches = find_pattern(image, hook.pattern, limit=20)
            applies = hook.profile == "any" or hook.profile == identity["profile"]
            if not applies:
                status = "other-profile"
            elif hook.expect is not None:
                status = "ok" if len(matches) == hook.expect else "missing" if not matches else "count-mismatch"
            else:
                status = "missing" if not matches else "found"
            if status in ("missing", "count-mismatch"):
                failures += 1
            rows.append({
                "name": hook.name,
                "profile": hook.profile,
                "source": hook.source,
                "length": len(hook.pattern),
                "matches": len(matches),
                "expect": hook.expect,
                "status": status,
                "va": [f"{image.offset_to_va(offset):#010x}" for offset in matches[:5]],
            })
        results.append({
            "exe": identity["path"],
            "sha256": identity["sha256"],
            "profile": identity["profile"],
            "known_build": identity["known_build"],
            "hooks": rows,
        })
    return {"schema": EXE_SCHEMA, "status": "passed" if failures == 0 else "issues",
            "failures": failures, "executables": results}


# --------------------------------------------------------------------------
# porting a hook site to another build


def _unique_in(image: PeImage, pattern: BytePattern) -> int | None:
    matches = find_pattern(image, pattern, limit=2)
    return matches[0] if len(matches) == 1 else None


def _data_fingerprint(image: PeImage, offset: int) -> bytes | None:
    """Bytes at a referenced address that identify the same data elsewhere."""

    data = image.data[offset:offset + 48]
    if len(data) < 12:
        return None
    # Delphi string literals: UTF-16 or ANSI text; also VMTs/type info with
    # their ShortString names. Mostly-zero or one-byte fills are useless.
    if data.count(0) > len(data) * 0.6 or len(set(data)) < 6:
        return None
    if image.is_relocated(offset) or any(image.is_relocated(offset + i) for i in range(1, 12)):
        # Pointer tables move with the image; their bytes are not stable.
        return None
    return data[:32]


def _describe_data(blob: bytes) -> str:
    try:
        text = blob.decode("utf-16-le").split("\0")[0]
        if len(text) >= 3 and all(ch.isprintable() for ch in text):
            return text[:40]
    except UnicodeDecodeError:
        pass
    text = blob.split(b"\0")[0]
    if len(text) >= 3 and all(32 <= b < 127 or b >= 0xC0 for b in text):
        return text.decode("cp1251", errors="replace")[:40]
    return blob[:8].hex(" ")


def _volatile_dword(image: PeImage, position: int) -> bool:
    """Whether the dword at ``position`` is an address or a branch target."""

    if any(image.is_relocated(position - k) for k in range(0, 4)):
        return True
    data = image.data
    for back in (1, 2):
        start = position - back
        if start < 0:
            continue
        if back == 1 and data[start] in (0xE8, 0xE9):
            return True
        if back == 2 and data[start] == 0x0F and 0x80 <= data[start + 1] <= 0x8F:
            return True
    return False


def _number_like(value: int) -> bool:
    """Values an imm32/disp32 plausibly holds: offsets, counts or floats."""

    if value < 0x01000000:
        return True
    # Single floats with a short mantissa: 1.0f = 0x3F800000, 10.0f = 0x41200000.
    exponent = (value >> 23) & 0xFF
    return value & 0xFFFF == 0 and 0x70 <= exponent <= 0x90


def _stable_constants(
    image: PeImage, start: int, end: int, *, strict: bool = False
) -> list[tuple[int, int]]:
    """Dwords in the range that are neither addresses nor branch targets.

    Small values are everywhere, and 0xFFFFxxxx are negative frame offsets of
    the $O- profile that $O+ code does not have. Around one hook site,
    dwords read across opcode bytes are kept on purpose: an instruction
    encoded the same way in both builds matches byte for byte, and on the
    XenoMods hook sites that placed 6 of 12 sites within 10 bytes against 2
    of 12 when only "real" numbers were kept. Over a whole function
    (``strict``) those reads swamp the evidence, so only number-like values
    are kept and, of overlapping reads, the smallest (the aligned operand).
    """

    result: list[tuple[int, int]] = []
    for position in range(start, end - 3):
        value = struct.unpack_from("<I", image.data, position)[0]
        if value < 0x100 or value >= 0xFFFF0000:
            continue
        if strict and not _number_like(value):
            continue
        if _volatile_dword(image, position):
            continue
        if strict and result and position - result[-1][0] < 4:
            if value < result[-1][1]:
                result[-1] = (position, value)
            continue
        result.append((position, value))
    return result


def _constant_positions(image: PeImage, value: int, *, limit: int) -> list[int]:
    needle = struct.pack("<I", value)
    found: list[int] = []
    for begin, end in image.executable_ranges():
        cursor = image.data.find(needle, begin, end)
        while cursor >= 0:
            if not _volatile_dword(image, cursor):
                found.append(cursor)
                if len(found) >= limit:
                    return found
            cursor = image.data.find(needle, cursor + 1, end)
    return found


_FRAME_PROLOGUE = bytes.fromhex("55 8B EC")


def _function_start_guess(image: PeImage, va: int, *, reach: int = 0x10000) -> int | None:
    """Nearest 4-aligned ``push ebp; mov ebp,esp`` at or before ``va``.

    Delphi aligns routines to four bytes and both profiles keep the frame
    prologue in routines with locals, so this is usually the routine that
    holds ``va``. Frameless routines make it a guess, reported as such.
    """

    try:
        offset = image.va_to_offset(va)
    except ValueError:
        return None
    floor = max(0, offset - reach)
    cursor = image.data.rfind(_FRAME_PROLOGUE, floor, offset + 3)
    while cursor >= 0:
        candidate = image.offset_to_va(cursor)
        if candidate % 4 == 0:
            return candidate
        cursor = image.data.rfind(_FRAME_PROLOGUE, floor, cursor + 2)
    return None


def port_address(
    old_exe: str | Path,
    va: int,
    new_exe: str | Path,
    *,
    radius: int = 1024,
    regions: Sequence[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    """Find ``va`` of ``old_exe`` in ``new_exe``.

    ``regions`` (VA ranges, e.g. every chunk of the function from a symbol
    map) replace the ``radius`` window as the source of references and
    constants; $O+ builds move cold paths such as raise blocks far from the
    function start.
    """

    old = load_pe(old_exe)
    new = load_pe(new_exe)
    old_identity = identify_executable(old_exe)
    new_identity = identify_executable(new_exe)
    offset = old.va_to_offset(va)
    result: dict[str, Any] = {
        "schema": EXE_SCHEMA,
        "old": {"path": old_identity["path"], "profile": old_identity["profile"],
                "known_build": old_identity["known_build"], "va": f"{va:#010x}"},
        "new": {"path": new_identity["path"], "profile": new_identity["profile"],
                "known_build": new_identity["known_build"]},
    }

    # 1. Same bytes around the site.
    for before, after in ((32, 96), (16, 64), (0, 48), (0, 32), (16, 16), (0, 24)):
        start = offset - before
        if start < 0:
            continue
        pattern = pattern_at(old, start, before + after)
        if pattern.fixed < 12:
            continue
        hit = _unique_in(new, pattern)
        if hit is not None:
            new_va = new.offset_to_va(hit + before)
            result.update(method="exact-window", confidence="high", va=f"{new_va:#010x}",
                          evidence={"window": [-before, after], "fixed_bytes": pattern.fixed},
                          signature=make_signature(new, new_va))
            return result

    # 2. Unique chunks of the surrounding code voting for one displacement.
    votes: Counter[int] = Counter()
    nearest: dict[int, int] = {}
    low = max(0, offset - radius)
    high = min(len(old.data), offset + radius)
    for chunk in range(low, high - 24, 8):
        pattern = pattern_at(old, chunk, 24)
        if pattern.fixed < 16:
            continue
        hit = _unique_in(new, pattern)
        if hit is None:
            continue
        delta = new.offset_to_va(hit) - old.offset_to_va(chunk)
        votes[delta] += 1
        distance = abs(chunk - offset)
        nearest[delta] = min(nearest.get(delta, distance), distance)
    estimate: dict[str, Any] | None = None
    if votes:
        delta, count = votes.most_common(1)[0]
        if count >= 3:
            new_va = va + delta
            try:
                new_offset = new.va_to_offset(new_va)
                exact = pattern_at(old, offset, 16).values == pattern_at(new, new_offset, 16).values
            except ValueError:
                exact = False
            evidence = {"votes": count, "displacement": delta, "nearest_anchor_bytes": nearest[delta],
                        "site_bytes_equal": exact,
                        "other_displacements": [[d, c] for d, c in votes.most_common(4)[1:]]}
            if exact:
                result.update(method="anchor-vote",
                              confidence="high" if nearest[delta] <= 64 else "medium",
                              va=f"{new_va:#010x}", evidence=evidence,
                              signature=make_signature(new, new_va))
                return result
            # Shared chunks but different bytes at the site: the function is
            # found, the instruction is not (typical across $O-/$O+).
            estimate = {"va": f"{new_va:#010x}", **evidence}

    # 3. What the surrounding code references: data such as string constants
    # and type info, rarely called functions, and number-like constants.
    # All of them survive a recompile with the other profile.
    if regions:
        spans = []
        for start_va, end_va in regions:
            try:
                spans.append((old.va_to_offset(start_va), old.va_to_offset(end_va - 1) + 1))
            except ValueError:
                continue
    else:
        spans = [(low, high)]
    exec_ranges = new.executable_ranges()
    new_users: dict[int, list[int]] = defaultdict(list)
    for reloc in new.reloc_offsets:
        if any(begin <= reloc < end for begin, end in exec_ranges):
            new_users[new.dword(reloc)].append(reloc)
    new_callers: dict[int, list[int]] = defaultdict(list)
    for begin, end in exec_ranges:
        code = new.data
        cursor = code.find(b"\xe8", begin, end - 5)
        while cursor >= 0:
            target = new.offset_to_va(cursor) + 5 + struct.unpack_from("<i", code, cursor + 1)[0]
            new_callers[target].append(cursor)
            cursor = code.find(b"\xe8", cursor + 1, end - 5)

    references: list[dict[str, Any]] = []
    weights: Counter[int] = Counter()
    labels: dict[int, set[str]] = defaultdict(set)

    def credit(users: Sequence[int], label: str, strength: float) -> None:
        if not users or len(users) > 40:
            return
        for user in users:
            bucket = new.offset_to_va(user) // 256
            weights[bucket] += strength / len(users)
            labels[bucket].add(label)

    seen_targets: set[int] = set()
    for span_start, span_end in spans:
        for reloc in old.relocs_between(span_start, span_end):
            target = old.dword(reloc)
            if target in seen_targets:
                continue
            seen_targets.add(target)
            try:
                target_offset = old.va_to_offset(target)
            except ValueError:
                continue
            fingerprint = _data_fingerprint(old, target_offset)
            if fingerprint is None:
                continue
            found = find_pattern(new, BytePattern(fingerprint, b"\x01" * len(fingerprint)),
                                 ranges=[(0, len(new.data))], limit=2)
            if len(found) != 1:
                continue
            new_target = new.offset_to_va(found[0])
            users = new_users.get(new_target, [])
            label = _describe_data(fingerprint)
            references.append({
                "kind": "data",
                "old_ref": f"{old.offset_to_va(reloc):#010x}",
                "distance": reloc - offset,
                "data": label,
                "new_target": f"{new_target:#010x}",
                "new_refs": [f"{new.offset_to_va(u):#010x}" for u in users[:6]],
            })
            # A unique string or type is far stronger evidence than a number.
            credit(users, label, 4.0)

    seen_callees: set[int] = set()
    old_exec = old.executable_ranges()
    for span_start, span_end in spans:
        cursor = old.data.find(b"\xe8", span_start, span_end - 5)
        while cursor >= 0:
            callee = old.offset_to_va(cursor) + 5 + struct.unpack_from("<i", old.data, cursor + 1)[0]
            cursor_next = old.data.find(b"\xe8", cursor + 1, span_end - 5)
            if callee not in seen_callees:
                seen_callees.add(callee)
                try:
                    callee_offset = old.va_to_offset(callee)
                except ValueError:
                    callee_offset = -1
                if callee_offset >= 0 and any(b <= callee_offset < e for b, e in old_exec):
                    body = pattern_at(old, callee_offset, 32)
                    hit = _unique_in(new, body) if body.fixed >= 20 else None
                    if hit is not None:
                        new_callee = new.offset_to_va(hit)
                        users = new_callers.get(new_callee, [])
                        if 0 < len(users) <= 40:
                            label = f"call {new_callee:#010x}"
                            references.append({
                                "kind": "call",
                                "old_ref": f"{old.offset_to_va(cursor):#010x}",
                                "distance": cursor - offset,
                                "old_target": f"{callee:#010x}",
                                "new_target": f"{new_callee:#010x}",
                                "new_refs": [f"{new.offset_to_va(u):#010x}" for u in users[:6]],
                            })
                            credit(users, label, 1.0)
            cursor = cursor_next

    # Immediates and displacements (field offsets, float constants) are the
    # same in both profiles; rare ones locate the function after a recompile.
    constant_spans = spans if regions else [(max(0, offset - 192), min(len(old.data), offset + 192))]
    site_constants: dict[int, int] = {}
    for span_start, span_end in constant_spans:
        for position, value in _stable_constants(old, span_start, span_end, strict=bool(regions)):
            distance = abs(position - offset)
            if value not in site_constants or distance < site_constants[value]:
                site_constants[value] = distance
    constant_hits: dict[int, list[int]] = {}
    for value in site_constants:
        hits = _constant_positions(new, value, limit=200)
        if hits and len(hits) < 200:
            constant_hits[value] = hits
            for hit in hits:
                bucket = new.offset_to_va(hit) // 256
                weights[bucket] += 1.0 / len(hits)
                labels[bucket].add(f"const {value:#x}")

    candidates: list[dict[str, Any]] = []
    for bucket in sorted(weights):
        if candidates and bucket * 256 - candidates[-1]["end"] <= 256:
            region = candidates[-1]
            region["end"] = bucket * 256 + 256
        else:
            region = {"start": bucket * 256, "end": bucket * 256 + 256, "weight": 0.0, "labels": set()}
            candidates.append(region)
        region["weight"] += weights[bucket]
        region["labels"] |= labels[bucket]
    candidates.sort(key=lambda item: item["weight"], reverse=True)
    ranked = []
    for item in candidates[:5]:
        entry = {"region": f"{item['start']:#010x}-{item['end'] - 1:#010x}",
                 "weight": round(item["weight"], 2),
                 "evidence": sorted(item["labels"])[:8]}
        start_guess = _function_start_guess(new, item["start"])
        if start_guess is not None:
            entry["function_start_guess"] = f"{start_guess:#010x}"
        ranked.append(entry)
    if estimate is not None and ranked:
        estimate_va = int(estimate["va"], 16)
        top = candidates[0]
        estimate["inside_top_candidate"] = top["start"] <= estimate_va < top["end"]
    if candidates:
        # Constants used right at the hook site, found again inside the best
        # region, point at the instruction to re-sign in the new build.
        top = candidates[0]
        hints = []
        for value, distance in sorted(site_constants.items(), key=lambda item: item[1]):
            if distance > 24:
                continue
            for hit in constant_hits.get(value, []):
                hit_va = new.offset_to_va(hit)
                if top["start"] - 256 <= hit_va < top["end"] + 256:
                    hints.append({"const": f"{value:#x}", "old_distance": distance, "new_va": f"{hit_va:#010x}"})
        if hints:
            result["site_hints"] = hints[:8]
    if estimate is not None or ranked:
        result.update(
            method="approximate" if estimate is not None else "references",
            confidence="low",
            message=(
                "Код вокруг адреса скомпилирован иначе (часто это переход $O- ↔ $O+): найдена "
                "соответствующая область, точное место хука нужно выбрать вручную и записать "
                "для этой сборки свою сигнатуру (exe sig)"
            ),
            candidates=ranked,
            references=references[:24],
        )
        if estimate is not None:
            result["estimate"] = estimate
        return result
    result.update(method="none", confidence="none",
                  message="рядом с адресом нет ни общих байтов, ни узнаваемых данных или вызовов")
    return result


# --------------------------------------------------------------------------
# reference symbols (e.g. a decompilation map of one build)


SymbolMap = dict[str, list[list[tuple[int, int]]]]


def load_symbols(path: str | Path) -> SymbolMap:
    """Read ``{"ranges": {"<start>": {"name": ..., "chunks": [...]}}}``.

    This is the layout of ``reference/native_ranges.json`` of the
    SpaceRangersHD_decomp project, which maps the 2026-10-06 prerelease.
    ModKit does not ship such maps; pass the file you have. Each function
    keeps every chunk: $O+ code moves cold paths out of the function body.
    """

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    ranges = raw.get("ranges", raw)
    symbols: SymbolMap = defaultdict(list)
    for start, item in ranges.items():
        name = item.get("name") if isinstance(item, dict) else None
        if not name:
            continue
        begin = int(start)
        chunks = [(int(chunk["start"]), int(chunk["end"])) for chunk in item.get("chunks") or []]
        chunks = sorted(chunks, key=lambda span: (span[0] != begin, span[0])) or [(begin, begin + 1)]
        symbols[name].append(chunks)
    return dict(symbols)


def find_symbol(symbols: SymbolMap, query: str) -> list[tuple[str, int, int]]:
    """``(name, start, end of the first chunk)``; exact name first."""

    folded = query.casefold()
    entries = [(name, chunks[0][0], chunks[0][1]) for name, items in symbols.items() for chunks in items]
    exact = [entry for entry in entries if entry[0].casefold() == folded]
    if exact:
        return exact
    return sorted((entry for entry in entries if folded in entry[0].casefold()),
                  key=lambda item: (len(item[0]), item[0]))[:20]


def symbol_chunks(symbols: SymbolMap, start: int) -> list[tuple[int, int]]:
    for items in symbols.values():
        for chunks in items:
            if chunks[0][0] == start:
                return chunks
    return []


def symbol_at(symbols: SymbolMap, va: int) -> tuple[str, int] | None:
    """Function containing ``va`` (any chunk) and its start address."""

    for name, items in symbols.items():
        for chunks in items:
            if any(begin <= va < max(end, begin + 1) for begin, end in chunks):
                return name, chunks[0][0]
    return None


def iter_executables(paths: Iterable[str | Path]) -> list[Path]:
    result: list[Path] = []
    for value in paths:
        path = Path(value)
        if path.is_dir():
            result.extend(sorted(p for p in path.iterdir() if p.suffix.casefold() == ".exe"))
        else:
            result.append(path)
    return result
