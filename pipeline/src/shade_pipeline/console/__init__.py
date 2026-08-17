"""The operations console: a terminal UI over the run state.

Imported lazily by the CLI, because :mod:`textual` is an optional extra
(``shade-pipeline[tui]``) and the base install must not need it.
"""

from shade_pipeline.console.app import ConsoleApp, run_console

__all__ = ["ConsoleApp", "run_console"]
