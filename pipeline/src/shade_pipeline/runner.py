"""The chain a city walks, driven from its state file rather than from memory.

``area`` -> ``basemap`` -> ``build`` -> ``graph`` -> ``tiles`` -> ``publish``.
Each step writes
its own log and event stream, records what it did in
:mod:`shade_pipeline.runstate`, and stops the chain if it fails. Re-running
picks up from the state file instead of starting over, which is the difference
between a six-hour render being interrupted and being lost.

The graph runs *before* the tiles even though nothing forces the order: it is
eight minutes against six hours, its output travels in the same rsync as the
rasters (which ``publish`` sends before the pyramids), and a failure in it is
worth discovering before a night of rendering rather than after.

Publishing is not part of what runs unattended. The chain stops in front of it
and leaves the step pending, because the thing worth automating is building a
city, not deciding that it is fit to serve. ``--yes`` says otherwise.
"""

import time
import traceback
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from shade_core.artifacts import METADATA_FILENAME
from shade_core.config import CityConfig, load_city
from shade_pipeline.area import plan_city
from shade_pipeline.basemap import DEFAULT_MARGIN_M, build_basemap, ensure_assets
from shade_pipeline.build import ARTIFACT_VERSION, build_city
from shade_pipeline.events import JsonlSink, emit
from shade_pipeline.graph import DEFAULT_SPACING_M, build_graph
from shade_pipeline.horizon import HorizonParams
from shade_pipeline.progress import format_bytes, format_duration
from shade_pipeline.runstate import CHAIN, UNATTENDED, RunState, StepStatus, config_digest
from shade_pipeline.sources import LidarSource
from shade_pipeline.tiles import (
    DEFAULT_MAX_ZOOM,
    DEFAULT_MIN_ZOOM,
    build_tiles,
    season_preset_instants,
)

# CHAIN and UNATTENDED are imported above from `runstate`, where they now live
# and where this module keeps re-exporting them from: naming the steps and
# running them are different weights, and something that only needs the six
# names -- the console's first table, `status` -- should not have to import the
# geospatial stack to get them.

OPTIONAL: frozenset[str] = frozenset({"basemap", "graph"})
"""Steps whose failure is worth recording and not worth stopping for.

Both are things a city can be built without, and both need the network at a
moment when nothing else does. ``CityRegistry.load`` treats a missing pedestrian
graph as an ordinary state and answers 503 for routes, so a city whose walk
network OSM does not have -- or whose Overpass call timed out -- still has a
perfectly good shade map. The basemap is a download from a third party with a
week of retention; losing four hours of sweeping because it was unreachable
would be absurd.

Optional here, and refused by ``publish``: the failure is recorded, the build
carries on, and the city does not reach a browser without a backdrop. That pair
is the whole design -- see :func:`shade_pipeline.publish.check_ready`.
"""


class ChainError(RuntimeError):
    """A step refused to run, or the chain was asked for something impossible."""


@dataclass(frozen=True)
class StepOutcome:
    """What one step of a chain run did, for the caller's summary."""

    step: str
    status: StepStatus
    detail: str
    elapsed_s: float = 0.0


@dataclass(frozen=True)
class ChainOptions:
    """Everything the chain needs that is not the city itself."""

    cities_dir: Path = Path("cities")
    output_root: Path = Path("data/cities")
    data_root: Path = Path("data")
    workers: int = 1
    tile_size: int = 512
    min_zoom: int = DEFAULT_MIN_ZOOM
    max_zoom: int = DEFAULT_MAX_ZOOM
    spacing_m: float = DEFAULT_SPACING_M
    instants: Sequence[datetime] | None = None
    resume: bool = True
    force: bool = False
    """Re-run steps whose state already says done. Off, so a resumed chain skips them."""
    cache_dir: Path | None = None
    """Where LiDAR is downloaded to. None means ``data/lidar/<city>``."""

    def lidar_cache(self, city_id: str) -> Path:
        return self.cache_dir if self.cache_dir is not None else Path("data/lidar") / city_id


def steps_between(first: str | None, last: str | None) -> tuple[str, ...]:
    """The slice of the chain from ``first`` to ``last``, both inclusive."""
    for name in (first, last):
        if name is not None and name not in CHAIN:
            raise ChainError(f"unknown step {name!r}; the chain is {', '.join(CHAIN)}")
    start = CHAIN.index(first) if first else 0
    stop = CHAIN.index(last) + 1 if last else len(UNATTENDED)
    if stop <= start:
        raise ChainError(f"--from {CHAIN[start]} comes after --to {CHAIN[stop - 1]}")
    return CHAIN[start:stop]


