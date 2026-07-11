"""COG writing: layout contract and lossless roundtrip."""

from pathlib import Path

import numpy as np
import rasterio
from numpy.testing import assert_array_equal

from shade_pipeline.cog import write_cog
from shade_pipeline.grid import transform_from_bbox

BBOX = (0.0, 0.0, 80.0, 80.0)


def test_multiband_uint8_roundtrip(tmp_path: Path) -> None:
    data = np.arange(3 * 80 * 80, dtype=np.uint8).reshape(3, 80, 80)
    path = tmp_path / "horizon.tif"
    transform = transform_from_bbox(BBOX, 1.0)
    write_cog(path, data, transform, "EPSG:25830", tags={"sectors": "3"})

    with rasterio.open(path) as src:
        assert src.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
        assert src.profile["tiled"] is True
        assert src.profile["compress"] == "deflate"
        assert src.count == 3
        assert src.dtypes == ("uint8", "uint8", "uint8")
        assert src.crs == rasterio.crs.CRS.from_string("EPSG:25830")
        assert src.transform == transform
        assert src.tags()["sectors"] == "3"
        assert_array_equal(src.read(), data)


def test_single_band_float32_roundtrip(tmp_path: Path) -> None:
    data = np.linspace(0.0, 25.0, 80 * 80, dtype=np.float32).reshape(80, 80)
    path = tmp_path / "dsm.tif"
    write_cog(path, data, transform_from_bbox(BBOX, 1.0), "EPSG:25830")

    with rasterio.open(path) as src:
        assert src.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
        assert src.count == 1
        assert src.dtypes == ("float32",)
        assert_array_equal(src.read(1), data)
        assert not (tmp_path / "dsm.tif.tmp.tif").exists()
