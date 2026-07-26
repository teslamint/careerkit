from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Iterable

ASCII_WHITESPACE = b" \t\r\n\f\v"


@dataclass(frozen=True)
class PdfVisualThresholds:
    raster_dpi: int = 144
    channel_threshold: int = 8
    max_differing_pixel_ratio: float = 0.0005


@dataclass(frozen=True)
class PageComparisonResult:
    matches: bool
    exact_hash_match: bool
    differing_pixels: int
    total_pixels: int
    differing_ratio: float
    baseline_hash: str
    candidate_hash: str
    baseline_size: tuple[int, int]
    candidate_size: tuple[int, int]
    reason: str | None = None
    diagnostic_paths: tuple[Path, ...] = ()


def _read_ppm(path: Path) -> tuple[int, int, bytes]:
    raw = path.read_bytes()
    if not raw.startswith(b"P6"):
        raise ValueError(f"Unsupported image format for {path}; expected binary PPM (P6)")

    idx = 2
    tokens: list[bytes] = []
    while len(tokens) < 3:
        while idx < len(raw) and raw[idx] in ASCII_WHITESPACE:
            idx += 1
        if idx < len(raw) and raw[idx] == 35:
            while idx < len(raw) and raw[idx] not in (10, 13):
                idx += 1
            continue
        start = idx
        while idx < len(raw) and raw[idx] not in ASCII_WHITESPACE:
            idx += 1
        tokens.append(raw[start:idx])

    width, height, max_value = map(int, tokens)
    if max_value != 255:
        raise ValueError(f"Unsupported max value {max_value} in {path}")
    while idx < len(raw) and raw[idx] in ASCII_WHITESPACE:
        idx += 1
    payload = raw[idx:]
    expected = width * height * 3
    if len(payload) != expected:
        raise ValueError(
            f"Unexpected payload length for {path}: expected {expected} bytes, got {len(payload)}"
        )
    return width, height, payload


def write_ppm(path: Path, width: int, height: int, rgb: bytes, comment: str | None = None) -> None:
    if len(rgb) != width * height * 3:
        raise ValueError("RGB payload length does not match width/height")
    header = b"P6\n"
    if comment:
        header += f"# {comment}\n".encode("ascii")
    header += f"{width} {height}\n255\n".encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + rgb)


def build_uniform_rgb(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    return bytes(color * (width * height))


def mutate_pixels(
    rgb: bytes,
    width: int,
    mutations: Iterable[tuple[int, int, tuple[int, int, int]]],
) -> bytes:
    buf = bytearray(rgb)
    for x, y, color in mutations:
        offset = (y * width + x) * 3
        buf[offset : offset + 3] = bytes(color)
    return bytes(buf)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare_ppm_pages(
    baseline_path: Path,
    candidate_path: Path,
    thresholds: PdfVisualThresholds,
    diagnostics_dir: Path | None = None,
    page_label: str = "page-1",
) -> PageComparisonResult:
    baseline_hash = sha256_file(baseline_path)
    candidate_hash = sha256_file(candidate_path)
    baseline_size = _read_ppm(baseline_path)[:2]
    candidate_size = _read_ppm(candidate_path)[:2]

    if baseline_hash == candidate_hash:
        total_pixels = baseline_size[0] * baseline_size[1]
        return PageComparisonResult(
            matches=True,
            exact_hash_match=True,
            differing_pixels=0,
            total_pixels=total_pixels,
            differing_ratio=0.0,
            baseline_hash=baseline_hash,
            candidate_hash=candidate_hash,
            baseline_size=baseline_size,
            candidate_size=candidate_size,
        )

    if baseline_size != candidate_size:
        return PageComparisonResult(
            matches=False,
            exact_hash_match=False,
            differing_pixels=0,
            total_pixels=baseline_size[0] * baseline_size[1],
            differing_ratio=1.0,
            baseline_hash=baseline_hash,
            candidate_hash=candidate_hash,
            baseline_size=baseline_size,
            candidate_size=candidate_size,
            reason="dimension-mismatch",
        )

    width, height, baseline_rgb = _read_ppm(baseline_path)
    _, _, candidate_rgb = _read_ppm(candidate_path)
    differing_pixels = 0
    for offset in range(0, len(baseline_rgb), 3):
        if any(
            abs(baseline_rgb[offset + channel] - candidate_rgb[offset + channel])
            > thresholds.channel_threshold
            for channel in range(3)
        ):
            differing_pixels += 1

    total_pixels = width * height
    ratio = differing_pixels / total_pixels
    matches = ratio <= thresholds.max_differing_pixel_ratio
    reason = None if matches else "pixel-threshold-exceeded"
    diagnostic_paths: tuple[Path, ...] = ()
    if diagnostics_dir is not None and not matches:
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        baseline_copy = diagnostics_dir / f"{page_label}-baseline.ppm"
        candidate_copy = diagnostics_dir / f"{page_label}-candidate.ppm"
        diff_copy = diagnostics_dir / f"{page_label}-diff-amplified.ppm"
        baseline_copy.write_bytes(baseline_path.read_bytes())
        candidate_copy.write_bytes(candidate_path.read_bytes())
        diff_rgb = bytearray(len(baseline_rgb))
        for offset in range(0, len(baseline_rgb), 3):
            if any(
                abs(baseline_rgb[offset + channel] - candidate_rgb[offset + channel])
                > thresholds.channel_threshold
                for channel in range(3)
            ):
                diff_rgb[offset : offset + 3] = bytes((255, 0, 0))
            else:
                diff_rgb[offset : offset + 3] = bytes((0, 0, 0))
        write_ppm(diff_copy, width, height, bytes(diff_rgb), comment="amplified-diff")
        diagnostic_paths = (baseline_copy, candidate_copy, diff_copy)

    return PageComparisonResult(
        matches=matches,
        exact_hash_match=False,
        differing_pixels=differing_pixels,
        total_pixels=total_pixels,
        differing_ratio=ratio,
        baseline_hash=baseline_hash,
        candidate_hash=candidate_hash,
        baseline_size=baseline_size,
        candidate_size=candidate_size,
        reason=reason,
        diagnostic_paths=diagnostic_paths,
    )
