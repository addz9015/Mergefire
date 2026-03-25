from __future__ import annotations

import logging
from typing import Sequence

import httpx

LOGGER = logging.getLogger(__name__)


class DiscordClient:
    def __init__(self, webhook_url: str | None, timeout_seconds: float = 10.0) -> None:
        self._webhook_url = webhook_url
        self._timeout_seconds = timeout_seconds

    @property
    def enabled(self) -> bool:
        return bool(self._webhook_url)

    async def send_high_risk_alert(
        self,
        repo_full_name: str,
        pr_number: int,
        pr_url: str,
        score: int,
        reasons: Sequence[str],
    ) -> bool:
        if not self._webhook_url:
            LOGGER.info("DISCORD_WEBHOOK_URL not configured. Skipping Discord alert.")
            return False

        reason_lines = "\n".join(f"- {reason}" for reason in reasons) or "- No reasons available"
        content = (
            "**HIGH RISK PR DETECTED**\n"
            f"Repository: `{repo_full_name}`\n"
            f"PR: {pr_url} (#{pr_number})\n"
            f"Risk score: **{score}**\n"
            "Why:\n"
            f"{reason_lines}"
        )

        payload = {"content": content}
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(self._webhook_url, json=payload)
            if response.status_code < 400:
                return True

            LOGGER.warning(
                "Discord alert failed (%s): %s",
                response.status_code,
                response.text,
            )
            return False

    async def send_review_findings_alert(
        self,
        repo_full_name: str,
        pr_number: int,
        pr_url: str,
        highest_severity: str,
        posted_comments: int,
        review_event: str,
    ) -> bool:
        if not self._webhook_url:
            LOGGER.info("DISCORD_WEBHOOK_URL not configured. Skipping Discord review alert.")
            return False

        content = (
            "**PR REVIEW FINDINGS DETECTED**\n"
            f"Repository: `{repo_full_name}`\n"
            f"PR: {pr_url} (#{pr_number})\n"
            f"Highest severity: **{highest_severity}**\n"
            f"Review event: **{review_event}**\n"
            f"Posted comments: **{posted_comments}**"
        )

        payload = {"content": content}
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(self._webhook_url, json=payload)
            if response.status_code < 400:
                return True

            LOGGER.warning(
                "Discord review alert failed (%s): %s",
                response.status_code,
                response.text,
            )
            return False
