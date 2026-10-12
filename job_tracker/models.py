from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class EmailMessage:
    source: str
    source_id: str
    message_id: str
    subject: str
    sender: str
    received_at: datetime
    body_text: str = ""
    source_url: str = ""

    @property
    def dedupe_key(self) -> str:
        return f"{self.source}:{self.message_id or self.source_id}"


@dataclass(slots=True)
class Classification:
    event_type: str
    stage: str
    disposition: str
    confidence: float
    summary: str
    requires_review: bool = False


@dataclass(slots=True)
class Application:
    id: int | None
    company: str
    role: str = ""
    status: str = "unknown"
    stage: str = "unconfirmed"
    affinity_score: float | None = None
    affinity_label: str = "insufficient_data"
    last_update: str = ""
    next_action: str = "Verify application details"
    next_action_due: str = ""
    needs_review: bool = True
    source: str = "manual"
    source_confidence: float = 0.0
    profile_json: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ApplicationEvent:
    application_id: int
    source_key: str
    event_type: str
    stage: str
    status: str
    occurred_at: str
    sender: str
    subject: str
    evidence_summary: str
    confidence: float
    requires_review: bool = False
    source_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_iso(value: datetime | None = None) -> str:
    value = value or datetime.now().astimezone()
    return value.isoformat(timespec="seconds")
