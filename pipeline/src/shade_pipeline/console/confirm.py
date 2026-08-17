"""Two small modals: confirm something consequential, and edit one value.

Kept apart from the screens that use them because both are asked for from more
than one place, and because a confirmation dialog that shows *exactly* what is
about to happen is the whole safety mechanism in front of publishing.
"""

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Static


class ConfirmScreen(ModalScreen[bool]):
    """Show what is about to happen, in full, and require an explicit yes."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "refuse", "Cancel")]
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > VerticalScroll {
        width: 90%; height: auto; max-height: 85%;
        border: thick $warning; background: $surface; padding: 1 2;
    }
    ConfirmScreen #body { height: auto; }
    ConfirmScreen Horizontal { height: auto; align: right middle; padding-top: 1; }
    """

    def __init__(self, title: str, body: str, confirm_label: str = "Do it") -> None:
        super().__init__()
        self.title_text = title
        self.body = body
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(f"[b]{self.title_text}[/b]")
            # The body is a plan rendered elsewhere: paths, commands and git's
            # own output, none of it written to be markup. It is shown, never
            # parsed.
            yield Static(self.body, id="body", markup=False)
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button(self.confirm_label, id="confirm", variant="warning")
        yield Footer(show_command_palette=False)

    def action_refuse(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class DetailScreen(ModalScreen[None]):
    """Everything a step has to say, when the table cell only had room for 70 characters.

    A failure names what it needs -- which tiles are missing, which file could
    not be read -- and the useful half of that sentence was routinely the half
    that got cut off.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
    ]
    DEFAULT_CSS = """
    DetailScreen { align: center middle; }
    DetailScreen > VerticalScroll {
        width: 84%; height: auto; max-height: 80%;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    DetailScreen #detail-body { height: auto; padding: 1 0; }
    """

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.title_text = title
        self.body = body

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(f"[b]{self.title_text}[/b]")
            # markup=False: this is a parser's or a driver's own words.
            yield Static(self.body, id="detail-body", markup=False)
        yield Footer(show_command_palette=False)

    def action_close(self) -> None:
        self.dismiss(None)


class EditScreen(ModalScreen[str | None]):
    """Edit one setting, with what it is and what it costs in front of you."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    EditScreen { align: center middle; }
    EditScreen > VerticalScroll {
        width: 76; height: auto; max-height: 80%;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    EditScreen #explain { height: auto; padding: 1 0; color: $text-muted; }
    EditScreen Horizontal { height: auto; align: right middle; padding-top: 1; }
    """

    def __init__(self, setting: str, value: str, explanation: str) -> None:
        super().__init__()
        self.setting = setting
        self.value = value
        self.explanation = explanation

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(f"[b]{self.setting}[/b]")
            yield Static(self.explanation, id="explain")
            yield Input(value=self.value, id="value")
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        self.query_one("#value", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.dismiss(self.query_one("#value", Input).value.strip())
        else:
            self.dismiss(None)
