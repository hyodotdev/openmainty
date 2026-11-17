"""Human-readable durations for run summaries."""


def format_seconds(seconds: int) -> str:
    """Format a non-negative number of seconds as a short duration."""
    if seconds < 0:
        raise ValueError("seconds must be non-negative")
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
