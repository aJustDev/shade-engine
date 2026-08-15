"""CLI: ``shade-engine build|predict|canopy|verify|import-layer|tiles|recolor|graph <city>``."""

import time
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer

from shade_core.artifacts import METADATA_FILENAME
from shade_core.config import CityConfig, load_city
from shade_core.db import make_engine
from shade_pipeline.budget import MemoryBudgetError
from shade_pipeline.build import ARTIFACT_VERSION, build_city
from shade_pipeline.canopy import CANOPY_MIN_HEIGHT_M, CANOPY_SIEVE_PX, derive_canopy
from shade_pipeline.cnig import CnigError, CnigSource
from shade_pipeline.footprints import OsmnxFootprintSource
from shade_pipeline.graph import DEFAULT_OSM_CACHE, DEFAULT_SPACING_M, OsmnxWalkSource, build_graph
from shade_pipeline.horizon import HorizonParams
from shade_pipeline.layers import import_parking_layer
from shade_pipeline.predict import prediction_table, read_points
from shade_pipeline.progress import format_bytes, format_duration
from shade_pipeline.recolor import PALETTES, recolor_city
from shade_pipeline.sources import CoverageError, LidarSource, LocalDirectory
from shade_pipeline.tiles import (
    DEFAULT_MAX_ZOOM,
    DEFAULT_MIN_ZOOM,
    build_tiles,
    season_preset_instants,
)
from shade_pipeline.verify import VerificationError, format_report, verify_artifacts

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
    """Group callback so commands stay subcommands (build, predict)."""


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
    workers: Annotated[
        int,
        typer.Option(
            min=1,
            help="Processes sweeping horizon tiles in parallel (1 = serial); "
            "output is identical whatever the count",
        ),
    ] = 1,
    step_mode: Annotated[
        StepMode,
        typer.Option(help="Horizon distance schedule: exact (half-pixel) or geometric (growing)"),
    ] = StepMode.exact,
    footprints: Annotated[
        bool,
        typer.Option(
            "--footprints/--no-footprints",
            help="Correct roof-height vegetation with OSM building outlines (needs Overpass)",
        ),
    ] = True,
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
        workers=workers,
    )
    try:
        out_dir = build_city(
            config,
            source,
            output_root,
            params,
            progress=typer.echo,
            footprints=OsmnxFootprintSource() if footprints else None,
        )
    except (CoverageError, CnigError, VerificationError, MemoryBudgetError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"artifacts written to {out_dir}")


@app.command()
def predict(
    city: str,
    points_csv: Annotated[Path, typer.Argument(help="CSV with id,name,lat,lon columns")],
    day: Annotated[str, typer.Option(help="Local calendar day, YYYY-MM-DD")],
    cities_dir: Annotated[Path, typer.Option(help="Directory holding <city>.yaml configs")] = Path(
        "cities"
    ),
    output_root: Annotated[Path, typer.Option(help="Artifact output root")] = Path("data/cities"),
) -> None:
    """Print the predicted shade timeline of each field point for DAY."""
    config = load_city(cities_dir / f"{city}.yaml")
    artifact_dir = output_root / config.id / ARTIFACT_VERSION
    if not (artifact_dir / METADATA_FILENAME).exists():
        typer.echo(
            f"error: no artifacts under {artifact_dir}; run shade-engine build first", err=True
        )
        raise typer.Exit(1)
    table = prediction_table(config, artifact_dir, read_points(points_csv), date.fromisoformat(day))
    typer.echo(table)


