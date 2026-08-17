"""Getting a built city onto the server: everything it needs, then a restart.

There is one order, and it is the whole content of this module. Everything the
server needs travels by rsync -- the rasters, the pedestrian graph, the metadata,
the tile pyramid and *the city's own YAML* -- and only then is the API restarted,
which is the moment it reads the new configs and drops the raster blocks it had
cached in RAM. The light theme is generated *on* the server because it rewrites
twenty bytes per tile and shipping it from here would be an hour of upload for
nothing.

Two things make the single order safe. The API opens its rasters at startup and
keeps the handles (:class:`shade_core.artifacts.SceneReader`), and rsync replaces
files by rename, so until the restart the running process is still reading the
old inodes -- whole and consistent, never half of each build. And the city config
is mounted from the host (``compose.yml``: ``./cities:/app/cities:ro``) rather
than baked into the image, so making a brand-new city appear is a restart and not
a code deploy.

It used to be two orders, and the second one had ``git commit``, ``git push`` and
a twenty-minute wait for CI in the middle of it, purely because the YAML rode
inside the image. See ADR-025. Committing the YAML is still worth doing, but it
is a separate act from publishing, and :func:`check_ready` says so out loud.

:func:`plan_unpublish` is the undo, and is built out of the same
:class:`Command` list rather than a machine of its own, so it inherits the dry
run, the log and the confirmation screen for free.

Nothing here is secret: the connection to the server is an ssh key, and every
step is a command you could have typed. ``--dry-run`` prints exactly those
commands and runs none of them.
"""

import json
import re
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from shade_core.artifacts import COVERAGE_FILENAME, METADATA_FILENAME, load_metadata
from shade_core.config import CityConfig
from shade_pipeline.tiles import (
    BASEMAP_FILENAME,
    MANIFEST_FILENAME,
    TILES_DIRNAME,
    read_render_state,
    render_state,
)
from shade_pipeline.verify import verify_artifacts

DEFAULT_HOST = "cartagena"
DEFAULT_REMOTE_ROOT = "/opt/shade"
LIVE_CONFIG_DIR = "live/cities"
"""Where the server keeps the configs it is actually serving, relative to the root.

Deliberately *outside* the deploy's git checkout. The first version of ADR-025
mounted ``./cities`` -- the checkout itself -- and that gave one directory two
owners: ``deploy.sh`` runs ``git reset --hard origin/main`` over it, while
``publish`` and ``unpublish`` write into it. The collision looked harmless
because a published YAML usually matches its commit byte for byte, but publish a
config you edited and have not committed and the next deploy reverts it under
artifacts that stay new -- silently, since ``CityRegistry.load`` cross-checks
only the CRS and takes ``name`` and ``timezone`` from the YAML unquestioned.

So: ``cities/`` in git is the catalogue of cities that exist as configurations,
and this directory is what *this server* currently serves. One writer each, and
a deploy has no opinion about the second.
"""
DEFAULT_BASE_URL = "https://shade.ajustino.dev"
LIGHT_PALETTE = "light"
CHECK_RETRY = ("--retry", "10", "--retry-delay", "3", "--retry-all-errors")
"""How the closing checks wait for the API they just restarted.

``docker compose restart`` returns when the container has *started*, not when
uvicorn is serving: the registry still has to open every city's rasters. The
first check used to fire into that gap and take a 502 from the proxy, failing a
publish that had in fact worked. Thirty seconds of patience, the same budget
``deploy/deploy.sh`` gives itself.
"""

RSYNC_REPORTING = ("-v", "--info=stats1")
"""How rsync reports into a log file.

Not ``--info=progress2``: it redraws one line with carriage returns, which in a
file is a single kilometre-long line. ``-v`` names each file as it lands (ten
rasters, a hundred-odd pmtiles archives: you watch the instants arrive) and
``stats1`` closes with the bytes and the rate.
"""


SAFE_CITY_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
"""What an id must look like before it is allowed into a remote path.

``CityConfig.id`` is a plain string, and unpublishing interpolates it into
``rm -rf`` on a production server. An empty id would delete every city's
artifacts; one containing ``/`` or ``..`` could reach further than that. Nothing
legitimate is excluded -- the id is also a filename and a URL segment -- and the
check costs a regex.
"""


class PublishError(RuntimeError):
    """The city is not fit to publish, or a step of the publish failed."""


