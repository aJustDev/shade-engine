"""Artifact verification: prove the artifacts on disk are internally sound.

Born from a real failure: an 11-hour Cordoba build silently lost the tail
bands of the horizon cube somewhere in the storage path (memmapped scratch
-> temp GTiff -> COG) and shipped artifacts that loaded fine but predicted
"sun" for every western azimuth. ``write_cog`` now verifies its own writes;
this module audits *finished artifact directories* -- fresh builds and
already-deployed ones -- using a domain invariant instead of checksums:

The sweep records a blocker class whenever a sector's final horizon angle is
positive and ``NO_BLOCKER`` otherwise (see ``shade_pipeline.horizon``), so
angle and class can never disagree in a healthy pair of cubes:

- quantized angle > 0 with blocker == ``NO_BLOCKER`` is impossible, full
  stop: any occurrence is corruption.
- quantized angle == 0 with a real blocker class only happens for true
  angles inside the quantization dead band (below half a quantum,
  45/255 ~ 0.176 deg). A few pixels per band are legitimate on flat
  outskirts; a large fraction means one cube lost data (the Cordoba
  corruption scored 30-100% on the dead bands).

The remaining checks are cheap layout and range sanity (georeference against
the metadata, dtypes, DTM without NaN, DSM >= DTM, categorical values in
range). Everything streams in 512 px windows: verifying a 2.4 GB city costs
about a minute and bounded memory.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
import rasterio
from rasterio.windows import Window

from shade_core.artifacts import (
    BLOCKER_CLASS_FILENAME,
    CANOPY_FILENAME,
    COVERAGE_FILENAME,
    DSM_FILENAME,
    DTM_FILENAME,
    HORIZON_FILENAME,
    HORIZON_NOVEG_FILENAME,
    LANDCOVER_FILENAME,
    METADATA_FILENAME,
    BuildMetadata,
    load_metadata,
)
from shade_core.shade import Landcover
from shade_pipeline.grid import grid_shape, transform_from_bbox

WINDOW_SIZE: Final = 512
QUANTUM_TOLERANCE: Final = 1
"""Slack, in quantization steps, between the two horizon cubes.

Where no vegetation is involved both cubes describe the same skyline, but one
reaches it through ``arctan2`` per sample and the other through ``arctan`` of
an accumulated tangent (see ``shade_pipeline.horizon``). The two agree to
floating-point noise, which lands on a different quantum only if the true
angle sits exactly on a rounding boundary. One step of slack absorbs that; a
real disagreement is orders of magnitude larger.
"""
Q0_BLOCKER_MAX_FRACTION: Final = 0.05
"""Tolerated per-band fraction of (angle == 0, blocker set) pixels.

Legitimate only inside the quantization dead band (true angle below ~0.176
deg), which flat rural sectors can populate; corruption scores far higher.
Tunable if a real city proves the default too tight.
"""

ARTIFACT_FILENAMES: Final = (
    DSM_FILENAME,
    DTM_FILENAME,
    LANDCOVER_FILENAME,
    CANOPY_FILENAME,
    HORIZON_FILENAME,
    BLOCKER_CLASS_FILENAME,
)


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one verification check; ``failure is None`` means pass."""

    name: str
    failure: str | None = None

    @property
    def passed(self) -> bool:
        return self.failure is None


class VerificationError(ValueError):
    """Artifact verification found failures; message lists every one."""


def _windows(rows: int, cols: int, size: int) -> list[Window]:
    return [
        Window(col0, row0, min(size, cols - col0), min(size, rows - row0))
        for row0 in range(0, rows, size)
        for col0 in range(0, cols, size)
    ]


