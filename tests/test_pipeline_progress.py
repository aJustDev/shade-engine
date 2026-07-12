"""Duration formatting for build progress lines."""

from shade_pipeline.progress import format_duration


def test_seconds() -> None:
    assert format_duration(42.7) == "42s"


def test_minutes() -> None:
    assert format_duration(754.2) == "12m 34s"


def test_hours() -> None:
    assert format_duration(3 * 3600 + 7 * 60 + 5) == "3h 07m"
