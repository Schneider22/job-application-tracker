from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from .cloud import publish_files
from .config import Settings, app_home, load_settings, save_settings
from .dashboard import render_dashboard
from .database import Database
from .emailer import GmailReportSender
from .models import Application
from .reporting import generate_reports
from .scheduler import due_slots, install_windows_tasks, remove_windows_tasks
from .security import CredentialStore
from .service import bootstrap_applications, bootstrap_local_conversations, sync_mail, update_affinities


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    result = input(f"{label}{suffix}: ").strip()
    return result or default


def command_setup(args: argparse.Namespace) -> int:
    settings = load_settings()
    store = CredentialStore()
    print("Job Application Tracker setup. Account secrets are stored in Windows Credential Manager.")
    mailbox_provider = _prompt("Mailbox provider: imap or gmail", settings.mailbox.provider or "imap").casefold()
    if mailbox_provider not in {"imap", "gmail"}:
        raise ValueError("Mailbox provider must be imap or gmail.")
    email_address = _prompt("Mailbox email", settings.mailbox.email)
    settings.mailbox.provider = mailbox_provider
    settings.mailbox.email = email_address
    if mailbox_provider == "imap":
        settings.mailbox.imap_host = _prompt("IMAP host", settings.mailbox.imap_host)
        settings.mailbox.imap_port = int(_prompt("IMAP port", str(settings.mailbox.imap_port or 993)))
        settings.mailbox.imap_mailbox = _prompt("Mailbox name", settings.mailbox.imap_mailbox or "INBOX")
        settings.mailbox.imap_tls = _prompt("Use TLS? (y/n)", "y").casefold() not in {"n", "no"}
        password = getpass.getpass("Mailbox password or application password (not saved in config): ")
        store.put("mailbox-password", password)
    else:
        settings.mailbox.gmail_client_file = _prompt("Path to local Google OAuth desktop client JSON", settings.mailbox.gmail_client_file)
        print("Gmail synchronization uses read-only access. Enabling report delivery separately requests Gmail send permission.")

    send_reports = _prompt("Send daily PDF and Excel reports to this same Gmail account? (y/n)",
                           "y" if settings.email_reports_enabled else "n").casefold() not in {"n", "no"}
    if send_reports and mailbox_provider != "gmail":
        raise ValueError("Self-addressed email reports currently require the Gmail provider.")
    settings.email_reports_enabled = send_reports

    cloud_provider = _prompt("Cloud dashboard: google, microsoft, or skip", settings.cloud.provider or "skip").casefold()
    if cloud_provider not in {"google", "microsoft", "skip"}:
        raise ValueError("Cloud provider must be google, microsoft, or skip.")
    if cloud_provider != "skip":
        settings.cloud.provider = cloud_provider
        settings.cloud.account_email = _prompt("Cloud account email", settings.cloud.account_email or email_address)
        settings.cloud.folder_name = _prompt("Cloud folder", settings.cloud.folder_name or "Job Application Tracker")
        if cloud_provider == "google":
            settings.cloud.google_client_file = _prompt("Path to local Google OAuth desktop client JSON", settings.cloud.google_client_file or settings.mailbox.gmail_client_file)
        else:
            settings.cloud.microsoft_client_id = _prompt("Microsoft Entra public client application ID", settings.cloud.microsoft_client_id)
    else:
        settings.cloud.provider = ""
    path = save_settings(settings)
    database = Database()
    imported = bootstrap_local_conversations(database)
    print(f"Local configuration saved to {path}")
    if imported:
        print(f"Loaded {imported} local conversation-derived candidate record(s); details remain on this computer.")
    print("No credential, email body, SQLite file, or generated report is stored in Git.")
    return 0


def command_init(args: argparse.Namespace) -> int:
    database = Database()
    local_imported = bootstrap_local_conversations(database)
    explicit_imported = bootstrap_applications(database, args.seed) if args.seed else 0
    print(f"Local database ready. Imported {local_imported + explicit_imported} conversation record(s) ({local_imported} from the local seed).")
    return 0


def command_import(args: argparse.Namespace) -> int:
    count = bootstrap_applications(Database(), args.path)
    print(f"Imported {count} local candidate record(s). Existing company/role pairs were left unchanged.")
    return 0


def command_add(args: argparse.Namespace) -> int:
    requirements = json.loads(Path(args.requirements).read_text(encoding="utf-8")) if args.requirements else {}
    app = Application(
        id=None, company=args.company, role=args.role or "", status="unknown", stage="unconfirmed",
        next_action="Verify application details", needs_review=True, source="manual", source_confidence=1.0,
        profile_json=requirements,
    )
    record_id = Database().add_application(app)
    print(f"Added local candidate {record_id}: {args.company}")
    return 0


