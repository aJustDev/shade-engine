"""Canopy mask: height threshold, sieve behavior, derive CLI."""

import shutil
from pathlib import Path

import numpy as np
import numpy.typing as npt
import rasterio
import yaml
from numpy.testing import assert_array_equal
from typer.testing import CliRunner

from conftest import CUBE_CITY
from shade_core import artifacts
from shade_core.shade import Landcover
from shade_pipeline.canopy import canopy_mask
from shade_pipeline.cli import app
from shade_pipeline.cog import write_cog


def _scene(
    size: int = 24,
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.float32], npt.NDArray[np.uint8]]:
    """Flat ground scene: dsm == dtm == 0, all-GROUND landcover."""
    dsm = np.zeros((size, size), dtype=np.float32)
    dtm = np.zeros((size, size), dtype=np.float32)
    landcover = np.full((size, size), Landcover.GROUND, dtype=np.uint8)
    return dsm, dtm, landcover


def _plant(
    dsm: npt.NDArray[np.float32],
    landcover: npt.NDArray[np.uint8],
    rows: slice,
    cols: slice,
    height: float,
) -> None:
    dsm[rows, cols] = height
    landcover[rows, cols] = Landcover.VEGETATION


def test_tall_vegetation_kept_threshold_inclusive() -> None:
    dsm, dtm, landcover = _scene()
    _plant(dsm, landcover, slice(4, 8), slice(4, 8), 2.5)  # exactly the threshold
    mask = canopy_mask(dsm, dtm, landcover)
    assert mask.dtype == np.uint8
    assert mask[4:8, 4:8].all()
    assert mask.sum() == 16


def test_low_vegetation_dropped() -> None:
    dsm, dtm, landcover = _scene()
    _plant(dsm, landcover, slice(4, 8), slice(4, 8), 2.49)
    assert canopy_mask(dsm, dtm, landcover).sum() == 0


def test_tall_non_vegetation_dropped() -> None:
    dsm, dtm, landcover = _scene()
    dsm[4:8, 4:8] = 10.0
    landcover[4:8, 4:8] = Landcover.BUILDING
    dsm[12:16, 12:16] = 10.0  # tall GROUND (e.g. a cliff): not canopy either
    assert canopy_mask(dsm, dtm, landcover).sum() == 0


def test_small_blob_sieved_away() -> None:
    dsm, dtm, landcover = _scene()
    _plant(dsm, landcover, slice(4, 6), slice(4, 6), 8.0)  # 2x2 = 4 px < 8
    assert canopy_mask(dsm, dtm, landcover).sum() == 0


def test_min_size_blob_survives() -> None:
    dsm, dtm, landcover = _scene()
    _plant(dsm, landcover, slice(4, 7), slice(4, 7), 8.0)  # 3x3 = 9 px >= 8
    mask = canopy_mask(dsm, dtm, landcover)
    assert mask[4:7, 4:7].all()
    assert mask.sum() == 9


def test_small_hole_inside_crown_is_filled() -> None:
    """Sieve removes small regions of EITHER value: sub-threshold holes close.

    Accepted bias, pinned here so it stays documented behavior: an 8 m2 gap
    inside a crown is shaded in practice anyway.
    """
    dsm, dtm, landcover = _scene()
    _plant(dsm, landcover, slice(4, 10), slice(4, 10), 8.0)  # 6x6 crown
    landcover[6, 6] = Landcover.GROUND  # 1 px enclosed hole
    mask = canopy_mask(dsm, dtm, landcover)
    assert mask[6, 6] == 1
    assert mask[4:10, 4:10].all()


def test_cli_canopy_derives_for_existing_artifacts(built_city: Path, tmp_path: Path) -> None:
    """`shade-engine canopy` backfills canopy.tif matching the build-time mask."""
    cities_dir = tmp_path / "cities"
    cities_dir.mkdir()
    (cities_dir / "cube.yaml").write_text(yaml.safe_dump(CUBE_CITY.model_dump(mode="json")))
    output_root = tmp_path / "data"
    artifact_dir = output_root / "cube" / "v1"
    shutil.copytree(built_city, artifact_dir)

    # Plant a 3x3, 8 m tall tree far from the cube so the derived mask has
    # content (the synthetic cube city has no vegetation at all).
    for name, patch in (
        (artifacts.DSM_FILENAME, 8.0),
        (artifacts.LANDCOVER_FILENAME, int(Landcover.VEGETATION)),
    ):
        with rasterio.open(artifact_dir / name) as src:
            data = src.read()[0]
            transform = src.transform
            crs = str(src.crs)
        data[10:13, 10:13] = patch
        write_cog(artifact_dir / name, data, transform, crs)
    (artifact_dir / artifacts.CANOPY_FILENAME).unlink()

    result = CliRunner().invoke(
        app,
        ["canopy", "cube", "--cities-dir", str(cities_dir), "--output-root", str(output_root)],
    )
    assert result.exit_code == 0, result.output
    assert "canopy.tif written" in result.output

    with rasterio.open(artifact_dir / artifacts.CANOPY_FILENAME) as src:
        derived = src.read()[0]
        tags = src.tags()
    expected = np.zeros_like(derived)
    expected[10:13, 10:13] = 1
    assert_array_equal(derived, expected)
    assert tags["min_height_m"] == "2.5"
    assert tags["sieve_px"] == "8"


def test_cli_canopy_requires_artifacts(tmp_path: Path) -> None:
    cities_dir = tmp_path / "cities"
    cities_dir.mkdir()
    (cities_dir / "cube.yaml").write_text(yaml.safe_dump(CUBE_CITY.model_dump(mode="json")))
    result = CliRunner().invoke(
        app,
        ["canopy", "cube", "--cities-dir", str(cities_dir), "--output-root", str(tmp_path / "d")],
    )
    assert result.exit_code == 1
    assert "run shade-engine build first" in result.output
