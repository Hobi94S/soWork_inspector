from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
import json
from zoneinfo import ZoneInfo

from .config import AppConfig
from .models import MemberRecord
from .reporting import build_clean_report
from .utils import json_dumps, parse_timestamp


class Storage:
    def __init__(self, config: AppConfig) -> None:
        self.root = config.output_dir
        self.time_zone_name = config.time_zone_name
        self.time_zone = ZoneInfo(config.time_zone_name)
        self.events_dir = self.root / "events"
        self.reports_dir = self.root / "reports"
        self.snapshots_dir = self.root / "snapshots"
        self.state_path = self.root / "state.json"
        self.summary_path = self.root / "daily_hours.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    def append_member_events(self, records: Iterable[MemberRecord]) -> None:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            day = self._day_key_from_datetime(record.timestamp)
            grouped.setdefault(day, []).append(record.to_dict())

        for day, payloads in grouped.items():
            path = self.events_dir / f"{day}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                for payload in payloads:
                    handle.write(json.dumps(payload, ensure_ascii=False))
                    handle.write("\n")

    def append_derived_events(self, events: Iterable[dict[str, Any]]) -> None:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            day = self._day_key_from_timestamp(event["timestamp"])
            grouped.setdefault(day, []).append(event)

        for day, payloads in grouped.items():
            path = self.events_dir / f"{day}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                for payload in payloads:
                    handle.write(json.dumps(payload, ensure_ascii=False))
                    handle.write("\n")

    def write_snapshot(self, records: list[MemberRecord], *, observed_at: datetime | None = None) -> Path:
        if observed_at is not None:
            timestamp = observed_at
        elif records:
            timestamp = records[0].timestamp
        else:
            from .utils import utc_now

            timestamp = utc_now()
        directory = self.snapshots_dir / self._day_key_from_datetime(timestamp)
        directory.mkdir(parents=True, exist_ok=True)
        filename = timestamp.isoformat().replace(":", "-").replace("+00:00", "Z")
        path = directory / f"{filename}_online_members.json"
        payload = [record.to_dict() for record in records]
        path.write_text(json_dumps(payload), encoding="utf-8")
        return path

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, payload: dict[str, Any]) -> None:
        temp_path = self.state_path.with_suffix(".tmp")
        temp_path.write_text(json_dumps(payload), encoding="utf-8")
        temp_path.replace(self.state_path)

    def write_summary(self, payload: dict[str, Any]) -> None:
        temp_path = self.summary_path.with_suffix(".tmp")
        temp_path.write_text(json_dumps(payload), encoding="utf-8")
        temp_path.replace(self.summary_path)

    def write_report(self, day_key: str, report: list[dict[str, Any]]) -> Path:
        path = self.reports_dir / f"{day_key}.json"
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(json_dumps(report), encoding="utf-8")
        temp_path.replace(path)
        return path

    def write_reports(self, payload: dict[str, dict[str, dict[str, Any]]]) -> list[Path]:
        written_paths: list[Path] = []
        for day_key, day_summary in sorted(payload.items()):
            written_paths.append(
                self.write_report(
                    day_key,
                    build_clean_report(day_summary, time_zone_name=self.time_zone_name),
                )
            )
        return written_paths

    def _day_key_from_datetime(self, value: datetime) -> str:
        return value.astimezone(self.time_zone).date().isoformat()

    def _day_key_from_timestamp(self, value: str) -> str:
        parsed = parse_timestamp(value)
        if parsed is None:
            raise ValueError("Runtime event is missing a valid timestamp.")
        return self._day_key_from_datetime(parsed)
