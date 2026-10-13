from __future__ import annotations

import subprocess
import sys
import os
from datetime import datetime
from pathlib import Path
import re

from .config import app_home


def validate_schedule_times(times: list[str]) -> None:
    if len(times) != 2:
        raise ValueError("Configure exactly two daily report times.")
    for value in times:
        if not re.fullmatch(r"\d{2}:\d{2}", value):
            raise ValueError("Schedule times must use HH:MM.")
        hour, minute = (int(part) for part in value.split(":"))
        if hour > 23 or minute > 59:
            raise ValueError("Schedule times must be valid 24-hour times.")


def due_slots(now: datetime, times: list[str], completed: set[str]) -> list[str]:
    """Return report slots that have passed today and have not completed."""
    validate_schedule_times(times)
    slots = []
    for slot, value in zip(("morning", "evening"), times):
        hour, minute = (int(part) for part in value.split(":"))
        if (now.hour, now.minute) >= (hour, minute) and slot not in completed:
            slots.append(slot)
    return slots


def install_windows_tasks(times: list[str] | None = None) -> list[str]:
    if sys.platform != "win32":
        raise RuntimeError("Windows Task Scheduler is available only on Windows.")
    times = times or ["08:00", "18:00"]
    validate_schedule_times(times)
    logs = app_home() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    names = []
    for slot, time_value in zip(("morning", "evening"), times):
        name = f"JobApplicationTracker-{slot}"
        command = f'"{sys.executable}" -m job_tracker.cli scheduled-run --slot {slot}'
        subprocess.run(["schtasks.exe", "/Create", "/TN", name, "/SC", "DAILY", "/ST", time_value,
                        "/TR", command, "/F", "/IT"], check=True, capture_output=True, text=True)
        names.append(name)
    catchup_name = "JobApplicationTracker-on-login"
    # schtasks.exe rejects an ONLOGON trigger for a standard interactive user on
    # some Windows configurations. Register that trigger through the native
    # ScheduledTasks cmdlets, passing user paths as environment data rather than
    # interpolating them into PowerShell source.
    environment = os.environ.copy()
    environment["JOB_TRACKER_TASK_PYTHON"] = sys.executable
    environment["JOB_TRACKER_TASK_WORKDIR"] = str(Path(__file__).resolve().parents[1])
    register_catchup = r"""
$ErrorActionPreference = 'Stop'
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $env:JOB_TRACKER_TASK_PYTHON -Argument '-m job_tracker.cli scheduled-run --slot catchup' -WorkingDirectory $env:JOB_TRACKER_TASK_WORKDIR
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
Register-ScheduledTask -TaskName 'JobApplicationTracker-on-login' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Recover pending Job Application Tracker report slots' -Force | Out-Null
"JobApplicationTracker-on-login"
"""
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", register_catchup],
                   check=True, capture_output=True, text=True, env=environment)
    names.append(catchup_name)
    return names


def remove_windows_tasks() -> list[str]:
    if sys.platform != "win32":
        raise RuntimeError("Windows Task Scheduler is available only on Windows.")
    names = ["JobApplicationTracker-morning", "JobApplicationTracker-evening", "JobApplicationTracker-on-login"]
    for name in names:
        subprocess.run(["schtasks.exe", "/Delete", "/TN", name, "/F"], check=False, capture_output=True, text=True)
    return names
