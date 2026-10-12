from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import CloudSettings
from .oauth import GoogleOAuthClient, MicrosoftOAuthClient
from .security import CredentialStore


class CloudPublishError(RuntimeError):
    pass


def _api_request(url: str, token: str, method: str = "GET", data: bytes | None = None,
                 content_type: str = "application/json") -> tuple[dict[str, Any], bytes]:
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read()
            try:
                return json.loads(raw.decode("utf-8")) if raw else {}, raw
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}, raw
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1200]
        raise CloudPublishError(f"Cloud upload failed ({exc.code}): {detail}") from exc
    except OSError as exc:
        raise CloudPublishError(f"Cloud upload failed: {exc}") from exc


class GoogleDrivePublisher:
    API = "https://www.googleapis.com/drive/v3"
    UPLOAD = "https://www.googleapis.com/upload/drive/v3"

    def __init__(self, settings: CloudSettings, store: CredentialStore):
        if not settings.google_client_file:
            raise CloudPublishError("Set the Google desktop OAuth client file in `job-tracker setup`.")
        if not settings.account_email:
            raise CloudPublishError("Set the Google cloud account email in `job-tracker setup`.")
        self.settings = settings
        self.oauth = GoogleOAuthClient(settings.google_client_file, store, settings.account_email,
                                       GoogleOAuthClient.DRIVE_SCOPES, "drive")
        self._token = ""
        self._folder_id = ""

    @property
    def token(self) -> str:
        self._token = self.oauth.token()
        return self._token

    def _ensure_folder(self) -> str:
        if self._folder_id:
            return self._folder_id
        query = f"name='{self.settings.folder_name.replace(chr(39), chr(92)+chr(39))}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        url = self.API + "/files?" + urllib.parse.urlencode({"q": query, "fields": "files(id,name)", "pageSize": "100"})
        result, _ = _api_request(url, self.token)
        folders = result.get("files", [])
        if folders:
            self._folder_id = folders[0]["id"]
            return self._folder_id
        metadata = json.dumps({
            "name": self.settings.folder_name,
            "mimeType": "application/vnd.google-apps.folder",
        }).encode()
        created, _ = _api_request(f"{self.API}/files?fields=id,name", self.token, "POST", metadata)
        if not created.get("id"):
            raise CloudPublishError("Google Drive did not return a folder ID.")
        self._folder_id = created["id"]
        return self._folder_id

    def _existing_file(self, name: str) -> dict[str, Any] | None:
        escaped = name.replace("'", "\\'")
        query = f"name='{escaped}' and '{self._ensure_folder()}' in parents and trashed=false"
        url = self.API + "/files?" + urllib.parse.urlencode({"q": query, "fields": "files(id,name,webViewLink)", "pageSize": "100"})
        result, _ = _api_request(url, self.token)
        return next(iter(result.get("files", [])), None)

    @staticmethod
    def _multipart(metadata: dict[str, Any], media: bytes, media_type: str) -> tuple[bytes, str]:
        boundary = "job-tracker-" + __import__("secrets").token_hex(12)
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
            + json.dumps(metadata, ensure_ascii=False).encode("utf-8")
            + f"\r\n--{boundary}\r\nContent-Type: {media_type}\r\n\r\n".encode()
            + media
            + f"\r\n--{boundary}--".encode()
        )
        return body, f"multipart/related; boundary={boundary}"

    def upload(self, path: Path, cloud_name: str | None = None, stable: bool = False) -> dict[str, str]:
        name = cloud_name or path.name
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        content = path.read_bytes()
        folder = self._ensure_folder()
        existing = self._existing_file(name) if stable else None
        if existing:
            url = f"{self.UPLOAD}/files/{urllib.parse.quote(existing['id'])}?uploadType=media&fields=id,name,webViewLink"
            result, _ = _api_request(url, self.token, "PATCH", content, media_type)
        else:
            metadata = {"name": name, "parents": [folder]}
            body, multipart_type = self._multipart(metadata, content, media_type)
            url = f"{self.UPLOAD}/files?uploadType=multipart&fields=id,name,webViewLink"
            result, _ = _api_request(url, self.token, "POST", body, multipart_type)
        if not result.get("id") and existing:
            result = existing
        if not result.get("id"):
            raise CloudPublishError(f"Google Drive did not confirm upload of {name}.")
        link = result.get("webViewLink", "")
        if not link:
            metadata, _ = _api_request(f"{self.API}/files/{urllib.parse.quote(result['id'])}?fields=id,name,webViewLink", self.token)
            link = metadata.get("webViewLink", "")
        return {"name": name, "id": result["id"], "url": link}


class OneDrivePublisher:
    GRAPH = "https://graph.microsoft.com/v1.0"

    def __init__(self, settings: CloudSettings, store: CredentialStore):
        if not settings.microsoft_client_id:
            raise CloudPublishError("Set a Microsoft OAuth client ID in `job-tracker setup`.")
        if not settings.account_email:
            raise CloudPublishError("Set the Microsoft cloud account email in `job-tracker setup`.")
        self.settings = settings
        self.oauth = MicrosoftOAuthClient(settings.microsoft_client_id, store, settings.account_email)
        self._token = ""

    @property
    def token(self) -> str:
        self._token = self.oauth.token()
        return self._token

    def _ensure_folder(self) -> None:
        encoded = urllib.parse.quote(self.settings.folder_name, safe="")
        url = f"{self.GRAPH}/me/drive/root:/{encoded}"
        try:
            _api_request(url, self.token)
            return
        except CloudPublishError as exc:
            if "404" not in str(exc):
                raise
        body = json.dumps({
            "name": self.settings.folder_name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        }).encode()
        _api_request(f"{self.GRAPH}/me/drive/root/children", self.token, "POST", body)

    def upload(self, path: Path, cloud_name: str | None = None, stable: bool = False) -> dict[str, str]:
        del stable  # Graph's PUT to a path replaces that file when it already exists.
        self._ensure_folder()
        name = cloud_name or path.name
        cloud_path = f"{self.settings.folder_name}/{name}"
        encoded = urllib.parse.quote(cloud_path, safe="/")
        url = f"{self.GRAPH}/me/drive/root:/{encoded}:/content"
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        result, _ = _api_request(url, self.token, "PUT", path.read_bytes(), content_type)
        return {"name": name, "id": result.get("id", ""), "url": result.get("webUrl", "")}


def publisher_for(settings: CloudSettings, store: CredentialStore):
    provider = settings.provider.casefold()
    if provider == "google":
        return GoogleDrivePublisher(settings, store)
    if provider == "microsoft":
        return OneDrivePublisher(settings, store)
    raise CloudPublishError("Cloud provider is not configured. Choose Google Drive or OneDrive in `job-tracker setup`.")


def publish_files(settings: CloudSettings, store: CredentialStore, files: dict[str, Path]) -> list[dict[str, str]]:
    publisher = publisher_for(settings, store)
    results: list[dict[str, str]] = []
    for key in ("dashboard_xlsx", "html", "pdf", "xlsx"):
        path = files.get(key)
        if path:
            stable = key in {"dashboard_xlsx", "html"}
            name = "Dashboard Aplicación a Empleos.xlsx" if key == "dashboard_xlsx" else "Dashboard Aplicación a Empleos.html" if key == "html" else path.name
            results.append(publisher.upload(path, cloud_name=name if stable else path.name, stable=stable))
    return results
