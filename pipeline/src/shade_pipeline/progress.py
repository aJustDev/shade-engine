"""Human-readable durations and sizes for build progress lines."""


def format_bytes(size: int) -> str:
    """Compact binary size: ``512 B``, ``66.2 MiB``, ``2.3 GiB``."""
    value = float(size)
    for unit in ("B", "KiB", "MiB"):
        if value < 1024:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def format_duration(seconds: float) -> str:
    """Compact duration: ``42s``, ``12m 34s``, ``3h 07m``."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"
