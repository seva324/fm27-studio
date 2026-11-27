"""FM27 ``.fmf`` training schedule (``.tsc``) encoder & decoder — pure Python 3.12 stdlib.

Produces genuine FM27 ``afe.`` container ``.fmf`` files loadable directly via
FM27 ``Training > Schedules > Load`` with zero third-party dependencies:

- **Outer container**: ``\\x02\\x01afe.\\x08\\x00\\x00`` header + tightly packed
  resource records + ``\\x02\\x01fmf.\\x08\\x00\\x00`` Zstandard catalog.
- **Resource sealing**: RFC-8878 raw-block Zstandard frame (``zstd_raw``) +
  per-resource random 16-byte key/IV + FIPS-197 AES-128-CTR.
- **Embedded resources**:
  - ``_data/details.aom`` (``\\x03\\x01moa.\\x04\\x00`` ``SCHEDULE_TYPE_HANDLER``)
  - ``image.img`` (10-byte placeholder ``01 00 00 00 00 00 00 00 00 00``)
  - ``<name>.tsc`` (14-byte ``\\x03\\x01cst.`` prefix + 7 × 8-byte day records +
    UTF-8 description + 8-byte zero trailer).
"""

from __future__ import annotations

import os
import re
import secrets
import struct
import sys
from typing import Any, Sequence

from . import fmplans
from . import tacticweb

# ---------------------------------------------------------------------------
# Format constants (verified across FM27 native & community .fmf schedules)
# ---------------------------------------------------------------------------

AFE_MAGIC = b"\x02\x01afe.\x08\x00\x00"
CATALOG_MAGIC = b"\x02\x01fmf.\x08\x00\x00"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
DATA_OFFSET = 26
CONTENT_FLAG = 3
SECTION_MARKER_VALUE = 17
EMPTY_RESOURCE_TIMESTAMP = -62135596800  # 0xFFFFFFF1886E0900 signed int64

TSC_MAGIC = b"\x03\x01cst."
DEFAULT_TSC_PREFIX = bytes.fromhex("03016373742e0300050100000001")
DEFAULT_TSC_TRAILER = bytes(8)
DEFAULT_DAY_MARKER = 4
DEFAULT_DAY_TRAILING = 0
DAYS_PER_WEEK = 7
SESSIONS_PER_DAY = 3

AOM_MAGIC = b"\x03\x01moa."
AOM_HEADER = bytes.fromhex("03016d6f612e0400")
SCHEDULE_HANDLER = "SCHEDULE_TYPE_HANDLER"
DEFAULT_AOM_TAIL = bytes.fromhex("0100010002b57f616b01001001")
DEFAULT_IMAGE_BYTES = bytes.fromhex("01000000000000000000")

MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_DESCRIPTION_BYTES = 4096
MAX_NAME_LEN = 120
IMPORT_STEP = "In FM27, open Training > Schedules > Load and import this .fmf file."


# ---------------------------------------------------------------------------
# Pure-Python FIPS-197 AES-128-CTR (zero external dependencies)
# ---------------------------------------------------------------------------

_AES_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)
_AES_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a: int) -> int:
    return (((a << 1) ^ 0x1B) & 0xFF) if (a & 0x80) else ((a << 1) & 0xFF)


