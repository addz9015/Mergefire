from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, Response, status

from agents.review_copilot import ReviewCopilotAgent
from agents.risk_radar import RiskRadarAgent
from config import Settings
from integrations.discord_client import DiscordClient
from integrations.github_client import GitHubClient
from integrations.groq_client import GroqClient
from integrations.slack_client import SlackClient

LOGGER = logging.getLogger(__name__)

ALLOWED_PULL_REQUEST_ACTIONS = {"opened", "reopened", "synchronize"}


class WebhookHandler:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._github_client = GitHubClient(settings.github_token, settings.request_timeout_seconds)
        self._groq_client = GroqClient(settings.groq_api_key, settings.groq_model)
        self._slack_client = SlackClient(settings.slack_webhook_url)
        self._discord_client = DiscordClient(settings.discord_webhook_url)

        prompts_dir = Path(__file__).resolve().parent / "prompts"
        self._review_agent = ReviewCopilotAgent(self._github_client, self._groq_client, prompts_dir)
        self._risk_agent = RiskRadarAgent(
            github_client=self._github_client,
            slack_client=self._slack_client,
            discord_client=self._discord_client,
            high_threshold=settings.risk_high_threshold,
            medium_threshold=settings.risk_medium_threshold,
        )

    def verify_signature(self, payload: bytes, signature_header: str) -> bool:
        if not signature_header:
            return False

        expected = "sha256=" + hmac.new(
            self._settings.github_webhook_secret.encode("utf-8"),
            msg=payload,
            digestmod=hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature_header)

    async def handle_event(self, event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if event_name != "pull_request":
            return {"status": "ignored", "reason": f"Unsupported event: {event_name}"}

        action = str(payload.get("action", "")).strip()
        if action not in ALLOWED_PULL_REQUEST_ACTIONS:
            return {"status": "ignored", "reason": f"Unsupported pull_request action: {action}"}

        repo_full_name = str(payload.get("repository", {}).get("full_name", "")).strip()
        pr_number_raw = payload.get("pull_request", {}).get("number")
        try:
            pr_number = int(pr_number_raw)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid pull_request.number in payload",
            )

        if not repo_full_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid repository.full_name in payload",
            )

        review_task = asyncio.create_task(self._review_agent.run(repo_full_name, pr_number))
        risk_task = asyncio.create_task(self._risk_agent.run(repo_full_name, pr_number))

        review_result, risk_result = await asyncio.gather(review_task, risk_task, return_exceptions=True)

        response: dict[str, Any] = {
            "status": "processed",
            "event": event_name,
            "action": action,
            "repository": repo_full_name,
            "pr_number": pr_number,
            "results": {},
        }

        if isinstance(review_result, Exception):
            LOGGER.exception("Review Copilot failed for %s#%s", repo_full_name, pr_number, exc_info=review_result)
            response["results"]["review_copilot"] = {"error": str(review_result)}
        else:
            response["results"]["review_copilot"] = review_result

        if isinstance(risk_result, Exception):
            LOGGER.exception("Risk Radar failed for %s#%s", repo_full_name, pr_number, exc_info=risk_result)
            response["results"]["risk_radar"] = {"error": str(risk_result)}
        else:
            response["results"]["risk_radar"] = risk_result

        if not isinstance(review_result, Exception) and not isinstance(risk_result, Exception):
            review_discord_alert_sent = await self._maybe_send_review_discord_alert(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                review_result=review_result,
                risk_result=risk_result,
            )
            response["results"]["review_copilot"]["discord_alert_sent"] = review_discord_alert_sent

        if isinstance(review_result, Exception) and isinstance(risk_result, Exception):
            response["status"] = "error"

        return response

    async def _maybe_send_review_discord_alert(
        self,
        repo_full_name: str,
        pr_number: int,
        review_result: dict[str, Any],
        risk_result: dict[str, Any],
    ) -> bool:
        if bool(risk_result.get("discord_alert_sent")):
            return False

        highest_severity = str(review_result.get("highest_severity", "LOW")).upper().strip()
        if highest_severity not in {"MEDIUM", "HIGH"}:
            return False

        posted_comments = int(review_result.get("posted_comments", 0) or 0)
        if posted_comments <= 0:
            return False

        context = await asyncio.to_thread(self._github_client.get_pr_context, repo_full_name, pr_number)
        return await self._discord_client.send_review_findings_alert(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            pr_url=context.html_url,
            highest_severity=highest_severity,
            posted_comments=posted_comments,
            review_event=str(review_result.get("event", "COMMENT")).upper().strip() or "COMMENT",
        )


def create_webhook_router(handler: WebhookHandler) -> APIRouter:
    router = APIRouter()

    @router.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
        x_github_event: str = Header(default=""),
        x_hub_signature_256: str = Header(default=""),
    ) -> Response:
        payload_bytes = await request.body()

        if not handler.verify_signature(payload_bytes, x_hub_signature_256):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid webhook signature",
            )

        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Malformed JSON payload",
            ) from exc

        # Respond to GitHub immediately — processing runs in the background
        # so the connection is never held open long enough to time out.
        background_tasks.add_task(
            handler.handle_event,
            event_name=x_github_event,
            payload=payload,
        )
        return Response(status_code=status.HTTP_202_ACCEPTED)

    return router
