from __future__ import annotations

import re
from html.parser import HTMLParser

from .models import Classification, EmailMessage


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def normalize_text(value: str) -> str:
    extractor = _TextExtractor()
    try:
        extractor.feed(value)
        value = " ".join(extractor.parts)
    except Exception:
        pass
    value = re.sub(r"https?://\S+", " ", value, flags=re.I)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.I) for pattern in patterns)


REJECTION = (
    r"no (?:continuaremos|continuaremos con|avanzaremos|has sido seleccionado)",
    r"(?:decidimos|hemos decidido) (?:no )?seguir con otros candidatos",
    r"no fue seleccionado|no has sido seleccionado|no avanzará(?:s)?",
    r"we (?:will not|won't) be moving forward|not selected|unfortunately.*application",
    r"application.*(?:rejected|unsuccessful)",
)
OFFER = (r"oferta (?:laboral|de empleo)|propuesta salarial|employment offer|job offer|offer letter",)
INTERVIEW = (r"entrevista|agend(?:ar|ada|ada la entrevista)|invitation to interview|interview invitation|schedule.*interview",)
ASSESSMENT = (r"prueba técnica|evaluación técnica|assessment|coding test|technical test|case study|enlace.*prueba",)
SCREENING = (r"llamada inicial|entrevista inicial|screening call|phone screen|recruiter call",)
RECEIVED = (r"hemos recibido tu (?:solicitud|postulación|candidatura)|confirmación de postulación|application received|thank you for applying",)
PROGRESS = (r"siguiente etapa|avanzaste|continuar(?:ás)? en el proceso|shortlist|shortlisted|next steps|moving forward",)
JOB_SIGNAL = (r"candidatura|postulación|vacante|proceso de selección|entrevista|prueba técnica|application|recruiter|talent acquisition|job opportunity",)


def classify_message(message: EmailMessage) -> Classification:
    text = normalize_text(f"{message.subject} {message.body_text}")[:12000]
    summary = text[:320]
    if _matches(text, REJECTION):
        return Classification("rejection", "rejected", "rejected", 0.96, summary)
    if _matches(text, OFFER):
        return Classification("offer", "offer", "offer", 0.94, summary)
    if _matches(text, INTERVIEW):
        return Classification("interview", "interview", "active", 0.92, summary)
    if _matches(text, ASSESSMENT):
        return Classification("assessment", "assessment", "active", 0.90, summary)
    if _matches(text, SCREENING):
        return Classification("screening", "screening", "active", 0.86, summary)
    if _matches(text, RECEIVED):
        return Classification("application_received", "application_received", "active", 0.84, summary)
    if _matches(text, PROGRESS):
        return Classification("progress", "screening", "active", 0.75, summary)
    if _matches(text, JOB_SIGNAL):
        return Classification("possible_job_update", "unconfirmed", "unknown", 0.35, summary, True)
    return Classification("unclassified", "unconfirmed", "unknown", 0.0, summary, True)


def match_application(message: EmailMessage, applications: list[dict]) -> dict | None:
    text = normalize_text(f"{message.subject} {message.body_text} {message.sender}").casefold()
    matches = [a for a in applications if a["company"].strip() and a["company"].casefold() in text]
    if not matches:
        return None
    return max(matches, key=lambda row: len(row["company"]))


def is_job_related(message: EmailMessage) -> bool:
    text = normalize_text(f"{message.subject} {message.body_text}")[:12000]
    return _matches(text, JOB_SIGNAL)
