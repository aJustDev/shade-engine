"""Command line interface: ``shade-engine build <city>``."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from shade_core.config import CityConfig, load_city
from shade_pipeline.build import build_city
from shade_pipeline.cnig import CnigError, CnigSource
from shade_pipeline.horizon import HorizonParams
from shade_pipeline.sources import CoverageError, LidarSource, LocalDirectory

app = typer.Typer(help="Offline pipeline that turns LiDAR into per-city shade artifacts.")


def _make_source(config: CityConfig, lidar_dir: Path | None, cache_dir: Path | None) -> LidarSource:
    """Pick the LiDAR driver: an explicit --lidar-dir always wins over downloads."""
    if lidar_dir is not None:
        return LocalDirectory(lidar_dir)
    if config.sources.get("lidar") == "pnoa":
        return CnigSource(
            cache_dir if cache_dir is not None else Path("data/lidar") / config.id,
            config.crs,
            cod_serie=config.sources.get("pnoa_series", "LIDA3"),
            progress=typer.echo,
        )
    typer.echo("error: no lidar driver configured for this city; pass --lidar-dir", err=True)
    raise typer.Exit(1)


class StepMode(StrEnum):
    """CLI mirror of ``HorizonParams.step_mode`` (typer needs an Enum, not a Literal)."""

    exact = "exact"
    geometric = "geometric"


@app.callback()
def main() -> None:
    """Keep ``build`` a subcommand even while it is the only command."""


@app.command()
def build(
    city: str,
    cities_dir: Annotated[Path, typer.Option(help="Directory holding <city>.yaml configs")] = Path(
        "cities"
    ),
    lidar_dir: Annotated[
        Path | None, typer.Option(help="Directory with LAZ/LAS tiles covering the padded bbox")
    ] = None,
    output_root: Annotated[Path, typer.Option(help="Artifact output root")] = Path("data/cities"),
    cache_dir: Annotated[
        Path | None,
        typer.Option(help="Download cache for the CNIG driver (default: data/lidar/<city>)"),
    ] = None,
    tile_size: Annotated[int, typer.Option(help="Horizon sweep tile size, pixels")] = 512,
    step_mode: Annotated[
        StepMode,
        typer.Option(help="Horizon distance schedule: exact (half-pixel) or geometric (growing)"),
    ] = StepMode.exact,
) -> None:
    """Build the raster artifacts for CITY, downloading LiDAR tiles if configured."""
    config = load_city(cities_dir / f"{city}.yaml")
    source = _make_source(config, lidar_dir, cache_dir)
    params = HorizonParams(
        sectors=config.horizon_sectors,
        max_distance_m=config.horizon_max_distance_m,
        observer_height_m=config.observer_height_m,
        tile_size=tile_size,
        step_mode="exact" if step_mode is StepMode.exact else "geometric",
    )
    try:
        out_dir = build_city(config, source, output_root, params)
    except (CoverageError, CnigError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"artifacts written to {out_dir}")