def preflight(config: CityConfig, options: ChainOptions) -> list[str]:
    """Price the whole chain before the first hour is spent; raise if it cannot fit.

    The check that earns its keep is the *tile* budget, and the reason is
    ordering. ``build`` already refuses to sweep with more workers than fit, and
    ``tiles`` already refuses to render with more than fit -- but the tile check
    happens after the four hours of sweeping. A city that can be swept and not
    rendered (the ~200 Mpx wall, which is where Barcelona sits) therefore used
    to burn the whole build before saying so. Here both are asked up front.

    The arithmetic is ``plan_city``'s, not this function's: the console shows
    the same numbers in its cost panel, and two cost models that could disagree
    would be worse than none.
    """
    plan = plan_city(
        config,
        tile_size=options.tile_size,
        workers=options.workers,
        cache_dir=options.lidar_cache(config.id),
        config_path=options.cities_dir / f"{config.id}.yaml",
    )
    megapixels = plan.rows * plan.cols / 1e6
    notes = [
        f"{plan.cols} x {plan.rows} px at {config.resolution_m:g} m ({megapixels:.1f} Mpx), "
        f"{plan.covered_px:,} px inside the computation area",
        f"sweep {format_bytes(plan.sweep_worker_bytes)} per worker, "
        f"tiles {format_bytes(plan.tiles_worker_bytes)} per worker",
    ]
    if plan.lidar.missing:
        notes.append(
            f"{len(plan.lidar.missing)} of {plan.lidar.needed} lidar tiles still to download"
        )

    if plan.tiles_workers_fit is None:
        # An unreadable budget is not a reason to block a build that may be
        # perfectly fine; the phases themselves check again when they start.
        notes.append("memory budget unreadable on this host; not checking it")
        return notes
    notes.append(
        f"{plan.tiles_workers_fit} tile workers fit, {plan.sweep_workers_fit} sweep workers"
    )
    # Zero is the floor: if a single worker does not fit, no number of them
    # does and the chain is impossible on this machine.
    if plan.tiles_workers_fit < 1:
        raise ChainError(
            f"one tile-render worker needs {format_bytes(plan.tiles_worker_bytes)} and this "
            f"machine cannot give it. The tile phase holds whole-raster arrays for one "
            f"instant, so the lever is a smaller bbox or a coarser resolution_m, not fewer "
            f"workers. Refusing now rather than after the build."
        )
    if plan.sweep_workers_fit is not None and plan.sweep_workers_fit < 1:
        raise ChainError(
            f"one horizon-sweep worker needs {format_bytes(plan.sweep_worker_bytes)} and "
            f"this machine cannot give it; try a smaller --tile-size"
        )
    return notes


@contextmanager
def step_scope(
    state: RunState, step: str, params: dict[str, Any]
) -> Iterator[Callable[[str], None]]:
    """Bracket one step: open its log and events, record it, close them all.

    Yields the progress callback the phase should use, which tees to the log
    file and leaves the structured stream to the sink underneath.

    Public because ``publish`` is a step of the same city that does not run
    inside the chain, and it spent its first life writing a log path into the
    state file that nobody ever opened. Recording a step and opening its log
    are the same act; there must be one place that does both.
    """
    log_path, events_path = state.paths_for(step)
    state.begin(step, params=params, log=log_path, events=events_path)
    started = time.monotonic()
    with log_path.open("a", encoding="utf-8") as log, JsonlSink(events_path) as sink:

        def say(message: str) -> None:
            log.write(message + "\n")
            log.flush()

        emit(sink, step, "began", **params)
        try:
            yield say
        except BaseException as error:
            say(traceback.format_exc())
            emit(sink, step, "failed", error=str(error))
            state.fail(step, f"{type(error).__name__}: {error}")
            raise
        emit(sink, step, "ended", elapsed_s=round(time.monotonic() - started, 1))


