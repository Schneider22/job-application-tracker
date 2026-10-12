from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from .config import app_home
from .models import Application, ApplicationEvent


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY,
  company TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'unknown',
  stage TEXT NOT NULL DEFAULT 'unconfirmed',
  affinity_score REAL,
  affinity_label TEXT NOT NULL DEFAULT 'insufficient_data',
  last_update TEXT NOT NULL DEFAULT '',
  next_action TEXT NOT NULL DEFAULT 'Verify application details',
  next_action_due TEXT NOT NULL DEFAULT '',
  needs_review INTEGER NOT NULL DEFAULT 1,
  source TEXT NOT NULL DEFAULT 'manual',
  source_confidence REAL NOT NULL DEFAULT 0,
  profile_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_application_company_role
  ON applications(lower(company), lower(role));
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  source_key TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  sender TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  evidence_summary TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL,
  requires_review INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_events_application_date ON events(application_id, occurred_at DESC);
CREATE TABLE IF NOT EXISTS processed_messages (
  source_key TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  message_id TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  sender TEXT NOT NULL DEFAULT '',
  received_at TEXT NOT NULL,
  processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_queue (
  id INTEGER PRIMARY KEY,
  source_key TEXT NOT NULL UNIQUE,
  application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL,
  company_guess TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  sender TEXT NOT NULL DEFAULT '',
  occurred_at TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

STAGE_RANK = {
    "unconfirmed": 0, "application_received": 1, "screening": 2,
    "assessment": 3, "interview": 4, "reference": 5, "offer": 6, "hired": 7,
}


class Database:
    def __init__(self, path: Path | None = None):
        self.path = path or (app_home() / "tracker.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(events)").fetchall()}
            if "source_url" not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN source_url TEXT NOT NULL DEFAULT ''")
            review_columns = {row["name"] for row in connection.execute("PRAGMA table_info(review_queue)").fetchall()}
            if "application_id" not in review_columns:
                connection.execute("ALTER TABLE review_queue ADD COLUMN application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _json(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def add_application(self, application: Application) -> int:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM applications WHERE lower(company)=lower(?) AND lower(role)=lower(?)",
                (application.company.strip(), application.role.strip()),
            ).fetchone()
            values = (
                application.company.strip(), application.role.strip(), application.status,
                application.stage, application.affinity_score, application.affinity_label,
                application.last_update, application.next_action, application.next_action_due,
                int(application.needs_review), application.source, application.source_confidence,
                self._json(application.profile_json), now,
            )
            if existing:
                connection.execute(
                    """UPDATE applications SET company=?,role=?,status=?,stage=?,affinity_score=?,
                       affinity_label=?,last_update=?,next_action=?,next_action_due=?,needs_review=?,
                       source=?,source_confidence=?,profile_json=?,updated_at=? WHERE id=?""",
                    (*values, existing["id"]),
                )
                return int(existing["id"])
            cursor = connection.execute(
                """INSERT INTO applications(company,role,status,stage,affinity_score,affinity_label,
                   last_update,next_action,next_action_due,needs_review,source,source_confidence,
                   profile_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*values[:-1], now, now),
            )
            return int(cursor.lastrowid)

    def get_application(self, application_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
        return self._application_dict(row) if row else None

    def find_application(self, company: str, role: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM applications WHERE lower(company)=lower(?)"
        params: tuple[Any, ...] = (company.strip(),)
        if role is not None:
            query += " AND lower(role)=lower(?)"
            params += (role.strip(),)
        query += " ORDER BY updated_at DESC LIMIT 1"
        with self.connect() as connection:
            row = connection.execute(query, params).fetchone()
        return self._application_dict(row) if row else None

    def list_applications(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM applications ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'offer' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END, company COLLATE NOCASE"
            ).fetchall()
        return [self._application_dict(row) for row in rows]

    @staticmethod
    def _application_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["needs_review"] = bool(result["needs_review"])
        result["profile_json"] = json.loads(result["profile_json"] or "{}")
        return result

    def message_seen(self, source_key: str) -> bool:
        with self.connect() as connection:
            return connection.execute("SELECT 1 FROM processed_messages WHERE source_key=?", (source_key,)).fetchone() is not None

    def mark_message(self, source_key: str, source: str, source_id: str, message_id: str,
                     subject: str, sender: str, received_at: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO processed_messages
                   (source_key,source,source_id,message_id,subject,sender,received_at,processed_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (source_key, source, source_id, message_id, subject[:500], sender[:500], received_at,
                 datetime.now().astimezone().isoformat(timespec="seconds")),
            )

    def record_event(self, event: ApplicationEvent) -> bool:
        """Insert evidence once; only sufficiently confident events update the current state."""
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO events(application_id,source_key,event_type,stage,status,
                   occurred_at,sender,subject,evidence_summary,source_url,confidence,requires_review)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event.application_id, event.source_key, event.event_type, event.stage, event.status,
                 event.occurred_at, event.sender[:500], event.subject[:500], event.evidence_summary[:360],
                 event.source_url[:1000], event.confidence, int(event.requires_review)),
            )
            if cursor.rowcount == 0:
                return False
            current = connection.execute(
                "SELECT status,stage,last_update FROM applications WHERE id=?", (event.application_id,)
            ).fetchone()
            if not current:
                return True
            try:
                event_time = datetime.fromisoformat(event.occurred_at)
                current_time = datetime.fromisoformat(current["last_update"]) if current["last_update"] else None
                if event_time.tzinfo is None:
                    event_time = event_time.astimezone()
                if current_time and current_time.tzinfo is None:
                    current_time = current_time.astimezone()
                if current_time and event_time < current_time:
                    return True
            except (TypeError, ValueError):
                event_time = datetime.now().astimezone()
                current_time = None
            event_at = event_time.isoformat(timespec="seconds")
            if event.requires_review or event.confidence < 0.80:
                connection.execute(
                    "UPDATE applications SET last_update=?,needs_review=1,updated_at=? WHERE id=?",
                    (event_at, datetime.now().astimezone().isoformat(timespec="seconds"), event.application_id),
                )
                self._add_review(connection, event.source_key, event.subject, event.sender,
                                 event.occurred_at, event.evidence_summary,
                                 "Email classification is uncertain or needs confirmation.",
                                 application_id=event.application_id)
                return True
            if current["status"] in {"rejected", "discarded"} and event.status in {"active", "offer", "hired"}:
                connection.execute("UPDATE applications SET last_update=?,needs_review=1,updated_at=? WHERE id=?",
                                   (event_at, datetime.now().astimezone().isoformat(timespec="seconds"), event.application_id))
                self._add_review(connection, event.source_key, event.subject, event.sender,
                                 event.occurred_at, event.evidence_summary,
                                 "A later message appears to reopen a closed application; confirm manually.",
                                 application_id=event.application_id)
                return True
            chosen_status, chosen_stage = event.status, event.stage
            chosen_action = self._next_action(event.stage, event.status)
            if (event.status == "active" and current["status"] == "active"
                    and STAGE_RANK.get(event.stage, 0) < STAGE_RANK.get(current["stage"], 0)):
                chosen_stage = current["stage"]
                chosen_action = self._next_action(current["stage"], current["status"])
            connection.execute(
                """UPDATE applications SET status=?,stage=?,needs_review=0,
                   next_action=?,next_action_due='',last_update=?,updated_at=? WHERE id=?""",
                (chosen_status, chosen_stage, chosen_action,
                 event_at, datetime.now().astimezone().isoformat(timespec="seconds"), event.application_id),
            )
            return True

    @staticmethod
    def _next_action(stage: str, status: str) -> str:
        if status == "rejected":
            return "Archive or review future opportunities"
        actions = {
            "application_received": "Wait for the next update; prepare a follow-up",
            "screening": "Prepare for the screening conversation",
            "assessment": "Complete or review the assessment instructions",
            "interview": "Prepare for the interview",
            "reference": "Prepare references and follow up",
            "offer": "Review the offer terms",
            "hired": "Confirm onboarding details",
        }
        return actions.get(stage, "Review the email and confirm the next action")

    def add_review(self, source_key: str, company_guess: str, subject: str, sender: str,
                   occurred_at: str, summary: str, reason: str, application_id: int | None = None) -> None:
        with self.connect() as connection:
            self._add_review(connection, source_key, subject, sender, occurred_at, summary, reason,
                             company_guess, application_id)

    @staticmethod
    def _add_review(connection: sqlite3.Connection, source_key: str, subject: str, sender: str,
                    occurred_at: str, summary: str, reason: str, company_guess: str = "",
                    application_id: int | None = None) -> None:
        connection.execute(
            """INSERT OR IGNORE INTO review_queue(source_key,application_id,company_guess,subject,sender,occurred_at,summary,reason)
               VALUES(?,?,?,?,?,?,?,?)""",
            (source_key, application_id, company_guess[:160], subject[:500], sender[:500], occurred_at,
             summary[:360], reason[:300]),
        )

    def list_reviews(self, pending_only: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM review_queue"
        if pending_only:
            query += " WHERE status='pending'"
        query += " ORDER BY occurred_at DESC"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query).fetchall()]

    def set_application_reviewed(self, application_id: int, status: str, stage: str,
                                 affinity_score: float | None = None) -> None:
        allowed_status = {"active", "discarded", "offer", "hired", "unknown"}
        allowed_stage = {"unconfirmed", "application_received", "screening", "assessment", "interview", "reference", "offer", "hired"}
        if status not in allowed_status or stage not in allowed_stage:
            raise ValueError("Unsupported status or stage.")
        if affinity_score is not None and not 0 <= affinity_score <= 100:
            raise ValueError("Affinity score must be between 0 and 100.")
        label = "insufficient_data"
        if affinity_score is not None:
            label = "promising" if affinity_score >= 80 else "intermediate" if affinity_score >= 60 else "low"
        with self.connect() as connection:
            connection.execute(
                """UPDATE applications SET status=?,stage=?,needs_review=0,
                   affinity_score=COALESCE(?,affinity_score),affinity_label=CASE WHEN ? IS NULL THEN affinity_label ELSE ? END,
                   updated_at=? WHERE id=?""",
                (status, stage, affinity_score, affinity_score, label,
                 datetime.now().astimezone().isoformat(timespec="seconds"), application_id),
            )
            connection.execute("UPDATE review_queue SET status='reviewed' WHERE application_id=? AND status='pending'", (application_id,))

    def resolve_review(self, review_id: int) -> None:
        with self.connect() as connection:
            row = connection.execute("SELECT application_id FROM review_queue WHERE id=?", (review_id,)).fetchone()
            connection.execute("UPDATE review_queue SET status='reviewed' WHERE id=?", (review_id,))
            if row and row["application_id"] is not None:
                still_pending = connection.execute(
                    "SELECT 1 FROM review_queue WHERE application_id=? AND status='pending' LIMIT 1",
                    (row["application_id"],),
                ).fetchone()
                if not still_pending:
                    connection.execute("UPDATE applications SET needs_review=0 WHERE id=?", (row["application_id"],))

    def set_affinity(self, application_id: int, score: float | None, label: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE applications SET affinity_score=?,affinity_label=?,updated_at=? WHERE id=?",
                (score, label, datetime.now().astimezone().isoformat(timespec="seconds"), application_id),
            )

    def refresh_followups(self, after_days: int = 7) -> int:
        """Set a transparent follow-up action only when a verified application is stale."""
        from datetime import timedelta
        cutoff = datetime.now().astimezone() - timedelta(days=after_days)
        updated = 0
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id,status,stage,last_update FROM applications WHERE status IN ('active','unknown') AND stage!='unconfirmed'"
            ).fetchall()
            for row in rows:
                try:
                    last = datetime.fromisoformat(row["last_update"])
                except (TypeError, ValueError):
                    continue
                if last.tzinfo is None:
                    last = last.astimezone()
                if last <= cutoff:
                    due = date.today().isoformat()
                    connection.execute(
                        "UPDATE applications SET next_action='Follow up on the application if no reply was received',next_action_due=?,updated_at=? WHERE id=?",
                        (due, datetime.now().astimezone().isoformat(timespec="seconds"), row["id"]),
                    )
                    updated += 1
        return updated

    def events_for(self, application_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM events"
        params: tuple[Any, ...] = ()
        if application_id is not None:
            query += " WHERE application_id=?"
            params = (application_id,)
        query += " ORDER BY occurred_at DESC LIMIT ?"
        params += (limit,)
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def set_metadata(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def get_metadata(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def dashboard_data(self) -> dict[str, Any]:
        applications = self.list_applications()
        events = self.events_for(limit=12)
        reviews = self.list_reviews()
        active = [a for a in applications if a["status"] in {"active", "offer", "hired", "unknown"}]
        interviews = [a for a in applications if a["stage"] == "interview"]
        promising = [a for a in applications if a["affinity_score"] is not None and a["affinity_score"] >= 80]
        next_actions = [
            {"application_id": a["id"], "company": a["company"], "role": a["role"],
             "action": a["next_action"], "due": a["next_action_due"]}
            for a in active if a["next_action"] and a["next_action"] != "Verify application details"
        ]
        return {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "title": "Aplicación a Empleos",
            "applications": applications,
            "events": events,
            "reviews": reviews,
            "summary": {
                "total": len(applications),
                "active": len(active),
                "interviews": len(interviews),
                "promising": len(promising),
                "discarded": sum(a["status"] in {"discarded", "rejected"} for a in applications),
                "needs_review": len(reviews) + sum(a["needs_review"] and not any(r.get("application_id") == a["id"] for r in reviews) for a in applications),
            },
            "next_actions": next_actions,
        }