def _check_layout(directory: Path, metadata: BuildMetadata) -> CheckResult:
    """Georeference, band count, dtype and tags of every file vs the metadata.

    Transforms compare exactly: both sides derive from the same bbox floats
    and JSON round-trips binary64 without loss, so any difference is a real
    georeference mismatch, not noise.
    """
    rows, cols = grid_shape(metadata.bbox, metadata.resolution_m)
    transform = transform_from_bbox(metadata.bbox, metadata.resolution_m)
    crs = rasterio.crs.CRS.from_string(metadata.crs)
    expected: dict[str, tuple[int, str]] = {
        DSM_FILENAME: (1, "float32"),
        DTM_FILENAME: (1, "float32"),
        LANDCOVER_FILENAME: (1, "uint8"),
        CANOPY_FILENAME: (1, "uint8"),
        HORIZON_FILENAME: (metadata.horizon.sectors, "uint8"),
        BLOCKER_CLASS_FILENAME: (metadata.horizon.sectors, "uint8"),
    }
    if (directory / HORIZON_NOVEG_FILENAME).exists():
        expected[HORIZON_NOVEG_FILENAME] = (metadata.horizon.sectors, "uint8")
    problems: list[str] = []
    for name, (bands, dtype) in expected.items():
        with rasterio.open(directory / name) as src:
            if src.shape != (rows, cols):
                problems.append(f"{name}: shape {src.shape} != {(rows, cols)}")
            if src.count != bands:
                problems.append(f"{name}: {src.count} bands != {bands}")
            if set(src.dtypes) != {dtype}:
                problems.append(f"{name}: dtype {src.dtypes[0]} != {dtype}")
            if src.transform != transform:
                problems.append(f"{name}: transform mismatch")
            if src.crs != crs:
                problems.append(f"{name}: crs {src.crs} != {crs}")
            if (
                name in (HORIZON_FILENAME, HORIZON_NOVEG_FILENAME)
                and "angle_max_deg" not in src.tags()
            ):
                problems.append(f"{name}: missing angle_max_deg tag")
    return CheckResult("layout", "; ".join(problems) or None)


def _check_horizon_noveg(directory: Path, metadata: BuildMetadata, window_size: int) -> CheckResult:
    """The vegetation-free cube against the full one, band by band.

    Two facts hold by construction and a storage failure breaks both:

    - Felling trees can only open the sky, never close it, so the
      vegetation-free angle never exceeds the full one.
    - Where a *building* won the sector, felling changes nothing at all: the
      winning cell keeps its height, so the two angles are the same number.

    A cube that lost bands scores massively on the first check (zeros against
    real angles pass) and on the second (zeros where a building blocks).
    """
    rows, cols = grid_shape(metadata.bbox, metadata.resolution_m)
    above = 0
    building_mismatch = 0
    with (
        rasterio.open(directory / HORIZON_FILENAME) as horizon,
        rasterio.open(directory / HORIZON_NOVEG_FILENAME) as noveg,
        rasterio.open(directory / BLOCKER_CLASS_FILENAME) as blocker,
    ):
        for window in _windows(rows, cols, window_size):
            full = horizon.read(window=window).astype(np.int16)
            free = noveg.read(window=window).astype(np.int16)
            classes = blocker.read(window=window)
            above += int((free > full + QUANTUM_TOLERANCE).sum())
            by_building = classes == Landcover.BUILDING
            building_mismatch += int(
                (by_building & (np.abs(full - free) > QUANTUM_TOLERANCE)).sum()
            )
    problems: list[str] = []
    if above:
        problems.append(f"{above} px where the vegetation-free horizon is above the full one")
    if building_mismatch:
        problems.append(
            f"{building_mismatch} px blocked by a building where the two horizons disagree"
        )
    return CheckResult("horizon-noveg invariant", "; ".join(problems) or None)


