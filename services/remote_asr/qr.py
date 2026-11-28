"""Pure-Python ISO/IEC 18004 byte-mode QR code generator (Versions 1-10, ECC L/M).

Zero external dependencies: generates crisp QR matrices and self-contained SVG
markup for host invite URLs and pairing links so phone/laptop cameras can scan
them directly from Guided Setup, Remote Settings, or the host invite web page.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

#: Maximum supported QR version (Version 10 fits up to 271 bytes at ECC-L
#: and 213 bytes at ECC-M — plenty for LAN/Tailscale invite URLs).
MAX_VERSION: Final[int] = 10

#: Error-correction level -> 2-bit format indicator per ISO/IEC 18004 Table 12.
_ECC_FORMAT_BITS: Final[dict[str, int]] = {
    "L": 0b01,
    "M": 0b00,
}

#: Alignment pattern center coordinates per version (ISO/IEC 18004 Annex E).
_ALIGNMENT_CENTERS: Final[dict[int, tuple[int, ...]]] = {
    1: (),
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
    6: (6, 34),
    7: (6, 22, 38),
    8: (6, 24, 42),
    9: (6, 26, 46),
    10: (6, 28, 50),
}

#: ISO/IEC 18004 Table 9 RS block layouts:
#: (version, ecc) -> (ec_codewords_per_block, g1_blocks, g1_data_cw, g2_blocks, g2_data_cw).
_RS_BLOCKS: Final[dict[tuple[int, str], tuple[int, int, int, int, int]]] = {
    # Level L
    (1, "L"): (7, 1, 19, 0, 0),
    (2, "L"): (10, 1, 34, 0, 0),
    (3, "L"): (15, 1, 55, 0, 0),
    (4, "L"): (20, 1, 80, 0, 0),
    (5, "L"): (26, 1, 108, 0, 0),
    (6, "L"): (18, 2, 68, 0, 0),
    (7, "L"): (20, 2, 78, 0, 0),
    (8, "L"): (24, 2, 97, 0, 0),
    (9, "L"): (30, 2, 116, 0, 0),
    (10, "L"): (18, 2, 68, 2, 69),
    # Level M
    (1, "M"): (10, 1, 16, 0, 0),
    (2, "M"): (16, 1, 28, 0, 0),
    (3, "M"): (26, 1, 44, 0, 0),
    (4, "M"): (18, 2, 32, 0, 0),
    (5, "M"): (24, 2, 43, 0, 0),
    (6, "M"): (16, 4, 27, 0, 0),
    (7, "M"): (18, 4, 31, 0, 0),
    (8, "M"): (22, 2, 38, 2, 39),
    (9, "M"): (22, 3, 36, 2, 37),
    (10, "M"): (26, 4, 43, 1, 44),
}

#: Safe CSS color token for SVG fill attributes (hex or simple keyword).
_SAFE_COLOR_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8}|[a-zA-Z]{3,20})$"
)

# Precompute GF(256) exp/log tables using QR primitive polynomial x^8+x^4+x^3+x^2+1 (0x11D).
_GF_EXP: list[int] = [0] * 512
_GF_LOG: list[int] = [0] * 256


def _init_gf_tables() -> None:
    val = 1
    for i in range(255):
        _GF_EXP[i] = val
        _GF_LOG[val] = i
        val <<= 1
        if val & 0x100:
            val ^= 0x11D
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]


_init_gf_tables()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator(degree: int) -> list[int]:
    poly = [1]
    for i in range(degree):
        root = _GF_EXP[i]
        next_poly = [0] * (len(poly) + 1)
        for j, coeff in enumerate(poly):
            next_poly[j] ^= coeff
            next_poly[j + 1] ^= _gf_mul(coeff, root)
        poly = next_poly
    return poly


def _rs_remainder(data: Sequence[int], ec_len: int) -> list[int]:
    gen = _rs_generator(ec_len)
    rem = [0] * ec_len
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        if factor != 0:
            for j in range(ec_len):
                rem[j] ^= _gf_mul(gen[j + 1], factor)
    return rem


def _count_bits_for_version(version: int) -> int:
    return 8 if version <= 9 else 16


def _data_capacity_codewords(version: int, ecc: str) -> int:
    _ec, g1_blocks, g1_data, g2_blocks, g2_data = _RS_BLOCKS[(version, ecc)]
    return g1_blocks * g1_data + g2_blocks * g2_data


def _select_version(byte_len: int, ecc: str) -> int:
    for version in range(1, MAX_VERSION + 1):
        cap_bits = _data_capacity_codewords(version, ecc) * 8
        needed_bits = 4 + _count_bits_for_version(version) + byte_len * 8
        if needed_bits <= cap_bits:
            return version
    max_bytes = (_data_capacity_codewords(MAX_VERSION, ecc) * 8 - 4 - _count_bits_for_version(MAX_VERSION)) // 8
    raise ValueError(
        f"QR payload is too long ({byte_len} bytes; maximum for ECC-{ecc} "
        f"version {MAX_VERSION} is {max_bytes} bytes)."
    )


def _encode_data_codewords(payload: bytes, version: int, ecc: str) -> list[int]:
    cap_cw = _data_capacity_codewords(version, ecc)
    cap_bits = cap_cw * 8
    bits: list[int] = []

    def append_bits(value: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            bits.append((value >> shift) & 1)

    # Byte mode indicator (0100) + character count + raw bytes.
    append_bits(0b0100, 4)
    append_bits(len(payload), _count_bits_for_version(version))
    for byte in payload:
        append_bits(byte, 8)

    # Up to 4 terminator bits, then pad to byte boundary.
    terminator = min(4, cap_bits - len(bits))
    bits.extend([0] * terminator)
    while len(bits) % 8 != 0:
        bits.append(0)

    codewords = []
    for i in range(0, len(bits), 8):
        byte = 0
        for b in bits[i : i + 8]:
            byte = (byte << 1) | b
        codewords.append(byte)

    pad_bytes = (0xEC, 0x11)
    pad_idx = 0
    while len(codewords) < cap_cw:
        codewords.append(pad_bytes[pad_idx & 1])
        pad_idx += 1
    return codewords


def _interleave_codewords(data_cw: Sequence[int], version: int, ecc: str) -> list[int]:
    ec_len, g1_blocks, g1_data, g2_blocks, g2_data = _RS_BLOCKS[(version, ecc)]
    data_blocks: list[list[int]] = []
    ec_blocks: list[list[int]] = []
    offset = 0
    for _ in range(g1_blocks):
        block = list(data_cw[offset : offset + g1_data])
        offset += g1_data
        data_blocks.append(block)
        ec_blocks.append(_rs_remainder(block, ec_len))
    for _ in range(g2_blocks):
        block = list(data_cw[offset : offset + g2_data])
        offset += g2_data
        data_blocks.append(block)
        ec_blocks.append(_rs_remainder(block, ec_len))

    result: list[int] = []
    max_data = max(len(b) for b in data_blocks)
    for i in range(max_data):
        for block in data_blocks:
            if i < len(block):
                result.append(block[i])
    for i in range(ec_len):
        for block in ec_blocks:
            result.append(block[i])
    return result


def _format_bits(ecc: str, mask_id: int) -> int:
    data = (_ECC_FORMAT_BITS[ecc] << 3) | mask_id
    rem = data << 10
    for shift in range(14, 9, -1):
        if rem & (1 << shift):
            rem ^= 0x537 << (shift - 10)
    return ((data << 10) | rem) ^ 0x5412


def _version_bits(version: int) -> int:
    rem = version << 12
    for shift in range(17, 11, -1):
        if rem & (1 << shift):
            rem ^= 0x1F25 << (shift - 12)
    return (version << 12) | rem


def _build_base_matrix(version: int) -> tuple[list[list[bool]], list[list[bool]]]:
    size = 17 + 4 * version
    modules = [[False] * size for _ in range(size)]
    is_func = [[False] * size for _ in range(size)]

    def set_func(r: int, c: int, dark: bool) -> None:
        if 0 <= r < size and 0 <= c < size:
            modules[r][c] = dark
            is_func[r][c] = True

    def place_finder(top: int, left: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = top + dr, left + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                if 0 <= dr <= 6 and 0 <= dc <= 6:
                    ring = max(abs(dr - 3), abs(dc - 3))
                    dark = ring != 2
                else:
                    dark = False
                set_func(r, c, dark)

    place_finder(0, 0)
    place_finder(0, size - 7)
    place_finder(size - 7, 0)

    # Alignment patterns (skip corners overlapping finder patterns).
    centers = _ALIGNMENT_CENTERS[version]
    for r_center in centers:
        for c_center in centers:
            if (
                (r_center <= 8 and c_center <= 8)
                or (r_center <= 8 and c_center >= size - 9)
                or (r_center >= size - 9 and c_center <= 8)
            ):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    ring = max(abs(dr), abs(dc))
                    set_func(r_center + dr, c_center + dc, ring != 1)

    # Timing patterns.
    for i in range(8, size - 8):
        dark = (i % 2) == 0
        if not is_func[6][i]:
            set_func(6, i, dark)
        if not is_func[i][6]:
            set_func(i, 6, dark)

    # Dark module (always dark at row 4*V+9, col 8).
    set_func(4 * version + 9, 8, True)

    # Reserve format info modules (actual bits written per mask candidate).
    for c in range(9):
        if c != 6:
            is_func[8][c] = True
    for r in range(8):
        if r != 6:
            is_func[r][8] = True
    for c in range(size - 8, size):
        is_func[8][c] = True
    for r in range(size - 7, size):
        is_func[r][8] = True

    # Version info for Version 7+.
    if version >= 7:
        vbits = _version_bits(version)
        for i in range(18):
            bit = bool((vbits >> i) & 1)
            a = size - 11 + (i % 3)
            b = i // 3
            set_func(a, b, bit)
            set_func(b, a, bit)

    return modules, is_func


def _write_format_info(modules: list[list[bool]], ecc: str, mask_id: int) -> None:
    size = len(modules)
    fbits = _format_bits(ecc, mask_id)

    def bit(idx: int) -> bool:
        return bool((fbits >> idx) & 1)

    # Top-left copy: MSB (bit 14) at (8, 0) -> LSB (bit 0) at (0, 8).
    tl_coords = (
        (0, 8),
        (1, 8),
        (2, 8),
        (3, 8),
        (4, 8),
        (5, 8),
        (7, 8),
        (8, 8),
        (8, 7),
        (8, 5),
        (8, 4),
        (8, 3),
        (8, 2),
        (8, 1),
        (8, 0),
    )
    for idx, (r, c) in enumerate(tl_coords):
        modules[r][c] = bit(idx)

    # Bottom-left & top-right copy: MSB (bit 14) at (size-1, 8) -> LSB (bit 0) at (8, size-1).
    split_coords = (
        (8, size - 1),
        (8, size - 2),
        (8, size - 3),
        (8, size - 4),
        (8, size - 5),
        (8, size - 6),
        (8, size - 7),
        (8, size - 8),
        (size - 7, 8),
        (size - 6, 8),
        (size - 5, 8),
        (size - 4, 8),
        (size - 3, 8),
        (size - 2, 8),
        (size - 1, 8),
    )
    for idx, (r, c) in enumerate(split_coords):
        modules[r][c] = bit(idx)


def _place_codewords(
    modules: list[list[bool]],
    is_func: Sequence[Sequence[bool]],
    codewords: Sequence[int],
) -> None:
    size = len(modules)
    bit_stream: list[bool] = []
    for cw in codewords:
        for shift in range(7, -1, -1):
            bit_stream.append(bool((cw >> shift) & 1))

    bit_idx = 0
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for offset in (0, 1):
                c = col - offset
                if not is_func[row][c]:
                    modules[row][c] = bit_stream[bit_idx] if bit_idx < len(bit_stream) else False
                    bit_idx += 1
        upward = not upward
        col -= 2


def _mask_condition(mask_id: int, r: int, c: int) -> bool:
    if mask_id == 0:
        return (r + c) % 2 == 0
    if mask_id == 1:
        return r % 2 == 0
    if mask_id == 2:
        return c % 3 == 0
    if mask_id == 3:
        return (r + c) % 3 == 0
    if mask_id == 4:
        return (r // 2 + c // 3) % 2 == 0
    if mask_id == 5:
        return ((r * c) % 2) + ((r * c) % 3) == 0
    if mask_id == 6:
        return (((r * c) % 2) + ((r * c) % 3)) % 2 == 0
    return (((r + c) % 2) + ((r * c) % 3)) % 2 == 0


def _penalty_score(modules: Sequence[Sequence[bool]]) -> int:
    size = len(modules)
    score = 0

    # N1: runs of 5+ same-color modules in rows and columns.
    for r in range(size):
        row = modules[r]
        run_color = row[0]
        run_len = 1
        for c in range(1, size):
            if row[c] == run_color:
                run_len += 1
            else:
                if run_len >= 5:
                    score += 3 + (run_len - 5)
                run_color = row[c]
                run_len = 1
        if run_len >= 5:
            score += 3 + (run_len - 5)

    for c in range(size):
        run_color = modules[0][c]
        run_len = 1
        for r in range(1, size):
            if modules[r][c] == run_color:
                run_len += 1
            else:
                if run_len >= 5:
                    score += 3 + (run_len - 5)
                run_color = modules[r][c]
                run_len = 1
        if run_len >= 5:
            score += 3 + (run_len - 5)

    # N2: 2x2 blocks of identical color.
    for r in range(size - 1):
        row0 = modules[r]
        row1 = modules[r + 1]
        for c in range(size - 1):
            v = row0[c]
            if v == row0[c + 1] == row1[c] == row1[c + 1]:
                score += 3

    # N3: finder-like 1:1:3:1:1 patterns with 4 light modules on either side.
    pat_a = (True, False, True, True, True, False, True, False, False, False, False)
    pat_b = (False, False, False, False, True, False, True, True, True, False, True)
    for r in range(size):
        row = modules[r]
        for c in range(size - 10):
            window = tuple(row[c : c + 11])
            if window == pat_a or window == pat_b:
                score += 40
    for c in range(size):
        for r in range(size - 10):
            window = tuple(modules[r + k][c] for k in range(11))
            if window == pat_a or window == pat_b:
                score += 40

    # N4: dark/light balance deviation from 50%.
    dark_count = sum(1 for row in modules for cell in row if cell)
    total = size * size
    deviation = abs(dark_count * 100 - total * 50) // (total * 5)
    score += deviation * 10
    return score


def encode_qr_matrix(data: str | bytes, *, ecc: str = "M") -> list[list[bool]]:
    """Encode ``data`` as a byte-mode QR code matrix (list of rows of bools).

    ``True`` represents a dark module and ``False`` represents a light module.
    Automatically picks the smallest QR version (1..10) that fits ``data``.
    If ``ecc="M"`` exceeds Version 10 capacity, callers may pass ``ecc="L"``.
    """
    level = (ecc or "M").strip().upper()
    if level not in _ECC_FORMAT_BITS:
        raise ValueError(f"Unsupported QR error correction level: {ecc!r} (expected 'L' or 'M').")
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if not payload:
        raise ValueError("QR payload cannot be empty.")

    version = _select_version(len(payload), level)
    data_cw = _encode_data_codewords(payload, version, level)
    final_cw = _interleave_codewords(data_cw, version, level)

    base_modules, is_func = _build_base_matrix(version)
    _place_codewords(base_modules, is_func, final_cw)

    size = len(base_modules)
    best_matrix: list[list[bool]] = []
    best_score: int | None = None
    for mask_id in range(8):
        candidate = [
            [
                (not base_modules[r][c]) if (not is_func[r][c] and _mask_condition(mask_id, r, c)) else base_modules[r][c]
                for c in range(size)
            ]
            for r in range(size)
        ]
        _write_format_info(candidate, level, mask_id)
        score = _penalty_score(candidate)
        if best_score is None or score < best_score:
            best_score = score
            best_matrix = candidate
    return best_matrix


def render_qr_svg(
    data: str | bytes,
    *,
    ecc: str = "M",
    module_size: int = 8,
    quiet_zone: int = 4,
    dark: str = "#0f172a",
    light: str = "#ffffff",
) -> str:
    """Render ``data`` into a clean, self-contained QR code SVG string.

    Falls back automatically from ``ecc="M"`` to ``ecc="L"`` only when explicitly
    requested via ``ecc="L"``; raises ``ValueError`` if payload exceeds capacity.
    """
    if module_size < 1 or module_size > 64:
        raise ValueError("module_size must be between 1 and 64.")
    if quiet_zone < 0 or quiet_zone > 16:
        raise ValueError("quiet_zone must be between 0 and 16.")
    if not _SAFE_COLOR_RE.fullmatch(dark) or not _SAFE_COLOR_RE.fullmatch(light):
        raise ValueError("Invalid SVG color specification.")

    matrix = encode_qr_matrix(data, ecc=ecc)
    n = len(matrix)
    total_modules = n + 2 * quiet_zone
    px = total_modules * module_size

    # Compact path in module coordinates: one horizontal run per dark span.
    path_parts: list[str] = []
    for r, row in enumerate(matrix):
        c = 0
        while c < n:
            if not row[c]:
                c += 1
                continue
            start = c
            while c < n and row[c]:
                c += 1
            width = c - start
            path_parts.append(f"M{start + quiet_zone},{r + quiet_zone}h{width}v1h-{width}z")
    path_data = "".join(path_parts)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total_modules} {total_modules}" '
        f'width="{px}" height="{px}" shape-rendering="crispEdges" role="img" aria-label="QR code">'
        f'<rect width="{total_modules}" height="{total_modules}" fill="{light}"/>'
        f'<path d="{path_data}" fill="{dark}"/>'
        "</svg>"
    )


def render_qr_svg_bytes(
    data: str | bytes,
    *,
    ecc: str = "M",
    module_size: int = 8,
    quiet_zone: int = 4,
    dark: str = "#0f172a",
    light: str = "#ffffff",
) -> bytes:
    """UTF-8 SVG bytes ready for Qt ``QSvgRenderer`` / ``QPixmap`` or HTTP responses."""
    return render_qr_svg(
        data,
        ecc=ecc,
        module_size=module_size,
        quiet_zone=quiet_zone,
        dark=dark,
        light=light,
    ).encode("utf-8")


def render_invite_qr_svg(
    url: str,
    *,
    module_size: int = 8,
    quiet_zone: int = 4,
    dark: str = "#0f172a",
    light: str = "#ffffff",
) -> str:
    """Render an invite URL using ECC-M when it fits, falling back to ECC-L for longer URLs."""
    try:
        return render_qr_svg(
            url,
            ecc="M",
            module_size=module_size,
            quiet_zone=quiet_zone,
            dark=dark,
            light=light,
        )
    except ValueError:
        return render_qr_svg(
            url,
            ecc="L",
            module_size=module_size,
            quiet_zone=quiet_zone,
            dark=dark,
            light=light,
        )


def render_invite_qr_svg_bytes(
    url: str,
    *,
    module_size: int = 8,
    quiet_zone: int = 4,
    dark: str = "#0f172a",
    light: str = "#ffffff",
) -> bytes:
    """UTF-8 SVG bytes for an invite URL with automatic ECC-M -> ECC-L fallback."""
    return render_invite_qr_svg(
        url,
        module_size=module_size,
        quiet_zone=quiet_zone,
        dark=dark,
        light=light,
    ).encode("utf-8")