@dataclass(frozen=True)
class Command:
    """One step of a publish: what it is for, and exactly what it runs."""

    what: str
    argv: tuple[str, ...]
    allow_failure: bool = False
    """True for steps whose failure is informative but not fatal (the rollback link)."""

    def shell(self) -> str:
        return " ".join(shlex.quote(part) for part in self.argv)


@dataclass
class PublishPlan:
    """The ordered commands, and the reasoning a reader needs to check them."""

    city: str
    commands: list[Command] = field(default_factory=list)
    headline: str = "everything it needs, then the restart that reads it"

    def render(self) -> str:
        lines = [f"{self.city}: {self.headline}", ""]
        for index, command in enumerate(self.commands, start=1):
            lines.append(f"{index:>2}. {command.what}")
            lines.append(f"    {command.shell()}")
        return "\n".join(lines)


def config_paths(config: CityConfig, cities_dir: Path) -> list[Path]:
    """The files that *are* this city's configuration, YAML plus its polygon."""
    paths = [cities_dir / f"{config.id}.yaml"]
    if config.area is not None:
        paths.append(Path(config.area).parent)
    return paths


def tracked(path: Path) -> bool:
    """Whether git knows this file at all.

    Asked of the file and not of the directory it sits in. "Is anything dirty
    around here" was the first version and it was wrong in both directions: one
    stray untracked export beside a perfectly committed YAML was enough to make
    ``publish`` claim the city was not in git, and to make ``unpublish`` forget
    to say that a deploy would put it back.

    Best effort: no git, no repository, no answer, and neither caller says
    anything. This picks the wording of a note, never whether work happens.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return True
    return result.returncode == 0


def check_ready(
    config: CityConfig, artifact_dir: Path, cities_dir: Path = Path("cities")
) -> list[str]:
    """Refuse to publish anything the server would serve wrongly; returns its notes.

    Everything here is answerable locally and cheaply, and every item is a
    mistake that has actually been made: shipping without ``coverage.tif`` (the
    only thing distinguishing "no data" from "sunny"), shipping without a
    basemap (the overlay on black, with nothing to read it against), shipping a
    tile pyramid rendered from the previous build, shipping a directory whose
    horizon cube is quietly corrupt.
    """
    notes: list[str] = []
    if not (artifact_dir / METADATA_FILENAME).exists():
        raise PublishError(f"no artifacts under {artifact_dir}; run the build first")
    metadata = load_metadata(artifact_dir)

    results = verify_artifacts(artifact_dir)
    failed = [result for result in results if not result.passed]
    if failed:
        raise PublishError(
            "the artifacts do not verify: " + "; ".join(result.name for result in failed)
        )
    notes.append(f"{len(results)} verify checks passed")

    if config.area is not None and not (artifact_dir / COVERAGE_FILENAME).exists():
        raise PublishError(
            f"{config.id} declares a computation area but {COVERAGE_FILENAME} is missing; "
            "outside the area the horizon cube is zeros, and a zero reads as open sky"
        )

    tiles_dir = artifact_dir / TILES_DIRNAME
    if not (tiles_dir / MANIFEST_FILENAME).exists():
        raise PublishError(
            f"no tile manifest at {tiles_dir / MANIFEST_FILENAME}; render them first"
        )
    # The overlay carries no street, no name and no building outline: all of
    # that is the basemap, drawn underneath. Without it the viewer paints the
    # shade on black, which at low zoom reads as a haze over nothing -- which is
    # exactly how Montalban went to production.
    if not (tiles_dir / BASEMAP_FILENAME).exists():
        raise PublishError(
            f"{config.id} has no {BASEMAP_FILENAME}; without it the viewer draws the "
            f"overlay on black, with no streets, no labels and no buildings. "
            f"Run `shade-engine basemap {config.id}`"
        )

    manifest = json.loads((tiles_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    recorded = read_render_state(tiles_dir)
    expected = render_state(int(manifest["min_zoom"]), int(manifest["max_zoom"]), metadata.built_at)
    if recorded is not None and recorded != expected:
        raise PublishError(
            "the tiles in this directory were rendered from other artifacts; "
            "re-render them before publishing, or the map will describe a city "
            "the API no longer agrees with"
        )
    notes.append(
        f"{len(manifest['instants'])} instants, z{manifest['min_zoom']}-{manifest['max_zoom']}"
    )

    # A note, never a refusal. Publishing sends the YAML to the server itself,
    # so the city works either way; what it cannot do is put it in git, and a
    # config that exists only on a VPS is one reinstall from gone.
    yaml_path = config_paths(config, cities_dir)[0]
    if not tracked(yaml_path):
        notes.append(
            f"{yaml_path} is not in git; production will serve it from the mount, "
            f"but git will not know about it"
        )
    return notes


def plan_publish(
    config: CityConfig,
    artifact_dir: Path,
    *,
    host: str = DEFAULT_HOST,
    remote_root: str = DEFAULT_REMOTE_ROOT,
    base_url: str = DEFAULT_BASE_URL,
    cities_dir: Path = Path("cities"),
    recolor: bool = True,
) -> PublishPlan:
    """The ordered commands that put ``config``'s artifacts into production."""
    city = config.id
    remote_city = f"{remote_root}/data/cities/{city}"
    remote_version = f"{remote_city}/v1"
    local = f"{artifact_dir}/"
    compose = f"cd {remote_root} && docker compose -f compose.yml"
    plan = PublishPlan(city=city)

    def add(what: str, *argv: str, allow_failure: bool = False) -> None:
        plan.commands.append(Command(what, tuple(argv), allow_failure))

    # A hardlink copy: same inodes, so it costs no disk and no time, and it is
    # the rollback that has been used by hand on every rebuild so far.
    add(
        "hardlink the current version aside as a rollback",
        "ssh",
        host,
        f"test -d {remote_version} && "
        f"cp -al {remote_version} {remote_version}.rollback.$(date +%s) || true",
        allow_failure=True,
    )
    # --mkpath because rsync creates only the last component of a destination:
    # for a city the server has never held, `<city>/` does not exist and the
    # transfer dies with "mkdir failed: No such file or directory", exit 11.
    # --delete with excludes is safe: rsync protects excluded files on the
    # receiver from deletion, so the tile tree survives this leg untouched.
    add(
        "send the rasters, graph and metadata (not the tiles)",
        "rsync",
        "-a",
        "--delete",
        "--mkpath",
        *RSYNC_REPORTING,
        f"--exclude={TILES_DIRNAME}/",
        "--exclude=tiles-light/",
        local,
        f"{host}:{remote_version}/",
    )
    add(
        "send the tile pyramid",
        "rsync",
        "-a",
        "--delete",
        "--mkpath",
        *RSYNC_REPORTING,
        f"{artifact_dir / TILES_DIRNAME}/",
        f"{host}:{remote_version}/{TILES_DIRNAME}/",
    )
    # The config is part of the payload, not part of the image (ADR-025). The
    # `./` marker is what --relative slices the path at, so these land as
    # <city>.yaml and <city>/ directly under the remote cities directory.
    sources = [f"{cities_dir}/./{city}.yaml"]
    if config.area is not None:
        sources.append(f"{cities_dir}/./{Path(config.area).parent.name}")
    add(
        "send the city config, which is what makes a new city exist at all",
        "rsync",
        "-a",
        "--relative",
        "--mkpath",
        *RSYNC_REPORTING,
        *sources,
        f"{host}:{remote_root}/{LIVE_CONFIG_DIR}/",
    )
    if recolor:
        # `pipeline` has no build of its own -- it runs `image: shade:prod`,
        # which the `api` service builds. Skipping this runs old code silently.
        add("rebuild the image the tools profile borrows", "ssh", host, f"{compose} build api")
        add(
            "generate the light theme on the server (it rewrites 20 bytes a tile)",
            "ssh",
            host,
            f"{compose} --profile tools run --rm pipeline "
            f"shade-engine recolor {city} --palette {LIGHT_PALETTE}",
        )
    # Last, and it is the only step that changes what anybody sees: the API
    # reads the city configs once at startup and caches raster blocks in RAM,
    # so until this runs it is serving the previous build in full.
    add(
        "restart the API, which reads the configs and drops its cached blocks",
        "ssh",
        host,
        f"{compose} restart api",
    )

    def check(what: str, url: str) -> None:
        add(what, "curl", "-fsS", *CHECK_RETRY, "-o", "/dev/null", url)

    check("the API is healthy", f"{base_url}/healthz")
    check("the city is listed", f"{base_url}/v1/cities/{city}")
    check(
        "the tile manifest is served",
        f"{base_url}/tiles/{city}/v1/tiles/{MANIFEST_FILENAME}",
    )
    return plan


