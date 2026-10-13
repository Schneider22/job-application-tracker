from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from .security import CredentialStore, load_json_secret, save_json_secret


class OAuthError(RuntimeError):
    pass


def _request_json(url: str, data: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
                  method: str | None = None) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1000]
        raise OAuthError(f"OAuth request failed ({exc.code}): {detail}") from exc
    except OSError as exc:
        raise OAuthError(f"OAuth request failed: {exc}") from exc
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise OAuthError("OAuth service returned an invalid response.") from exc


class GoogleOAuthClient:
    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    GMAIL_SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)
    GMAIL_SEND_SCOPES = (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    )
    DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive.file",)

    def __init__(self, credential_file: str | Path, store: CredentialStore, account: str,
                 scopes: tuple[str, ...] | None = None, purpose: str = "gmail"):
        self.credential_file = Path(credential_file).expanduser()
        self.store = store
        self.account = account.casefold()
        self.scopes = scopes or self.GMAIL_SCOPES
        self.key = f"oauth/google/{purpose}/{self.account}"
        if not self.credential_file.is_file():
            raise OAuthError(f"Google OAuth client file was not found: {self.credential_file}")
        raw = json.loads(self.credential_file.read_text(encoding="utf-8"))
        self.client = raw.get("installed") or raw.get("web")
        if not self.client or not self.client.get("client_id"):
            raise OAuthError("Google OAuth client JSON must contain an installed or web client.")

    def authorize(self) -> dict[str, Any]:
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        handler_state: dict[str, str] = {}

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                handler_state["state"] = query.get("state", [""])[0]
                handler_state["code"] = query.get("code", [""])[0]
                handler_state["error"] = query.get("error", [""])[0]
                body = b"Authorization received. You can close this window and return to Job Application Tracker."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                threading.Thread(target=self.server.shutdown, daemon=True).start()

            def log_message(self, format: str, *args: Any) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
        server.timeout = 240
        redirect_uri = f"http://127.0.0.1:{server.server_port}/"
        auth_params = {
            "client_id": self.client["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        url = self.AUTH_URL + "?" + urllib.parse.urlencode(auth_params)
        print("Opening Google sign-in in your browser. The callback is limited to this computer.")
        if not webbrowser.open(url):
            print(url)
        server.serve_forever(poll_interval=0.2)
        server.server_close()
        if handler_state.get("state") != state:
            raise OAuthError("Google OAuth state check failed.")
        if handler_state.get("error"):
            raise OAuthError(f"Google authorization was declined: {handler_state['error']}")
        if not handler_state.get("code"):
            raise OAuthError("Google authorization timed out before a code was received.")
        token = _request_json(self.TOKEN_URL, {
            "code": handler_state["code"], "client_id": self.client["client_id"],
            "client_secret": self.client.get("client_secret", ""), "redirect_uri": redirect_uri,
            "grant_type": "authorization_code", "code_verifier": verifier,
        })
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
        save_json_secret(self.store, self.key, token)
        return token

    def token(self) -> str:
        token = load_json_secret(self.store, self.key)
        if not token:
            token = self.authorize()
        if float(token.get("expires_at", 0)) <= time.time() + 60:
            refresh = token.get("refresh_token")
            if not refresh:
                token = self.authorize()
            else:
                updated = _request_json(self.TOKEN_URL, {
                    "client_id": self.client["client_id"],
                    "client_secret": self.client.get("client_secret", ""),
                    "refresh_token": refresh, "grant_type": "refresh_token",
                })
                updated["refresh_token"] = refresh
                updated["expires_at"] = time.time() + int(updated.get("expires_in", 3600))
                token.update(updated)
                save_json_secret(self.store, self.key, token)
        return token["access_token"]


class MicrosoftOAuthClient:
    TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    DEVICE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
    SCOPES = "offline_access User.Read Files.ReadWrite"

    def __init__(self, client_id: str, store: CredentialStore, account: str):
        self.client_id = client_id.strip()
        self.store = store
        self.account = account.casefold()
        self.key = f"oauth/microsoft/{self.account}"
        if not self.client_id:
            raise OAuthError("Microsoft OAuth client ID is not configured.")

    def authorize(self) -> dict[str, Any]:
        result = _request_json(self.DEVICE_URL, {"client_id": self.client_id, "scope": self.SCOPES})
        print(result.get("message", "Open the verification URL and enter the displayed code."))
        if result.get("verification_uri_complete"):
            webbrowser.open(result["verification_uri_complete"])
        interval = int(result.get("interval", 5))
        deadline = time.time() + int(result.get("expires_in", 900))
        while time.time() < deadline:
            time.sleep(interval)
            try:
                token = _request_json(self.TOKEN_URL, {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self.client_id, "device_code": result["device_code"],
                })
            except OAuthError as exc:
                detail = str(exc).casefold()
                if "authorization_pending" in detail:
                    continue
                if "slow_down" in detail:
                    interval += 5
                    continue
                raise
            token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
            save_json_secret(self.store, self.key, token)
            return token
        raise OAuthError("Microsoft sign-in expired before it was completed.")

    def token(self) -> str:
        token = load_json_secret(self.store, self.key)
        if not token:
            token = self.authorize()
        if float(token.get("expires_at", 0)) <= time.time() + 60:
            refresh = token.get("refresh_token")
            if not refresh:
                token = self.authorize()
            else:
                updated = _request_json(self.TOKEN_URL, {
                    "client_id": self.client_id, "refresh_token": refresh,
                    "grant_type": "refresh_token", "scope": self.SCOPES,
                })
                updated["refresh_token"] = updated.get("refresh_token", refresh)
                updated["expires_at"] = time.time() + int(updated.get("expires_in", 3600))
                token.update(updated)
                save_json_secret(self.store, self.key, token)
        return token["access_token"]
