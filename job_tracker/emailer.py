from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from .config import MailboxSettings
from .oauth import GoogleOAuthClient
from .security import CredentialStore


class EmailDeliveryError(RuntimeError):
    pass


class GmailReportSender:
    API = "https://gmail.googleapis.com/gmail/v1/users/me"
    MAX_ATTACHMENT_BYTES = 18 * 1024 * 1024

    def __init__(self, settings: MailboxSettings, store: CredentialStore, expected_account: str = ""):
        if settings.provider != "gmail":
            raise EmailDeliveryError("Automatic report email currently requires a Gmail mailbox.")
        if not settings.gmail_client_file:
            raise EmailDeliveryError("Configure the local Google OAuth desktop client before sending reports.")
        mailbox_address = settings.email.strip().casefold()
        cloud_address = expected_account.strip().casefold()
        if mailbox_address and cloud_address and mailbox_address != cloud_address:
            raise EmailDeliveryError("The configured mailbox and cloud report account do not match.")
        self.expected_address = mailbox_address or cloud_address
        self.oauth = GoogleOAuthClient(
            settings.gmail_client_file, store, settings.email,
            GoogleOAuthClient.GMAIL_SEND_SCOPES, "gmail-send",
        )

    def _request_json(self, url: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url, data=body, method=method,
            headers={"Authorization": f"Bearer {self.oauth.token()}",
                     **({"Content-Type": "application/json"} if body is not None else {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # Do not surface Google response bodies: they can contain account or message metadata.
            raise EmailDeliveryError(f"Gmail report request failed (HTTP {exc.code}).") from None
        except OSError:
            raise EmailDeliveryError("Gmail report request could not reach Google.") from None
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EmailDeliveryError("Gmail returned an invalid report response.") from None

    def _self_address(self) -> str:
        profile = self._request_json(f"{self.API}/profile")
        address = parseaddr(str(profile.get("emailAddress", "")))[1].strip().casefold()
        if not address or "@" not in address:
            raise EmailDeliveryError("Gmail did not return the authenticated account address.")
        if self.expected_address and address != self.expected_address:
            raise EmailDeliveryError("The authenticated Gmail account does not match the configured mailbox.")
        return address

    @staticmethod
    def _attachments(files: dict[str, Path]) -> list[Path]:
        selected = [files[key] for key in ("pdf", "xlsx") if key in files]
        if len(selected) != 2 or any(not path.is_file() for path in selected):
            raise EmailDeliveryError("A PDF and Excel report are required before sending.")
        total = sum(path.stat().st_size for path in selected)
        if total > GmailReportSender.MAX_ATTACHMENT_BYTES:
            raise EmailDeliveryError("The PDF and Excel reports exceed the safe email attachment limit.")
        return selected

    def send(self, files: dict[str, Path], run_label: str, generated_at: datetime) -> dict[str, int | str]:
        attachments = self._attachments(files)
        recipient = self._self_address()
        slot = {"morning": "08:00", "evening": "18:00"}.get(run_label, "manual")
        message = EmailMessage(policy=policy.SMTP)
        message["To"] = recipient
        message["From"] = recipient
        message["Subject"] = f"Reporte de candidaturas · {slot} · {generated_at:%Y-%m-%d}"
        message.set_content(
            "Adjuntamos el corte de candidaturas en PDF y Excel.\n"
            "El dashboard permanece en Google Drive y el correo completo sigue en la cuenta de origen."
        )
        for path in attachments:
            content_type = "application/pdf" if path.suffix.casefold() == ".pdf" else (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                if path.suffix.casefold() == ".xlsx" else ""
            )
            if not content_type:
                raise EmailDeliveryError("Only PDF and Excel reports can be attached.")
            maintype, subtype = content_type.split("/", 1)
            message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
        self._request_json(f"{self.API}/messages/send", "POST", {"raw": raw})
        return {"status": "sent", "attachment_count": len(attachments)}
