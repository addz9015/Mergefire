from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    github_token: str
    github_webhook_secret: str
    groq_api_key: str
    slack_webhook_url: str | None
    discord_webhook_url: str | None
    risk_high_threshold: int
    risk_medium_threshold: int
    port: int
    groq_model: str
    request_timeout_seconds: float

    @property
    def risk_low_threshold(self) -> int:
        return 0


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings(
        github_token=_required("GITHUB_TOKEN"),
        github_webhook_secret=_required("GITHUB_WEBHOOK_SECRET"),
        groq_api_key=_required("GROQ_API_KEY"),
        slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", "").strip() or None,
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", "").strip() or None,
        risk_high_threshold=int(os.getenv("RISK_HIGH_THRESHOLD", "67")),
        risk_medium_threshold=int(os.getenv("RISK_MEDIUM_THRESHOLD", "34")),
        port=int(os.getenv("PORT", "8000")),
        groq_model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
        or "llama-3.3-70b-versatile",
        request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "25")),
    )
    if settings.risk_medium_threshold >= settings.risk_high_threshold:
        raise RuntimeError("RISK_MEDIUM_THRESHOLD must be less than RISK_HIGH_THRESHOLD")
    return settings
