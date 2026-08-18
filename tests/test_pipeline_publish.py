"""Publishing: the order, and the refusals that come before it.

There is one order and it is the whole content of the module, so it is the
whole content of these tests. They run against a fake command runner -- no
server, no ssh -- and assert the sequence, because the sequence is what breaks:
restarting before the artifacts land makes the API skip the city until somebody
restarts it again, and restarting before the config arrives means it never
heard of the city at all.
"""

import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import CUBE_CITY
from shade_core.artifacts import COVERAGE_FILENAME, METADATA_FILENAME
from shade_pipeline.basemap import declare_in_manifest
from shade_pipeline.publish import (
    KEEP_ROLLBACKS,
    Command,
    PublishError,
    PublishPlan,
    check_ready,
    execute,
    plan_publish,
    plan_unpublish,
    unpublish_notes,
)
from shade_pipeline.tiles import (
    BASEMAP_FILENAME,
    MANIFEST_FILENAME,
    RENDER_STATE_FILENAME,
    build_tiles,
)

NOON = datetime(2026, 6, 21, 13, 0, tzinfo=ZoneInfo("Europe/Madrid"))


@pytest.fixture
def publishable(built_city: Path, tmp_path: Path) -> Path:
    """Artifacts with a tile pyramid rendered from exactly these artifacts.

    And a basemap, because ``check_ready`` refuses without one: the overlay
    carries no street, no label and no building outline, so a city published
    without a backdrop is shade drawn on black.
    """
    target = tmp_path / "city"
    shutil.copytree(built_city, target)
    tiles_dir = build_tiles(CUBE_CITY, target, [NOON], min_zoom=17, max_zoom=18)
    (tiles_dir / BASEMAP_FILENAME).write_bytes(b"PMTiles")
    declare_in_manifest(tiles_dir)
    return target


