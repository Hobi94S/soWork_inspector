from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import random
import time
from typing import Any, Callable, Iterable, TypeVar

T = TypeVar("T")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def to_iso_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def retry(
    operation: Callable[[], T],
    *,
    attempts: int,
    base_delay_seconds: float,
    retry_exceptions: tuple[type[BaseException], ...],
    description: str,
    logger: Any,
    jitter_ratio: float = 0.2,
    rate_limit_cooldown_seconds: float = 30.0,
) -> T:
    delay = base_delay_seconds
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except retry_exceptions as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code in (401, 403):
                logger.error(
                    "%s failed because the SoWork session was rejected (HTTP %s); "
                    "not retrying authentication failures.",
                    description,
                    status_code,
                )
                raise
            if attempt >= attempts:
                raise
            wait_seconds = (
                max(delay, rate_limit_cooldown_seconds)
                if status_code == 429
                else delay
            )
            wait_seconds *= random.SystemRandom().uniform(
                1.0 - jitter_ratio, 1.0 + jitter_ratio
            )
            logger.warning(
                "%s failed on attempt %s/%s: %s. Retrying in %.1fs.",
                description,
                attempt,
                attempts,
                exc,
                wait_seconds,
            )
            time.sleep(wait_seconds)
            delay *= 2
    raise RuntimeError(f"Retry loop for {description} exited unexpectedly.")


def chunk_time_range(start: datetime, end: datetime, *, local_midnight_utc: Callable[[datetime], datetime]) -> Iterable[tuple[datetime, datetime]]:
    cursor = start
    while cursor < end:
        next_boundary = local_midnight_utc(cursor)
        if next_boundary <= cursor:
            next_boundary = cursor + timedelta(days=1)
        yield cursor, min(end, next_boundary)
        cursor = min(end, next_boundary)
