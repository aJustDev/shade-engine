"""Command line interface: ``shade-engine build <city>``."""

from pathlib import Path
from typing import Annotated

import typer

from shade_core.config import load_city
from shade_pipeline.build import build_city
from shade_pipeline.horizon import HorizonParams
from shade_pipeline.sources import CoverageError, LocalDirectory

app = typer.Typer(help="Offline pipeline that turns LiDAR into per-city shade artifacts.")


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
    tile_size: Annotated[int, typer.Option(help="Horizon sweep tile size, pixels")] = 512,
) -> None:
    """Build the raster artifacts for CITY from local LiDAR tiles."""
    config = load_city(cities_dir / f"{city}.yaml")
    if lidar_dir is None:
        typer.echo("The PNOA downloader is not implemented yet; pass --lidar-dir", err=True)
        raise typer.Exit(1)
    params = HorizonParams(
        sectors=config.horizon_sectors,
        max_distance_m=config.horizon_max_distance_m,
        observer_height_m=config.observer_height_m,
        tile_size=tile_size,
    )
    try:
        out_dir = build_city(config, LocalDirectory(lidar_dir), output_root, params)
    except CoverageError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"artifacts written to {out_dir}")