def _check_horizon_blocker(
    directory: Path, metadata: BuildMetadata, window_size: int
) -> CheckResult:
    """The angle/class invariant, accumulated per band over the whole raster."""
    rows, cols = grid_shape(metadata.bbox, metadata.resolution_m)
    no_blocker = metadata.no_blocker_value
    sky_with_blocker = 0
    zero_with_blocker = np.zeros(metadata.horizon.sectors, dtype=np.int64)
    with (
        rasterio.open(directory / HORIZON_FILENAME) as horizon,
        rasterio.open(directory / BLOCKER_CLASS_FILENAME) as blocker,
    ):
        for window in _windows(rows, cols, window_size):
            angles = horizon.read(window=window)
            classes = blocker.read(window=window)
            sky_with_blocker += int(((angles > 0) & (classes == no_blocker)).sum())
            zero_with_blocker += ((angles == 0) & (classes != no_blocker)).sum(axis=(1, 2))
    problems: list[str] = []
    if sky_with_blocker:
        problems.append(f"{sky_with_blocker} px with angle > 0 but blocker == {no_blocker}")
    # Against the pixels that were computed, not against the grid: an area
    # covering a fifth of its bbox would otherwise divide by five times the
    # population and dilute the detector to uselessness.
    computed = metadata.coverage.covered_px if metadata.coverage is not None else rows * cols
    fractions = zero_with_blocker / float(computed)
    worst = int(fractions.argmax())
    if fractions[worst] > Q0_BLOCKER_MAX_FRACTION:
        bad = int((fractions > Q0_BLOCKER_MAX_FRACTION).sum())
        problems.append(
            f"{bad} band(s) with angle == 0 but a real blocker class beyond "
            f"{Q0_BLOCKER_MAX_FRACTION:.0%} of pixels (worst: band {worst + 1}, "
            f"{fractions[worst]:.0%}) -- the horizon cube likely lost data"
        )
    return CheckResult("horizon-blocker invariant", "; ".join(problems) or None)


def _check_coverage(directory: Path, metadata: BuildMetadata, window_size: int) -> CheckResult:
    """Outside the computation area, the cubes say nothing at all.

    The one check that cannot be derived from the others: a pixel outside the
    area must be angle 0 with class ``NO_BLOCKER`` in both cubes. Any other
    value there is a leak from a masking bug, and it would be read downstream
    as a real, confident answer about a pixel the build never computed.

    Also pins the mask against the metadata's own count, which is what
    ``_check_horizon_blocker`` divides by.
    """
    rows, cols = grid_shape(metadata.bbox, metadata.resolution_m)
    no_blocker = metadata.no_blocker_value
    leaked_angle = 0
    leaked_class = 0
    covered = 0
    has_noveg = (directory / HORIZON_NOVEG_FILENAME).exists()
    with (
        rasterio.open(directory / COVERAGE_FILENAME) as coverage,
        rasterio.open(directory / HORIZON_FILENAME) as horizon,
        rasterio.open(directory / BLOCKER_CLASS_FILENAME) as blocker,
    ):
        if (int(coverage.height), int(coverage.width)) != (rows, cols):
            return CheckResult(
                "coverage",
                f"{COVERAGE_FILENAME} is {coverage.height}x{coverage.width}, "
                f"expected {rows}x{cols}",
            )
        noveg = rasterio.open(directory / HORIZON_NOVEG_FILENAME) if has_noveg else None
        try:
            for window in _windows(rows, cols, window_size):
                outside = coverage.read(1, window=window) == 0
                covered += int((~outside).sum())
                if not outside.any():
                    continue
                leaked_angle += int(horizon.read(window=window)[:, outside].any(axis=0).sum())
                leaked_class += int(
                    (blocker.read(window=window)[:, outside] != no_blocker).any(axis=0).sum()
                )
                if noveg is not None:
                    leaked_angle += int(noveg.read(window=window)[:, outside].any(axis=0).sum())
        finally:
            if noveg is not None:
                noveg.close()

    problems: list[str] = []
    if covered == 0:
        problems.append(f"{COVERAGE_FILENAME} covers no pixel at all")
    if leaked_angle:
        problems.append(f"{leaked_angle} px outside the area with a horizon angle above 0")
    if leaked_class:
        problems.append(f"{leaked_class} px outside the area with a blocker class set")
    if metadata.coverage is not None and metadata.coverage.covered_px != covered:
        problems.append(
            f"metadata says {metadata.coverage.covered_px} covered px, "
            f"{COVERAGE_FILENAME} has {covered}"
        )
    return CheckResult("coverage", "; ".join(problems) or None)


