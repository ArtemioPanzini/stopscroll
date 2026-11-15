"""Local calendar days with a configurable daily reset hour."""
from datetime import datetime, timedelta, tzinfo


def day_key(ts: float, reset_hour: int, tz: tzinfo | None = None) -> str:
    return (datetime.fromtimestamp(ts, tz) - timedelta(hours=reset_hour)).date().isoformat()


def next_reset(ts: float, reset_hour: int, tz: tzinfo | None = None) -> float:
    local = datetime.fromtimestamp(ts, tz)
    reset = local.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
    if reset.timestamp() <= ts:
        reset += timedelta(days=1)
    return reset.timestamp()