def unpublish_notes(config: CityConfig, cities_dir: Path = Path("cities")) -> list[str]:
    """What is worth knowing before taking a city off the server.

    Only one thing is, now that the live config directory is outside the deploy's
    checkout: whether git still has the YAML. If it does, unpublishing is
    entirely reversible -- republishing is the whole of putting the city back. If
    it does not, the copy in front of you becomes the only one there is.
    """
    yaml_path = config_paths(config, cities_dir)[0]
    if tracked(yaml_path):
        return []
    return [
        f"{yaml_path} is not in git; the server's copy is about to go, which "
        f"leaves this working tree holding the only one"
    ]


def plan_unpublish(
    config: CityConfig,
    *,
    host: str = DEFAULT_HOST,
    remote_root: str = DEFAULT_REMOTE_ROOT,
    base_url: str = DEFAULT_BASE_URL,
) -> PublishPlan:
    """The commands that take a city off the server: artifacts, config, restart.

    The mirror image of :func:`plan_publish`, and deliberately built out of the
    same :class:`Command` list so it inherits the dry run, the log and the
    confirmation screen rather than growing a second machine beside them.

    Both halves go: removing the artifacts alone would already unserve the city
    (the registry skips a config it has no ``metadata.json`` for), but leaving
    the YAML behind means the next publish is an update rather than the arrival
    of a new city, and that is the path worth being able to rehearse.
    """
    city = config.id
    if not SAFE_CITY_ID.fullmatch(city):
        raise PublishError(
            f"refusing to build remote commands for city id {city!r}: "
            f"an id goes straight into `rm -rf` on the server and must match "
            f"{SAFE_CITY_ID.pattern}"
        )
    compose = f"cd {remote_root} && docker compose -f compose.yml"
    plan = PublishPlan(city=city, headline="removed from the server, artifacts and config")

    def add(what: str, *argv: str) -> None:
        plan.commands.append(Command(what, tuple(argv)))

    # -rf, so unpublishing twice is not an error: the second run is a no-op and
    # says so by succeeding. Takes the rollback hardlinks with it, which is the
    # point -- "unpublished" should not leave 600 MB of previous builds behind.
    add(
        "delete the artifacts, tile pyramids and rollbacks",
        "ssh",
        host,
        f"rm -rf {remote_root}/data/cities/{city}",
    )
    add(
        "delete the city config, which is what the API lists it from",
        "ssh",
        host,
        f"rm -rf {remote_root}/{LIVE_CONFIG_DIR}/{city}.yaml "
        f"{remote_root}/{LIVE_CONFIG_DIR}/{city}",
    )
    add(
        "restart the API, which is what makes it forget",
        "ssh",
        host,
        f"{compose} restart api",
    )
    add(
        "the API is healthy", "curl", "-fsS", *CHECK_RETRY, "-o", "/dev/null", f"{base_url}/healthz"
    )
    # Inverted on purpose: here success is the city being gone. `curl -f` gives
    # exit 22 on a 404, so the test is on the status code and not on the exit.
    add(
        "the city is no longer served",
        "bash",
        "-c",
        f'test "$(curl -s -o /dev/null -w %{{http_code}} {base_url}/v1/cities/{city})" = 404',
    )
    return plan


