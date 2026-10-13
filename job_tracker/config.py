from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def app_home() -> Path:
    override = os.environ.get("JOB_TRACKER_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "JobApplicationTracker"
    return Path.home() / ".local" / "share" / "job-application-tracker"


@dataclass(slots=True)
class MailboxSettings:
    provider: str = "imap"
    email: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_tls: bool = True
    imap_mailbox: str = "INBOX"
    gmail_client_file: str = ""
    gmail_query: str = "newer_than:365d"


@dataclass(slots=True)
class CloudSettings:
    provider: str = ""
    account_email: str = ""
    folder_name: str = "Job Application Tracker"
    google_client_file: str = ""
    microsoft_client_id: str = ""


@dataclass(slots=True)
class ScheduleSettings:
    timezone: str = "America/Bogota"
    times: list[str] = field(default_factory=lambda: ["08:00", "18:00"])


@dataclass(slots=True)
class Settings:
    mailbox: MailboxSettings = field(default_factory=MailboxSettings)
    cloud: CloudSettings = field(default_factory=CloudSettings)
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    email_reports_enabled: bool = False
    email_lookback_days: int = 365
    follow_up_after_days: int = 7
    profile: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Settings":
        return cls(
            mailbox=MailboxSettings(**raw.get("mailbox", {})),
            cloud=CloudSettings(**raw.get("cloud", {})),
            schedule=ScheduleSettings(**raw.get("schedule", {})),
            email_reports_enabled=bool(raw.get("email_reports_enabled", False)),
            email_lookback_days=int(raw.get("email_lookback_days", 365)),
            follow_up_after_days=int(raw.get("follow_up_after_days", 7)),
            profile=raw.get("profile", {}),
        )


def config_path(home: Path | None = None) -> Path:
    return (home or app_home()) / "config.json"


def load_settings(home: Path | None = None) -> Settings:
    path = config_path(home)
    if not path.exists():
        return Settings()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Settings.from_dict(raw)


def save_settings(settings: Settings, home: Path | None = None) -> Path:
    directory = home or app_home()
    directory.mkdir(parents=True, exist_ok=True)
    path = config_path(directory)
    fd, temporary = tempfile.mkstemp(prefix="config-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(settings), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path
