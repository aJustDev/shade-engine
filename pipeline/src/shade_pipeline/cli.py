"""CLI: ``shade-engine run|area|build|graph|tiles|publish`` plus the utilities.

``run`` walks the whole chain for a city and is what an unattended build uses;
the individual commands remain, both because they are what ``run`` calls and
because a single phase is often all you want.
"""

import subprocess
import sys
import time
from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer

from shade_core.artifacts import METADATA_FILENAME
from shade_core.config import CityConfig, load_city
from shade_core.db import make_engine
from shade_pipeline.area import (
    WGS84,
    AreaError,
    area_geojson,
    format_plan,
    plan_area,
    read_area,
    rewrite_config,
)
from shade_pipeline.budget import MemoryBudgetError, cpu_budget
from shade_pipeline.build import ARTIFACT_VERSION, build_city
from shade_pipeline.canopy import CANOPY_MIN_HEIGHT_M, CANOPY_SIEVE_PX, derive_canopy
from shade_pipeline.cnig import CnigError, CnigSource
from shade_pipeline.footprints import OsmnxFootprintSource
from shade_pipeline.graph import DEFAULT_OSM_CACHE, DEFAULT_SPACING_M, OsmnxWalkSource, build_graph
from shade_pipeline.horizon import HorizonParams
from shade_pipeline.layers import import_parking_layer
from shade_pipeline.predict import prediction_table, read_points
from shade_pipeline.preview import (
    DEFAULT_API_PORT,
    DEFAULT_WEB_DIR,
    DEFAULT_WEB_PORT,
    PreviewError,
    preview,
)
from shade_pipeline.progress import format_bytes, format_duration
from shade_pipeline.publish import (
    DEFAULT_BASE_URL,
    DEFAULT_HOST,
    DEFAULT_REMOTE_ROOT,
    PublishError,
    check_ready,
    execute,
    plan_publish,
)
from shade_pipeline.recolor import PALETTES, recolor_city
from shade_pipeline.runner import (
    CHAIN,
    ChainError,
    ChainOptions,
    run_chain,
    step_scope,
    steps_between,
)
from shade_pipeline.runstate import RunState, StepStatus
from shade_pipeline.sources import CoverageError, LidarSource, LocalDirectory
from shade_pipeline.tiles import (
    DEFAULT_MAX_ZOOM,
    DEFAULT_MIN_ZOOM,
    build_tiles,
    season_preset_instants,
)
from shade_pipeline.trees import WfsTreeSource
from shade_pipeline.verify import VerificationError, format_report, verify_artifacts

app = typer.Typer(help="Offline pipeline that turns LiDAR into per-city shade artifacts.")


