"""CLI: ``shade-engine run|area|basemap|build|graph|tiles|publish`` plus the utilities.

``run`` walks the whole chain for a city and is what an unattended build uses;
the individual commands remain, both because they are what ``run`` calls and
because a single phase is often all you want.
"""

import signal
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
from shade_pipeline.basemap import (
    ASSETS_DIRNAME,
    DEFAULT_MARGIN_M,
    SPRITE_SET,
    BasemapError,
    build_basemap,
    ensure_assets,
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
    claim_ports,
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
    plan_unpublish,
    unpublish_notes,
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
from shade_pipeline.runstate import (
    LATEST_DIRNAME,
    LOG_STEPS,
    RunState,
    StepStatus,
    config_digest,
)
from shade_pipeline.sources import CoverageError, LidarSource, LocalDirectory
from shade_pipeline.tiles import (
    BASEMAP_FILENAME,
    DEFAULT_MAX_ZOOM,
    DEFAULT_MIN_ZOOM,
    TILES_DIRNAME,
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
        typer.Option(
            help="Where a drawn .geojson gets exported to "
            "(default: ~/Descargas or ~/Downloads, if either is there)"
        ),
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
    data_root: Annotated[Path, typer.Option(help="Where run state and logs live")] = Path("data"),
) -> None:
    """Serve the built artifacts locally and open the viewer on them, until Ctrl-C.

    The step that actually looks at the result instead of at numbers, and the
    one that used to be skipped because it meant starting two servers by hand in
    two repositories. A city with no basemap extract, or a machine with no
    glyphs and sprites, is previewed with a warning saying so: the overlay would
    otherwise be drawn on black and look like a fault in the shade itself.

    Both servers log to ``data/runs/<city>/preview/``, which is what the console
    shows and ``shade-engine logs CITY preview`` prints. A preview that is up but
    wrong -- a tile it cannot find, a proxy answering 502 -- says so only there.
    """
    if city is not None:
        artifact_dir = output_root / city / ARTIFACT_VERSION
        if not (artifact_dir / METADATA_FILENAME).exists():
            typer.echo(f"error: no artifacts under {artifact_dir}", err=True)
            raise typer.Exit(1)
        # Warnings and not refusals: previewing the overlay alone is a perfectly
        # good reason to start this. But a black map with no streets is what a
        # missing backdrop looks like, and it looks exactly like a broken build.
        if not (artifact_dir / TILES_DIRNAME / BASEMAP_FILENAME).exists():
            typer.echo(
                f"warning: {city} has no {BASEMAP_FILENAME}; the viewer will fall back to "
                f"OSM online. Run `shade-engine basemap {city}` for the real backdrop"
            )
        if not (output_root / ASSETS_DIRNAME / SPRITE_SET).is_dir():
            typer.echo(
                f"warning: no glyphs or sprites under {output_root / ASSETS_DIRNAME}; the "
                f"basemap will draw with no labels at all. Run `shade-engine assets`"
            )

    state = log_path = None
    if city is not None:
        state = RunState.open(city, cities_dir=cities_dir, data_root=data_root)
        # Before recording anything. A second preview cannot work -- the ports
        # are taken -- and the damage was not the failure but the bookkeeping:
        # begin() marked it running, the port check then failed it, and the
        # record of the preview that *was* alive had already been overwritten.
        # After that the console could no longer see it to stop it, so every
        # press of `v` started another one.
        # And before recording, not after: a preview that cannot have the ports
        # is not a run, and letting it write a failed record was how the live
        # one got forgotten.
        try:
            claim_ports(api_port, web_port)
        except PreviewError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        current = state.record("preview")
        if state.status("preview") is StepStatus.RUNNING and current.is_alive:
            typer.echo(
                f"error: {city} is already being previewed by pid {current.pid}; "
                f"stop that one first (v in the console, or kill it)",
                err=True,
            )
            raise typer.Exit(1)
        log_path, events_path = state.paths_for("preview")
        state.begin(
            "preview",
            params={"api_port": api_port, "web_port": web_port},
            log=log_path,
            events=events_path,
        )

    # SIGTERM is how the console stops a preview, and the default action would
    # kill this process without unwinding -- orphaning both servers, which is
    # exactly the pile-up that makes the next preview land on another port.
    def stop(*_: object) -> None:
        # One shot. A second SIGTERM arriving while the first is still tearing
        # the servers down would raise inside the cleanup and abandon it half
        # done -- and pressing `v` twice during vite's slow first start is
        # exactly how that happens.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        with preview(
            cities_dir=cities_dir,
            output_root=output_root,
            web_dir=web_dir,
            api_port=api_port,
            web_port=web_port,
            log=log_path,
            progress=typer.echo,
        ) as running:
            typer.echo(f"\nopen {running.url}" + (f"  (city: {city})" if city else ""))
            if log_path is not None:
                typer.echo(f"both servers log to {log_path}")
            typer.echo("Ctrl-C to stop\n")
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        # Outside the `with`, not inside it, because a stop can arrive while the
        # servers are still coming up -- vite's first start re-optimizes its
        # dependencies and takes the best part of a minute. Caught in there, the
        # interrupt escaped past `except PreviewError` and the step was left
        # claiming to run for ever, with nothing alive behind it.
        typer.echo("stopping")
    except PreviewError as exc:
        if state is not None:
            state.fail("preview", str(exc))
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if state is not None:
        # Stopping a preview on purpose is how a preview ends; there is no other
        # way for it to finish, so this is `complete` and never `fail`.
        state.complete("preview")


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
def unpublish(
    city: str,
    host: Annotated[str, typer.Option(help="ssh host holding the deployment")] = DEFAULT_HOST,
    remote_root: Annotated[
        str, typer.Option(help="Deployment directory on that host")
    ] = DEFAULT_REMOTE_ROOT,
    base_url: Annotated[str, typer.Option(help="Public API, for the checks")] = DEFAULT_BASE_URL,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the commands in order and run none of them")
    ] = False,
    cities_dir: Annotated[Path, typer.Option(help="Directory holding <city>.yaml configs")] = Path(
        "cities"
    ),
    data_root: Annotated[Path, typer.Option(help="Where run state and logs live")] = Path("data"),
) -> None:
    """Take CITY off the server: delete its artifacts and config, then restart.

    The undo of ``publish``, and the way to rehearse it: with both halves gone,
    the next publish is a new city arriving rather than an update. Nothing local
    is touched -- the build stays where it is, and republishing costs only the
    upload.

    The step goes back to ``pending`` afterwards, because that is what it now is.

    ``--dry-run`` prints every command and runs none. Worth doing first: two of
    them are ``rm -rf`` on a production server.
    """
    config = load_city(cities_dir / f"{city}.yaml")
    try:
        notes = unpublish_notes(config, cities_dir)
        plan = plan_unpublish(config, host=host, remote_root=remote_root, base_url=base_url)
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
        # Recorded against `publish`, because `publish` is the step whose state
        # this changes; the console follows that log and the table shows it move.
        with step_scope(state, "publish", {"host": host, "undo": True}) as say:

            def report(message: str) -> None:
                say(message)
                typer.echo(message)

            report(f"removing {city} from {host}")
            for note in notes:
                report(f"  {note}")
            execute(plan, progress=report)
    except PublishError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    state.undo("publish")
    typer.echo(f"{city} is off {base_url}; publish it again when you want it back")