def run_chain(
    city: str,
    *,
    steps: Sequence[str],
    options: ChainOptions,
    source: Callable[[CityConfig, Callable[[str], None]], LidarSource],
    graph_source: Callable[[], Any] | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[StepOutcome]:
    """Walk ``steps`` for ``city``, stopping at the first failure.

    ``source`` and ``graph_source`` are passed in rather than built here so the
    CLI decides what talks to the network and a test can hand over neither.
    ``source`` receives the step's own progress callback: the LiDAR download is
    the slowest silent thing a new city does, and it has to report into the
    step's log rather than to a stdout nobody is reading.
    """
    echo = progress if progress is not None else lambda _message: None
    config = load_city(options.cities_dir / f"{city}.yaml")
    state = RunState.open(city, cities_dir=options.cities_dir, data_root=options.data_root)
    artifact_dir = options.output_root / config.id / ARTIFACT_VERSION
    outcomes: list[StepOutcome] = []

    for step in steps:
        status = state.status(step)
        if status is StepStatus.DONE and not options.force:
            echo(f"{step}: already done, skipping (--force to redo it)")
            outcomes.append(StepOutcome(step, StepStatus.DONE, "already done"))
            continue
        if status is StepStatus.RUNNING:
            raise ChainError(
                f"{step} is already running as pid {state.record(step).pid}; "
                "wait for it or kill it before starting another"
            )
        if status is StepStatus.STALE:
            echo(f"{step}: redoing it, {state.stale_reason(step)}")
        if step == "publish":
            # The gate. Reached only when the caller asked for it explicitly.
            echo("publish: waiting for your go-ahead; run shade-engine publish when ready")
            outcomes.append(StepOutcome(step, StepStatus.PENDING, "awaiting approval"))
            break

        started = time.monotonic()
        try:
            detail = _run_step(
                step, config, state, options, artifact_dir, source, graph_source, echo
            )
        except Exception as error:
            if step not in OPTIONAL:
                raise
            # step_scope already recorded the failure and kept the log; the chain
            # goes on because nothing downstream needs what this produces.
            echo(f"{step}: failed ({error}); it is optional, carrying on")
            outcomes.append(
                StepOutcome(
                    step, StepStatus.FAILED, str(error), round(time.monotonic() - started, 1)
                )
            )
            continue
        elapsed = time.monotonic() - started
        state.complete(step, digest=config_digest(options.cities_dir, city))
        echo(f"{step}: done in {format_duration(elapsed)}")
        outcomes.append(StepOutcome(step, StepStatus.DONE, detail, round(elapsed, 1)))
    return outcomes


def _run_step(
    step: str,
    config: CityConfig,
    state: RunState,
    options: ChainOptions,
    artifact_dir: Path,
    source: Callable[[CityConfig, Callable[[str], None]], LidarSource],
    graph_source: Callable[[], Any] | None,
    echo: Callable[[str], None],
) -> str:
    """Dispatch one step; the state bracketing and ordering belong to the caller."""
    if step == "area":
        notes = preflight(config, options)
        with step_scope(state, "area", {"tile_size": options.tile_size}) as say:
            for note in notes:
                say(note)
                echo(f"  {note}")
        return "; ".join(notes)

    if step == "basemap":
        # Behind `area` because `area --write` can move the bbox, and the
        # extract is cut from it; in front of `build` because it is a minute
        # against four hours and a missing binary is worth hearing about early.
        with step_scope(state, "basemap", {"margin_m": DEFAULT_MARGIN_M}) as say:
            ensure_assets(options.output_root, progress=say)
            out = build_basemap(config, artifact_dir, progress=say)
        return str(out)

    if step == "build":
        # Built here and handed over, rather than left to `build_city`'s own
        # default: without it the sweep ran with `workers=1` and `tile_size=512`
        # whatever the chain was asked for, so `run --workers 13` priced a build
        # with thirteen and then ran it serially. Measured on Montalban: 3m 18s
        # of sweep where eleven tiles over thirteen workers is one round.
        # `check_worker_budget` inside the sweep still refuses a number that
        # does not fit, so this passes the request on, it does not override it.
        sweep = HorizonParams(
            sectors=config.horizon_sectors,
            max_distance_m=config.horizon_max_distance_m,
            observer_height_m=config.observer_height_m,
            tile_size=options.tile_size,
            workers=options.workers,
        )
        with step_scope(
            state, "build", {"workers": sweep.workers, "tile_size": sweep.tile_size}
        ) as say:
            out = build_city(
                config,
                source(config, say),
                options.output_root,
                sweep,
                progress=say,
                events=_sink_of(state, "build"),
            )
        return str(out)

    if step == "graph":
        _require_artifacts(artifact_dir, "graph")
        if graph_source is None:
            raise ChainError("the pedestrian graph needs a source; none was given")
        with step_scope(state, "graph", {"spacing_m": options.spacing_m}) as say:
            out = build_graph(
                config, artifact_dir, graph_source(), spacing_m=options.spacing_m, progress=say
            )
        return str(out)

    if step == "tiles":
        _require_artifacts(artifact_dir, "tiles")
        instants = options.instants
        if instants is None:
            instants = season_preset_instants(ZoneInfo(config.timezone))
        params = {
            "workers": options.workers,
            "min_zoom": options.min_zoom,
            "max_zoom": options.max_zoom,
            "instants": len(instants),
            "resume": options.resume,
        }
        with step_scope(state, "tiles", params) as say:
            out = build_tiles(
                config,
                artifact_dir,
                instants,
                min_zoom=options.min_zoom,
                max_zoom=options.max_zoom,
                workers=options.workers,
                resume=options.resume,
                progress=say,
                events=_sink_of(state, "tiles"),
            )
        return str(out)

    raise ChainError(f"unknown step {step!r}")


def _sink_of(state: RunState, step: str) -> JsonlSink:
    """The events file the bracket just opened, reopened for the phase to append to.

    Two handles on one file rather than threading the sink through the context
    manager's yield: they only ever append whole flushed lines, and the reader
    skips anything partial.
    """
    recorded = state.record(step).events
    assert recorded is not None  # begin() always sets it
    return JsonlSink(Path(recorded))


def _require_artifacts(artifact_dir: Path, step: str) -> None:
    if not (artifact_dir / METADATA_FILENAME).exists():
        raise ChainError(f"{step} needs artifacts under {artifact_dir}; run build first")
