from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from integrations.discord_client import DiscordClient
from integrations.github_client import GitHubClient
from integrations.slack_client import SlackClient

SENSITIVE_PATH_PATTERNS = (
    "auth/",
    "payments/",
    "payment/",
    "migrations/",
    "migration/",
    "secrets/",
)


@dataclass
class RiskAssessment:
    score: int
    band: str
    reasons: list[str]
    files_changed: int
    sensitive_files: list[str]
    test_delta: str
    friday_after_3pm: bool


class RiskRadarAgent:
    def __init__(
        self,
        github_client: GitHubClient,
        slack_client: SlackClient,
        discord_client: DiscordClient,
        high_threshold: int,
        medium_threshold: int,
    ) -> None:
        self._github_client = github_client
        self._slack_client = slack_client
        self._discord_client = discord_client
        self._high_threshold = high_threshold
        self._medium_threshold = medium_threshold

    async def run(self, repo_full_name: str, pr_number: int) -> dict[str, Any]:
        changed_files = await asyncio.to_thread(self._github_client.get_changed_files, repo_full_name, pr_number)
        context = await asyncio.to_thread(self._github_client.get_pr_context, repo_full_name, pr_number)

        assessment = self._assess_risk(changed_files)
        label = f"risk:{assessment.band}"

        await asyncio.to_thread(self._github_client.clear_risk_labels, repo_full_name, pr_number)
        await asyncio.to_thread(self._github_client.add_label, repo_full_name, pr_number, label)

        required_reviewers_enforced = False
        if assessment.band == "high":
            required_reviewers_enforced = await asyncio.to_thread(
                self._github_client.set_required_reviewers,
                repo_full_name,
                pr_number,
                2,
            )

        friday_warning_posted = False
        if assessment.friday_after_3pm and assessment.band in {"high", "medium"}:
            friday_warning = (
                "Risk Radar warning: This PR is marked "
                f"`{assessment.band.upper()}` and was opened Friday after 3pm. "
                "Recommend delaying merge until business hours unless this is an urgent fix."
            )
            await asyncio.to_thread(
                self._github_client.post_issue_comment,
                repo_full_name,
                pr_number,
                friday_warning,
            )
            friday_warning_posted = True

        slack_sent = False
        discord_sent = False
        if assessment.band == "high":
            slack_sent = await self._slack_client.send_high_risk_alert(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                pr_url=context.html_url,
                score=assessment.score,
                reasons=assessment.reasons,
            )
            discord_sent = await self._discord_client.send_high_risk_alert(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                pr_url=context.html_url,
                score=assessment.score,
                reasons=assessment.reasons,
            )

        return {
            "agent": "risk_radar",
            "score": assessment.score,
            "band": assessment.band,
            "label": label,
            "reasons": assessment.reasons,
            "required_reviewers_enforced": required_reviewers_enforced,
            "friday_warning_posted": friday_warning_posted,
            "slack_alert_sent": slack_sent,
            "discord_alert_sent": discord_sent,
        }

    def _assess_risk(self, changed_files: list[dict[str, Any]], now: datetime | None = None) -> RiskAssessment:
        evaluated_at = now or datetime.now()

        files_changed = len(changed_files)
        sensitive_files = [
            entry["filename"]
            for entry in changed_files
            if _is_sensitive_path(str(entry.get("filename", "")))
        ]
        test_delta = _get_test_delta(changed_files)
        friday_after_3pm = _is_friday_after_3pm(evaluated_at)

        files_score = _score_files_changed(files_changed)
        sensitive_score = _score_sensitive_paths(len(sensitive_files))
        tests_score = _score_test_delta(test_delta)
        time_score = _score_time_window(evaluated_at)

        total_score = min(100, files_score + sensitive_score + tests_score + time_score)

        if total_score >= self._high_threshold:
            band = "high"
        elif total_score >= self._medium_threshold:
            band = "medium"
        else:
            band = "low"

        reasons = [
            f"Files changed: {files_changed} (score +{files_score})",
            f"Sensitive files touched: {len(sensitive_files)} (score +{sensitive_score})",
            f"Test coverage delta: {test_delta} (score +{tests_score})",
            f"Time window: {_describe_time_window(evaluated_at)} (score +{time_score})",
        ]
        if sensitive_files:
            reasons.append("Sensitive paths: " + ", ".join(sensitive_files[:5]))

        return RiskAssessment(
            score=total_score,
            band=band,
            reasons=reasons,
            files_changed=files_changed,
            sensitive_files=sensitive_files,
            test_delta=test_delta,
            friday_after_3pm=friday_after_3pm,
        )


def _is_sensitive_path(path: str) -> bool:
    normalized = path.lower().replace("\\", "/")
    return any(pattern in normalized for pattern in SENSITIVE_PATH_PATTERNS)


def _is_test_file(path: str) -> bool:
    normalized = path.lower().replace("\\", "/")
    basename = normalized.split("/")[-1]
    return (
        "/tests/" in normalized
        or normalized.startswith("tests/")
        or basename.startswith("test_")
        or basename.endswith("_test.py")
        or bool(re.search(r"(\.spec\.|\.test\.)", basename))
    )


def _get_test_delta(changed_files: list[dict[str, Any]]) -> str:
    added_or_modified = 0
    removed = 0

    for entry in changed_files:
        filename = str(entry.get("filename", ""))
        if not _is_test_file(filename):
            continue

        status = str(entry.get("status", "")).lower()
        if status in {"added", "modified", "renamed"}:
            added_or_modified += 1
        elif status == "removed":
            removed += 1

    if removed > added_or_modified and removed > 0:
        return "tests_deleted"
    if added_or_modified > 0:
        return "tests_added"
    return "tests_unchanged"


def _score_files_changed(files_changed: int) -> int:
    if files_changed <= 5:
        return 10
    if files_changed <= 15:
        return 25
    return 40


def _score_sensitive_paths(sensitive_count: int) -> int:
    if sensitive_count == 0:
        return 0
    if sensitive_count == 1:
        return 20
    return 35


def _score_test_delta(test_delta: str) -> int:
    if test_delta == "tests_added":
        return 0
    if test_delta == "tests_unchanged":
        return 10
    return 25


def _score_time_window(now: datetime) -> int:
    if _is_friday_after_3pm(now):
        return 15
    if now.weekday() in {0, 1, 2, 3} and now.hour < 12:
        return 0
    return 8


def _is_friday_after_3pm(now: datetime) -> bool:
    return now.weekday() == 4 and now.hour >= 15


def _describe_time_window(now: datetime) -> str:
    if _is_friday_after_3pm(now):
        return "Friday after 3pm"
    if now.weekday() in {0, 1, 2, 3} and now.hour < 12:
        return "Mon-Thu morning"
    return "Mon-Thu afternoon / other"
