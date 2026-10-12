from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes


class CredentialStoreError(RuntimeError):
    pass


class CredentialStore:
    """Windows Credential Manager adapter; secrets are never written as files."""

    def _require_windows(self) -> None:
        if os.name != "nt":
            raise CredentialStoreError("The secure credential store requires Windows Credential Manager.")

    @staticmethod
    def _target(key: str) -> str:
        return f"JobApplicationTracker/{key}"

    def put(self, key: str, secret: str) -> None:
        self._require_windows()
        advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        cred_write = advapi.CredWriteW
        cred_write.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        cred_write.restype = wintypes.BOOL

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.c_void_p),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        encoded = secret.encode("utf-16-le")
        blob = ctypes.create_string_buffer(encoded)
        credential = CREDENTIALW()
        credential.Flags = 0
        credential.Type = 1  # CRED_TYPE_GENERIC
        credential.TargetName = self._target(key)
        credential.Comment = "Job Application Tracker local secret"
        credential.CredentialBlobSize = len(encoded)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.c_void_p)
        credential.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
        credential.AttributeCount = 0
        credential.Attributes = None
        credential.TargetAlias = None
        credential.UserName = "Job Application Tracker"
        if not cred_write(ctypes.byref(credential), 0):
            raise CredentialStoreError(f"Windows Credential Manager write failed: {ctypes.get_last_error()}")

    def get(self, key: str) -> str | None:
        self._require_windows()
        advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        cred_read = advapi.CredReadW
        cred_read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        cred_read.restype = wintypes.BOOL
        cred_free = advapi.CredFree
        cred_free.argtypes = [ctypes.c_void_p]

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.c_void_p),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        pointer = ctypes.c_void_p()
        if not cred_read(self._target(key), 1, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == 1168:  # ERROR_NOT_FOUND
                return None
            raise CredentialStoreError(f"Windows Credential Manager read failed: {error}")
        try:
            credential = ctypes.cast(pointer, ctypes.POINTER(CREDENTIALW)).contents
            raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return raw.decode("utf-16-le")
        finally:
            cred_free(pointer)

    def delete(self, key: str) -> None:
        self._require_windows()
        advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        delete = advapi.CredDeleteW
        delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        delete.restype = wintypes.BOOL
        if not delete(self._target(key), 1, 0):
            error = ctypes.get_last_error()
            if error != 1168:
                raise CredentialStoreError(f"Windows Credential Manager delete failed: {error}")


def save_json_secret(store: CredentialStore, key: str, value: dict) -> None:
    store.put(key, json.dumps(value, separators=(",", ":")))


def load_json_secret(store: CredentialStore, key: str) -> dict | None:
    value = store.get(key)
    return json.loads(value) if value else None