@app.command()
def canopy(
    city: str,
    cities_dir: Annotated[Path, typer.Option(help="Directory holding <city>.yaml configs")] = Path(
        "cities"
    ),
    output_root: Annotated[Path, typer.Option(help="Artifact output root")] = Path("data/cities"),
) -> None:
    """Derive CITY's canopy mask artifact (canopy.tif) from its existing rasters.

    ``build`` writes the mask itself; this backfills artifact directories
    built before the mask existed, without re-running the horizon sweep.
    """
    config = load_city(cities_dir / f"{city}.yaml")
    artifact_dir = output_root / config.id / ARTIFACT_VERSION
    if not (artifact_dir / METADATA_FILENAME).exists():
        typer.echo(
            f"error: no artifacts under {artifact_dir}; run shade-engine build first", err=True
        )
        raise typer.Exit(1)
    typer.echo(
        f"deriving canopy mask (vegetation with height >= {CANOPY_MIN_HEIGHT_M} m, "
        f"sieve {CANOPY_SIEVE_PX} px)"
    )
    start = time.monotonic()
    try:
        path, canopy_px, total_px = derive_canopy(artifact_dir)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"{path.name} written ({format_bytes(path.stat().st_size)}, "
        f"{format_duration(time.monotonic() - start)}): "
        f"{canopy_px:,} of {total_px:,} px under canopy ({100.0 * canopy_px / total_px:.1f}%)"
    )


@app.command()
def verify(
    city: str,
    cities_dir: Annotated[Path, typer.Option(help="Directory holding <city>.yaml configs")] = Path(
        "cities"
    ),
    output_root: Annotated[Path, typer.Option(help="Artifact output root")] = Path("data/cities"),
) -> None:
    """Audit CITY's artifacts: layout, value ranges and the horizon-blocker invariant.

    ``build`` verifies its own output; this audits any artifact directory
    after the fact -- a fresh build, or one already rsynced to a server. It
    is the check that would have caught the corrupted horizon cube Cordoba's
    first build shipped (western sectors silently zeroed).
    """
    config = load_city(cities_dir / f"{city}.yaml")
    artifact_dir = output_root / config.id / ARTIFACT_VERSION
    if not (artifact_dir / METADATA_FILENAME).exists():
        typer.echo(
            f"error: no artifacts under {artifact_dir}; run shade-engine build first", err=True
        )
        raise typer.Exit(1)
    results = verify_artifacts(artifact_dir, progress=typer.echo)
    typer.echo(format_report(results))
    if any(not result.passed for result in results):
        typer.echo("error: artifact verification failed", err=True)
        raise typer.Exit(1)


@app.command()
def tiles(
    city: str,
    at: Annotated[
        list[str] | None,
        typer.Option(
            "--at",
            help="Local ISO instant, repeatable (naive = city timezone); "
            "default: the 2026 solstice/equinox preset",
        ),
    ] = None,
    min_zoom: Annotated[int, typer.Option(help="Web Mercator min zoom")] = DEFAULT_MIN_ZOOM,
    max_zoom: Annotated[
        int, typer.Option(help="Web Mercator max zoom (17 ~ 1 m/px at lat 37.9)")
    ] = DEFAULT_MAX_ZOOM,
    cities_dir: Annotated[Path, typer.Option(help="Directory holding <city>.yaml configs")] = Path(
        "cities"
    ),
    output_root: Annotated[Path, typer.Option(help="Artifact output root")] = Path("data/cities"),
) -> None:
    """Render CITY's per-instant shade overlay PMTiles plus their index manifest."""
    config = load_city(cities_dir / f"{city}.yaml")
    artifact_dir = output_root / config.id / ARTIFACT_VERSION
    if not (artifact_dir / METADATA_FILENAME).exists():
        typer.echo(
            f"error: no artifacts under {artifact_dir}; run shade-engine build first", err=True
        )
        raise typer.Exit(1)
    zone = ZoneInfo(config.timezone)
    if at:
        # Same rule as the API's ?at=: a naive instant means the city's local
        # clock; an explicit offset is honored and converted.
        instants = []
        for value in at:
            parsed = datetime.fromisoformat(value)
            instants.append(
                parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)
            )
    else:
        instants = season_preset_instants(zone)
    try:
        out_dir = build_tiles(
            config,
            artifact_dir,
            instants,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            progress=typer.echo,
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"tiles written to {out_dir}")