def _make_source(
    config: CityConfig,
    lidar_dir: Path | None,
    cache_dir: Path | None,
    progress: Callable[[str], None] = typer.echo,
) -> LidarSource:
    """Pick the LiDAR driver: an explicit --lidar-dir always wins over downloads.

    ``progress`` is a parameter and not simply ``typer.echo`` because the
    download is the first thing a fresh city does and it takes about a minute
    per PNOA tile. Sent to stdout it vanishes in a detached run -- which made a
    perfectly healthy twelve-minute download look like a hang, with the log
    stopped on the computation-area line and nothing else to see.
    """
    if lidar_dir is not None:
        return LocalDirectory(lidar_dir)
    if config.sources.get("lidar") == "pnoa":
        return CnigSource(
            cache_dir if cache_dir is not None else Path("data/lidar") / config.id,
            config.crs,
            cod_serie=config.sources.get("pnoa_series", "LIDA3"),
            progress=progress,
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
def run(
    city: str,
    from_step: Annotated[
        str | None, typer.Option("--from", help=f"First step to run ({', '.join(CHAIN)})")
    ] = None,
    to_step: Annotated[str | None, typer.Option("--to", help="Last step to run")] = None,
    only: Annotated[str | None, typer.Option(help="Run exactly this step and nothing else")] = None,
    workers: Annotated[
        int, typer.Option(min=1, help="Processes for the sweep and the tile render")
    ] = 1,
    tile_size: Annotated[int, typer.Option(help="Horizon sweep tile size, pixels")] = 512,
    min_zoom: Annotated[int, typer.Option(help="Web Mercator min zoom")] = DEFAULT_MIN_ZOOM,
    max_zoom: Annotated[int, typer.Option(help="Web Mercator max zoom")] = DEFAULT_MAX_ZOOM,
    lidar_dir: Annotated[
        Path | None, typer.Option(help="Directory with LAZ/LAS tiles; overrides the config driver")
    ] = None,
    cache_dir: Annotated[Path | None, typer.Option(help="LiDAR download cache")] = None,
    resume: Annotated[
        bool, typer.Option("--resume/--no-resume", help="Keep tile units already rendered")
    ] = True,
    force: Annotated[
        bool, typer.Option("--force", help="Redo steps whose state already says done")
    ] = False,
    detach: Annotated[
        bool, typer.Option("--detach", help="Run in the background and return immediately")
    ] = False,
    cities_dir: Annotated[Path, typer.Option(help="Directory holding <city>.yaml configs")] = Path(
        "cities"
    ),
    output_root: Annotated[Path, typer.Option(help="Artifact output root")] = Path("data/cities"),
    data_root: Annotated[Path, typer.Option(help="Where run state and logs live")] = Path("data"),
) -> None:
    """Walk CITY through the build chain, recording each step so it can be resumed.

    Stops before ``publish``: what is worth doing unattended is building a city,
    not deciding it is fit to serve. Run ``shade-engine publish`` for that.

    With ``--detach`` the chain runs in its own session and survives the
    terminal that started it, which is what a six-hour render needs; follow it
    with ``shade-engine status CITY``.
    """
    if detach:
        # start_new_session is setsid: the child leaves this process group, so
        # closing the terminal (or losing the ssh session) cannot signal it.
        child = subprocess.Popen(
            [argument for argument in sys.argv if argument != "--detach"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        typer.echo(f"{city}: running in the background as pid {child.pid}")
        typer.echo(f"follow it with: shade-engine status {city}")
        return

    if only is not None:
        steps: tuple[str, ...] = (only,)
        if only not in CHAIN:
            typer.echo(f"error: unknown step {only!r}; the chain is {', '.join(CHAIN)}", err=True)
            raise typer.Exit(1)
    else:
        try:
            steps = steps_between(from_step, to_step)
        except ChainError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc

    options = ChainOptions(
        cities_dir=cities_dir,
        output_root=output_root,
        data_root=data_root,
        workers=workers,
        tile_size=tile_size,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        resume=resume,
        force=force,
    )
    typer.echo(f"{city}: {' -> '.join(steps)}")
    try:
        outcomes = run_chain(
            city,
            steps=steps,
            options=options,
            source=lambda config, say: _make_source(config, lidar_dir, cache_dir, say),
            graph_source=lambda: OsmnxWalkSource(
                cache_dir if cache_dir is not None else DEFAULT_OSM_CACHE
            ),
            progress=typer.echo,
        )
    except (ChainError, CoverageError, CnigError, MemoryBudgetError, VerificationError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (ValueError, FileNotFoundError, OSError, RuntimeError) as exc:
        # The step already recorded its own failure; this is the exit code.
        typer.echo(f"error: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(1) from exc
    done = [outcome for outcome in outcomes if outcome.status is StepStatus.DONE]
    typer.echo(f"{city}: {len(done)} of {len(steps)} steps done")


@app.command()
def console(
    watch_dir: Annotated[
        Path | None,
        typer.Option(help="Where a drawn .geojson gets exported to (default: ~/Descargas)"),
    ] = None,
    cities_dir: Annotated[Path, typer.Option(help="Directory holding <city>.yaml configs")] = Path(
        "cities"
    ),
    output_root: Annotated[Path, typer.Option(help="Artifact output root")] = Path("data/cities"),
    data_root: Annotated[Path, typer.Option(help="Where run state and logs live")] = Path("data"),
) -> None:
    """Open the operations console: every city, every step, and what each knob costs.

    Needs the optional extra: ``uv sync --all-extras`` (or install
    ``shade-pipeline[tui]``). The console owns nothing -- it reads the run state
    and launches work detached -- so closing it never touches a running build.
    """
    try:
        from shade_pipeline.console import run_console
    except ModuleNotFoundError as exc:
        typer.echo(
            "error: the console needs the 'tui' extra; install it with "
            "`uv sync --all-extras` or `pip install 'shade-pipeline[tui]'`",
            err=True,
        )
        raise typer.Exit(1) from exc
    run_console(
        cities_dir=cities_dir,
        output_root=output_root,
        data_root=data_root,
        watch_dir=watch_dir,
    )


@app.command("preview")
def preview_command(
    city: Annotated[str | None, typer.Argument(help="Only to check it is servable")] = None,
    api_port: Annotated[int, typer.Option(help="Port for the local API")] = DEFAULT_API_PORT,
    web_port: Annotated[int, typer.Option(help="Port for the viewer")] = DEFAULT_WEB_PORT,
    web_dir: Annotated[
        Path, typer.Option(help="Checkout of shade-web; skipped if absent")
    ] = DEFAULT_WEB_DIR,
    cities_dir: Annotated[Path, typer.Option(help="Directory holding <city>.yaml configs")] = Path(
        "cities"
    ),
    output_root: Annotated[Path, typer.Option(help="Artifact output root")] = Path("data/cities"),
) -> None:
    """Serve the built artifacts locally and open the viewer on them, until Ctrl-C.

    The step that actually looks at the result instead of at numbers, and the
    one that used to be skipped because it meant starting two servers by hand in
    two repositories. A city with no basemap extract previews fine: the client
    falls back to OSM online, so the overlay is what production will show.
    """
    if city is not None:
        artifact_dir = output_root / city / ARTIFACT_VERSION
        if not (artifact_dir / METADATA_FILENAME).exists():
            typer.echo(f"error: no artifacts under {artifact_dir}", err=True)
            raise typer.Exit(1)
    try:
        with preview(
            cities_dir=cities_dir,
            output_root=output_root,
            web_dir=web_dir,
            api_port=api_port,
            web_port=web_port,
            progress=typer.echo,
        ) as running:
            typer.echo(f"\nopen {running.url}" + (f"  (city: {city})" if city else ""))
            typer.echo("Ctrl-C to stop\n")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                typer.echo("stopping")
    except PreviewError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def publish(
    city: str,
    host: Annotated[str, typer.Option(help="ssh host holding the deployment")] = DEFAULT_HOST,
    remote_root: Annotated[
        str, typer.Option(help="Deployment directory on that host")
    ] = DEFAULT_REMOTE_ROOT,
    base_url: Annotated[str, typer.Option(help="Public API, for the checks")] = DEFAULT_BASE_URL,
    recolor_theme: Annotated[
        bool, typer.Option("--recolor/--no-recolor", help="Generate tiles-light on the server")
    ] = True,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the commands in order and run none of them")
    ] = False,
    cities_dir: Annotated[Path, typer.Option(help="Directory holding <city>.yaml configs")] = Path(
        "cities"
    ),
    output_root: Annotated[Path, typer.Option(help="Artifact output root")] = Path("data/cities"),
    data_root: Annotated[Path, typer.Option(help="Where run state and logs live")] = Path("data"),
) -> None:
    """Put CITY's built artifacts into production: send everything, then restart.

    Rasters, graph, metadata, the tile pyramid and the city's own YAML all
    travel by rsync; the restart at the end is what makes the API read the new
    configs and drop the raster blocks it had cached. New city or tenth rebuild,
    it is the same order.

    ``--dry-run`` prints every command and runs none, which is also the fastest
    way to review what this would do to the server.
    """
    config = load_city(cities_dir / f"{city}.yaml")
    artifact_dir = output_root / config.id / ARTIFACT_VERSION
    try:
        notes = check_ready(config, artifact_dir, cities_dir)
        plan = plan_publish(
            config,
            artifact_dir,
            host=host,
            remote_root=remote_root,
            base_url=base_url,
            cities_dir=cities_dir,
            recolor=recolor_theme,
        )
    except PublishError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc

    if dry_run:
        for note in notes:
            typer.echo(f"  {note}")
        typer.echo(plan.render())
        typer.echo("\nnothing run; drop --dry-run to do it")
        return

    state = RunState.open(city, cities_dir=cities_dir, data_root=data_root)
    try:
        # step_scope is what opens the log: recording the step and opening its
        # file are one act, and doing them separately is how publish spent its
        # first life naming a log that never existed.
        with step_scope(state, "publish", {"host": host, "commands": len(plan.commands)}) as say:

            def report(message: str) -> None:
                say(message)
                typer.echo(message)

            for note in notes:
                report(f"  {note}")
            execute(plan, progress=report)
    except PublishError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    state.complete("publish")
    typer.echo(f"{city} is live at {base_url}")


@app.command()
def status(
    city: Annotated[str | None, typer.Argument(help="One city, or all of them")] = None,
    cities_dir: Annotated[Path, typer.Option(help="Directory holding <city>.yaml configs")] = Path(
        "cities"
    ),
    data_root: Annotated[Path, typer.Option(help="Where run state and logs live")] = Path("data"),
) -> None:
    """Where each city stands in the chain: one row per city, one column per step."""
    names = [city] if city else sorted(path.stem for path in cities_dir.glob("*.yaml"))
    if not names:
        typer.echo(f"no city configs under {cities_dir}")
        return
    width = max(len(name) for name in names)
    typer.echo(" " * width + "  " + "  ".join(f"{step:>8}" for step in CHAIN))
    for name in names:
        if not (cities_dir / f"{name}.yaml").exists():
            typer.echo(f"error: no config at {cities_dir / f'{name}.yaml'}", err=True)
            raise typer.Exit(1)
        state = RunState.open(name, cities_dir=cities_dir, data_root=data_root)
        cells = [f"{state.status(step).value:>8}" for step in CHAIN]
        typer.echo(f"{name:<{width}}  " + "  ".join(cells))
        for step in CHAIN:
            record = state.record(step)
            if state.status(step) is StepStatus.FAILED and record.error:
                typer.echo(f"{' ' * width}  {step}: {record.error}")
            if state.status(step) is StepStatus.RUNNING:
                typer.echo(f"{' ' * width}  {step}: pid {record.pid}, log {record.log}")


@app.command()
def area(
    city: str,
    geojson: Annotated[
        Path, typer.Argument(help="Drawn area as GeoJSON (EPSG:4326 unless --geojson-crs)")
    ],
    cities_dir: Annotated[Path, typer.Option(help="Directory holding <city>.yaml configs")] = Path(
        "cities"
    ),
    geojson_crs: Annotated[
        str, typer.Option(help="CRS of the input file; GeoJSON is EPSG:4326 by RFC 7946")
    ] = WGS84,
    tile_size: Annotated[int, typer.Option(help="Sweep tile size the estimates assume")] = 256,
    workers: Annotated[
        int | None, typer.Option(min=1, help="Workers to price (default: the cores available)")
    ] = None,
    cache_dir: Annotated[
        Path | None, typer.Option(help="LiDAR cache to check (default: data/lidar/<city>)")
    ] = None,
    area_path: Annotated[
        Path | None,
        typer.Option(
            help="Where the normalized area goes (default: <cities-dir>/<city>/area.geojson)"
        ),
    ] = None,
    write: Annotated[
        bool, typer.Option("--write", help="Apply: write the area file and edit the city YAML")
    ] = False,
) -> None:
    """Price CITY's computation area from a drawn polygon, and optionally apply it.

    Drawing is somebody else's job (geojson.io, QGIS); this does the arithmetic
    that decides whether the shape is worth it: the bbox it implies, the sweep
    tiles it skips at each tile size, the minutes and memory it costs, and the
    PNOA tiles still missing from the cache. Without ``--write`` it only reports.
    """
    config_path = cities_dir / f"{city}.yaml"
    if not config_path.exists():
        typer.echo(
            f"error: {config_path} not found; write the city YAML first "
            "(a provisional bbox is enough, this command replaces it)",
            err=True,
        )
        raise typer.Exit(1)
    config = load_city(config_path)
    destination = area_path if area_path is not None else cities_dir / config.id / "area.geojson"
    try:
        drawn = read_area(geojson, config.crs, source_crs=geojson_crs)
    except AreaError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    plan = plan_area(
        config,
        drawn,
        geojson,
        tile_size=tile_size,
        workers=workers if workers is not None else cpu_budget(),
        cache_dir=cache_dir if cache_dir is not None else Path("data/lidar") / config.id,
        area_path=destination,
        config_path=config_path,
    )
    typer.echo(format_plan(plan, config))
    if not write:
        typer.echo("\nnothing written; rerun with --write to apply")
        return
    try:
        updated = rewrite_config(config_path.read_text(encoding="utf-8"), plan.bbox, destination)
    except (AreaError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(area_geojson(drawn, config.id), encoding="utf-8")
    config_path.write_text(updated, encoding="utf-8")
    typer.echo(f"\nwrote {destination} and updated {config_path}")


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
    tree_inventory: Annotated[
        bool,
        typer.Option(
            "--tree-inventory/--no-tree-inventory",
            help="Audit the canopy mask against the city's tree inventory, if it declares one",
        ),
    ] = True,
    declutter: Annotated[
        bool,
        typer.Option(
            "--declutter/--no-declutter",
            help="Remove cables and awnings from the DSM before sweeping (ADR-022)",
        ),
    ] = True,
) -> None:
    """Build the raster artifacts for CITY, downloading LiDAR tiles if configured."""
    config = load_city(cities_dir / f"{city}.yaml")
    source = _make_source(config, lidar_dir, cache_dir)
    trees = (
        WfsTreeSource(url=config.tree_inventory.wfs, layers=tuple(config.tree_inventory.layers))
        if tree_inventory and config.tree_inventory is not None
        else None
    )
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
            trees=trees,
            declutter=declutter,
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
    workers: Annotated[
        int,
        typer.Option(
            min=1,
            help="Processes rendering instants in parallel (1 = serial); "
            "output is identical whatever the count",
        ),
    ] = 1,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume/--no-resume",
            help="Keep units already rendered from these artifacts and this zoom range",
        ),
    ] = False,
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
            workers=workers,
            resume=resume,
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
