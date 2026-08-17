"""The operations console: the whole flow of a city, without leaving the terminal.

Register it, price it while you change the settings that decide the price, build
it, watch the bar move, look at the result, publish it. Every one of those was a
command with its own flags, and remembering which came next was the job this
replaces.

The console owns nothing. It reads the state files
:mod:`shade_pipeline.runstate` writes and starts work with ``start_new_session``,
so closing it -- or losing the ssh session it runs over -- has no effect on a
six-hour render. That inversion is deliberate, and it is what the ``setsid
nohup`` era taught: a job that dies with its window is a job you cannot leave
alone.
"""

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual import work
from textual.app import App
from textual.binding import Binding, BindingType

from shade_core.config import CityConfig, load_city
from shade_pipeline.console.cities import CitiesScreen
from shade_pipeline.runstate import CHAIN, RunState, StepStatus

if TYPE_CHECKING:
    from shade_pipeline.publish import PublishPlan


class ConsoleApp(App[None]):
    """Paths in, state read on demand, work launched detached."""

    CSS = """
    Screen { layout: vertical; }
    """
    TITLE = "shade-engine"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        # The footer only has room for the shortcuts of the day, so the full
        # list of them lives here: Textual's own help panel, which knows the
        # bindings of whatever screen is in front and scrolls.
        Binding("question_mark", "show_help_panel", "Help"),
    ]

    def __init__(
        self,
        *,
        cities_dir: Path,
        output_root: Path,
        data_root: Path,
        watch_dir: Path | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__()
        self.cities_dir = cities_dir
        self.output_root = output_root
        self.data_root = data_root
        # Left as None on purpose. Which directory to watch is the registration
        # screen's question, and answering it here would mean importing that
        # screen -- and the geospatial stack behind it -- before the first
        # table is painted.
        self.watch_dir = watch_dir
        self._base_url = base_url

    @property
    def base_url(self) -> str:
        """Where a published city will live, resolved as late as its module is imported."""
        if self._base_url is None:
            from shade_pipeline.publish import DEFAULT_BASE_URL

            self._base_url = DEFAULT_BASE_URL
        return self._base_url

    def on_mount(self) -> None:
        self.push_screen(CitiesScreen())
        self.warm_up()

    @work(thread=True, group="warmup")
    def warm_up(self) -> None:
        """Load the geospatial stack in the background, before anything asks for it.

        Deferring these imports is what got startup from 1.604 ms down to 295,
        but somebody has to pay them, and paying them on the first city opened
        just moves the wait somewhere more annoying. Here they are paid in a
        thread while the first table is already on screen and being read.
        """
        import shade_pipeline.area  # noqa: F401

    def cities(self) -> list[str]:
        return sorted(path.stem for path in self.cities_dir.glob("*.yaml"))

    def config_of(self, city: str) -> CityConfig:
        return load_city(self.cities_dir / f"{city}.yaml")

    def state_of(self, city: str) -> RunState:
        # Re-read every refresh rather than caching: the writer is another
        # process, and a cached view is precisely the stale answer to avoid.
        return RunState.open(city, cities_dir=self.cities_dir, data_root=self.data_root)

    def is_busy(self, city: str) -> bool:
        state = self.state_of(city)
        return any(state.status(step) is StepStatus.RUNNING for step in CHAIN)

    def publish_plan(self, city: str) -> PublishPlan | str:
        """The plan for publishing a city, or the reason there is not one.

        The refusals happen here rather than in the dialog, so what the user is
        asked to confirm is only ever a publish that would actually work.
        """
        # `publish` and `build` are imported on the way to publishing, not on
        # the way to the first screen: between them they pull pyproj, shapely
        # and rasterio, which is close to a second of startup for a feature
        # used once a city.
        from shade_pipeline.build import ARTIFACT_VERSION
        from shade_pipeline.publish import PublishError, check_ready, plan_publish

        try:
            config = self.config_of(city)
            artifact_dir = self.output_root / city / ARTIFACT_VERSION
            check_ready(config, artifact_dir, self.cities_dir)
            return plan_publish(
                config,
                artifact_dir,
                base_url=self.base_url,
                cities_dir=self.cities_dir,
            )
        except (PublishError, OSError, ValueError) as error:
            return str(error)

    def unpublish_plan(self, city: str) -> PublishPlan | str:
        """The plan for taking a city off the server, or the reason there is not one.

        No ``check_ready`` here: what is being removed does not have to be fit to
        serve, and often the reason for removing it is that it is not. The notes
        that do apply -- what git will put back -- ride in the plan's headline so
        the confirmation screen shows them.
        """
        from shade_pipeline.publish import PublishError, plan_unpublish, unpublish_notes

        try:
            config = self.config_of(city)
            plan = plan_unpublish(config, base_url=self.base_url)
            for note in unpublish_notes(config, self.cities_dir):
                plan.headline += f"\n\nnote: {note}"
            return plan
        except (PublishError, OSError, ValueError) as error:
            return str(error)


def run_console(
    *,
    cities_dir: Path,
    output_root: Path,
    data_root: Path,
    watch_dir: Path | None = None,
) -> None:
    # Defaulted here rather than in the CLI signature: the CLI must not import
    # anything from this package at module level, or the base install would
    # need textual.
    ConsoleApp(
        cities_dir=cities_dir,
        output_root=output_root,
        data_root=data_root,
        watch_dir=watch_dir,
    ).run()