@app.command()
def graph(
    city: str,
    cities_dir: Annotated[Path, typer.Option(help="Directory holding <city>.yaml configs")] = Path(
        "cities"
    ),
    output_root: Annotated[Path, typer.Option(help="Artifact output root")] = Path("data/cities"),
    cache_dir: Annotated[
        Path | None, typer.Option(help="OSM download cache (default: data/cache/osm)")
    ] = None,
    spacing: Annotated[float, typer.Option(help="Edge sampling step, meters")] = DEFAULT_SPACING_M,
) -> None:
    """Build CITY's pedestrian graph artifact with per-edge sun fractions.

    Downloads the walk network from OpenStreetMap (Overpass, cached under
    --cache-dir) and precomputes each edge's sun fraction against the
    declination-ladder instants, reusing the existing raster artifacts:
    run ``build`` first. Serving routes only needs the resulting
    ``graph/`` directory rsynced next to the other artifacts.
    """
    config = load_city(cities_dir / f"{city}.yaml")
    artifact_dir = output_root / config.id / ARTIFACT_VERSION
    if not (artifact_dir / METADATA_FILENAME).exists():
        typer.echo(
            f"error: no artifacts under {artifact_dir}; run shade-engine build first", err=True
        )
        raise typer.Exit(1)
    source = OsmnxWalkSource(cache_dir if cache_dir is not None else DEFAULT_OSM_CACHE)
    try:
        build_graph(config, artifact_dir, source, spacing_m=spacing, progress=typer.echo)
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("import-layer")
def import_layer(
    city: str,
    layer: str,
    database_url: Annotated[
        str,
        typer.Option(envvar="SHADE_DATABASE_URL", help="PostGIS URL, e.g. postgresql+psycopg://"),
    ],
    cities_dir: Annotated[Path, typer.Option(help="Directory holding <city>.yaml configs")] = Path(
        "cities"
    ),
) -> None:
    """Load CITY's LAYER (declared under ``layers:`` in its YAML) into PostGIS.

    Layer paths in the YAML are resolved against the working directory,
    like every other CLI path. Re-running replaces the city's rows.
    """
    config = load_city(cities_dir / f"{city}.yaml")
    if layer != "parking":
        typer.echo(f"error: unsupported layer {layer!r} (only 'parking' for now)", err=True)
        raise typer.Exit(1)
    declared = config.layers.get(layer)
    if declared is None:
        typer.echo(f"error: city {city!r} declares no {layer!r} layer in its YAML", err=True)
        raise typer.Exit(1)
    layer_path = Path(declared)
    if not layer_path.exists():
        typer.echo(f"error: layer file not found: {layer_path}", err=True)
        raise typer.Exit(1)
    engine = make_engine(database_url)
    try:
        count = import_parking_layer(config, layer_path, engine)
    finally:
        engine.dispose()
    typer.echo(f"imported {count} {layer} zones for {config.id}")


@app.command()
def recolor(
    city: str,
    palette: Annotated[str, typer.Option(help="Theme name; only 'light' for now")] = "light",
    output_root: Annotated[Path, typer.Option(help="Artifact output root")] = Path("data/cities"),
) -> None:
    """Write CITY's tile tree in another theme WITHOUT recomputing any shade.

    The tiles are paletted PNGs, so a theme swap rewrites 20 bytes per tile and
    leaves the pixel data alone: minutes of I/O instead of the hours a render
    costs. Output goes to a sibling tree (``v1/tiles-<palette>/``), which Caddy
    serves with no configuration change.
    """
    chosen = PALETTES.get(palette)
    if chosen is None:
        known = ", ".join(sorted(PALETTES))
        typer.echo(f"error: unknown palette {palette!r} (known: {known})", err=True)
        raise typer.Exit(1)

    started = time.perf_counter()
    try:
        report = recolor_city(output_root, city, chosen, progress=typer.echo)
    except FileNotFoundError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1) from error

    elapsed = format_duration(time.perf_counter() - started)
    typer.echo(
        f"{report.palette}: {report.tiles} tiles across {report.archives} archives "
        f"({', '.join(report.copied)} copied) -> {report.destination} in {elapsed}"
    )
