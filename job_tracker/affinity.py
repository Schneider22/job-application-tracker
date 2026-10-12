from __future__ import annotations

from typing import Any


def calculate_affinity(profile: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]:
    """Transparent 0-100 estimate; unavailable factors are excluded, never guessed."""
    profile_skills = {str(item).strip().casefold() for item in profile.get("skills", []) if str(item).strip()}
    required_skills = {str(item).strip().casefold() for item in requirements.get("required_skills", []) if str(item).strip()}
    preferred_skills = {str(item).strip().casefold() for item in requirements.get("preferred_skills", []) if str(item).strip()}
    factors: dict[str, dict[str, float]] = {}
    if required_skills:
        factors["required_skills"] = {"weight": 0.60, "score": len(profile_skills & required_skills) / len(required_skills) * 100}
    if preferred_skills:
        factors["preferred_skills"] = {"weight": 0.20, "score": len(profile_skills & preferred_skills) / len(preferred_skills) * 100}

    profile_years = profile.get("years_experience")
    required_years = requirements.get("years_experience")
    if isinstance(profile_years, (int, float)) and isinstance(required_years, (int, float)) and required_years > 0:
        factors["experience"] = {"weight": 0.10, "score": min(100.0, float(profile_years) / float(required_years) * 100)}

    profile_location = str(profile.get("location", "")).strip().casefold()
    required_location = str(requirements.get("location", "")).strip().casefold()
    if required_location and profile_location:
        remote_ok = bool(requirements.get("remote", False))
        factors["location"] = {"weight": 0.10, "score": 100.0 if remote_ok or required_location in profile_location or profile_location in required_location else 0.0}

    if not factors:
        return {"score": None, "label": "insufficient_data", "factors": {}, "method": "No comparable profile and vacancy data."}
    total_weight = sum(item["weight"] for item in factors.values())
    score = round(sum(item["score"] * item["weight"] for item in factors.values()) / total_weight, 1)
    label = "promising" if score >= 80 else "intermediate" if score >= 60 else "low"
    return {"score": score, "label": label, "factors": factors, "method": "Weighted profile-to-requirements match; missing factors are excluded."}
