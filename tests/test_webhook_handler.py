from __future__ import annotations

import hashlib
import hmac
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from config import Settings
from webhook_handler import WebhookHandler


class _StubAgent:
    def __init__(self, result: dict[str, object]) -> None:
        self._result = result

    async def run(self, repo_full_name: str, pr_number: int) -> dict[str, object]:
        return self._result


class _StubDiscordClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_review_findings_alert(
        self,
        repo_full_name: str,
        pr_number: int,
        pr_url: str,
        highest_severity: str,
        posted_comments: int,
        review_event: str,
    ) -> bool:
        self.calls.append(
            {
                "repo_full_name": repo_full_name,
                "pr_number": pr_number,
                "pr_url": pr_url,
                "highest_severity": highest_severity,
                "posted_comments": posted_comments,
                "review_event": review_event,
            }
        )
        return True


class TestWebhookHandler(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _settings() -> Settings:
        return Settings(
            github_token="token",
            github_webhook_secret="secret",
            groq_api_key="groq",
            slack_webhook_url=None,
            discord_webhook_url=None,
            risk_high_threshold=67,
            risk_medium_threshold=34,
            port=8000,
            groq_model="llama-3.3-70b-versatile",
            request_timeout_seconds=25.0,
        )

    def _build_handler(self) -> WebhookHandler:
        with patch("webhook_handler.GitHubClient"), patch("webhook_handler.GroqClient"), patch(
            "webhook_handler.SlackClient"
        ), patch("webhook_handler.DiscordClient"):
            return WebhookHandler(self._settings())

    def test_verify_signature_valid_and_invalid(self) -> None:
        handler = self._build_handler()
        payload = b'{"action":"opened"}'

        valid_header = "sha256=" + hmac.new(
            b"secret",
            msg=payload,
            digestmod=hashlib.sha256,
        ).hexdigest()
        self.assertTrue(handler.verify_signature(payload, valid_header))
        self.assertFalse(handler.verify_signature(payload, "sha256=bad"))
        self.assertFalse(handler.verify_signature(payload, ""))

    async def test_handle_event_ignores_unsupported_event(self) -> None:
        handler = self._build_handler()

        result = await handler.handle_event("push", {"action": "opened"})

        self.assertEqual(result["status"], "ignored")

    async def test_handle_event_ignores_unsupported_action(self) -> None:
        handler = self._build_handler()

        payload = {
            "action": "closed",
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 12},
        }
        result = await handler.handle_event("pull_request", payload)

        self.assertEqual(result["status"], "ignored")

    async def test_handle_event_validates_pr_number(self) -> None:
        handler = self._build_handler()

        payload = {
            "action": "opened",
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": "abc"},
        }

        with self.assertRaises(HTTPException) as ctx:
            await handler.handle_event("pull_request", payload)

        self.assertEqual(ctx.exception.status_code, 400)

    async def test_handle_event_runs_review_and_risk_agents(self) -> None:
        handler = self._build_handler()
        handler._review_agent = _StubAgent({"agent": "review_copilot", "posted_comments": 2})
        handler._risk_agent = _StubAgent({"agent": "risk_radar", "score": 72, "band": "high"})

        payload = {
            "action": "opened",
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 25},
        }

        result = await handler.handle_event("pull_request", payload)

        self.assertEqual(result["status"], "processed")
        self.assertEqual(result["repository"], "owner/repo")
        self.assertEqual(result["pr_number"], 25)
        self.assertIn("review_copilot", result["results"])
        self.assertIn("risk_radar", result["results"])

    async def test_handle_event_sends_discord_for_review_findings(self) -> None:
        handler = self._build_handler()
        handler._review_agent = _StubAgent(
            {
                "agent": "review_copilot",
                "event": "REQUEST_CHANGES",
                "posted_comments": 3,
                "highest_severity": "HIGH",
            }
        )
        handler._risk_agent = _StubAgent(
            {
                "agent": "risk_radar",
                "score": 28,
                "band": "low",
                "discord_alert_sent": False,
            }
        )
        handler._discord_client = _StubDiscordClient()

        with patch.object(
            handler._github_client,
            "get_pr_context",
            return_value=type(
                "Context",
                (),
                {"html_url": "https://github.com/owner/repo/pull/25"},
            )(),
        ):
            payload = {
                "action": "opened",
                "repository": {"full_name": "owner/repo"},
                "pull_request": {"number": 25},
            }

            result = await handler.handle_event("pull_request", payload)

        self.assertTrue(result["results"]["review_copilot"]["discord_alert_sent"])
        self.assertEqual(len(handler._discord_client.calls), 1)


if __name__ == "__main__":
    unittest.main()
