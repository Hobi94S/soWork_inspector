from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .utils import to_iso_z


@dataclass
class MemberRecord:
    timestamp: datetime
    membership_id: str | None
    user_id: str
    display_name: str = ""
    full_name: str = ""
    email: str = ""
    state: str = "Unknown"
    room_id: str | None = None
    group_id: str | None = None
    last_present_at: datetime | None = None
    source: str = "unknown"
    event_type: str = "presence"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "timestamp": to_iso_z(self.timestamp),
            "membership_id": self.membership_id,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "full_name": self.full_name,
            "email": self.email,
            "state": self.state,
            "room_id": self.room_id,
            "group_id": self.group_id,
            "last_present_at": to_iso_z(self.last_present_at),
            "source": self.source,
            "event_type": self.event_type,
        }
        if self.raw:
            payload["raw"] = self.raw
        return payload


@dataclass
class SessionState:
    started_at: datetime
    last_event_at: datetime
    state: str
    room_id: str | None
    user_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": to_iso_z(self.started_at),
            "last_event_at": to_iso_z(self.last_event_at),
            "state": self.state,
            "room_id": self.room_id,
            "user_id": self.user_id,
        }