CommandRunner = Callable[[Sequence[str]], int]
"""Runs one command and returns its exit status. Injected so tests need no server."""


def shell_runner(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), check=False).returncode


def logged_runner(say: Callable[[str], None]) -> CommandRunner:
    """A runner that pipes the command's own output into ``say``, line by line.

    The default runner lets the child inherit stdout, which is right at a
    terminal and useless anywhere else: launched detached from the console,
    stdout is ``/dev/null``, so rsync's file list and -- worse -- the reason it
    failed went nowhere. stderr is folded into stdout so the two stay in the
    order they happened.
    """

    def run(argv: Sequence[str]) -> int:
        with subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ) as process:
            assert process.stdout is not None
            for line in process.stdout:
                say("    " + line.rstrip("\n"))
        return process.returncode

    return run


def execute(
    plan: PublishPlan,
    *,
    run: CommandRunner | None = None,
    progress: Callable[[str], None] | None = None,
) -> None:
    """Run every command in order, stopping at the first real failure."""
    echo = progress if progress is not None else lambda _message: None
    runner = run if run is not None else logged_runner(echo)
    total = len(plan.commands)
    for index, command in enumerate(plan.commands, start=1):
        echo(f"[{index}/{total}] {command.what}")
        echo(f"    {command.shell()}")
        code = runner(command.argv)
        if code != 0 and not command.allow_failure:
            raise PublishError(
                f"step {index} of {total} failed ({command.what}) with exit {code}; "
                f"nothing after it ran"
            )