def command_list(args: argparse.Namespace) -> int:
    database = Database()
    bootstrap_local_conversations(database)
    if args.json:
        print(json.dumps(database.list_applications(), ensure_ascii=False, indent=2))
        return 0
    rows = database.list_applications()
    if not rows:
        print("No applications are loaded yet. Use `job-tracker import-json <local-file>` or `job-tracker add`.")
        return 0
    print(f"{'ID':<4} {'COMPANY':<26} {'ROLE':<28} {'STATUS':<12} {'STAGE':<18} {'FIT':<8} REVIEW")
    for row in rows:
        fit = f"{row['affinity_score']}%" if row["affinity_score"] is not None else "—"
        print(f"{row['id']:<4} {row['company'][:25]:<26} {row['role'][:27]:<28} {row['status']:<12} {row['stage']:<18} {fit:<8} {'yes' if row['needs_review'] else 'no'}")
    return 0


def command_sync(args: argparse.Namespace) -> int:
    settings = load_settings()
    database = Database()
    bootstrap_local_conversations(database)
    sync_succeeded = True
    try:
        result = sync_mail(database, settings, CredentialStore())
        print("Email sync:", json.dumps(result, ensure_ascii=False))
        update_affinities(database, settings)
    except Exception as exc:
        sync_succeeded = False
        print(f"Email sync was not completed: {exc}", file=sys.stderr)
        if not (args.reports or args.publish or args.email):
            return 1
    if args.reports or args.publish or args.email:
        files = generate_reports(database)
        print("Reports written locally:", json.dumps({key: str(value) for key, value in files.items()}, ensure_ascii=False))
        if args.publish:
            try:
                results = publish_files(settings.cloud, CredentialStore(), files)
                print("Published:", json.dumps(results, ensure_ascii=False))
            except Exception as exc:
                print(f"Cloud publication was not completed: {exc}", file=sys.stderr)
                return 1
        if args.email:
            if not sync_succeeded:
                print("Report email was not sent because synchronization failed.", file=sys.stderr)
                return 1
            try:
                result = GmailReportSender(settings.mailbox, CredentialStore(), settings.cloud.account_email).send(
                    files, "manual", datetime.now().astimezone()
                )
                print("Report email:", json.dumps(result, ensure_ascii=False))
            except Exception as exc:
                print(f"Report email was not sent: {exc}", file=sys.stderr)
                return 1
    return 0


def command_report(args: argparse.Namespace) -> int:
    settings = load_settings()
    database = Database()
    bootstrap_local_conversations(database)
    files = generate_reports(database)
    print(json.dumps({key: str(value) for key, value in files.items()}, ensure_ascii=False, indent=2))
    if args.publish:
        results = publish_files(settings.cloud, CredentialStore(), files)
        print("Published:", json.dumps(results, ensure_ascii=False, indent=2))
    if args.email:
        result = GmailReportSender(settings.mailbox, CredentialStore(), settings.cloud.account_email).send(
            files, "manual", datetime.now().astimezone()
        )
        print("Report email:", json.dumps(result, ensure_ascii=False))
    return 0


def command_profile(args: argparse.Namespace) -> int:
    settings = load_settings()
    profile = json.loads(Path(args.path).read_text(encoding="utf-8"))
    if not isinstance(profile, dict):
        raise ValueError("Profile JSON must be an object with skills, years_experience, and location fields.")
    settings.profile = profile
    path = save_settings(settings)
    updated = update_affinities(Database(), settings)
    print(f"Local profile saved to {path}; recalculated {updated} affinity estimate(s).")
    return 0


def command_review(args: argparse.Namespace) -> int:
    database = Database()
    if args.resolve_id is not None:
        database.resolve_review(args.resolve_id)
        print(f"Review record {args.resolve_id} marked reviewed.")
        return 0
    if args.application_id is not None:
        database.set_application_reviewed(args.application_id, args.status, args.stage, args.affinity)
        print(f"Application {args.application_id} updated locally and marked reviewed.")
        return 0
    rows = database.list_reviews()
    print(json.dumps(rows, ensure_ascii=False, indent=2) if rows else "No pending email review records.")
    return 0


def command_schedule(args: argparse.Namespace) -> int:
    if args.remove:
        print("Removed:", ", ".join(remove_windows_tasks()))
    else:
        settings = load_settings()
        print("Installed:", ", ".join(install_windows_tasks(settings.schedule.times)))
        print("Tasks run in the current user's interactive session so Windows Credential Manager remains available.")
    return 0


