"""Where LiDAR files come from.

A :class:`LidarSource` yields the LAZ/LAS files covering a (padded) city
bbox. The MVP ships only :class:`LocalDirectory` -- the user downloads PNOA
tiles by hand from the CNIG download center. An automated CNIG driver is
planned for the real-city phase; the download center publishes no API, so
that driver will wrap its internal endpoints and must stay isolated behind
this same interface.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import laspy
from shapely import box, unary_union

from shade_core.config import Bbox


class CoverageError(RuntimeError):
    """The available files do not cover the requested (padded) bbox."""


class LidarSource(Protocol):
    def files_covering(self, bbox: Bbox, buffer_m: float) -> list[Path]:
        """Files whose points cover ``bbox`` expanded by ``buffer_m`` meters."""
        ...


@dataclass(frozen=True)
class LocalDirectory:
    """LAZ/LAS files sitting in a directory, e.g. hand-downloaded PNOA tiles.

    File extents come from the LAS header (mins/maxs), which bound the
    *points*, not the nominal tile: points sit up to roughly one point
    spacing inside the tile edge. ``coverage_tolerance_m`` shrinks the
    required area accordingly before the containment check.
    """

    directory: Path
    coverage_tolerance_m: float = 1.0

    def files_covering(self, bbox: Bbox, buffer_m: float) -> list[Path]:
        min_x, min_y, max_x, max_y = bbox
        target = box(min_x - buffer_m, min_y - buffer_m, max_x + buffer_m, max_y + buffer_m)
        selected: list[Path] = []
        footprints = []
        for path in sorted([*self.directory.glob("*.laz"), *self.directory.glob("*.las")]):
            with laspy.open(path) as reader:
                mins, maxs = reader.header.mins, reader.header.maxs
            footprint = box(float(mins[0]), float(mins[1]), float(maxs[0]), float(maxs[1]))
            if footprint.intersects(target):
                selected.append(path)
                footprints.append(footprint)
        required = target.buffer(-self.coverage_tolerance_m)
        if not footprints or not unary_union(footprints).contains(required):
            raise CoverageError(
                f"LAZ files under {self.directory} do not cover bbox {bbox} "
                f"plus a {buffer_m} m buffer (found {len(selected)} intersecting files)"
            )
        return selected
