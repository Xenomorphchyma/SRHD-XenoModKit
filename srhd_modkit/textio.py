from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DecodedText:
    text: str
    encoding: str
    had_bom: bool = False


def decode_bytes(data: bytes) -> DecodedText:
    """Decode the encodings commonly used by SRHD text files."""
    if not data:
        return DecodedText("", "utf-8")

    if data.startswith(b"\xff\xfe"):
        return DecodedText(
            data[2:].decode("utf-16-le", errors="replace"), "utf-16-le", True
        )
    if data.startswith(b"\xfe\xff"):
        return DecodedText(
            data[2:].decode("utf-16-be", errors="replace"), "utf-16-be", True
        )
    if data.startswith(b"\xef\xbb\xbf"):
        return DecodedText(data.decode("utf-8-sig", errors="replace"), "utf-8-sig", True)

    sample = data[:4096]
    if sample.count(b"\x00") > max(6, len(sample) // 12):
        even_nulls = sample[0::2].count(0)
        odd_nulls = sample[1::2].count(0)
        encoding = "utf-16-le" if odd_nulls >= even_nulls else "utf-16-be"
        return DecodedText(data.decode(encoding, errors="replace").lstrip("\ufeff"), encoding)

    try:
        return DecodedText(data.decode("utf-8").lstrip("\ufeff"), "utf-8")
    except UnicodeDecodeError:
        # BlockParEditor can export otherwise valid UTF-8 with a trailing lead
        # byte when an old DAT value was truncated. Preserve those bytes with
        # surrogateescape instead of mis-decoding the whole document as OEM.
        utf8_lossless = data.decode("utf-8", errors="surrogateescape").lstrip("\ufeff")
        invalid = sum(0xDC80 <= ord(char) <= 0xDCFF for char in utf8_lossless)
        high_bytes = sum(byte >= 0x80 for byte in data)
        if high_bytes and invalid / high_bytes <= 0.40:
            return DecodedText(utf8_lossless, "utf-8-surrogateescape")

    for encoding in ("cp1251", "cp866"):
        try:
            return DecodedText(data.decode(encoding).lstrip("\ufeff"), encoding)
        except UnicodeDecodeError:
            continue

    return DecodedText(data.decode("cp1251", errors="replace").lstrip("\ufeff"), "cp1251")


def read_text(path: str | Path) -> DecodedText:
    path = Path(path)
    return decode_bytes(path.read_bytes())
