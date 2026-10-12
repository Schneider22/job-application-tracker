from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PARTS = {".local", "data", "reports", ".venv", "venv", "vendor"}
FORBIDDEN_NAMES = {".env", "credentials.json", "token.json", "config.json", "profile.json"}
FORBIDDEN_NAME_PARTS = ("credential", "token", "config", "profile", "client_secret", "oauth")
FORBIDDEN_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".pem", ".p12"}
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]


def main() -> int:
    try:
        result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"Git tracked-file check could not run: {exc}", file=sys.stderr)
        return 2
    tracked = [Path(part.decode("utf-8", "replace")) for part in result.stdout.split(b"\0") if part]
    violations: list[str] = []
    for relative in tracked:
        if any(part.casefold() in FORBIDDEN_PARTS for part in relative.parts):
            violations.append(f"forbidden data path: {relative.as_posix()}")
        folded_name = relative.name.casefold()
        sensitive_json = relative.suffix.casefold() == ".json" and any(
            part in folded_name for part in FORBIDDEN_NAME_PARTS
        )
        if (folded_name in FORBIDDEN_NAMES or relative.suffix.casefold() in FORBIDDEN_SUFFIXES
                or sensitive_json):
            violations.append(f"forbidden local file: {relative.as_posix()}")
        path = ROOT / relative
        try:
            content = path.read_bytes()
        except OSError:
            continue
        text = content.decode("utf-8", "ignore")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                violations.append(f"possible secret in {relative.as_posix()} ({pattern.pattern})")
    if violations:
        print("Security gate rejected the tracked tree:", file=sys.stderr)
        for violation in violations:
            print(f" - {violation}", file=sys.stderr)
        return 1
    print(f"Security gate passed for {len(tracked)} tracked file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
