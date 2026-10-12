from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from .database import Database


def render_dashboard(data: dict[str, Any]) -> bytes:
    template = files("job_tracker").joinpath("dashboard.html").read_text(encoding="utf-8")
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    serialized = serialized.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return template.replace("__TRACKER_DATA__", serialized).encode("utf-8")


def snapshot(database: Database) -> bytes:
    return render_dashboard(database.dashboard_data())