@app.command()
def logs(
    city: Annotated[str, typer.Argument(help="City whose runs to look at")],
    step: Annotated[
        str | None, typer.Argument(help=f"One of {', '.join(LOG_STEPS)}; omit to list them")
    ] = None,
    history: Annotated[
        bool, typer.Option("--history", help="Every run this city has had, oldest first")
    ] = False,
    path_only: Annotated[
        bool, typer.Option("--path", help="Print the path instead of the contents")
    ] = False,
    lines: Annotated[int, typer.Option("--lines", "-n", help="Show only the last N lines")] = 0,
    cities_dir: Annotated[Path, typer.Option(help="Directory holding <city>.yaml configs")] = Path(
        "cities"
    ),
    data_root: Annotated[Path, typer.Option(help="Where run state and logs live")] = Path("data"),
) -> None:
    """Find or print the log of a step, without guessing its timestamp.

    Every run writes a stamped file so a failed attempt survives the retry that
    replaced it, which makes the newest one unguessable: five ``publish-*.log``
    in a directory and none of them says which is today's. ``latest/<step>.log``
    is a symlink that always does, and this is how you find it.

    With no STEP it lists what there is. With one it prints the log, so
    ``shade-engine logs CITY publish -n 40`` is the quick look and ``--path``
    gives you something to hand to an editor or another pair of eyes.
    """
    state = RunState.open(city, cities_dir=cities_dir, data_root=data_root)
    # Self-healing: a city built before latest/ existed has its logs on disk and
    # no links to them, and so does one whose runs were tidied up by hand.
    state.refresh_latest()
    latest = state.directory / LATEST_DIRNAME

    if history:
        records = [entry for entry in state.history() if step is None or entry.step == step]
        if not records:
            typer.echo(f"no runs recorded for {city} yet ({state.history_path})")
            return
        typer.echo(f"{'step':>8}  {'when':<16}  {'took':>7}  status")
        for entry in records:
            when = (
                entry.started_at.astimezone().strftime("%d %b %H:%M") if entry.started_at else "-"
            )
            took = format_duration(entry.duration_s) if entry.duration_s else "-"
            detail = entry.error or ", ".join(f"{k}={v}" for k, v in entry.params.items())
            typer.echo(f"{entry.step:>8}  {when:<16}  {took:>7}  {entry.status.value:<8} {detail}")
        return

    if step is None:
        typer.echo(f"{city}: {state.directory}")
        for name in LOG_STEPS:
            link = latest / f"{name}.log"
            if not link.exists():
                typer.echo(f"  {name:>8}  -")
                continue
            target = link.resolve()
            when = datetime.fromtimestamp(target.stat().st_mtime).strftime("%d %b %H:%M")
            size = format_bytes(target.stat().st_size)
            typer.echo(f"  {name:>8}  {when}  {size:>9}  {link}")
        return

    if step not in LOG_STEPS:
        typer.echo(f"error: unknown step {step!r}; known: {', '.join(LOG_STEPS)}", err=True)
        raise typer.Exit(1)
    link = latest / f"{step}.log"
    if not link.exists():
        typer.echo(f"error: {city} has no {step} log under {state.directory}", err=True)
        raise typer.Exit(1)
    if path_only:
        typer.echo(str(link))
        return
    text = link.read_text(encoding="utf-8", errors="replace")
    if lines > 0:
        text = "\n".join(text.splitlines()[-lines:])
    typer.echo(text)


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
        try:
            state = RunState.open(name, cities_dir=cities_dir, data_root=data_root)
        except (OSError, ValueError) as error:
            # Every status is derived from the digest of the config, so a city
            # whose file cannot be read has none. It gets its row and the other
            # cities keep theirs -- they are why the command was run.
            typer.echo(f"{name:<{width}}  " + "  ".join(f"{'error':>8}" for _ in CHAIN))
            typer.echo(f"{' ' * width}  {error}", err=True)
            if city is not None:
                raise typer.Exit(1) from error
            continue
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