def _aes_expand_key(key: bytes) -> list[list[int]]:
    if len(key) != 16:
        raise ValueError("AES-128 key must be 16 bytes")
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = [
                _AES_SBOX[temp[1]] ^ _AES_RCON[i // 4 - 1],
                _AES_SBOX[temp[2]],
                _AES_SBOX[temp[3]],
                _AES_SBOX[temp[0]],
            ]
        prev = words[i - 4]
        words.append([prev[0] ^ temp[0], prev[1] ^ temp[1], prev[2] ^ temp[2], prev[3] ^ temp[3]])
    return [[b for col in words[r * 4:(r + 1) * 4] for b in col] for r in range(11)]


def _aes_encrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    rk0 = round_keys[0]
    s = [block[i] ^ rk0[i] for i in range(16)]
    sbox = _AES_SBOX
    for rnd in range(1, 11):
        s = [sbox[b] for b in s]
        s = [
            s[0], s[5], s[10], s[15],
            s[4], s[9], s[14], s[3],
            s[8], s[13], s[2], s[7],
            s[12], s[1], s[6], s[11],
        ]
        if rnd < 10:
            mixed: list[int] = []
            for c in range(0, 16, 4):
                a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
                t = a0 ^ a1 ^ a2 ^ a3
                mixed.extend((
                    a0 ^ t ^ _xtime(a0 ^ a1),
                    a1 ^ t ^ _xtime(a1 ^ a2),
                    a2 ^ t ^ _xtime(a2 ^ a3),
                    a3 ^ t ^ _xtime(a3 ^ a0),
                ))
            s = mixed
        rk = round_keys[rnd]
        s = [s[i] ^ rk[i] for i in range(16)]
    return bytes(s)


def aes_128_ctr(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Encrypt or decrypt ``data`` using AES-128-CTR with a 128-bit big-endian counter."""
    if len(key) != 16 or len(iv) != 16:
        raise ValueError("AES-128-CTR requires a 16-byte key and 16-byte IV")
    round_keys = _aes_expand_key(key)
    counter = int.from_bytes(iv, "big")
    mask = (1 << 128) - 1
    out = bytearray(len(data))
    for offset in range(0, len(data), 16):
        ks = _aes_encrypt_block(counter.to_bytes(16, "big"), round_keys)
        counter = (counter + 1) & mask
        chunk = data[offset:offset + 16]
        for idx, byte in enumerate(chunk):
            out[offset + idx] = byte ^ ks[idx]
    return bytes(out)


# ---------------------------------------------------------------------------
# Zstandard framing (pure-stdlib writer + raw/RLE/optional-zstd reader)
# ---------------------------------------------------------------------------

def zstd_raw(data: bytes) -> bytes:
    """Wrap ``data`` in a valid RFC-8878 Zstandard frame of raw (uncompressed) blocks."""
    raw = bytes(data)
    out = bytearray(ZSTD_MAGIC + b"\xa0" + struct.pack("<I", len(raw)))
    if not raw:
        out += (1).to_bytes(3, "little")
        return bytes(out)
    for offset in range(0, len(raw), 65536):
        block = raw[offset:offset + 65536]
        last = 1 if (offset + len(block) >= len(raw)) else 0
        out += ((len(block) << 3) | last).to_bytes(3, "little") + block
    return bytes(out)


def _decompress_zstd_raw_only(frame: bytes) -> bytes | None:
    """Pure-Python decoder for Zstandard frames containing raw/RLE blocks."""
    if len(frame) < 5 or frame[:4] != ZSTD_MAGIC:
        raise ValueError("Missing Zstandard frame magic")
    pos = 4
    desc = frame[pos]
    pos += 1
    if desc & 0x08:
        raise ValueError("Invalid Zstandard frame header reserved bit")
    if desc & 0x03:
        return None  # dictionary frame
    single = (desc >> 5) & 1
    fcs_flag = (desc >> 6) & 3
    checksum = 4 if (desc & 0x04) else 0
    if not single:
        pos += 1
    size_bytes = 1 if (fcs_flag == 0 and single) else (0, 2, 4, 8)[fcs_flag]
    if pos + size_bytes > len(frame):
        raise ValueError("Truncated Zstandard frame header")
    declared = int.from_bytes(frame[pos:pos + size_bytes], "little") if size_bytes else None
    if fcs_flag == 1 and declared is not None:
        declared += 256
    pos += size_bytes
    result = bytearray()
    while True:
        if pos + 3 > len(frame):
            raise ValueError("Truncated Zstandard block header")
        hdr = int.from_bytes(frame[pos:pos + 3], "little")
        pos += 3
        last = hdr & 1
        btype = (hdr >> 1) & 3
        bsize = hdr >> 3
        if btype == 0:
            if pos + bsize > len(frame):
                raise ValueError("Truncated Zstandard raw block")
            result += frame[pos:pos + bsize]
            pos += bsize
        elif btype == 1:
            if pos >= len(frame):
                raise ValueError("Truncated Zstandard RLE block")
            result += frame[pos:pos + 1] * bsize
            pos += 1
        else:
            return None  # compressed block — needs full zstd decompressor
        if last:
            break
    if declared is not None and len(result) != declared:
        raise ValueError("Zstandard frame length mismatch")
    if pos + checksum > len(frame):
        raise ValueError("Truncated Zstandard checksum")
    return bytes(result)


def _find_zstd_decompressor():
    """Return a callable ``decompress(bytes) -> bytes`` if available in Python or Tactics Lab."""
    if sys.version_info >= (3, 14):
        try:
            from compression import zstd as _cz  # type: ignore[import-not-found]
            return lambda b: _cz.ZstdDecompressor().decompress(b)
        except Exception:
            pass
    for mod_name in ("zstandard", "backports.zstd"):
        try:
            mod = __import__(mod_name, fromlist=["ZstdDecompressor"])
            return lambda b, m=mod: m.ZstdDecompressor().decompress(b)
        except Exception:
            continue
    # Fallback: if FM27 Tactics Lab is installed locally, its bundled Python runtime
    # includes backports.zstd so we can also decode externally-compressed FM27 schedules.
    try:
        import subprocess
        from . import tacticslab
        install = tacticslab.detect_lab_installation()
        if install.get("installed") and install.get("python_exe"):
            py_exe = str(install["python_exe"])
            vendor_dir = str(install.get("vendor_dir") or "")
            if not vendor_dir and install.get("release_dir"):
                vendor_dir = os.path.join(str(install["release_dir"]), "source", "vendor")
            if os.path.isfile(py_exe) and os.path.isdir(vendor_dir):
                def _sub_decomp(frame: bytes, _py: str = py_exe, _vd: str = vendor_dir) -> bytes:
                    script = (
                        "import sys; sys.path.insert(0, sys.argv[1]); "
                        "from backports import zstd; "
                        "sys.stdout.buffer.write(zstd.ZstdDecompressor().decompress(sys.stdin.buffer.read()))"
                    )
                    proc = subprocess.run(
                        [_py, "-c", script, _vd],
                        input=frame,
                        capture_output=True,
                        timeout=10,
                        check=True,
                    )
                    return proc.stdout
                return _sub_decomp
    except Exception:
        pass
    return None


def zstd_decompress(frame: bytes) -> bytes:
    """Decompress a Zstandard frame (raw/RLE natively; compressed blocks via zstd if installed)."""
    plain = _decompress_zstd_raw_only(frame)
    if plain is not None:
        return plain
    decomp = _find_zstd_decompressor()
    if decomp is not None:
        return bytes(decomp(frame))
    raise ValueError(
        "This externally-compressed .fmf schedule uses Zstandard compressed blocks, "
        "which require Python 3.14+ or a zstd decompressor to inspect."
    )


# ---------------------------------------------------------------------------
# String & catalog helpers
# ---------------------------------------------------------------------------

def _pack_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def _read_string(buf: bytes, pos: int) -> tuple[str, int]:
    if pos + 4 > len(buf):
        raise ValueError("Truncated string length")
    size = struct.unpack_from("<I", buf, pos)[0]
    pos += 4
    if size > 65536 or pos + size > len(buf):
        raise ValueError("Invalid string length")
    return buf[pos:pos + size].decode("utf-8"), pos + size


def _safe_stem(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned or cleaned in (".", ".."):
        return "fm27-schedule"
    return cleaned[:MAX_NAME_LEN].strip(" .") or "fm27-schedule"


# ---------------------------------------------------------------------------
# .tsc training schedule payload codec & details.aom sidecar
# ---------------------------------------------------------------------------

def _resolve_activity_id(token: Any) -> int:
    """Accept an integer ID or English/Chinese activity label from ``fmplans``."""
    if isinstance(token, bool):
        raise ValueError(f"Invalid activity token: {token!r}")
    if isinstance(token, int):
        if 0 <= token <= 65535:
            return token
        raise ValueError(f"Activity id out of range: {token}")
    text = str(token or "").strip()
    if not text:
        raise ValueError("Empty training activity token")
    if text.isdigit():
        val = int(text)
        if 0 <= val <= 65535:
            return val
    norm = re.sub(r"[\s_-]+", "", text).lower()
    for aid, eng, loc, _group in fmplans.TRAINING_ACTIVITIES:
        if norm in (
            re.sub(r"[\s_-]+", "", eng).lower(),
            re.sub(r"[\s_-]+", "", loc).lower(),
        ):
            return aid
    raise ValueError(
        f"Unknown training activity {token!r}; call fm27_training_activities for valid IDs."
    )


def normalize_week_days(days: Sequence[Sequence[Any]]) -> list[list[int]]:
    """Validate and normalize a 7-day × 3-session schedule matrix into integer IDs."""
    if not isinstance(days, (list, tuple)) or len(days) != DAYS_PER_WEEK:
        raise ValueError("A training schedule requires exactly 7 days (Monday..Sunday).")
    out: list[list[int]] = []
    for idx, day in enumerate(days):
        if isinstance(day, str) and "/" in day:
            parts: Sequence[Any] = [p.strip() for p in day.split("/")]
        elif isinstance(day, dict) and isinstance(day.get("sessions"), list):
            parts = [
                (s.get("id") if isinstance(s, dict) else s)
                for s in day["sessions"]
            ]
        elif isinstance(day, (list, tuple)):
            parts = day
        else:
            raise ValueError(f"{fmplans.DAYS[idx]} must contain 3 sessions.")
        if len(parts) != SESSIONS_PER_DAY:
            raise ValueError(f"{fmplans.DAYS[idx]} must contain exactly {SESSIONS_PER_DAY} sessions.")
        out.append([_resolve_activity_id(item) for item in parts])
    return out


def encode_tsc(days: Sequence[Sequence[Any]], description: str = "",
               *, prefix: bytes = DEFAULT_TSC_PREFIX,
               trailer: bytes = DEFAULT_TSC_TRAILER) -> bytes:
    """Encode a 7-day × 3-session FM27 ``.tsc`` binary payload."""
    if len(prefix) != 14 or not prefix.startswith(TSC_MAGIC):
        raise ValueError("Invalid .tsc 14-byte prefix")
    if len(trailer) > 64:
        raise ValueError("Invalid .tsc trailer length")
    week = normalize_week_days(days)
    desc_bytes = (description or "").encode("utf-8")
    if len(desc_bytes) > MAX_DESCRIPTION_BYTES:
        desc_bytes = desc_bytes[:MAX_DESCRIPTION_BYTES].decode("utf-8", "ignore").encode("utf-8")
    buf = bytearray(prefix)
    for s1, s2, s3 in week:
        buf += struct.pack("<BHHHB", DEFAULT_DAY_MARKER, s1, s2, s3, DEFAULT_DAY_TRAILING)
    buf += struct.pack("<I", len(desc_bytes)) + desc_bytes + trailer
    return bytes(buf)


def decode_tsc(payload: bytes) -> dict[str, Any]:
    """Decode an FM27 ``.tsc`` binary payload into structured days & metadata."""
    if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) < 74:
        raise ValueError("The embedded training schedule is truncated")
    raw = bytes(payload)
    if not raw.startswith(TSC_MAGIC):
        raise ValueError("The embedded training schedule is not a supported FM27 .tsc payload")
    prefix = raw[:14]
    days_ids: list[list[int]] = []
    days_labeled: list[dict[str, Any]] = []
    pos = 14
    for idx in range(DAYS_PER_WEEK):
        marker, s1, s2, s3, trailing = struct.unpack_from("<BHHHB", raw, pos)
        pos += 8
        ids = [s1, s2, s3]
        days_ids.append(ids)
        days_labeled.append({
            "day": fmplans.DAYS[idx],
            "marker": marker,
            "trailing": trailing,
            "sessions": [
                {
                    "id": aid,
                    "name": fmplans.ACTIVITY_BY_ID.get(aid, (aid, f"Unknown ({aid})", "", ""))[1],
                    "localised": fmplans.ACTIVITY_BY_ID.get(aid, (aid, "", "", ""))[2],
                }
                for aid in ids
            ],
        })
    desc_len = struct.unpack_from("<I", raw, pos)[0]
    pos += 4
    if desc_len > MAX_DESCRIPTION_BYTES or pos + desc_len > len(raw):
        raise ValueError("The embedded training schedule description is damaged")
    description = raw[pos:pos + desc_len].decode("utf-8")
    pos += desc_len
    trailer = raw[pos:]
    return {
        "prefix": prefix,
        "days_ids": days_ids,
        "days": days_labeled,
        "description": description,
        "trailer": trailer,
    }


def build_details_aom(name: str, short_desc: str = "", author: str = "FM27 Studio") -> bytes:
    """Build an FM27 ``_data/details.aom`` v4 sidecar for ``SCHEDULE_TYPE_HANDLER``."""
    return (
        AOM_HEADER
        + _pack_string(SCHEDULE_HANDLER)
        + _pack_string(name)
        + _pack_string(short_desc)
        + bytes(6)
        + (b"\xff" * 16)
        + _pack_string(author)
        + DEFAULT_AOM_TAIL
    )


def read_details_aom_name(payload: bytes) -> str:
    """Extract the schedule name from a ``_data/details.aom`` sidecar."""
    if len(payload) < 16 or not payload.startswith(AOM_MAGIC):
        raise ValueError("Not a valid FM27 details.aom sidecar")
    _handler, pos = _read_string(payload, 8)
    name, _ = _read_string(payload, pos)
    return name


# ---------------------------------------------------------------------------
# AFE .fmf archive writer & reader
# ---------------------------------------------------------------------------

def _seal_resource(payload: bytes) -> bytes:
    compressed = zstd_raw(payload)
    key = secrets.token_bytes(16)
    iv = secrets.token_bytes(16)
    ciphertext = aes_128_ctr(key, iv, compressed)
    return struct.pack("<II", 16, 16) + key + iv + ciphertext


def _unseal_resource(record: bytes, expected_raw_len: int) -> bytes:
    if len(record) < 8:
        raise ValueError("Truncated FMF resource record")
    key_len, iv_len = struct.unpack_from("<II", record, 0)
    if key_len != 16 or iv_len != 16 or len(record) < 8 + key_len + iv_len:
        raise ValueError("Unsupported FMF resource cipher header")
    key = record[8:24]
    iv = record[24:40]
    ciphertext = record[40:]
    compressed = aes_128_ctr(key, iv, ciphertext)
    plain = zstd_decompress(compressed)
    if expected_raw_len >= 0 and len(plain) != expected_raw_len:
        raise ValueError(f"FMF resource size mismatch: got {len(plain)}, expected {expected_raw_len}")
    return plain


def build_schedule_fmf(name: str, days: Sequence[Sequence[Any]],
                       description: str = "") -> bytes:
    """Build a complete FM27 ``.fmf`` training schedule archive in memory."""
    clean_name = (name or "").strip()[:MAX_NAME_LEN].strip()
    if not clean_name:
        raise ValueError("A training schedule name is required.")
    tsc_bytes = encode_tsc(days, description=description)
    details_bytes = build_details_aom(clean_name)
    img_bytes = DEFAULT_IMAGE_BYTES

    # Resource order matches FM27 & FM AI Assistant 2.1.0:
    # 1. _data/details.aom, 2. image.img, 3. <name>.tsc
    resources = [
        ("_data", "details", ".aom", details_bytes),
        ("", "image", ".img", img_bytes),
        ("", clean_name, ".tsc", tsc_bytes),
    ]
    stored: list[tuple[str, str, str, bytes, int, int]] = []
    offset = 0
    for directory, base_name, ext, raw in resources:
        rec = _seal_resource(raw)
        stored.append((directory, base_name, ext, rec, offset, len(raw)))
        offset += len(rec)

    root_files = [item for item in stored if not item[0]]
    sub_dirs: dict[str, list[tuple[str, str, str, bytes, int, int]]] = {}
    for item in stored:
        if item[0]:
            sub_dirs.setdefault(item[0], []).append(item)

    def _pack_entry(item: tuple[str, str, str, bytes, int, int]) -> bytes:
        _d, bn, ext, rec, off, raw_len = item
        return (
            _pack_string(bn)
            + _pack_string(ext)
            + struct.pack(
                "<QQQqq",
                off,
                len(rec),
                raw_len,
                EMPTY_RESOURCE_TIMESTAMP,
                EMPTY_RESOURCE_TIMESTAMP,
            )
        )

    cat = bytearray()
    cat += _pack_string(clean_name)
    cat += struct.pack("<I", len(root_files))
    for item in root_files:
        cat += _pack_entry(item)
    cat += struct.pack("<I", len(sub_dirs))
    for dir_name, items in sub_dirs.items():
        cat += _pack_string(dir_name)
        cat += struct.pack("<I", len(items))
        for item in items:
            cat += _pack_entry(item)
    cat += struct.pack("<I", 0)

    cat_frame = zstd_raw(bytes(cat))
    data_blob = b"".join(item[3] for item in stored)
    first_section_len = (DATA_OFFSET + len(data_blob)) - 9
    header = AFE_MAGIC + struct.pack("<QQB", first_section_len, SECTION_MARKER_VALUE, CONTENT_FLAG)
    return header + data_blob + CATALOG_MAGIC + cat_frame


def read_schedule_fmf_bytes(raw: bytes) -> dict[str, Any]:
    """Parse an FM27 ``.fmf`` training schedule archive from raw bytes."""
    if not isinstance(raw, (bytes, bytearray, memoryview)) or len(raw) < DATA_OFFSET + 9:
        raise ValueError("Not a valid FM27 .fmf archive")
    data = bytes(raw)
    if not data.startswith(AFE_MAGIC):
        raise ValueError("Unsupported .fmf archive header (expected afe.)")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("The .fmf archive exceeds the 8 MiB safety limit")
    first_len = struct.unpack_from("<Q", data, 9)[0]
    cat_offset = 9 + first_len
    if cat_offset < DATA_OFFSET or cat_offset + 9 > len(data):
        raise ValueError("The .fmf archive catalog offset is out of range")
    if data[cat_offset:cat_offset + 9] != CATALOG_MAGIC:
        raise ValueError("The .fmf archive catalog magic is missing")
    toc = zstd_decompress(data[cat_offset + 9:])

    pos = 0
    root_name, pos = _read_string(toc, pos)
    entries: list[tuple[str, str, str, int, int, int]] = []

    def _read_dir_entries(folder: str) -> None:
        nonlocal pos
        if pos + 4 > len(toc):
            raise ValueError("Truncated FMF catalog file count")
        count = struct.unpack_from("<I", toc, pos)[0]
        pos += 4
        for _ in range(count):
            bn, pos = _read_string(toc, pos)
            ext, pos = _read_string(toc, pos)
            if pos + 40 > len(toc):
                raise ValueError("Truncated FMF catalog entry")
            off, stored_len, raw_len, _c, _m = struct.unpack_from("<QQQqq", toc, pos)
            pos += 40
            entries.append((folder, bn, ext, off, stored_len, raw_len))

    _read_dir_entries("")
    if pos + 4 <= len(toc):
        dir_count = struct.unpack_from("<I", toc, pos)[0]
        pos += 4
        for _ in range(dir_count):
            sub_name, pos = _read_string(toc, pos)
            _read_dir_entries(sub_name)

    tsc_entry = next((e for e in entries if e[2].lower() == ".tsc"), None)
    if tsc_entry is None:
        raise ValueError("The .fmf archive does not contain a .tsc training schedule")
    _d, _bn, _ext, off, stored_len, raw_len = tsc_entry
    start = DATA_OFFSET + off
    end = start + stored_len
    if end > cat_offset:
        raise ValueError("The .tsc resource exceeds archive bounds")
    tsc_plain = _unseal_resource(data[start:end], raw_len)
    parsed = decode_tsc(tsc_plain)
    return {
        "name": root_name,
        "description": parsed["description"],
        "days": parsed["days"],
        "days_ids": parsed["days_ids"],
        "size": len(data),
    }


def read_schedule_fmf(path_or_name: str, *, directory: str | None = None) -> dict[str, Any]:
    """Read & decode an FM27 ``.fmf`` schedule file from ``FM27/schedules``."""
    sched_dir = (directory or "").strip() or tacticweb.detect_fm27_schedules_dir()
    candidate = (path_or_name or "").strip()
    if not candidate:
        raise ValueError("A schedule filename or path is required.")
    if os.path.isabs(candidate) and os.path.isfile(candidate):
        target = candidate
    else:
        if not candidate.lower().endswith(".fmf"):
            candidate_fmf = candidate + ".fmf"
        else:
            candidate_fmf = candidate
        target = os.path.join(sched_dir, os.path.basename(candidate_fmf))
        if not os.path.isfile(target) and sched_dir and os.path.isdir(sched_dir):
            # Case-insensitive / substring fallback inside schedules folder
            low = candidate.lower().removesuffix(".fmf")
            for entry in os.scandir(sched_dir):
                if entry.is_file() and entry.name.lower().endswith(".fmf"):
                    stem = os.path.splitext(entry.name)[0].lower()
                    if stem == low or low in stem:
                        target = entry.path
                        break
    if not os.path.isfile(target):
        raise ValueError(f"Training schedule file not found in {sched_dir}: {path_or_name!r}")
    with open(target, "rb") as handle:
        raw = handle.read(MAX_ARCHIVE_BYTES + 1)
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise ValueError("Training schedule file exceeds 8 MiB safety limit")
    info = read_schedule_fmf_bytes(raw)
    info["path"] = os.path.abspath(target)
    info["filename"] = os.path.basename(target)
    info["schedules_dir"] = sched_dir
    return info


def export_schedule_fmf(
    name: str,
    days: Sequence[Sequence[Any]],
    description: str = "",
    *,
    directory: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Write a genuine loadable FM27 ``.fmf`` schedule into ``FM27/schedules``."""
    clean_name = (name or "").strip()[:MAX_NAME_LEN].strip()
    if not clean_name:
        raise ValueError("A training schedule name is required.")
    sched_dir = (directory or "").strip() or tacticweb.detect_fm27_schedules_dir()
    if not sched_dir:
        raise ValueError("Could not determine the FM27 schedules folder.")
    os.makedirs(sched_dir, exist_ok=True)
    stem = _safe_stem(clean_name)
    target = os.path.join(sched_dir, f"{stem}.fmf")
    if not overwrite:
        suffix = 2
        while os.path.exists(target):
            target = os.path.join(sched_dir, f"{stem} ({suffix}).fmf")
            suffix += 1
    archive_bytes = build_schedule_fmf(clean_name, days, description=description)
    with open(target, "wb") as handle:
        handle.write(archive_bytes)
    return {
        "ok": True,
        "name": clean_name,
        "filename": os.path.basename(target),
        "path": os.path.abspath(target),
        "schedules_dir": os.path.abspath(sched_dir),
        "size": len(archive_bytes),
        "import_step": IMPORT_STEP,
    }
