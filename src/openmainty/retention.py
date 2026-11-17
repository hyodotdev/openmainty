"""Retention windows for run and review artifacts."""

DEFAULT_KEEP_DAYS = 14


def keep_days(config: dict) -> int:
    """Days to keep finished runs, from config or the default."""
    value = config.get("keep_days", DEFAULT_KEEP_DAYS)
    return max(1, int(value))


def expired(age_days: float, config: dict) -> bool:
    """Whether an artifact of this age is past its retention window."""
    return age_days > keep_days(config)
