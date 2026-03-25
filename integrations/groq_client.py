from __future__ import annotations

import json
import logging
import re
from typing import Any

from groq import Groq

LOGGER = logging.getLogger(__name__)

MAX_DIFF_CHARS = 50_000


class GroqClient:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = Groq(api_key=api_key)
        self._model = model

    def analyze_diff(
        self,
        diff_text: str,
        security_prompt: str,
        quality_prompt: str,
        pr_title: str,
        pr_body: str,
    ) -> dict[str, Any]:
        trimmed_diff = diff_text
        if len(trimmed_diff) > MAX_DIFF_CHARS:
            trimmed_diff = (
                trimmed_diff[:MAX_DIFF_CHARS]
                + "\n\n[DIFF TRUNCATED FOR TOKEN LIMIT. PRIORITIZE SECURITY AND HIGH-RISK FILES.]"
            )

        system_prompt = self._build_system_prompt(security_prompt, quality_prompt)
        user_prompt = self._build_user_prompt(trimmed_diff, pr_title, pr_body)

        completion = self._client.chat.completions.create(
            model=self._model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        raw_content = completion.choices[0].message.content or "{}"
        return self._normalize_response(raw_content)

    @staticmethod
    def _build_system_prompt(security_prompt: str, quality_prompt: str) -> str:
        return (
            "You are PR Review Copilot. Analyze diffs for security vulnerabilities and code quality issues.\n\n"
            "Security prompt:\n"
            f"{security_prompt}\n\n"
            "Code quality prompt:\n"
            f"{quality_prompt}\n\n"
            "Return ONLY valid JSON with this exact schema:\n"
            "{\n"
            '  "summary": "Short overall summary",\n'
            '  "event": "COMMENT|REQUEST_CHANGES",\n'
            '  "comments": [\n'
            "    {\n"
            '      "path": "relative/file/path.py",\n'
            '      "line": 42,\n'
            '      "severity": "HIGH|MEDIUM|LOW",\n'
            '      "category": "security|quality",\n'
            '      "message": "What is wrong",\n'
            '      "suggestion": "How to fix"\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "Rules:\n"
            "- Do not include comments for unchanged files.\n"
            "- Prefer high-signal findings over nitpicks.\n"
            "- Use REQUEST_CHANGES when any critical issue exists.\n"
            "- Keep comments actionable and concise.\n"
            "- If README/docs claims contradict repository facts or changed code, add a finding on that docs file.\n"
            "- Treat misleading project description, setup, or capability claims as at least MEDIUM severity.\n"
        )

    @staticmethod
    def _build_user_prompt(diff_text: str, pr_title: str, pr_body: str) -> str:
        return (
            f"PR title: {pr_title}\n"
            f"PR body: {pr_body}\n\n"
            "Unified diff:\n"
            f"{diff_text}"
        )

    def _normalize_response(self, raw_content: str) -> dict[str, Any]:
        payload = self._extract_json(raw_content)

        summary = str(payload.get("summary", "Automated review completed.")).strip()
        event = str(payload.get("event", "COMMENT")).upper().strip()
        if event not in {"COMMENT", "REQUEST_CHANGES"}:
            event = "COMMENT"

        comments_in = payload.get("comments", [])
        if not isinstance(comments_in, list):
            comments_in = []

        comments_out: list[dict[str, Any]] = []
        for item in comments_in:
            if not isinstance(item, dict):
                continue

            path = str(item.get("path", "")).strip()
            message = str(item.get("message", "")).strip()
            if not path or not message:
                continue

            line_raw = item.get("line")
            try:
                line = int(line_raw)
            except (TypeError, ValueError):
                continue

            severity = str(item.get("severity", "MEDIUM")).upper().strip()
            if severity not in {"HIGH", "MEDIUM", "LOW"}:
                severity = "MEDIUM"

            category = str(item.get("category", "quality")).lower().strip()
            if category not in {"security", "quality"}:
                category = "quality"

            suggestion = str(item.get("suggestion", "")).strip()

            comments_out.append(
                {
                    "path": path,
                    "line": line,
                    "severity": severity,
                    "category": category,
                    "message": message,
                    "suggestion": suggestion,
                }
            )

        if any(comment["severity"] == "HIGH" for comment in comments_out):
            event = "REQUEST_CHANGES"

        return {
            "summary": summary,
            "event": event,
            "comments": comments_out,
        }

    @staticmethod
    def _extract_json(raw_content: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw_content)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            LOGGER.debug("Groq response is not pure JSON, attempting recovery")

        match = re.search(r"\{.*\}", raw_content, re.DOTALL)
        if match:
            try:
                payload = json.loads(match.group(0))
                if isinstance(payload, dict):
                    return payload
            except json.JSONDecodeError:
                LOGGER.warning("Failed to recover JSON from Groq output")

        return {}
