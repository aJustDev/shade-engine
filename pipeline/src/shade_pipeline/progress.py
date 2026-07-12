"""Human-readable durations for build progress lines."""


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
