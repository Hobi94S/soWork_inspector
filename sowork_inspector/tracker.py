from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .models import MemberRecord, SessionState
from .utils import chunk_time_range, parse_timestamp, to_iso_z


OFFLINE_GROUP_ID = "__offline__"


class HourTracker:
    def __init__(self, *, time_zone_name: str, away_counts_as_online: bool, logger: Any) -> None:
        self.time_zone = ZoneInfo(time_zone_name)
        self.logger = logger
        self.countable_states = {"Present", "Active"} | ({"Away"} if away_counts_as_online else set())

        self.active_sessions: dict[str, SessionState] = {}
        self.last_presence: dict[str, MemberRecord] = {}
        self.user_to_membership: dict[str, str] = {}
        self.member_cache: dict[str, dict[str, Any]] = {}
        self.summary: dict[str, dict[str, dict[str, Any]]] = {}

    @classmethod
    def from_state(cls, payload: dict[str, Any], *, time_zone_name: str, away_counts_as_online: bool, logger: Any) -> "HourTracker":
        tracker = cls(
            time_zone_name=time_zone_name,
            away_counts_as_online=away_counts_as_online,
            logger=logger,
        )
        tracker.user_to_membership = dict(payload.get("user_to_membership", {}))
        tracker.member_cache = dict(payload.get("member_cache", {}))
        tracker.summary = dict(payload.get("summary", {}))

        for membership_id, session in payload.get("active_sessions", {}).items():
            tracker.active_sessions[membership_id] = SessionState(
                started_at=parse_timestamp(session["started_at"]) or datetime.now(tz=tracker.time_zone),
                last_event_at=parse_timestamp(session["last_event_at"]) or datetime.now(tz=tracker.time_zone),
                state=session["state"],
                room_id=session.get("room_id"),
                user_id=session["user_id"],
            )

        for membership_id, raw_presence in payload.get("last_presence", {}).items():
            tracker.last_presence[membership_id] = MemberRecord(
                timestamp=parse_timestamp(raw_presence["timestamp"]) or datetime.now(tz=tracker.time_zone),
                membership_id=membership_id,
                user_id=raw_presence["user_id"],
                display_name=raw_presence.get("display_name", ""),
                full_name=raw_presence.get("full_name", ""),
                email=raw_presence.get("email", ""),
                state=raw_presence.get("state", "Unknown"),
                room_id=raw_presence.get("room_id"),
                group_id=raw_presence.get("group_id"),
                last_present_at=parse_timestamp(raw_presence.get("last_present_at")),
                source=raw_presence.get("source", "restored"),
                event_type=raw_presence.get("event_type", "presence"),
            )
        return tracker

    def export_state(self) -> dict[str, Any]:
        return {
            "active_sessions": {
                membership_id: session.to_dict()
                for membership_id, session in self.active_sessions.items()
            },
            "last_presence": {
                membership_id: record.to_dict()
                for membership_id, record in self.last_presence.items()
            },
            "user_to_membership": self.user_to_membership,
            "member_cache": self.member_cache,
            "summary": self.summary,
        }

    def project_summary(self, *, as_of: datetime | None = None) -> dict[str, dict[str, dict[str, Any]]]:
        projected_summary = deepcopy(self.summary)
        projection_time = as_of or self._latest_observed_at()
        if projection_time is None:
            return projected_summary

        for membership_id, session in self.active_sessions.items():
            previous = self.last_presence.get(membership_id)
            if not self._is_countable_state(session.state) or (
                previous is not None and not self._is_countable_record(previous)
            ):
                continue

            projected_record = self._build_projected_record(
                membership_id=membership_id,
                session=session,
                as_of=projection_time,
            )
            bucket = self._update_summary_metadata_in(projected_summary, projected_record)
            if bucket is not None:
                self._set_first_online_at(
                    bucket,
                    max(session.started_at, self._current_local_day_start_utc(projection_time)),
                )
            self._allocate_duration_in(
                projected_summary,
                session.started_at,
                projection_time,
                projected_record,
                increment_session_count=False,
            )

        return projected_summary

    def process_snapshot(self, records: list[MemberRecord], *, observed_at: datetime | None = None) -> list[dict[str, Any]]:
        derived_events: list[dict[str, Any]] = []
        seen: set[str] = set()
        snapshot_timestamp = observed_at or (records[0].timestamp if records else datetime.now(tz=self.time_zone))

        for record in records:
            resolved = self._resolve_identity(record)
            if resolved.membership_id is None:
                self.logger.warning("Ignoring snapshot record with no org membership id for user %s.", record.user_id)
                continue
            seen.add(resolved.membership_id)
            derived_events.extend(self._process_record(resolved))

        for membership_id, previous in list(self.last_presence.items()):
            if membership_id in seen:
                continue
            if previous.state == "Offline":
                continue
            offline = MemberRecord(
                timestamp=snapshot_timestamp,
                membership_id=membership_id,
                user_id=previous.user_id,
                display_name=previous.display_name,
                full_name=previous.full_name,
                email=previous.email,
                state="Offline",
                room_id=None,
                group_id=previous.group_id,
                last_present_at=previous.last_present_at,
                source="snapshot_missing",
                event_type="presence",
            )
            derived_events.extend(self._process_record(offline))

        return derived_events

    def process_presence_updates(self, records: list[MemberRecord]) -> list[dict[str, Any]]:
        derived_events: list[dict[str, Any]] = []
        for record in records:
            resolved = self._resolve_identity(record)
            if resolved.membership_id is None:
                self.logger.warning("Ignoring websocket presence update with no org membership id for user %s.", record.user_id)
                continue
            derived_events.extend(self._process_record(resolved))
        return derived_events

    def _resolve_identity(self, record: MemberRecord) -> MemberRecord:
        membership_id = record.membership_id or self.user_to_membership.get(record.user_id)
        if membership_id:
            self.user_to_membership[record.user_id] = membership_id
            self.member_cache[membership_id] = {
                "user_id": record.user_id,
                "display_name": record.display_name,
                "full_name": record.full_name,
                "email": record.email,
            }
            record.membership_id = membership_id
        return record

    def _process_record(self, record: MemberRecord) -> list[dict[str, Any]]:
        if record.membership_id is None:
            return []

        bucket = self._update_summary_metadata(record)
        previous = self.last_presence.get(record.membership_id)
        previous_state = previous.state if previous else "Offline"
        previous_countable = (
            self._is_countable_record(previous)
            if previous is not None
            else self._is_countable_state(previous_state)
        )
        current_countable = self._is_countable_record(record)
        derived_events: list[dict[str, Any]] = []

        if current_countable and bucket is not None:
            self._set_first_online_at(bucket, record.timestamp)

        if current_countable and record.membership_id not in self.active_sessions:
            self.active_sessions[record.membership_id] = SessionState(
                started_at=record.timestamp,
                last_event_at=record.timestamp,
                state=record.state,
                room_id=record.room_id,
                user_id=record.user_id,
            )
            derived_events.append(self._derived_event("session_started", record))
        elif current_countable:
            session = self.active_sessions[record.membership_id]
            session.last_event_at = record.timestamp
            session.state = record.state
            session.room_id = record.room_id

        if previous_countable and not current_countable:
            derived_events.extend(self._close_session(record))

        if (
            previous
            and previous.room_id != record.room_id
            and current_countable
            and previous.room_id is not None
            and record.room_id is not None
        ):
            derived_events.append(self._derived_event("room_changed", record))

        self.last_presence[record.membership_id] = record
        return derived_events

    def _close_session(self, record: MemberRecord) -> list[dict[str, Any]]:
        if record.membership_id is None:
            return []
        session = self.active_sessions.pop(record.membership_id, None)
        if session is None:
            return []

        self._allocate_duration(session.started_at, record.timestamp, record)
        return [self._derived_event("session_stopped", record, session_started_at=session.started_at)]

    def _update_summary_metadata(self, record: MemberRecord) -> dict[str, Any] | None:
        return self._update_summary_metadata_in(self.summary, record)

    def _update_summary_metadata_in(
        self,
        summary: dict[str, dict[str, dict[str, Any]]],
        record: MemberRecord,
    ) -> dict[str, Any] | None:
        if record.membership_id is None:
            return None
        day_key = record.timestamp.astimezone(self.time_zone).date().isoformat()
        day_summary = summary.setdefault(day_key, {})
        bucket = day_summary.setdefault(record.membership_id, self._summary_bucket(record))
        bucket["user_id"] = record.user_id
        bucket["display_name"] = record.display_name
        bucket["full_name"] = record.full_name
        bucket["email"] = record.email
        bucket["last_state"] = record.state
        bucket["last_room_id"] = record.room_id
        bucket["last_seen_at"] = to_iso_z(record.timestamp)
        return bucket

    def _allocate_duration(self, start: datetime, end: datetime, record: MemberRecord) -> None:
        self._allocate_duration_in(self.summary, start, end, record, increment_session_count=True)

    def _allocate_duration_in(
        self,
        summary: dict[str, dict[str, dict[str, Any]]],
        start: datetime,
        end: datetime,
        record: MemberRecord,
        *,
        increment_session_count: bool,
    ) -> None:
        if end <= start or record.membership_id is None:
            return

        for range_start, range_end in chunk_time_range(start, end, local_midnight_utc=self._next_local_midnight_utc):
            seconds = int((range_end - range_start).total_seconds())
            if seconds <= 0:
                continue
            day_key = range_start.astimezone(self.time_zone).date().isoformat()
            bucket = summary.setdefault(day_key, {}).setdefault(record.membership_id, self._summary_bucket(record))
            bucket["tracked_seconds"] += seconds
            bucket["tracked_minutes"] = round(bucket["tracked_seconds"] / 60, 2)
            bucket["tracked_hours"] = round(bucket["tracked_seconds"] / 3600, 2)
            if increment_session_count:
                bucket["session_count"] += 1
            self._set_first_online_at(bucket, range_start)
            bucket["display_name"] = record.display_name
            bucket["full_name"] = record.full_name
            bucket["email"] = record.email

    def _summary_bucket(self, record: MemberRecord) -> dict[str, Any]:
        return {
            "user_id": record.user_id,
            "display_name": record.display_name,
            "full_name": record.full_name,
            "email": record.email,
            "tracked_seconds": 0,
            "tracked_minutes": 0,
            "tracked_hours": 0.0,
            "session_count": 0,
            "first_online_at": None,
            "last_state": record.state,
            "last_room_id": record.room_id,
            "last_seen_at": to_iso_z(record.timestamp),
        }

    def latest_observed_at(self) -> datetime | None:
        return self._latest_observed_at()

    def _set_first_online_at(self, bucket: dict[str, Any], candidate: datetime) -> None:
        existing = parse_timestamp(bucket.get("first_online_at"))
        if existing is None or candidate < existing:
            bucket["first_online_at"] = to_iso_z(candidate)

    def _latest_observed_at(self) -> datetime | None:
        candidates = [session.last_event_at for session in self.active_sessions.values()]
        candidates.extend(record.timestamp for record in self.last_presence.values())
        if not candidates:
            return None
        return max(candidates)

    def _build_projected_record(
        self,
        *,
        membership_id: str,
        session: SessionState,
        as_of: datetime,
    ) -> MemberRecord:
        previous = self.last_presence.get(membership_id)
        cached = self.member_cache.get(membership_id, {})
        return MemberRecord(
            timestamp=as_of,
            membership_id=membership_id,
            user_id=session.user_id,
            display_name=(previous.display_name if previous else cached.get("display_name", "")),
            full_name=(previous.full_name if previous else cached.get("full_name", "")),
            email=(previous.email if previous else cached.get("email", "")),
            state=session.state,
            room_id=session.room_id,
            group_id=previous.group_id if previous else None,
            last_present_at=previous.last_present_at if previous else None,
            source="projected_report",
            event_type="presence",
        )

    def _next_local_midnight_utc(self, value: datetime) -> datetime:
        local_value = value.astimezone(self.time_zone)
        next_day = local_value.date() + timedelta(days=1)
        next_midnight_local = datetime.combine(next_day, datetime.min.time(), tzinfo=self.time_zone)
        return next_midnight_local.astimezone(value.tzinfo)

    def _current_local_day_start_utc(self, value: datetime) -> datetime:
        local_value = value.astimezone(self.time_zone)
        day_start_local = datetime.combine(local_value.date(), datetime.min.time(), tzinfo=self.time_zone)
        return day_start_local.astimezone(value.tzinfo)

    def _is_countable_state(self, state: str) -> bool:
        return state in self.countable_states

    def _is_countable_record(self, record: MemberRecord) -> bool:
        return (
            self._is_countable_state(record.state)
            and record.group_id is not None
            and record.group_id != OFFLINE_GROUP_ID
        )

    def _derived_event(
        self,
        event_type: str,
        record: MemberRecord,
        *,
        session_started_at: datetime | None = None,
    ) -> dict[str, Any]:
        payload = record.to_dict()
        payload["event_type"] = event_type
        if session_started_at:
            payload["session_started_at"] = to_iso_z(session_started_at)
        return payload
