"""Selecting and validating local LiDAR tiles against a padded bbox."""

from pathlib import Path

import numpy as np
import pytest

import laz_fixture
from shade_pipeline.sources import CoverageError, LocalDirectory


def _write_tile(path: Path, min_x: float, min_y: float, max_x: float, max_y: float) -> None:
    """A minimal tile: ground points at the four corners."""
    xs = np.array([min_x, max_x, min_x, max_x])
    ys = np.array([min_y, min_y, max_y, max_y])
    laz_fixture.write_laz(path, xs, ys, np.zeros(4), np.full(4, 2, dtype=np.uint8))


def test_selects_only_intersecting_files(tmp_path: Path) -> None:
    _write_tile(tmp_path / "west.laz", 0.5, 0.5, 59.5, 119.5)
    _write_tile(tmp_path / "east.laz", 60.5, 0.5, 119.5, 119.5)
    files = LocalDirectory(tmp_path).files_covering((10.0, 10.0, 50.0, 50.0), 5.0)
    assert [path.name for path in files] == ["west.laz"]


def test_seam_gaps_between_tiles_are_absorbed(tmp_path: Path) -> None:
    # Real PNOA tiles quantize to mm: the west tile's points end just short
    # of the shared edge, leaving a hairline seam no outer tolerance fixes.
    # (The fixture's 1 cm LAS scale makes the gap 0.01 m instead of 0.001.)
    _write_tile(tmp_path / "west.laz", 0.0, 0.0, 59.99, 120.0)
    _write_tile(tmp_path / "east.laz", 60.0, 0.0, 120.0, 120.0)
    files = LocalDirectory(tmp_path).files_covering((10.0, 10.0, 110.0, 110.0), 5.0)
    assert [path.name for path in files] == ["east.laz", "west.laz"]


def test_raises_when_union_does_not_cover(tmp_path: Path) -> None:
    _write_tile(tmp_path / "west.laz", 0.5, 0.5, 59.5, 119.5)
    with pytest.raises(CoverageError, match="do not cover"):
        LocalDirectory(tmp_path).files_covering((10.0, 10.0, 110.0, 50.0), 5.0)


def test_raises_on_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(CoverageError):
        LocalDirectory(tmp_path).files_covering((0.0, 0.0, 10.0, 10.0), 0.0)