class Recorder:
    """A command runner that records instead of running."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.seen: list[tuple[str, ...]] = []
        self.fail_at = fail_at

    def __call__(self, argv: Sequence[str]) -> int:
        self.seen.append(tuple(argv))
        return 1 if self.fail_at == len(self.seen) else 0

    def index_of(self, needle: str) -> int:
        for position, argv in enumerate(self.seen):
            if any(needle in part for part in argv):
                return position
        raise AssertionError(f"no command mentioning {needle!r} in {self.seen}")


def _plan(publishable: Path) -> PublishPlan:
    return plan_publish(CUBE_CITY, publishable)


def test_everything_lands_before_the_restart_that_reads_it(publishable: Path) -> None:
    """The single order, and the reason it is safe to have only one.

    The API opens its rasters at startup and holds the handles, and rsync
    replaces by rename, so until the restart it is reading whole old inodes
    rather than half of each build. That lets the restart go last, which is
    also what a city the server has never heard of needs -- and with the
    config mounted, one order serves both.
    """
    run = Recorder()

    execute(_plan(publishable), run=run)

    rasters = run.index_of("--exclude=tiles/")
    tiles = run.index_of("/tiles/")
    config = run.index_of("--relative")
    restart = run.index_of("restart api")
    assert rasters < tiles < config < restart


def test_publishing_never_touches_git(publishable: Path) -> None:
    """Committing the YAML is worth doing; it is not part of putting data on a server.

    It used to be, because the config rode inside the image, and it dragged a
    CI run and a twenty-minute wait into the middle of an rsync (ADR-025).
    """
    plan = _plan(publishable)

    shells = [command.shell() for command in plan.commands]
    assert not any(shell.startswith("git ") for shell in shells)
    assert not any("sleep" in shell for shell in shells)


def test_the_artifacts_go_to_a_directory_that_may_not_exist_yet(publishable: Path) -> None:
    """rsync creates the last component of a destination, not its parents.

    A city the server has never held has no `<city>/`, and the transfer dies
    with "mkdir failed: No such file or directory", exit 11 -- which is exactly
    how montalban failed.
    """
    plan = _plan(publishable)

    transfers = [command for command in plan.commands if command.argv[0] == "rsync"]
    artifact_transfers = [command for command in transfers if "--relative" not in command.argv]
    assert len(artifact_transfers) == 2
    assert all("--mkpath" in command.argv for command in artifact_transfers)


def test_the_city_config_travels_with_its_data(publishable: Path) -> None:
    """What makes a new city exist is its YAML arriving, not a deploy."""
    with_area = CUBE_CITY.model_copy(update={"area": "cities/cube/area.geojson"})
    plan = plan_publish(with_area, publishable, cities_dir=Path("cities"))

    sent = next(command for command in plan.commands if "--relative" in command.argv)
    assert "cities/./cube.yaml" in sent.argv
    assert "cities/./cube" in sent.argv
    # Not /opt/shade/cities: that is the deploy's git checkout (see below).
    assert sent.argv[-1].endswith(":/opt/shade/live/cities/")


def test_a_city_without_a_polygon_sends_only_its_yaml(publishable: Path) -> None:
    """Naming a directory that is not there would fail the transfer outright."""
    plan = _plan(publishable)

    sent = next(command for command in plan.commands if "--relative" in command.argv)
    assert "cities/./cube.yaml" in sent.argv
    assert "cities/./cube" not in sent.argv


def test_rsync_reports_in_lines_a_log_can_hold(publishable: Path) -> None:
    """--info=progress2 redraws with carriage returns: one endless line in a file."""
    plan = _plan(publishable)

    for command in plan.commands:
        if command.argv[0] == "rsync":
            assert "--info=progress2" not in command.argv
            assert "--info=stats1" in command.argv


def test_the_checks_wait_for_the_api_they_just_restarted(publishable: Path) -> None:
    """`docker compose restart` returns on started, not on serving.

    The registry opens every city's rasters before uvicorn answers, and the
    first check used to fire into that gap and take a 502 from the proxy --
    failing a publish that had worked.
    """
    plan = _plan(publishable)

    checks = [command for command in plan.commands if command.argv[0] == "curl"]
    assert len(checks) == 3
    assert all("--retry" in command.argv for command in checks)


def test_the_rollback_is_taken_before_anything_is_overwritten(publishable: Path) -> None:
    run = Recorder()

    execute(_plan(publishable), run=run)

    assert run.index_of("cp -al") == 0


def test_the_image_is_rebuilt_before_the_tools_profile_borrows_it(publishable: Path) -> None:
    """`pipeline` runs `image: shade:prod`, which only the `api` service builds.

    Skipping the rebuild does not fail: it silently runs the previous code.
    """
    run = Recorder()

    execute(_plan(publishable), run=run)

    assert run.index_of("build api") < run.index_of("recolor")


def test_the_checks_come_last(publishable: Path) -> None:
    run = Recorder()

    execute(_plan(publishable), run=run)

    assert run.index_of("healthz") > run.index_of("restart api")


def test_the_rollbacks_are_pruned_where_they_are_made(publishable: Path) -> None:
    """One per publish, and nothing else on the server ever looks at them again.

    Free the day they are taken -- `cp -al` shares inodes -- and a full copy of
    the previous build from the first publish that replaces the rasters.
    """
    plan = _plan(publishable)

    shells = [command.shell() for command in plan.commands]

    assert "cp -al" in shells[0]
    assert f"tail -n +{KEEP_ROLLBACKS + 1}" in shells[1]
    assert "xargs -r rm -rf" in shells[1]
    assert plan.commands[1].allow_failure, "a server with no rollbacks has nothing to prune"


def test_publishing_refuses_an_id_that_would_reach_out_of_its_directory() -> None:
    """The prune is an `rm -rf` with the id in the path, like unpublish's."""
    with pytest.raises(PublishError, match="rm -rf"):
        plan_publish(CUBE_CITY.model_copy(update={"id": "../etc"}), Path("nowhere"))


def test_a_failure_stops_everything_after_it(publishable: Path) -> None:
    plan = _plan(publishable)
    # The third command, because the first two are the rollback and its pruning
    # and both are allowed to fail: a first deployment has nothing to link
    # aside, and a server with no rollbacks has nothing to prune.
    run = Recorder(fail_at=3)

    with pytest.raises(PublishError, match="nothing after it ran"):
        execute(plan, run=run)

    assert len(run.seen) == 3