def command_scheduled_run(args: argparse.Namespace) -> int:
    from zoneinfo import ZoneInfo
    home = app_home()
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log_dir / f"scheduled-{datetime.now().astimezone():%Y%m%d}.log",
                        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    slot = args.slot
    now_local = datetime.now(ZoneInfo(settings.schedule.timezone))
    today = now_local.date().isoformat()
    database = Database()
    bootstrap_local_conversations(database)
    if slot == "catchup":
        completed = {
            name for name in ("morning", "evening")
            if database.get_metadata(f"scheduled-{today}-{name}")
        }
        slots = due_slots(now_local, settings.schedule.times, completed)
        if not slots:
            logging.info("catch-up skipped; no scheduled report slot is due")
            return 0
    else:
        slots = [slot]

    status = 0
    for slot in slots:
        slot_key = f"scheduled-{today}-{slot}"
        if database.get_metadata(slot_key):
            logging.info("scheduled slot %s already completed", slot)
            continue
        slot_status = 0
        try:
            result = sync_mail(database, settings, CredentialStore())
            logging.info("sync=%s", json.dumps(result, ensure_ascii=False))
            update_affinities(database, settings)
        except Exception as exc:
            logging.exception("mail synchronization failed: %s", exc)
            slot_status = 1
        try:
            files = generate_reports(database, timezone_name=settings.schedule.timezone, run_label=slot)
            results = publish_files(settings.cloud, CredentialStore(), files)
            logging.info("published=%s", json.dumps(results, ensure_ascii=False))
            if slot_status == 0 and settings.email_reports_enabled:
                result = GmailReportSender(settings.mailbox, CredentialStore(), settings.cloud.account_email).send(
                    files, slot, now_local
                )
                logging.info("report_email=%s", json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            logging.exception("report generation, cloud publication, or email delivery failed: %s", exc)
            slot_status = 1
        if slot_status == 0:
            database.set_metadata(slot_key, now_local.isoformat(timespec="seconds"))
        status = max(status, slot_status)
    return status


def command_serve(args: argparse.Namespace) -> int:
    database = Database()
    bootstrap_local_conversations(database)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path not in {"/", "/index.html"}:
                self.send_error(404)
                return
            current = render_dashboard(Database().dashboard_data())
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(current)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(current)

        def log_message(self, format: str, *args: Any) -> None:
            return

    address = ("127.0.0.1", args.port)
    server = HTTPServer(address, Handler)
    url = f"http://{address[0]}:{server.server_port}/"
    print(f"Dashboard available locally at {url}. Press Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-tracker", description="Local-first tracking for job applications.")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="Configure email, cloud, and local credentials.")
    setup.set_defaults(handler=command_setup)
    init = sub.add_parser("init", help="Create the local database and optionally import a local seed JSON.")
    init.add_argument("--seed", help="Path to a local-only JSON file; never store real data in Git.")
    init.set_defaults(handler=command_init)
    add = sub.add_parser("add", help="Add one candidate to the local database.")
    add.add_argument("--company", required=True)
    add.add_argument("--role", default="")
    add.add_argument("--requirements", help="Local JSON object containing required_skills, preferred_skills, etc.")
    add.set_defaults(handler=command_add)
    imp = sub.add_parser("import-json", help="Import conversation-derived candidate records from a local JSON file.")
    imp.add_argument("path")
    imp.set_defaults(handler=command_import)
    ls = sub.add_parser("list", help="List local candidates.")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(handler=command_list)
    sync = sub.add_parser("sync", help="Read new messages without changing the mailbox.")
    sync.add_argument("--reports", action="store_true", help="Generate PDF, Excel, and HTML dashboard files.")
    sync.add_argument("--publish", action="store_true", help="Publish reports and dashboard to the configured cloud account.")
    sync.add_argument("--email", action="store_true", help="Email the PDF and Excel report to the authenticated Gmail account.")
    sync.set_defaults(handler=command_sync)
    report = sub.add_parser("report", help="Generate local PDF, Excel, and dashboard snapshot files.")
    report.add_argument("--publish", action="store_true")
    report.add_argument("--email", action="store_true", help="Email the PDF and Excel report to the authenticated Gmail account.")
    report.set_defaults(handler=command_report)
    profile = sub.add_parser("profile", help="Import a local profile JSON and calculate affinity estimates.")
    profile.add_argument("path")
    profile.set_defaults(handler=command_profile)
    review = sub.add_parser("review", help="Review uncertain messages or manually confirm one application.")
    review.add_argument("--resolve-id", type=int)
    review.add_argument("--application-id", type=int)
    review.add_argument("--status", choices=["active", "discarded", "offer", "hired", "unknown"], default="unknown")
    review.add_argument("--stage", choices=["unconfirmed", "application_received", "screening", "assessment", "interview", "reference", "offer", "hired"], default="unconfirmed")
    review.add_argument("--affinity", type=float)
    review.set_defaults(handler=command_review)
    schedule = sub.add_parser("schedule", help="Install or remove local Windows Task Scheduler jobs.")
    schedule.add_argument("--remove", action="store_true")
    schedule.set_defaults(handler=command_schedule)
    scheduled = sub.add_parser("scheduled-run", help="Internal runner used by scheduled tasks.")
    scheduled.add_argument("--slot", choices=["morning", "evening", "catchup"], default="catchup")
    scheduled.set_defaults(handler=command_scheduled_run)
    serve = sub.add_parser("serve", help="Open the responsive local dashboard.")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true")
    serve.set_defaults(handler=command_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
