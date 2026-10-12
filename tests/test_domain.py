from __future__ import annotations

import json
import unittest
import uuid
from datetime import datetime
from pathlib import Path

from job_tracker.affinity import calculate_affinity
from job_tracker.classification import classify_message, is_job_related, match_application
from job_tracker.config import Settings, load_settings, save_settings
from job_tracker.database import Database
from job_tracker.models import Application, ApplicationEvent, EmailMessage
from job_tracker.scheduler import due_slots
from job_tracker.service import bootstrap_applications, sync_mail
from unittest.mock import patch


TEST_TMP = Path(__file__).resolve().parents[1] / ".local" / "tmp"


def temporary_file(name: str) -> Path:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    return TEST_TMP / f"{name}-{uuid.uuid4().hex}"


def make_event(application_id: int, key: str, occurred_at: str, stage: str,
               status: str = "active", confidence: float = 0.92) -> ApplicationEvent:
    return ApplicationEvent(application_id, key, stage, stage, status, occurred_at,
                            "talent@example.com", f"Update {stage}", f"Evidence for {stage}", confidence)


class ClassificationTests(unittest.TestCase):
    def message(self, subject: str, body: str = "") -> EmailMessage:
        return EmailMessage("imap", "100", "msg-100", subject, "People Team <jobs@example.com>",
                            datetime.fromisoformat("2026-10-12T10:00:00-05:00"), body)

    def test_classifies_common_job_updates(self):
        cases = [
            ("Confirmación de postulación", "Hemos recibido tu candidatura", "application_received", "active"),
            ("Entrevista con el equipo", "Agenda tu entrevista", "interview", "active"),
            ("Prueba técnica", "Completa la evaluación", "assessment", "active"),
            ("Actualización del proceso", "No avanzaremos con tu candidatura", "rejection", "rejected"),
            ("Oferta laboral", "Adjuntamos la propuesta salarial", "offer", "offer"),
        ]
        for subject, body, expected_event, expected_status in cases:
            with self.subTest(subject=subject):
                result = classify_message(self.message(subject, body))
                self.assertEqual(result.event_type, expected_event)
                self.assertEqual(result.disposition, expected_status)
                self.assertGreaterEqual(result.confidence, 0.8)

    def test_ambiguous_job_message_is_flagged(self):
        result = classify_message(self.message("Proceso de selección", "Te compartimos más información."))
        self.assertTrue(result.requires_review)
        self.assertEqual(result.disposition, "unknown")
        self.assertTrue(is_job_related(self.message("Proceso de selección")))

    def test_company_match_uses_known_names(self):
        message = self.message("Entrevista en BairesDev", "Nos gustaría conocerte.")
        result = match_application(message, [{"id": 1, "company": "BairesDev"}, {"id": 2, "company": "Dev"}])
        self.assertEqual(result["id"], 1)


