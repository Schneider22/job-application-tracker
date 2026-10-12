from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .affinity import calculate_affinity
from .classification import classify_message, is_job_related, match_application
from .config import Settings
from .database import Database
from .mailbox import mailbox_for
from .models import ApplicationEvent
from .security import CredentialStore


def sync_mail(database: Database, settings: Settings, store: CredentialStore) -> dict[str, int]:
    connector = mailbox_for(settings.mailbox, store)
    cursor_key = f"last_email_sync:{settings.mailbox.provider}:{settings.mailbox.email.casefold()}"
    last_sync = database.get_metadata(cursor_key)
    if last_sync:
        try:
            since = datetime.fromisoformat(last_sync).date() - timedelta(days=2)
        except ValueError:
            since = date.today() - timedelta(days=2)
    else:
        since = date.today() - timedelta(days=settings.email_lookback_days)

    totals = {"scanned": 0, "events": 0, "reviews": 0, "duplicates": 0, "unrelated": 0}
    for message in connector.fetch(since):
        totals["scanned"] += 1
        if database.message_seen(message.dedupe_key):
            totals["duplicates"] += 1
            continue
        classification = classify_message(message)
        applications = database.list_applications()
        application = match_application(message, applications)
        occurred_at = message.received_at.isoformat(timespec="seconds")
        if not application:
            if is_job_related(message) or classification.event_type != "unclassified":
                database.add_review(
                    message.dedupe_key, _company_guess(message.sender), message.subject,
                    message.sender, occurred_at, classification.summary,
                    "Could not reliably match this job-related email to a tracked application.",
                )
                totals["reviews"] += 1
            else:
                totals["unrelated"] += 1
            database.mark_message(message.dedupe_key, message.source, message.source_id,
                                  message.message_id, message.subject, message.sender, occurred_at)
            continue

        event = ApplicationEvent(
            application_id=int(application["id"]), source_key=message.dedupe_key,
            event_type=classification.event_type, stage=classification.stage,
            status=classification.disposition, occurred_at=occurred_at,
            sender=message.sender, subject=message.subject,
            evidence_summary=classification.summary, confidence=classification.confidence,
            requires_review=classification.requires_review, source_url=message.source_url,
        )
        inserted = database.record_event(event)
        database.mark_message(message.dedupe_key, message.source, message.source_id,
                              message.message_id, message.subject, message.sender, occurred_at)
        if inserted:
            if event.requires_review or event.confidence < 0.80:
                totals["reviews"] += 1
            else:
                totals["events"] += 1
    database.set_metadata(cursor_key, datetime.now().astimezone().isoformat(timespec="seconds"))
    database.refresh_followups(settings.follow_up_after_days)
    return totals


def update_affinities(database: Database, settings: Settings) -> int:
    updated = 0
    for application in database.list_applications():
        requirements = application.get("profile_json") or {}
        result = calculate_affinity(settings.profile, requirements)
        if result["score"] is None:
            continue
        database.set_affinity(int(application["id"]), result["score"], result["label"])
        updated += 1
    return updated


def _company_guess(sender: str) -> str:
    """A cautious hint for the review queue; never used to change application state."""
    address = sender.split("<")[-1].split(">", 1)[0].strip().casefold()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    labels = [part for part in domain.split(".") if part and part not in {"com", "co", "net", "org", "mail"}]
    return labels[-2].replace("-", " ").title() if len(labels) >= 2 else (labels[0].title() if labels else "")


def bootstrap_applications(database: Database, path: str) -> int:
    """Import user-specific conversation records from a local, ignored JSON file."""
    import json
    from pathlib import Path
    from .models import Application

    source = Path(path).expanduser()
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Conversation seed must be a JSON array.")
    count = 0
    for raw in data:
        if not isinstance(raw, dict) or not raw.get("company"):
            continue
        application = Application(
            id=None,
            company=str(raw["company"]),
            role=str(raw.get("role", "")),
            status=str(raw.get("status", "unknown")),
            stage=str(raw.get("stage", "unconfirmed")),
            affinity_score=raw.get("affinity_score"),
            affinity_label=str(raw.get("affinity_label", "insufficient_data")),
            last_update=str(raw.get("last_update", "")),
            next_action=str(raw.get("next_action", "Verify application details")),
            next_action_due=str(raw.get("next_action_due", "")),
            needs_review=bool(raw.get("needs_review", True)),
            source=str(raw.get("source", "conversation")),
            source_confidence=float(raw.get("source_confidence", 0.0)),
            profile_json=raw.get("requirements", {}),
        )
        if application.status not in {"active", "discarded", "rejected", "offer", "hired", "unknown"}:
            application.status = "unknown"
            application.needs_review = True
        existing = database.find_application(application.company, application.role)
        if existing:
            continue
        database.add_application(application)
        count += 1
    return count


def bootstrap_local_conversations(database: Database) -> int:
    """Load only the user's ignored local seed; no personal records are shipped in Git."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    seed = root / ".local" / "conversation_seed.json"
    if database.get_metadata("conversation_seed_imported"):
        return 0
    if not seed.is_file():
        database.set_metadata("conversation_seed_imported", "no-local-seed")
        return 0
    count = bootstrap_applications(database, str(seed))
    database.set_metadata("conversation_seed_imported", datetime.now().astimezone().isoformat(timespec="seconds"))
    return count
