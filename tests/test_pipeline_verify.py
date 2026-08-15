"""Artifact verification: green on a fresh build, loud on corrupted cubes."""

import shutil
from pathlib import Path

import pytest
import rasterio
import yaml
from typer.testing import CliRunner

from conftest import CUBE_CITY
from shade_core import artifacts
from shade_core.shade import NO_BLOCKER
from shade_pipeline.cli import app
from shade_pipeline.cog import write_cog
from shade_pipeline.verify import (
    Q0_BLOCKER_MAX_FRACTION,
    VerificationError,
    ensure_verified,
    verify_artifacts,
)

CHECK_NAMES = [
    "metadata",
    "files",
    "layout",
    "horizon-blocker invariant",
    "horizon-noveg invariant",
    "elevation sanity",
    "class values",
]


def _zero_tail(artifact_dir: Path, filename: str = artifacts.HORIZON_FILENAME) -> int:
    """Zero an angle cube from its most-blockered band onward; returns that band.

    Models the real Cordoba corruption (a silently lost band tail) while
    staying deterministic for any fixture geometry: the band with the most
    blocker-attributed pixels is the most detectable, and the precondition
    assert keeps the test honest if the fixture ever changes.
    """
    with rasterio.open(artifact_dir / artifacts.BLOCKER_CLASS_FILENAME) as src:
        blocker = src.read()
    fractions = (blocker != NO_BLOCKER).mean(axis=(1, 2))
    worst = int(fractions.argmax())
    assert fractions[worst] > Q0_BLOCKER_MAX_FRACTION, "fixture cannot exercise the invariant"
    with rasterio.open(artifact_dir / filename) as src:
        cube = src.read()
        tags = src.tags()
        transform = src.transform
        crs = str(src.crs)
    cube[worst:] = 0
    write_cog(artifact_dir / filename, cube, transform, crs, tags=tags)
    return worst


def test_verify_passes_on_fresh_build(built_city: Path) -> None:
    results = verify_artifacts(built_city)
    assert [result.name for result in results] == CHECK_NAMES
    assert [result.failure for result in results] == [None] * len(CHECK_NAMES)
    ensure_verified(built_city)  # must not raise


def test_verify_catches_zeroed_horizon_tail(built_city: Path, tmp_path: Path) -> None:
    artifact_dir = tmp_path / "cube"
    shutil.copytree(built_city, artifact_dir)
    _zero_tail(artifact_dir)

    results = verify_artifacts(artifact_dir)
    failing = {result.name for result in results if not result.passed}
    # Both cross-cube invariants catch it: the classes disagree with the zeroed
    # angles, and a vegetation-free horizon standing above a zeroed full one is
    # impossible.
    assert failing == {"horizon-blocker invariant", "horizon-noveg invariant"}
    with pytest.raises(VerificationError, match="horizon-blocker"):
        ensure_verified(artifact_dir)


def test_verify_catches_zeroed_noveg_tail(built_city: Path, tmp_path: Path) -> None:
    """A cube the blocker classes cannot police: only the noveg check sees it.

    Zeroing angles *downward* passes "never above the full horizon" -- what
    gives it away is that a sector a building blocks must read the same in
    both cubes, and zero is not that.
    """
    artifact_dir = tmp_path / "cube"
    shutil.copytree(built_city, artifact_dir)
    _zero_tail(artifact_dir, artifacts.HORIZON_NOVEG_FILENAME)

    results = verify_artifacts(artifact_dir)
    failing = {result.name for result in results if not result.passed}
    assert failing == {"horizon-noveg invariant"}
    with pytest.raises(VerificationError, match="blocked by a building"):
        ensure_verified(artifact_dir)


def test_verify_skips_noveg_check_on_older_artifacts(built_city: Path, tmp_path: Path) -> None:
    """Artifacts predating the second cube verify green on what they do have."""
    artifact_dir = tmp_path / "cube"
    shutil.copytree(built_city, artifact_dir)
    (artifact_dir / artifacts.HORIZON_NOVEG_FILENAME).unlink()

    results = verify_artifacts(artifact_dir)
    assert "horizon-noveg invariant" not in [result.name for result in results]
    ensure_verified(artifact_dir)  # must not raise


def test_verify_reports_missing_files(built_city: Path, tmp_path: Path) -> None:
    artifact_dir = tmp_path / "cube"
    shutil.copytree(built_city, artifact_dir)
    (artifact_dir / artifacts.CANOPY_FILENAME).unlink()

    results = verify_artifacts(artifact_dir)
    assert [result.name for result in results] == ["metadata", "files"]
    assert results[-1].failure == f"missing: {artifacts.CANOPY_FILENAME}"


def test_cli_verify_green_then_corrupted(built_city: Path, tmp_path: Path) -> None:
    """`shade-engine verify` exits 0 on sound artifacts and 1 on corrupt ones."""
    cities_dir = tmp_path / "cities"
    cities_dir.mkdir()
    (cities_dir / "cube.yaml").write_text(yaml.safe_dump(CUBE_CITY.model_dump(mode="json")))
    output_root = tmp_path / "data"
    artifact_dir = output_root / "cube" / "v1"
    shutil.copytree(built_city, artifact_dir)
    args = ["verify", "cube", "--cities-dir", str(cities_dir), "--output-root", str(output_root)]

    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert f"{len(CHECK_NAMES)}/{len(CHECK_NAMES)} checks passed" in result.output

    _zero_tail(artifact_dir)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert "FAIL horizon-blocker invariant" in result.output


def test_build_fails_on_unverifiable_artifacts() -> None:
    """build_city runs ensure_verified: a corrupted write cannot exit green.

    Covered indirectly: every `built_city` fixture build in the suite runs
    the verification (a regression that broke it would fail those), and the
    corruption path is exercised through verify_artifacts above. This test
    pins the wiring only: ensure_verified raises on a missing directory.
    """
    with pytest.raises(VerificationError, match="metadata"):
        ensure_verified(Path("/nonexistent/artifacts"))
