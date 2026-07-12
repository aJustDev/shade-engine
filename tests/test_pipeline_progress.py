"""Duration and size formatting for build progress lines."""

from shade_pipeline.progress import format_bytes, format_duration


def test_bytes() -> None:
    assert format_bytes(512) == "512 B"


def test_mebibytes() -> None:
    assert format_bytes(69_400_000) == "66.2 MiB"


def test_gibibytes() -> None:
    assert format_bytes(2_500_000_000) == "2.3 GiB"


def test_seconds() -> None:
    assert format_duration(42.7) == "42s"


def test_minutes() -> None:
    assert format_duration(754.2) == "12m 34s"


def test_hours() -> None:
    assert format_duration(3 * 3600 + 7 * 60 + 5) == "3h 07m"
