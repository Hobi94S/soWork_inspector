from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable
import json
from zoneinfo import ZoneInfo

from .models import MemberRecord
from .tracker import HourTracker
from .utils import parse_timestamp


def build_clean_report(
    day_summary: dict[str, dict[str, Any]],
    *,
    time_zone_name: str,
) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    for membership_id, bucket in day_summary.items():
        tracked_seconds = int(bucket.get("tracked_seconds", 0) or 0)
        report.append(
            {
                "name": (bucket.get("full_name") or bucket.get("display_name") or "").strip(),
                "email": bucket.get("email", ""),
                "id": bucket.get("user_id") or membership_id,
                "hours_online": round(tracked_seconds / 3600, 2),
                "first_time_recorded": _format_local_report_time(
                    bucket.get("first_online_at"),
                    time_zone_name=time_zone_name,
                ),
            }
        )

    report.sort(
        key=lambda item: (
            item["name"].casefold(),
            item["email"].casefold(),
            item["id"],
        )
    )
    return report


def rebuild_clean_reports_from_events(
    event_paths: Iterable[Path],
    *,
    time_zone_name: str,
    away_counts_as_online: bool,
    logger: Any,
    day_key: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    tracker, projected_summary = rebuild_summary_from_events(
        event_paths,
        time_zone_name=time_zone_name,
        away_counts_as_online=away_counts_as_online,
        logger=logger,
    )
    if day_key is not None:
        return {
            day_key: build_clean_report(
                projected_summary.get(day_key, {}),
                time_zone_name=time_zone_name,
            )
        }
    return {
        current_day: build_clean_report(day_summary, time_zone_name=time_zone_name)
        for current_day, day_summary in sorted(projected_summary.items())
    }


def rebuild_summary_from_events(
    event_paths: Iterable[Path],
    *,
    time_zone_name: str,
    away_counts_as_online: bool,
    logger: Any,
) -> tuple[HourTracker, dict[str, dict[str, dict[str, Any]]]]:
    tracker = HourTracker(
        time_zone_name=time_zone_name,
        away_counts_as_online=away_counts_as_online,
        logger=logger,
    )
    snapshot_batch: list[MemberRecord] = []
    snapshot_timestamp: str | None = None

    def flush_snapshot_batch() -> None:
        nonlocal snapshot_batch
        if not snapshot_batch:
            return
        tracker.process_snapshot(snapshot_batch)
        snapshot_batch = []

    for event_path in sorted(event_paths):
        with event_path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                payload = json.loads(raw_line)
                event_type = payload.get("event_type")
                if event_type not in {"snapshot", "presence"}:
                    continue

                record = _member_record_from_payload(payload)
                if event_type == "snapshot":
                    current_timestamp = payload["timestamp"]
                    if snapshot_batch and current_timestamp != snapshot_timestamp:
                        flush_snapshot_batch()
                    snapshot_timestamp = current_timestamp
                    snapshot_batch.append(record)
                    continue

                flush_snapshot_batch()
                tracker.process_presence_updates([record])

    flush_snapshot_batch()
    return tracker, tracker.project_summary()


def _member_record_from_payload(payload: dict[str, Any]) -> MemberRecord:
    timestamp = parse_timestamp(payload.get("timestamp"))
    if timestamp is None:
        raise ValueError("Runtime event is missing a valid timestamp.")

    return MemberRecord(
        timestamp=timestamp,
        membership_id=payload.get("membership_id"),
        user_id=payload.get("user_id", ""),
        display_name=payload.get("display_name", ""),
        full_name=payload.get("full_name", ""),
        email=payload.get("email", ""),
        state=payload.get("state", "Unknown"),
        room_id=payload.get("room_id"),
        group_id=payload.get("group_id"),
        last_present_at=parse_timestamp(payload.get("last_present_at")),
        source=payload.get("source", "runtime_event"),
        event_type=payload.get("event_type", "presence"),
        raw=payload.get("raw", {}),
    )


def _format_local_report_time(value: str | None, *, time_zone_name: str) -> str | None:
    if not value:
        return None

    timestamp = parse_timestamp(value)
    if timestamp is None:
        return None

    local_timestamp = timestamp.astimezone(ZoneInfo(time_zone_name))
    offset = local_timestamp.utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    offset_hours, offset_minutes = divmod(total_minutes, 60)
    return f"{local_timestamp:%H:%M}h UTC{sign}{offset_hours:02d}:{offset_minutes:02d}"