def _check_elevation(directory: Path, metadata: BuildMetadata, window_size: int) -> CheckResult:
    """DTM has no NaN and DSM never dips below it (both hold by construction)."""
    rows, cols = grid_shape(metadata.bbox, metadata.resolution_m)
    nan_dsm = 0
    nan_dtm = 0
    below = 0
    with (
        rasterio.open(directory / DSM_FILENAME) as dsm_src,
        rasterio.open(directory / DTM_FILENAME) as dtm_src,
    ):
        for window in _windows(rows, cols, window_size):
            dsm = dsm_src.read([1], window=window)[0]
            dtm = dtm_src.read([1], window=window)[0]
            nan_dsm += int(np.isnan(dsm).sum())
            nan_dtm += int(np.isnan(dtm).sum())
            below += int((dsm < dtm).sum())
    problems: list[str] = []
    if nan_dsm:
        problems.append(f"{DSM_FILENAME}: {nan_dsm} NaN px")
    if nan_dtm:
        problems.append(f"{DTM_FILENAME}: {nan_dtm} NaN px")
    if below:
        problems.append(f"{below} px with dsm < dtm")
    return CheckResult("elevation sanity", "; ".join(problems) or None)


def _check_classes(directory: Path, metadata: BuildMetadata, window_size: int) -> CheckResult:
    """Categorical rasters only hold their declared values."""
    rows, cols = grid_shape(metadata.bbox, metadata.resolution_m)
    landcover_values = [int(member) for member in Landcover]
    problems: list[str] = []
    for name, allowed in ((CANOPY_FILENAME, [0, 1]), (LANDCOVER_FILENAME, landcover_values)):
        bad = 0
        with rasterio.open(directory / name) as src:
            for window in _windows(rows, cols, window_size):
                data = src.read([1], window=window)[0]
                bad += int((~np.isin(data, allowed)).sum())
        if bad:
            problems.append(f"{name}: {bad} px outside {allowed}")
    return CheckResult("class values", "; ".join(problems) or None)


def verify_artifacts(
    artifact_dir: str | Path,
    *,
    window_size: int = WINDOW_SIZE,
    progress: Callable[[str], None] | None = None,
) -> list[CheckResult]:
    """Run every check against an artifact directory; never raises on findings.

    Returns one :class:`CheckResult` per check. Metadata or missing-file
    failures end the run early: the remaining checks derive their
    expectations (shape, band count, no-blocker value) from the metadata.
    """
    echo = progress if progress is not None else lambda _message: None
    directory = Path(artifact_dir)
    try:
        metadata = load_metadata(directory)
    except (OSError, ValueError) as exc:
        return [CheckResult("metadata", f"{METADATA_FILENAME}: {exc}")]
    results = [CheckResult("metadata")]

    missing = [name for name in ARTIFACT_FILENAMES if not (directory / name).exists()]
    if missing:
        results.append(CheckResult("files", f"missing: {', '.join(missing)}"))
        return results
    results.append(CheckResult("files"))

    echo("verifying layout")
    layout = _check_layout(directory, metadata)
    results.append(layout)
    if not layout.passed:
        # Windowed checks assume the metadata grid; do not read a raster
        # whose shape already disagrees with it.
        return results

    echo("verifying horizon-blocker invariant")
    results.append(_check_horizon_blocker(directory, metadata, window_size))
    if (directory / HORIZON_NOVEG_FILENAME).exists():
        echo("verifying horizon-noveg invariant")
        results.append(_check_horizon_noveg(directory, metadata, window_size))
    if (directory / COVERAGE_FILENAME).exists():
        echo("verifying computation area")
        results.append(_check_coverage(directory, metadata, window_size))
    echo("verifying elevation rasters")
    results.append(_check_elevation(directory, metadata, window_size))
    echo("verifying class rasters")
    results.append(_check_classes(directory, metadata, window_size))
    return results


def format_report(results: list[CheckResult]) -> str:
    lines = [
        f"{'ok  ' if result.passed else 'FAIL'} {result.name}"
        + (f": {result.failure}" if result.failure else "")
        for result in results
    ]
    passed = sum(1 for result in results if result.passed)
    lines.append(f"{passed}/{len(results)} checks passed")
    return "\n".join(lines)


def ensure_verified(
    artifact_dir: str | Path,
    *,
    window_size: int = WINDOW_SIZE,
    progress: Callable[[str], None] | None = None,
) -> None:
    """Raise :class:`VerificationError` unless every check passes."""
    results = verify_artifacts(artifact_dir, window_size=window_size, progress=progress)
    failures = [result for result in results if not result.passed]
    if failures:
        raise VerificationError(
            "artifact verification failed: "
            + "; ".join(f"[{result.name}] {result.failure}" for result in failures)
        )