@app.command()
def basemap(
    city: str,
    margin: Annotated[
        float, typer.Option(help="Metres past the bbox to include")
    ] = DEFAULT_MARGIN_M,
    build: Annotated[
        str | None, typer.Option(help="Planet build to cut from, YYYYMMDD (default: the newest)")
    ] = None,
    cities_dir: Annotated[Path, typer.Option(help="Directory holding <city>.yaml configs")] = Path(
        "cities"
    ),
    output_root: Annotated[Path, typer.Option(help="Artifact output root")] = Path("data/cities"),
    data_root: Annotated[Path, typer.Option(help="Where run state and logs live")] = Path("data"),
) -> None:
    """Cut CITY out of the Protomaps planet build: streets, labels, buildings.

    The shade tiles are a transparent overlay and carry none of that; without
    this the viewer draws them on black. A town is a few hundred kilobytes and
    under a minute, because ``pmtiles extract`` reads the 137 GB archive with
    HTTP Range requests and downloads only the tiles inside the bbox.

    Also part of ``run``. It is here on its own because a city built before this
    step existed needs it applied without re-rendering anything, which is what
    the manifest rewrite at the end is for.
    """
    config = load_city(cities_dir / f"{city}.yaml")
    artifact_dir = output_root / config.id / ARTIFACT_VERSION
    state = RunState.open(city, cities_dir=cities_dir, data_root=data_root)
    try:
        with step_scope(state, "basemap", {"margin_m": margin, "build": build or "newest"}) as say:

            def report(message: str) -> None:
                say(message)
                typer.echo(message)

            ensure_assets(output_root, progress=report)
            build_basemap(config, artifact_dir, margin_m=margin, stamp=build, progress=report)
    except BasemapError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    state.complete("basemap", digest=config_digest(cities_dir, city))


@app.command()
def assets(
    force: Annotated[bool, typer.Option("--force", help="Download them again")] = False,
    output_root: Annotated[Path, typer.Option(help="Artifact output root")] = Path("data/cities"),
) -> None:
    """Download the glyphs and sprites every city's basemap style needs.

    Once per machine, not once per city: they are the same bytes everywhere, so
    they live at ``<output-root>/assets/`` and are served as though "assets"
    were a city of its own. Without the glyphs there are no labels at all --
    MapLibre cannot draw text it has no glyphs for -- and without the sprites
    the style's fill patterns are missing.

    ``run`` calls this itself as part of the basemap step; it is separate
    because a fresh working copy needs it once and no city in particular.
    """
    try:
        ensure_assets(output_root, force=force, progress=typer.echo)
    except BasemapError as exc:
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