class AffinityTests(unittest.TestCase):
    def test_affinity_uses_only_available_factors(self):
        result = calculate_affinity({"skills": ["SQL", "Python"]}, {"required_skills": ["SQL", "Python", "R"]})
        self.assertEqual(result["score"], 66.7)
        self.assertEqual(result["label"], "intermediate")
        self.assertIn("required_skills", result["factors"])

    def test_missing_profile_or_requirements_returns_no_score(self):
        result = calculate_affinity({}, {})
        self.assertIsNone(result["score"])
        self.assertEqual(result["label"], "insufficient_data")

    def test_complete_match_is_promising(self):
        result = calculate_affinity({"skills": ["SQL", "Python"]}, {"required_skills": ["SQL", "Python"]})
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["label"], "promising")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.db_path = temporary_file("tracker").with_suffix(".sqlite3")
        self.database = Database(self.db_path)
        self.application_id = self.database.add_application(Application(None, "Example Corp", "Data Analyst"))

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.db_path}{suffix}").unlink(missing_ok=True)

    def test_duplicate_event_does_not_reapply(self):
        event = make_event(self.application_id, "gmail:msg-1", "2026-10-12T09:00:00-05:00", "interview")
        self.assertTrue(self.database.record_event(event))
        self.assertFalse(self.database.record_event(event))
        app = self.database.get_application(self.application_id)
        self.assertEqual(app["stage"], "interview")
        self.assertEqual(len(self.database.events_for(self.application_id)), 1)

    def test_old_email_does_not_downgrade_progress(self):
        self.database.record_event(make_event(self.application_id, "gmail:new", "2026-10-12T15:00:00-05:00", "interview"))
        self.database.record_event(make_event(self.application_id, "gmail:old", "2026-10-10T10:00:00-05:00", "application_received"))
        app = self.database.get_application(self.application_id)
        self.assertEqual(app["stage"], "interview")
        self.assertEqual(app["last_update"], "2026-10-12T15:00:00-05:00")

    def test_later_confirmed_lower_stage_does_not_reduce_highest_progress(self):
        self.database.record_event(make_event(self.application_id, "gmail:interview", "2026-10-10T10:00:00-05:00", "interview"))
        self.database.record_event(make_event(self.application_id, "gmail:received", "2026-10-12T10:00:00-05:00", "application_received"))
        app = self.database.get_application(self.application_id)
        self.assertEqual(app["stage"], "interview")
        self.assertEqual(app["last_update"], "2026-10-12T10:00:00-05:00")

    def test_uncertain_update_requires_review_and_does_not_change_stage(self):
        self.database.record_event(make_event(self.application_id, "gmail:interview", "2026-10-10T10:00:00-05:00", "interview"))
        uncertain = make_event(self.application_id, "gmail:uncertain", "2026-10-12T10:00:00-05:00", "unconfirmed", confidence=0.4)
        self.database.record_event(uncertain)
        app = self.database.get_application(self.application_id)
        self.assertEqual(app["stage"], "interview")
        self.assertTrue(app["needs_review"])
        self.assertEqual(len(self.database.list_reviews()), 1)

    def test_closed_process_is_not_automatically_reopened(self):
        self.database.record_event(make_event(self.application_id, "gmail:reject", "2026-10-10T10:00:00-05:00", "rejected", "rejected"))
        self.database.record_event(make_event(self.application_id, "gmail:new-process", "2026-10-12T10:00:00-05:00", "interview"))
        app = self.database.get_application(self.application_id)
        self.assertEqual(app["status"], "rejected")
        self.assertTrue(app["needs_review"])

    def test_body_is_not_stored_in_database(self):
        body = "Hemos recibido tu candidatura. " + ("private-body-marker " * 100)
        message = EmailMessage("imap", "42", "<unique-id>", "Confirmación", "jobs@example.com",
                               datetime.fromisoformat("2026-10-12T10:00:00-05:00"), body)
        event = ApplicationEvent(self.application_id, message.dedupe_key, "application_received",
                                 "application_received", "active", message.received_at.isoformat(),
                                 message.sender, message.subject, body[:320], 0.84, False, "")
        self.database.record_event(event)
        with self.database.connect() as connection:
            stored = " ".join(str(value) for row in connection.execute("SELECT * FROM events") for value in row)
            stored += " ".join(str(value) for row in connection.execute("SELECT * FROM processed_messages") for value in row)
        self.assertNotIn(body, stored)
        self.assertLessEqual(len(self.database.events_for()[0]["evidence_summary"]), 360)

    def test_seed_import_ignores_existing_company_role_pair(self):
        self.database.add_application(Application(None, "Siigo", "Analyst", source="conversation"))
        path = temporary_file("seed").with_suffix(".json")
        try:
            path.write_text(json.dumps([
                {"company": "Siigo", "role": "Analyst", "status": "active"},
                {"company": "Dropi", "status": "unknown"},
            ]), encoding="utf-8")
            count = bootstrap_applications(self.database, str(path))
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(count, 1)
        self.assertIsNotNone(self.database.find_application("Dropi"))

    def test_sync_deduplicates_messages_and_matches_candidate(self):
        message = EmailMessage("imap:test@example.com", "uid-3", "msg-3", "Entrevista en Example Corp",
                               "Hiring <jobs@example.com>", datetime.fromisoformat("2026-10-12T10:00:00-05:00"),
                               "Te invitamos a entrevista.")

        class FakeMailbox:
            def fetch(self, since):
                yield message

        settings = Settings()
        settings.mailbox.provider = "imap"
        settings.mailbox.email = "test@example.com"
        with patch("job_tracker.service.mailbox_for", return_value=FakeMailbox()):
            first = sync_mail(self.database, settings, object())
            second = sync_mail(self.database, settings, object())
        self.assertEqual(first["events"], 1)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(self.database.get_application(self.application_id)["stage"], "interview")

    def test_unmatched_job_email_goes_to_review_without_creating_candidate(self):
        message = EmailMessage("imap:test@example.com", "uid-4", "msg-4", "Proceso de selección",
                               "Hiring <jobs@other.example>", datetime.fromisoformat("2026-10-12T10:00:00-05:00"),
                               "Tu candidatura continúa en evaluación.")

        class FakeMailbox:
            def fetch(self, since):
                yield message

        settings = Settings()
        settings.mailbox.provider = "imap"
        settings.mailbox.email = "test@example.com"
        with patch("job_tracker.service.mailbox_for", return_value=FakeMailbox()):
            result = sync_mail(self.database, settings, object())
        self.assertEqual(result["reviews"], 1)
        self.assertEqual(len(self.database.list_applications()), 1)
        self.assertEqual(self.database.list_reviews()[0]["application_id"], None)


class ConfigTests(unittest.TestCase):
    def test_password_is_not_a_config_field(self):
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        settings = Settings()
        settings.mailbox.email = "candidate@example.com"
        path = save_settings(settings, TEST_TMP)
        raw = path.read_text(encoding="utf-8")
        loaded = load_settings(TEST_TMP)
        path.unlink(missing_ok=True)
        self.assertNotIn("password", raw.casefold())
        self.assertEqual(loaded.mailbox.email, "candidate@example.com")


class SchedulerTests(unittest.TestCase):
    def test_login_catchup_returns_both_due_slots(self):
        now = datetime.fromisoformat("2026-10-12T19:00:00-05:00")
        self.assertEqual(due_slots(now, ["08:00", "18:00"], set()), ["morning", "evening"])
        self.assertEqual(due_slots(now, ["08:00", "18:00"], {"morning"}), ["evening"])

    def test_catchup_does_not_run_future_slot(self):
        now = datetime.fromisoformat("2026-10-12T12:00:00-05:00")
        self.assertEqual(due_slots(now, ["08:00", "18:00"], {"morning"}), [])

    def test_invalid_schedule_times_are_rejected(self):
        with self.assertRaises(ValueError):
            due_slots(datetime.now(), ["8:00", "18:00"], set())


if __name__ == "__main__":
    unittest.main()
