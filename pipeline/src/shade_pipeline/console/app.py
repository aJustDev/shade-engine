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
from typing import ClassVar

from textual.app import App
from textual.binding import Binding, BindingType

from shade_core.config import CityConfig, load_city
from shade_pipeline.build import ARTIFACT_VERSION
from shade_pipeline.console.cities import CitiesScreen
from shade_pipeline.console.newcity import DEFAULT_WATCH
from shade_pipeline.publish import (
    DEFAULT_BASE_URL,
    PublishError,
    PublishPlan,
    check_ready,
    plan_publish,
)
from shade_pipeline.runner import CHAIN
from shade_pipeline.runstate import RunState, StepStatus


class ConsoleApp(App[None]):
    """Paths in, state read on demand, work launched detached."""

    CSS = """
    Screen { layout: vertical; }
    """
    TITLE = "shade-engine"
    BINDINGS: ClassVar[list[BindingType]] = [Binding("q", "quit", "Quit")]

    def __init__(
        self,
        *,
        cities_dir: Path,
        output_root: Path,
        data_root: Path,
        watch_dir: Path = DEFAULT_WATCH,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        super().__init__()
        self.cities_dir = cities_dir
        self.output_root = output_root
        self.data_root = data_root
        self.watch_dir = watch_dir
        self.base_url = base_url

    def on_mount(self) -> None:
        self.push_screen(CitiesScreen())

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
        watch_dir=watch_dir or DEFAULT_WATCH,
    ).run()