def test_the_rollback_may_fail_without_stopping_the_publish(publishable: Path) -> None:
    """First deployment of a city: there is no previous version to link aside."""
    run = Recorder(fail_at=1)

    execute(_plan(publishable), run=run)

    assert len(run.seen) > 1


def test_the_output_of_every_command_reaches_the_log(tmp_path: Path) -> None:
    """The bug that made a failed publish unreadable: nobody captured this.

    The default runner let the child inherit stdout, and the console launches
    publish detached, so rsync's own words -- including why it stopped -- went
    to /dev/null.
    """
    plan = PublishPlan(city="cube")
    plan.commands.append(
        Command(
            "say something on both streams",
            (
                sys.executable,
                "-c",
                "import sys; print('on stdout'); print('on stderr', file=sys.stderr)",
            ),
        )
    )
    written: list[str] = []

    execute(plan, progress=written.append)

    assert any("on stdout" in line for line in written)
    assert any("on stderr" in line for line in written)


def test_a_city_git_does_not_know_about_is_published_with_a_warning(
    publishable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publishing works without git; a config that lives only on a VPS is a risk."""
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "cities").mkdir()
    (tmp_path / "cities" / f"{CUBE_CITY.id}.yaml").write_text("id: cube\n", encoding="utf-8")

    notes = check_ready(CUBE_CITY, publishable, Path("cities"))

    assert any("is not in git" in note for note in notes)


def test_publishing_without_a_tile_manifest_is_refused(built_city: Path, tmp_path: Path) -> None:
    target = tmp_path / "city"
    shutil.copytree(built_city, target)

    with pytest.raises(PublishError, match="no tile manifest"):
        check_ready(CUBE_CITY, target)


def test_publishing_tiles_from_an_older_build_is_refused(publishable: Path) -> None:
    """The exact mistake the render state exists to catch."""
    state_path = publishable / "tiles" / RENDER_STATE_FILENAME
    recorded = json.loads(state_path.read_text(encoding="utf-8"))
    recorded["artifact_built_at"] = "2020-01-01T00:00:00+00:00"
    state_path.write_text(json.dumps(recorded), encoding="utf-8")

    with pytest.raises(PublishError, match="rendered from other artifacts"):
        check_ready(CUBE_CITY, publishable)


def test_publishing_without_a_basemap_is_refused(publishable: Path) -> None:
    """The mistake that put Montalban into production looking like a haze.

    The shade tiles are a transparent overlay: no street, no label, no building
    outline. All of that is the basemap underneath, and without it the viewer
    draws the overlay on black -- which at low zoom is exactly what an
    unreadable smear looks like. Optional in the chain, refused here: a build
    should not stop because a third-party download was unreachable, and a
    browser should not be shown the result.
    """
    (publishable / "tiles" / BASEMAP_FILENAME).unlink()

    with pytest.raises(PublishError, match="no streets, no labels and no buildings"):
        check_ready(CUBE_CITY, publishable)


def test_a_city_with_an_area_must_ship_its_coverage(publishable: Path) -> None:
    """Outside the area the horizon cube is zeros, and a zero reads as open sky."""
    with_area = CUBE_CITY.model_copy(update={"area": "cities/cube/area.geojson"})
    (publishable / COVERAGE_FILENAME).unlink(missing_ok=True)

    with pytest.raises(PublishError, match="reads as open sky"):
        check_ready(with_area, publishable)


def test_an_artifact_from_an_older_engine_is_published_with_a_note(publishable: Path) -> None:
    """A note and not a refusal.

    A city built by an older engine serves perfectly well; it answers with the
    geometry of the day it was built. Refusing would have left every city
    already in production unable to be republished until it was rebuilt.
    """
    metadata_path = publishable / METADATA_FILENAME
    recorded = json.loads(metadata_path.read_text(encoding="utf-8"))
    del recorded["engine_version"]
    metadata_path.write_text(json.dumps(recorded), encoding="utf-8")

    notes = check_ready(CUBE_CITY, publishable)

    assert any("did not say which" in note for note in notes)


def test_check_ready_reports_what_it_saw(publishable: Path) -> None:
    notes = check_ready(CUBE_CITY, publishable)

    assert any("verify checks passed" in note for note in notes)
    assert any("instants" in note for note in notes)
    manifest = json.loads((publishable / "tiles" / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["city"] == CUBE_CITY.id


def test_every_command_is_printable(publishable: Path) -> None:
    """--dry-run has to show something a person could paste and check."""
    plan = _plan(publishable)

    rendered = plan.render()
    assert "then the restart that reads it" in rendered
    for command in plan.commands:
        assert isinstance(command, Command)
        assert command.shell()


# ------------------------------------------------------------------- unpublish


def test_unpublish_removes_both_halves_and_then_restarts() -> None:
    """Artifacts alone would unserve it; the config has to go for the rehearsal.

    Leaving the YAML behind means the next publish is an update, and the path
    worth being able to practise is a new city arriving.
    """
    run = Recorder()

    execute(plan_unpublish(CUBE_CITY), run=run)

    artifacts = run.index_of("data/cities/cube")
    config = run.index_of("/opt/shade/live/cities/cube.yaml")
    restart = run.index_of("restart api")
    assert artifacts < config < restart


def test_unpublish_checks_the_city_is_gone_not_that_it_is_there() -> None:
    """The one assertion that is inverted, and the easiest to get backwards."""
    plan = plan_unpublish(CUBE_CITY)

    final = plan.commands[-1].shell()
    assert "404" in final
    assert f"/v1/cities/{CUBE_CITY.id}" in final


def test_unpublish_takes_the_rollbacks_with_it() -> None:
    """ "Unpublished" should not leave 600 MB of previous builds on the disk."""
    plan = plan_unpublish(CUBE_CITY)

    removal = plan.commands[0].argv[-1]
    assert removal == "rm -rf /opt/shade/data/cities/cube"
    assert "v1" not in removal


@pytest.mark.parametrize("bad", ["", ".", "..", "/", "cube/../..", "cube; rm -rf /", "-rf"])
def test_an_id_that_could_widen_an_rm_is_refused(bad: str) -> None:
    """The id goes straight into `rm -rf` on a production server.

    CityConfig.id is a plain string, so nothing upstream guarantees this. An
    empty one would delete every city's artifacts at once.
    """
    dangerous = CUBE_CITY.model_copy(update={"id": bad})

    with pytest.raises(PublishError, match="rm -rf"):
        plan_unpublish(dangerous)


def test_neither_publish_nor_unpublish_writes_into_the_deploys_checkout() -> None:
    """One directory, two owners was the bug: `git reset --hard` versus publish.

    It looked harmless because a published YAML usually matches its commit byte
    for byte. Publish one you edited and did not commit, though, and the next
    deploy reverts it under artifacts that stayed new -- and nothing checks:
    CityRegistry cross-checks only the CRS and takes name and timezone from the
    YAML as given.
    """
    for command in plan_unpublish(CUBE_CITY).commands:
        assert "/opt/shade/cities" not in command.shell()


def test_unpublish_says_when_the_working_tree_is_about_to_be_the_only_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Committed, it is reversible by republishing; uncommitted, it is not."""
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "cities").mkdir()
    (tmp_path / "cities" / f"{CUBE_CITY.id}.yaml").write_text("id: cube\n", encoding="utf-8")

    assert any("only one" in note for note in unpublish_notes(CUBE_CITY, Path("cities")))

    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
        cwd=tmp_path,
        check=True,
    )

    assert unpublish_notes(CUBE_CITY, Path("cities")) == []


def test_a_stray_file_beside_the_yaml_does_not_change_what_git_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The question is about the YAML, not about the tidiness of its directory.

    Asking "is anything dirty around here" was wrong in both directions: one
    uncommitted export left beside a committed YAML made publish claim the city
    was not in git, and made unpublish warn about a copy that was safe all along.
    """
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "cities" / CUBE_CITY.id).mkdir(parents=True)
    (tmp_path / "cities" / f"{CUBE_CITY.id}.yaml").write_text("id: cube\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
        cwd=tmp_path,
        check=True,
    )
    with_area = CUBE_CITY.model_copy(update={"area": f"cities/{CUBE_CITY.id}/area.geojson"})
    (tmp_path / "cities" / CUBE_CITY.id / "raw-export.geojson").write_text("{}", encoding="utf-8")

    assert unpublish_notes(with_area) == []
