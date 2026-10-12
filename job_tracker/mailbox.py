from __future__ import annotations

import base64
import email
import email.header
import imaplib
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email import policy
from email.utils import parsedate_to_datetime
from typing import Iterator

from .config import MailboxSettings
from .models import EmailMessage
from .oauth import GoogleOAuthClient
from .security import CredentialStore


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(value)))
    except (LookupError, UnicodeDecodeError, email.errors.HeaderParseError):
        return value


def _message_text(parsed: email.message.Message) -> str:
    parts: list[str] = []
    for part in parsed.walk():
        if part.get_content_maintype() != "text" or part.get_content_disposition() == "attachment":
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError, AttributeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if part.get_content_subtype() == "html":
            from .classification import normalize_text
            content = normalize_text(str(content))
        parts.append(str(content))
        if sum(map(len, parts)) > 20000:
            break
    return "\n".join(parts)[:20000]


def parse_rfc822(source: str, source_id: str, raw: bytes, source_url: str = "") -> EmailMessage:
    parsed = email.message_from_bytes(raw, policy=policy.default)
    received_raw = parsed.get("Date", "")
    try:
        received_at = parsedate_to_datetime(received_raw)
        if received_at.tzinfo is None:
            received_at = received_at.astimezone()
    except (TypeError, ValueError, OverflowError):
        received_at = datetime.now().astimezone()
    return EmailMessage(
        source=source,
        source_id=source_id,
        message_id=_decode_header(parsed.get("Message-ID", "")).strip("<> "),
        subject=_decode_header(parsed.get("Subject", "")),
        sender=_decode_header(parsed.get("From", "")),
        received_at=received_at,
        body_text=_message_text(parsed),
        source_url=source_url,
    )


class ImapMailbox:
    def __init__(self, settings: MailboxSettings, password: str):
        self.settings = settings
        self.password = password

    def fetch(self, since: date) -> Iterator[EmailMessage]:
        if not self.settings.imap_host:
            raise ValueError("IMAP host is not configured.")
        if self.settings.imap_tls:
            client = imaplib.IMAP4_SSL(self.settings.imap_host, self.settings.imap_port)
        else:
            client = imaplib.IMAP4(self.settings.imap_host, self.settings.imap_port)
            client.starttls()
        try:
            client.login(self.settings.email, self.password)
            status, _ = client.select(self.settings.imap_mailbox, readonly=True)
            if status != "OK":
                raise RuntimeError(f"Unable to open mailbox {self.settings.imap_mailbox!r} in read-only mode.")
            status, results = client.uid("search", None, "SINCE", since.strftime("%d-%b-%Y"))
            if status != "OK":
                raise RuntimeError("IMAP search failed.")
            ids = results[0].split() if results and results[0] else []
            for uid in ids:
                status, fetched = client.uid("fetch", uid, "(UID BODY.PEEK[])")
                if status != "OK":
                    continue
                raw = next((item[1] for item in fetched if isinstance(item, tuple) and isinstance(item[1], bytes)), None)
                if raw is None:
                    continue
                yield parse_rfc822(f"imap:{self.settings.email.casefold()}", uid.decode("ascii", "replace"), raw)
        finally:
            try:
                client.logout()
            except Exception:
                pass


class GmailApiMailbox:
    API = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(self, oauth: GoogleOAuthClient, query: str, account: str = ""):
        self.oauth = oauth
        self.query = query
        self.account = account.casefold()

    def _get_json(self, url: str) -> dict:
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.oauth.token()}"})
        with urllib.request.urlopen(request, timeout=45) as response:
            import json
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _header(headers: list[dict], key: str) -> str:
        return next((item.get("value", "") for item in headers if item.get("name", "").casefold() == key.casefold()), "")

    @staticmethod
    def _body(payload: dict) -> str:
        chunks: list[str] = []
        stack = [payload]
        while stack:
            item = stack.pop()
            mime = item.get("mimeType", "")
            if mime in {"text/plain", "text/html"} and item.get("body", {}).get("data"):
                data = item["body"]["data"]
                data += "=" * (-len(data) % 4)
                raw = base64.urlsafe_b64decode(data)
                text = raw.decode("utf-8", "replace")
                if mime == "text/html":
                    from .classification import normalize_text
                    text = normalize_text(text)
                chunks.append(text)
            stack.extend(item.get("parts", []))
            if sum(map(len, chunks)) >= 20000:
                break
        return "\n".join(chunks)[:20000]

    def fetch(self, since: date) -> Iterator[EmailMessage]:
        query = self.query.strip()
        floor = f"after:{since.strftime('%Y/%m/%d')}"
        query = f"{query} {floor}".strip()
        next_page = ""
        while True:
            params = urllib.parse.urlencode({"q": query, "maxResults": 100, **({"pageToken": next_page} if next_page else {})})
            result = self._get_json(f"{self.API}/messages?{params}")
            for item in result.get("messages", []):
                message_id = item["id"]
                detail = self._get_json(f"{self.API}/messages/{urllib.parse.quote(message_id)}?format=full")
                payload = detail.get("payload", {})
                headers = payload.get("headers", [])
                timestamp = int(detail.get("internalDate", "0")) / 1000
                received_at = datetime.fromtimestamp(timestamp).astimezone()
                yield EmailMessage(
                    source=f"gmail:{self.account}" if self.account else "gmail", source_id=message_id,
                    message_id=self._header(headers, "Message-ID").strip("<> "),
                    subject=self._header(headers, "Subject"), sender=self._header(headers, "From"),
                    received_at=received_at, body_text=self._body(payload),
                    source_url=f"https://mail.google.com/mail/u/0/#all/{message_id}",
                )
            next_page = result.get("nextPageToken", "")
            if not next_page:
                break


def mailbox_for(settings: MailboxSettings, store: CredentialStore):
    if settings.provider == "imap":
        password = store.get("mailbox-password")
        if not password:
            raise RuntimeError("No IMAP password is stored in Windows Credential Manager. Run `job-tracker setup`.")
        return ImapMailbox(settings, password)
    if settings.provider == "gmail":
        if not settings.gmail_client_file:
            raise RuntimeError("Google OAuth client file is not configured. Run `job-tracker setup`.")
        oauth = GoogleOAuthClient(settings.gmail_client_file, store, settings.email,
                                  GoogleOAuthClient.GMAIL_SCOPES, "gmail")
        return GmailApiMailbox(oauth, settings.gmail_query, settings.email)
    raise ValueError(f"Unsupported mailbox provider: {settings.provider}")
